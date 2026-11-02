"""Versioned config registry (ADR-014) and kill switches.

Resolution: package defaults are already embedded in the tenant document at publish time; compliance monotonicity
against the package is checked on publish. Turns record the config_version_id they ran with.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import yaml
from psycopg import AsyncConnection

from agentplatform.core.config.schema import ComplianceConfig, PlatformConfig, validate_against_package

_CACHE_TTL_S = 30.0
_KS_TTL_S = 5.0


@dataclass(frozen=True)
class ActiveConfig:
    version_id: UUID
    version: int
    config: PlatformConfig


class ConfigError(ValueError):
    """`code` becomes a sentence in the console (api/ui.py); the message is for logs and the command line."""

    def __init__(self, message: str, code: str = "", detail: str = ""):
        super().__init__(message)
        self.code, self.detail = code, detail


def canary_bucket(conversation_id: UUID | str) -> int:
    """Stable 0..99 bucket so a conversation never flips between config versions mid-way."""
    return int.from_bytes(hashlib.sha256(str(conversation_id).encode()).digest()[:8], "big") % 100


def load_yaml(path: str | Path) -> PlatformConfig:
    return PlatformConfig.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def resolve_with_package(cfg: PlatformConfig, package_compliance: ComplianceConfig) -> PlatformConfig:
    """platform ⊕ package ⊕ tenant: compliance is the UNION (tenant can only add restrictions; plan 13 §1)."""
    t, p = cfg.compliance, package_compliance
    merged = t.model_copy(update={
        "forbidden_phrases": list(dict.fromkeys([*p.forbidden_phrases, *t.forbidden_phrases])),
        "required_disclosures": {**p.required_disclosures, **{k: v for k, v in t.required_disclosures.items()
                                                               if p.required_disclosures.get(k, "") in v}},
        "never_request": list(dict.fromkeys([*p.never_request, *t.never_request])),
        "opt_out_patterns": list(dict.fromkeys([*p.opt_out_patterns, *t.opt_out_patterns])),
        "opt_out_keywords": list(dict.fromkeys([*p.opt_out_keywords, *t.opt_out_keywords])),
        "extra_rules": list(dict.fromkeys([*p.extra_rules, *t.extra_rules])),
        "require_with": {**t.require_with, **p.require_with},
        "judge_risk_threshold": min(p.judge_risk_threshold, t.judge_risk_threshold),
    })
    return cfg.model_copy(update={"compliance": merged})


def _hash(doc: dict) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()


def config_hash(cfg: PlatformConfig) -> str:
    """What an evaluation vouches for: the exact resolved document."""
    return _hash(json.loads(cfg.model_dump_json()))


def merge_changes(base: dict, draft: dict, current: dict, path: str = "") -> tuple[dict, list[str]]:
    """What a draft changed from its base, applied on top of the current version. A list is one value. Returns (merged, paths changed on both sides)."""
    out, conflicts = dict(current), []
    for key in {*base, *draft}:
        where = f"{path}.{key}" if path else key
        b, d = base.get(key), draft.get(key)
        if b == d:
            continue
        c = current.get(key)
        if isinstance(b, dict) and isinstance(d, dict) and isinstance(c, dict):
            out[key], inner = merge_changes(b, d, c, where)
            conflicts += inner
        elif c == b or c == d:
            if key in draft:
                out[key] = d
            else:
                out.pop(key, None)
        else:
            conflicts.append(where)
    return out, sorted(conflicts)


def _only_operational_change(active: dict, draft: dict) -> bool:
    """A top-up or a feature flag does not change what the assistant says: it keeps the evaluation the active version already passed."""
    def strip(doc: dict) -> dict:
        ops = {k: v for k, v in (doc.get("operations") or {}).items() if k != "feature_flags"}
        return {**{k: v for k, v in doc.items() if k != "budget"}, "operations": ops}
    return strip(active) == strip(draft)


def normalized(doc: dict) -> dict:
    """A stored document as the current schema reads it: without options that no longer exist, with the new ones at their defaults. Two versions are
    compared this way, or every old version would seem to change things nobody touched."""
    return json.loads(PlatformConfig.model_validate(doc).model_dump_json())


class ConfigRegistry:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, ActiveConfig]] = {}
        self._ks_cache: tuple[float, list[dict]] | None = None

    def forget(self, tenant_id: UUID) -> None:
        self._cache.pop(str(tenant_id), None)

    async def _check_search_index(self, conn: AsyncConnection, cfg: PlatformConfig) -> None:
        """Vectors from one model mean nothing to another. Changing the model goes through a new index (knowledge.admin.start_reindex)."""
        idx = await (await conn.execute("select embedding_model, dims, status from kb_indexes where name=%s", (cfg.rag.active_index_id,))).fetchone()
        if not idx:
            return
        if idx["embedding_model"] != cfg.models.embedding.model or idx["dims"] != cfg.models.embedding.dims:
            raise ConfigError(f"index {cfg.rag.active_index_id} was built with {idx['embedding_model']}/{idx['dims']}, config says "
                              f"{cfg.models.embedding.model}/{cfg.models.embedding.dims}", code="embedding_mismatch")
        if idx["status"] not in ("active", "ready"):
            raise ConfigError(f"index {cfg.rag.active_index_id} is {idx['status']}", code="index_not_ready")

    async def _base_for_draft(self, conn: AsyncConnection, tenant_id: UUID, env: str) -> UUID | None:
        """The version a new draft is a copy of: the active one, or before the first activation, the newest proposal."""
        row = await (await conn.execute(
            """select id from config_versions where tenant_id=%s and env=%s and status in ('active','draft')
               order by (status = 'active') desc, version desc limit 1""", (tenant_id, env))).fetchone()
        return row["id"] if row else None

    async def publish(self, conn: AsyncConnection, tenant_id: UUID, cfg: PlatformConfig, *,
                      package_compliance: ComplianceConfig | None, created_by: UUID | None = None,
                      eval_run_id: UUID | None = None, activate: bool = False, note: str | None = None,
                      base_version_id: UUID | None = None) -> UUID:
        if package_compliance is not None:
            cfg = resolve_with_package(cfg, package_compliance)
            try:
                validate_against_package(cfg, package_compliance)
            except ValueError as e:
                raise ConfigError(str(e), code="compliance_weakened", detail=str(e)) from e
        if activate and cfg.environment == "prod" and eval_run_id is None:
            raise ConfigError("prod configs can only be activated with an approved eval run (gate)", code="needs_evaluation")
        await self._check_search_index(conn, cfg)
        base = None if activate else (base_version_id or await self._base_for_draft(conn, tenant_id, cfg.environment))
        doc = json.loads(cfg.model_dump_json())
        row = await (await conn.execute(
            "select coalesce(max(version), 0) + 1 as v from config_versions where tenant_id = %s and env = %s",
            (tenant_id, cfg.environment))).fetchone()
        status = "active" if activate else "draft"
        if activate:
            await conn.execute(
                "update config_versions set status = 'superseded' where tenant_id = %s and env = %s and status = 'active'",
                (tenant_id, cfg.environment))
        rec = await (await conn.execute(
            """insert into config_versions (tenant_id, env, version, document, content_hash, status, eval_run_id, created_by, base_version_id, note)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id""",
            (tenant_id, cfg.environment, row["v"], json.dumps(doc), _hash(doc), status, eval_run_id, created_by, base, note))).fetchone()
        await conn.execute(
            "insert into audit_log (tenant_id, actor, actor_kind, action, target, after) values (%s,%s,'user','config.publish',%s,%s)",
            (tenant_id, created_by, str(rec["id"]), json.dumps({"version": row["v"], "status": status})))
        self._cache.pop(str(tenant_id), None)
        return rec["id"]

    async def rollback(self, conn: AsyncConnection, tenant_id: UUID, env: str) -> UUID:
        rows = await (await conn.execute(
            """select id, status from config_versions where tenant_id = %s and env = %s and status in ('active','superseded')
               order by version desc limit 2""", (tenant_id, env))).fetchall()
        if len(rows) < 2 or rows[0]["status"] != "active":
            raise ConfigError("nothing to roll back to", code="nothing_to_roll_back")
        await conn.execute("update config_versions set status = 'rolled_back' where id = %s", (rows[0]["id"],))
        await conn.execute("update config_versions set status = 'active' where id = %s", (rows[1]["id"],))
        self._cache.pop(str(tenant_id), None)
        return rows[1]["id"]

    async def active(self, conn: AsyncConnection, tenant_id: UUID, env: str) -> ActiveConfig:
        key = str(tenant_id)
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL_S:
            return hit[1]
        row = await (await conn.execute(
            "select id, version, document from config_versions where tenant_id = %s and env = %s and status = 'active'",
            (tenant_id, env))).fetchone()
        if not row:
            raise ConfigError(f"no active config for tenant {tenant_id} ({env})")
        active = ActiveConfig(row["id"], row["version"], PlatformConfig.model_validate(row["document"]))
        self._cache[key] = (time.monotonic(), active)
        return active

    # ----- two-person change flow (F2-CON-04): propose -> approve by ANOTHER user -> active or canary -> promote -----
    async def propose(self, conn: AsyncConnection, tenant_id: UUID, cfg: PlatformConfig, *,
                      package_compliance: ComplianceConfig | None, created_by: UUID, canary_percent: int = 0, note: str | None = None) -> UUID:
        if not 0 <= canary_percent <= 100:
            raise ConfigError("canary_percent must be between 0 and 100")
        vid = await self.publish(conn, tenant_id, cfg, package_compliance=package_compliance, created_by=created_by, note=note)
        await conn.execute("update config_versions set canary_percent = %s where id = %s", (canary_percent, vid))
        return vid

    async def approve(self, conn: AsyncConnection, tenant_id: UUID, version_id: UUID, approver: UUID) -> str:
        """Second person approves a draft. canary_percent = 0 -> active now; > 0 -> canary (replaces any running canary)."""
        row = await (await conn.execute(
            "select env, status, created_by, approved_by, canary_percent, eval_run_id, base_version_id from config_versions "
            "where id = %s and tenant_id = %s for update", (version_id, tenant_id))).fetchone()
        if not row or row["status"] != "draft":
            raise ConfigError("only drafts can be approved", code="not_a_draft")
        if row["created_by"] is None or str(row["created_by"]) == str(approver) or approver in (row["approved_by"] or []):
            raise ConfigError("two-person rule: the approver must be a different user from the proposer", code="two_person")
        current = await (await conn.execute(
            "select id from config_versions where tenant_id = %s and env = %s and status = 'active'", (tenant_id, row["env"]))).fetchone()
        if current and row["base_version_id"] != current["id"]:      # it would silently undo what was activated after this copy was made
            raise ConfigError("this draft was made from another version: bring it up to date first", code="stale_draft")
        if row["env"] == "prod" and row["eval_run_id"] is None:
            docs = await (await conn.execute(
                """select (select document from config_versions where id = %s) as draft, document as active, eval_run_id
                   from config_versions where tenant_id = %s and env = %s and status = 'active'""",
                (version_id, tenant_id, row["env"]))).fetchone()
            if not docs or docs["eval_run_id"] is None or not _only_operational_change(docs["active"], docs["draft"]):
                raise ConfigError("prod configs can only be activated with an approved eval run (gate)", code="needs_evaluation")
            await conn.execute("update config_versions set eval_run_id = %s where id = %s", (docs["eval_run_id"], version_id))
        if row["canary_percent"] > 0:
            await conn.execute("update config_versions set status = 'rolled_back' where tenant_id = %s and env = %s "
                               "and status = 'canary'", (tenant_id, row["env"]))
            status = "canary"
        else:
            await conn.execute("update config_versions set status = 'superseded' where tenant_id = %s and env = %s "
                               "and status in ('active', 'canary')", (tenant_id, row["env"]))
            status = "active"
        await conn.execute("update config_versions set status = %s, approved_by = approved_by || %s::uuid where id = %s",
                           (status, approver, version_id))
        await conn.execute(
            "insert into audit_log (tenant_id, actor, actor_kind, action, target, after) values (%s,%s,'user','config.approve',%s,%s)",
            (tenant_id, approver, str(version_id), json.dumps({"status": status, "canary_percent": row["canary_percent"]})))
        self._cache.pop(str(tenant_id), None)
        return status

    async def rebase(self, conn: AsyncConnection, tenant_id: UUID, version_id: UUID, actor: UUID) -> UUID:
        """The same change on top of the current version, as a new draft of the same author. The merge is mechanical, so it adds nothing of its own,
        and the new version needs its own evaluation."""
        row = await (await conn.execute(
            "select id, env, document, status, base_version_id, created_by, canary_percent, note from config_versions "
            "where id=%s and tenant_id=%s for update", (version_id, tenant_id))).fetchone()
        if not row or row["status"] != "draft":
            raise ConfigError("only drafts can be brought up to date", code="not_a_draft")
        current = await (await conn.execute(
            "select id, document from config_versions where tenant_id=%s and env=%s and status='active'", (tenant_id, row["env"]))).fetchone()
        if not current or row["base_version_id"] == current["id"]:
            return version_id
        base = await (await conn.execute("select document from config_versions where id=%s", (row["base_version_id"],))).fetchone()
        if not base:
            raise ConfigError("the version this draft came from is unknown", code="no_base")
        merged, conflicts = merge_changes(normalized(base["document"]), normalized(row["document"]), normalized(current["document"]))
        if conflicts:
            raise ConfigError(f"changed on both sides: {conflicts}", code="merge_conflict", detail=", ".join(conflicts))
        cfg = PlatformConfig.model_validate(merged)
        new_id = await self.publish(conn, tenant_id, cfg, package_compliance=None, created_by=row["created_by"], note=row["note"],
                                    base_version_id=current["id"])
        await conn.execute("update config_versions set canary_percent=%s where id=%s", (row["canary_percent"], new_id))
        await conn.execute("update config_versions set status='discarded' where id=%s", (version_id,))
        await conn.execute(
            "insert into audit_log (tenant_id, actor, actor_kind, action, target, after) values (%s,%s,'user','config.rebase',%s,%s)",
            (tenant_id, actor, str(version_id), json.dumps({"new_version": str(new_id)})))
        self._cache.pop(str(tenant_id), None)
        return new_id

    async def discard(self, conn: AsyncConnection, tenant_id: UUID, version_id: UUID, actor: UUID) -> None:
        row = await (await conn.execute(
            "update config_versions set status='discarded' where id=%s and tenant_id=%s and status='draft' returning id",
            (version_id, tenant_id))).fetchone()
        if not row:
            raise ConfigError("only drafts can be discarded", code="not_a_draft")
        await conn.execute(
            "insert into audit_log (tenant_id, actor, actor_kind, action, target) values (%s,%s,'user','config.discard',%s)",
            (tenant_id, actor, str(version_id)))

    async def promote_canary(self, conn: AsyncConnection, tenant_id: UUID, env: str, actor: UUID) -> UUID:
        row = await (await conn.execute("select id from config_versions where tenant_id = %s and env = %s and status = 'canary'",
                                        (tenant_id, env))).fetchone()
        if not row:
            raise ConfigError("no canary running", code="no_canary")
        await conn.execute("update config_versions set status = 'superseded' where tenant_id = %s and env = %s and status = 'active'",
                           (tenant_id, env))
        await conn.execute("update config_versions set status = 'active', canary_percent = 0 where id = %s", (row["id"],))
        await conn.execute(
            "insert into audit_log (tenant_id, actor, actor_kind, action, target) values (%s,%s,'user','config.promote',%s)",
            (tenant_id, actor, str(row["id"])))
        self._cache.pop(str(tenant_id), None)
        return row["id"]

    async def abort_canary(self, conn: AsyncConnection, tenant_id: UUID, env: str, actor: UUID) -> None:
        await conn.execute("update config_versions set status = 'rolled_back' where tenant_id = %s and env = %s and status = 'canary'",
                           (tenant_id, env))
        await conn.execute(
            "insert into audit_log (tenant_id, actor, actor_kind, action, target) values (%s,%s,'user','config.canary_abort',%s)",
            (tenant_id, actor, env))
        self._cache.pop(str(tenant_id), None)

    async def for_conversation(self, conn: AsyncConnection, tenant_id: UUID, env: str, conversation_id: UUID) -> ActiveConfig:
        """Active config, or the canary for the stable slice of conversations with bucket < canary_percent.
        Callers that do not know the conversation keep using `active` (canary never applies to them)."""
        row = await (await conn.execute(
            "select id, version, document, canary_percent from config_versions where tenant_id = %s and env = %s "
            "and status = 'canary'", (tenant_id, env))).fetchone()
        if row and canary_bucket(conversation_id) < row["canary_percent"]:
            return ActiveConfig(row["id"], row["version"], PlatformConfig.model_validate(row["document"]))
        return await self.active(conn, tenant_id, env)

    # ----- kill switches (effective within _KS_TTL_S) -----
    async def kill_switch_on(self, conn: AsyncConnection, *, tenant_id: UUID | None, channel: str | None = None,
                             feature: str | None = None, campaign: str | None = None) -> str | None:
        if self._ks_cache is None or time.monotonic() - self._ks_cache[0] > _KS_TTL_S:
            rows = await (await conn.execute("select tenant_id, scope, scope_ref from kill_switches where is_on")).fetchall()
            self._ks_cache = (time.monotonic(), rows)
        for r in self._ks_cache[1]:
            if r["scope"] == "global":
                return "global"
            if r["scope"] == "tenant" and tenant_id and str(r["scope_ref"]) == str(tenant_id):
                return "tenant"
            if r["scope"] == "channel" and channel == r["scope_ref"] and (r["tenant_id"] in (None, tenant_id)):
                return f"channel:{channel}"
            if r["scope"] == "feature" and feature == r["scope_ref"] and (r["tenant_id"] in (None, tenant_id)):
                return f"feature:{feature}"
            if r["scope"] == "campaign" and campaign and campaign == r["scope_ref"]:
                return f"campaign:{campaign}"
        return None

    async def set_channel_stop(self, conn: AsyncConnection, tenant_id: UUID, channel: str, reason: str) -> None:
        """Code-decided stop of one tenant's channel; `reason` lets only the matching recovery lift it."""
        await conn.execute(
            """insert into kill_switches (tenant_id, scope, scope_ref, is_on, reason)
               values (%s, 'channel', %s, true, %s)
               on conflict (scope, coalesce(scope_ref, ''), coalesce(tenant_id, '00000000-0000-0000-0000-000000000000'))
               do update set is_on = true, reason = excluded.reason, changed_at = now()""", (tenant_id, channel, reason))

    async def lift_channel_stop(self, conn: AsyncConnection, tenant_id: UUID, channel: str, reasons: tuple[str, ...]) -> bool:
        row = await (await conn.execute(
            """update kill_switches set is_on = false, changed_at = now()
               where tenant_id = %s and scope = 'channel' and scope_ref = %s and is_on and reason = any(%s) returning id""",
            (tenant_id, channel, list(reasons)))).fetchone()
        return bool(row)

    def invalidate_kill_switches(self) -> None:
        self._ks_cache = None


registry = ConfigRegistry()
