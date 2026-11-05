"""The assistant in plain forms (ADR-022): who it is, how it replies, which models, which rules, testing the search, joining the console, and what is
still missing to go live. Every behaviour change is a normal draft: two people, an evaluation in production, a rollback. Nothing here sets a rule of
its own: PlatformConfig validates what a form sends, the guards still check every reply, and the YAML stays available for the rest."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import string
import unicodedata
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from agentplatform.api import ui
from agentplatform.api.auth import forget_session, hash_password, require, require_post, totp_required
from agentplatform.api.console import KB_ROLES, MIN_PASSWORD_LEN, _audit, _can_index, _labels, _page, _queue, _tid
from agentplatform.core.agent import policy
from agentplatform.core.comments.settings import text_problems
from agentplatform.core.config.presets import RAG_PRESETS
from agentplatform.core.config.registry import ConfigError, registry
from agentplatform.core.config.schema import PlatformConfig
from agentplatform.core.db.pool import platform_tx, tenant_tx
from agentplatform.core.followups.engine import parse_delay, trigger_stages
from agentplatform.core.knowledge import admin as K
from agentplatform.core.knowledge.service import KnowledgeService
from agentplatform.core.models import pricing
from agentplatform.core.models.gateway import BudgetDenied, ModelCallError, tenant_context
from agentplatform.core.packages.contract import package_datasets
from agentplatform.core.security.keys import crypto, decrypt_field, encrypt_field
from agentplatform.core.security.pii import mask
from agentplatform.settings import get_settings

router = APIRouter(prefix="/console")
log = logging.getLogger(__name__)
_MESSAGE_CHECKS = ("url_not_allowed", "forbidden_phrase", "required_context", "never_request", "pii_placeholder", "pii_in_output", "human_impersonation")
INVITE_ROLES = ("tenant_owner", "reviewer", "seller")
MODEL_NODES = ("respond", "analyze_turn", "output_judge", "summarize", "comment_intent", "table_extract")
BLANK_EXAMPLES = 2
BLANK_GUIDELINES = 1


async def _setup_runtime(tid: UUID):
    from agentplatform.runtime import get_runtime
    return await get_runtime().tenant_for_change(tid)


def _set(doc: dict, path: str, value) -> None:
    parts = path.split(".")
    for p in parts[:-1]:
        doc = doc.setdefault(p, {})
    doc[parts[-1]] = value


def _problems(e: ValidationError) -> list[str]:
    return [f"{ui.config_field('.'.join(str(p) for p in err['loc']))}: {ui.config_message(err)}" for err in e.errors()]


async def _propose(tr, tid: UUID, user: dict, changes: dict, note: str) -> tuple[UUID | None, list[str]]:
    """A draft with these fields changed on top of the current version. Returns (draft, problems); no draft and no problems means nothing changed."""
    current = json.loads(tr.config.model_dump_json())
    doc = json.loads(json.dumps(current))
    for path, value in changes.items():
        _set(doc, path, value)
    try:
        cfg = PlatformConfig.model_validate(doc)
    except ValidationError as e:
        return None, _problems(e)
    if json.loads(cfg.model_dump_json()) == current:
        return None, []
    try:
        async with tenant_tx(tid) as conn:
            vid = await registry.propose(conn, tid, cfg, package_compliance=tr.package.manifest.compliance, created_by=UUID(user["id"]), note=note[:300] or None)
            await _audit(conn, tid, UUID(user["id"]), "config.propose", str(vid), {"fields": sorted(changes), "note": note[:300]})
    except ConfigError as e:
        return None, [ui.config_error(e.code, e.detail)]
    return vid, []


def _to_config(vid: UUID | None) -> RedirectResponse:
    return RedirectResponse(f"/console/config?proposta={vid}" if vid else "/console/config?igual=1", status_code=303)


# ======================= the assistant =======================
def _assistant_ctx(tr, form: dict | None = None) -> dict:
    c = tr.config
    examples = [e.model_dump() for e in c.conversation.reply_examples]
    return {"cfg": c, "package_examples": tr.package.manifest.reply_examples,
            "examples": form["examples"] if form else [*examples, *[{"customer": "", "reply": ""}] * BLANK_EXAMPLES],
            "form": form or {}}


@router.get("/assistant", response_class=HTMLResponse)
async def assistant_page(request: Request):
    user = require(request, "tenant_owner")
    return _page(request, "assistant.html", {"user": user, **_assistant_ctx(await _setup_runtime(_tid(user)))})


@router.post("/assistant", response_class=HTMLResponse)
async def assistant_save(request: Request):
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    tr = await _setup_runtime(tid)
    f = await request.form()
    text = lambda name: str(f.get(name) or "").strip()
    examples = [{"customer": c.strip(), "reply": r.strip()} for c, r in zip(f.getlist("example_customer"), f.getlist("example_reply"))
                if str(c).strip() or str(r).strip()]
    holidays = [h.strip() for h in text("holidays_local").splitlines() if h.strip()]
    messages = {"conversation.waiting_message": text("waiting_message"), "conversation.first_message_disclosure": text("first_message_disclosure"),
                "conversation.safe_failure_message": text("safe_failure_message"),
                "conversation.opt_out_confirmation": text("opt_out_confirmation"), "handoff.expectation_message_in_hours": text("expectation_in"),
                "handoff.expectation_message_off_hours": text("expectation_off")}
    changes = {
        "conversation.persona.seller_display_name": text("seller_display_name"), "conversation.persona.assistant_name": text("assistant_name"),
        "conversation.persona.company_name": text("company_name"), "conversation.allowed_urls": [u.strip() for u in text("allowed_urls").splitlines() if u.strip()],
        "conversation.persona.tone": text("tone"), "conversation.persona.disclose_on_first_message": bool(f.get("disclose")),
        "conversation.business_instructions": text("business_instructions"), "conversation.reply_examples": examples, **messages,
        "handoff.send_expectation_message": bool(f.get("send_expectation")), "handoff.sla_minutes": text("sla_minutes"),
        "handoff.off_hours_mode": text("off_hours_mode"), "handoff.notify_off_hours": text("notify_off_hours"),
        "handoff.human_unanswered_realert_min": text("realert_min"),
        "sending.quiet_hours.start": text("quiet_start"), "sending.quiet_hours.end": text("quiet_end"), "sending.quiet_hours.timezone": text("timezone"),
        "sending.holidays_national": bool(f.get("holidays_national")), "sending.holidays_local": holidays,
        "sending.frequency_cap_per_contact_day": text("cap_day"), "sending.frequency_cap_per_contact_week": text("cap_week"),
        "followups.enabled": bool(f.get("followups_enabled")),
    }
    errors = []
    if not text("seller_display_name") or not text("assistant_name"):
        errors.append("Preencha o nome de quem atende e o nome do assistente.")
    for path, value in messages.items():
        if path != "conversation.waiting_message" and not value:
            errors.append(f"{ui.config_field(path)}: não pode ficar em branco.")
        checked = text_problems(tr.config, "message", value, tr.config.output_guard.max_bytes, tr.allowed_urls, checked=_MESSAGE_CHECKS) if value else []
        errors += [f"{ui.config_field(path)}: {ui.TEXT_PROBLEM.get(p.rule, 'texto não permitido.')}" for p in checked]
    vid = None
    if not errors:
        vid, errors = await _propose(tr, tid, user, changes, text("note") or "Ajustes do assistente")
    if errors:
        form = {"examples": [*examples, *[{"customer": "", "reply": ""}] * BLANK_EXAMPLES], "values": {k: v for k, v in f.items() if not k.startswith("example_")}}
        return _page(request, "assistant.html", {"user": user, "errors": errors, **_assistant_ctx(tr, form)}, 400)
    return _to_config(vid)


# ======================= models =======================
def _model_choices() -> dict:
    """What the configured provider can route, with its price. A model without a price is refused by the gateway, so it is not offered."""
    openai_only = get_settings().llm_provider == "openai"
    usable = lambda name: name.startswith("openai/") if openai_only else name not in pricing.DIRECT_KEY_ONLY
    return {"chat": [{"name": k, "input": p.input_per_m, "output": p.output_per_m} for k, p in sorted(pricing.CHAT.items()) if usable(k)],
            "embedding": [{"name": k, "per_m": v, "dims": pricing.EMBEDDING_DIMS.get(k)} for k, v in sorted(pricing.EMBEDDING_PER_M.items()) if usable(k)],
            "transcription": [{"name": k, "per_min": v} for k, v in sorted(pricing.TRANSCRIPTION_PER_MIN.items()) if usable(k)],
            "openai_only": openai_only}


@router.get("/assistant/models", response_class=HTMLResponse)
async def models_page(request: Request):
    user = require(request, "tenant_owner")
    tr = await _setup_runtime(_tid(user))
    nodes = [(n, getattr(tr.config.models, n)) for n in MODEL_NODES if getattr(tr.config.models, n) is not None]
    return _page(request, "models.html", {"user": user, "cfg": tr.config, "nodes": nodes, "choices": _model_choices(), "labels": ui.MODEL_NODE})


@router.post("/assistant/models", response_class=HTMLResponse)
async def models_save(request: Request):
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    tr = await _setup_runtime(tid)
    f = await request.form()
    choices = _model_choices()
    known = {c["name"] for c in choices["chat"]}
    changes, errors = {}, []
    for n in MODEL_NODES:
        spec = getattr(tr.config.models, n)
        if spec is None:
            continue
        primary, fallback = str(f.get(f"{n}_primary") or spec.primary), str(f.get(f"{n}_fallback") or "")
        if (primary not in known and primary != spec.primary) or (fallback and fallback not in known and fallback not in spec.fallbacks):
            errors.append(f"{ui.MODEL_NODE.get(n, n)}: modelo não disponível neste ambiente.")
            continue
        changes[f"models.{n}.primary"] = primary
        if spec.fallbacks and fallback == spec.fallbacks[0] and primary not in spec.fallbacks:      # a longer chain set in the file stays as it is
            changes[f"models.{n}.fallbacks"] = list(spec.fallbacks)
        else:
            changes[f"models.{n}.fallbacks"] = [fallback] if fallback and fallback != primary else []
    transcription = str(f.get("transcription") or tr.config.models.transcription.model)
    if transcription not in {c["name"] for c in choices["transcription"]} and transcription != tr.config.models.transcription.model:
        errors.append("Transcrição de áudio: modelo não disponível neste ambiente.")
    else:
        changes["models.transcription.model"] = transcription
    vid = None
    if not errors:
        vid, errors = await _propose(tr, tid, user, changes, str(f.get("note") or "Troca de modelos").strip())
    if errors:
        nodes = [(n, getattr(tr.config.models, n)) for n in MODEL_NODES if getattr(tr.config.models, n) is not None]
        return _page(request, "models.html", {"user": user, "cfg": tr.config, "nodes": nodes, "choices": choices, "labels": ui.MODEL_NODE, "errors": errors}, 400)
    return _to_config(vid)


# ======================= rules =======================
def _rules_ctx(tr) -> dict:
    m, c = tr.package.manifest, tr.config
    overrides = {o.id: o for o in c.guidelines.overrides}
    package_phrases = set(m.compliance.forbidden_phrases)
    return {"guidelines": [{"spec": g, "enabled": overrides[g.id].enabled if g.id in overrides else True,
                            "priority": overrides[g.id].priority if g.id in overrides and overrides[g.id].priority is not None else g.priority,
                            "action": (overrides[g.id].action if g.id in overrides else None) or g.action, "changed": bool(g.id in overrides and overrides[g.id].action)}
                           for g in sorted(m.guidelines, key=lambda g: g.priority)],
            "intent_labels": m.intent_labels,
            "custom": [*c.guidelines.custom, *[None] * BLANK_GUIDELINES], "cfg": c, "stages": m.stages,
            "intents": m.intents, "can_turn_off": policy.can_turn_off(m), "locked_reasons": [t.reason for t in m.handoff_triggers if t.locked],
            "package_phrases": sorted(package_phrases), "own_phrases": [p for p in c.compliance.forbidden_phrases if p not in package_phrases],
            "tools": tr.package.tools()}


@router.get("/assistant/rules", response_class=HTMLResponse)
async def rules_page(request: Request):
    user = require(request, "tenant_owner")
    return _page(request, "rules.html", {"user": user, **_rules_ctx(await _setup_runtime(_tid(user)))})


def _words(text: str) -> list[str]:
    return [w.strip() for w in text.replace("\n", ",").split(",") if w.strip()]


@router.post("/assistant/rules", response_class=HTMLResponse)
async def rules_save(request: Request):
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    tr = await _setup_runtime(tid)
    m = tr.package.manifest
    f = await request.form()
    overrides = []
    for g in m.guidelines:
        enabled = bool(f.get(f"g_on_{g.id}"))
        raw = str(f.get(f"g_priority_{g.id}") or "").strip()
        priority = int(raw) if raw.lstrip("-").isdigit() else g.priority
        said = str(f.get(f"g_action_{g.id}") or "").strip()
        action = said if said and said != g.action else None      # back to the package's words when they match, or when left blank
        if not enabled or priority != g.priority or action:
            overrides.append({"id": g.id, "enabled": enabled, "priority": priority if priority != g.priority else None, "action": action})
    custom = []
    had = {c.id: c.priority for c in tr.config.guidelines.custom}
    for action, words, intents, stages in zip(f.getlist("c_action"), f.getlist("c_words"), f.getlist("c_intents"), f.getlist("c_stages")):
        if not str(action).strip():
            continue
        cid = f"own_{hashlib.sha256(str(action).encode()).hexdigest()[:8]}"
        custom.append({"id": cid, "action": str(action).strip(), "when_keywords": _words(str(words)), "when_intents": _words(str(intents)), "stages": _words(str(stages)),
                       **({"priority": had[cid]} if cid in had else {})})
    limits = {st.name: int(str(f.get(f"limit_{st.name}"))) for st in m.stages if str(f.get(f"limit_{st.name}") or "").strip().isdigit()
              and int(str(f.get(f"limit_{st.name}"))) != st.max_turns_without_progress and int(str(f.get(f"limit_{st.name}"))) > 0}
    own = [p.strip() for p in str(f.get("own_phrases") or "").splitlines() if p.strip()]
    tools = {}
    for spec in tr.package.tools():
        current = tr.config.tools.get(spec.name)
        if not f.get(f"tool_{spec.name}") or current:
            tools[spec.name] = {"enabled": bool(f.get(f"tool_{spec.name}")), "allowed_stages": current.allowed_stages if current else None,
                                "timeout_s": current.timeout_s if current else 5}
    disabled = [r for r in policy.can_turn_off(m) if not f.get(f"reason_{r}")]
    vid, errors = await _propose(tr, tid, user, {
        "guidelines.overrides": overrides, "guidelines.custom": custom, "funnel.max_turns_without_progress_override": limits,
        "handoff.disabled_reasons": disabled, "tools": tools,
        "compliance.forbidden_phrases": list(dict.fromkeys([*m.compliance.forbidden_phrases, *own])),
    }, str(f.get("note") or "Ajustes de regras").strip())
    if errors:
        return _page(request, "rules.html", {"user": user, "errors": errors, **_rules_ctx(tr)}, 400)
    return _to_config(vid)


# ======================= testing the search =======================
def _rag_from_form(tr, f) -> dict:
    """What a person is trying now: a preset or their own numbers, over the current settings. Nothing is saved by trying."""
    c = tr.config.rag
    base = {"faq_threshold": c.faq_threshold, "min_score": c.min_score, "top_k": c.top_k}
    preset = str(f.get("preset") or "")
    if preset in RAG_PRESETS:
        return {**base, **RAG_PRESETS[preset]}
    out = dict(base)
    for key, cast in (("faq_threshold", float), ("min_score", float), ("top_k", int)):
        raw = str(f.get(key) or "").replace(",", ".").strip()
        try:
            out[key] = cast(raw) if raw else out[key]
        except ValueError:
            pass
    return out


def _test_ctx(tr, values: dict, **more) -> dict:
    return {"values": values, "presets": RAG_PRESETS, "current": tr.config.rag, "preset_labels": ui.RAG_PRESET, **more}


@router.get("/knowledge/test", response_class=HTMLResponse)
async def knowledge_test_page(request: Request):
    user = require(request, *KB_ROLES)
    tr = await _setup_runtime(_tid(user))
    c = tr.config.rag
    return _page(request, "knowledge_test.html", {"user": user, **_test_ctx(tr, {"faq_threshold": c.faq_threshold, "min_score": c.min_score, "top_k": c.top_k})})


@router.post("/knowledge/test", response_class=HTMLResponse)
async def knowledge_test(request: Request):
    from agentplatform.runtime import get_runtime
    user = await require_post(request, *KB_ROLES)
    tid = _tid(user)
    tr = await _setup_runtime(tid)
    f = await request.form()
    question = mask(str(f.get("question") or "").strip()[:500]).text      # the same masking a customer's message gets before leaving the server
    values = _rag_from_form(tr, f)
    ctx = _test_ctx(tr, values, question=question)
    if not question:
        return _page(request, "knowledge_test.html", {"user": user, **ctx, "error": "Escreva uma pergunta."}, 400)
    try:
        rag = tr.config.rag.model_copy(update=PlatformConfig.model_validate({**json.loads(tr.config.model_dump_json()), "rag": {
            **json.loads(tr.config.rag.model_dump_json()), **values}}).rag.model_dump())
    except ValidationError as e:
        return _page(request, "knowledge_test.html", {"user": user, **ctx, "error": "; ".join(_problems(e))}, 400)
    gateway = get_runtime().gateway
    if gateway is None:
        return _page(request, "knowledge_test.html", {"user": user, **ctx, "error": ui.KB_REFUSAL["no_gateway"]}, 503)
    service = KnowledgeService(gateway, tr.config.models.embedding, rag, tr.package.manifest.name)
    try:
        async with tenant_tx(tid) as conn:
            found = await service.inspect(conn, question, tenant_context(tr.config.budget, tid, "knowledge_test", purpose="knowledge_test"))
    except BudgetDenied:
        return _page(request, "knowledge_test.html", {"user": user, **ctx, "error": ui.KB_ERROR["budget"]}, 402)
    except ModelCallError:
        return _page(request, "knowledge_test.html", {"user": user, **ctx, "error": ui.KB_ERROR["provider"]}, 503)
    return _page(request, "knowledge_test.html", {"user": user, **ctx, "found": found})


@router.post("/knowledge/settings", response_class=HTMLResponse)
async def knowledge_settings(request: Request):
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    tr = await _setup_runtime(tid)
    f = await request.form()
    values = _rag_from_form(tr, f)
    vid, errors = await _propose(tr, tid, user, {f"rag.{k}": v for k, v in values.items()}, str(f.get("note") or "Ajuste da busca").strip())
    if errors:
        return _page(request, "knowledge_test.html", {"user": user, **_test_ctx(tr, values), "error": "; ".join(errors)}, 400)
    return _to_config(vid)


# ======================= replacing the embedding model =======================
@router.post("/documents/reindex", response_class=HTMLResponse)
async def reindex_start(request: Request, model: str = Form("")):
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    if not _can_index():
        return RedirectResponse("/console/documents?" + ui.query(erro=ui.kb_refusal("provider_blocked", "")), status_code=303)
    tr = await _setup_runtime(tid)
    try:
        async with tenant_tx(tid) as conn:
            slug = (await (await conn.execute("select slug from tenants where id=%s", (tid,))).fetchone())["slug"]
            run = await K.start_reindex(conn, tid, slug, UUID(user["id"]), model, tr.config.rag.active_index_id)
            await _audit(conn, tid, UUID(user["id"]), "kb.reindex_start", str(run), {"model": model})
    except K.Refused as e:
        return RedirectResponse("/console/documents?" + ui.query(erro=ui.kb_refusal(e.reason, e.detail)), status_code=303)
    await _queue([K.reindex_job(tid, run)])
    return RedirectResponse("/console/documents", status_code=303)


@router.post("/documents/reindex/{run_id}/{action}")
async def reindex_action(request: Request, run_id: UUID, action: str):
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    try:
        async with tenant_tx(tid) as conn:
            if action == "retry":
                await K.retry_reindex(conn, tid, run_id)
            elif action == "cancel":
                await K.cancel_reindex(conn, tid, run_id)
            else:
                raise HTTPException(404)
            await _audit(conn, tid, UUID(user["id"]), f"kb.reindex_{action}", str(run_id))
    except K.Refused:
        raise HTTPException(404) from None
    if action == "retry":
        await _queue([K.reindex_job(tid, run_id)])
    return RedirectResponse("/console/documents", status_code=303)


@router.post("/documents/conflicts/{conflict_id}/dismiss")
async def conflict_dismiss(request: Request, conflict_id: UUID, back: str = Form("/console/documents")):
    user = await require_post(request, *KB_ROLES)
    tid = _tid(user)
    try:
        async with tenant_tx(tid) as conn:
            await K.dismiss_conflict(conn, tid, conflict_id, UUID(user["id"]))
            await _audit(conn, tid, UUID(user["id"]), "kb.conflict_dismiss", str(conflict_id))
    except K.Refused:
        raise HTTPException(404) from None
    return RedirectResponse(back if back.startswith("/console/documents") else "/console/documents", status_code=303)


# ======================= the rest of the settings =======================
def _at(doc: dict, path: str):
    for part in path.split("."):
        doc = doc.get(part) if isinstance(doc, dict) else None
    return doc


def _shown(s: ui.Setting, value):
    if s.kind == "lines":
        return "\n".join(value or [])
    if s.kind == "percents":
        return ", ".join(f"{float(x) * 100:g}" for x in value or [])
    return "" if value is None else value


def _posted(s: ui.Setting, f, current):
    """What a field holds as the configuration wants it. ValueError: the sentence for the person."""
    if s.kind == "bool":
        return s.path in f
    if s.kind in ("map_text", "map_int"):
        out = {}
        for key in current or {}:
            raw = str(f.get(f"{s.path}:{key}") or "").strip()
            if not raw:
                raise ValueError(f"{s.label}: preencha todos os campos.")
            if s.kind == "map_int" and not raw.isdigit():
                raise ValueError(f"{s.label}: use números inteiros.")
            out[key] = int(raw) if s.kind == "map_int" else raw
        return out
    raw = str(f.get(s.path) or "").strip()
    if s.kind == "lines":
        return [x.strip() for x in raw.splitlines() if x.strip()]
    if s.kind == "percents":
        try:
            return [float(x.strip().replace(",", ".")) / 100 for x in raw.replace(";", ",").split(",") if x.strip()]
        except ValueError:
            raise ValueError(f"{s.label}: use números separados por vírgula.") from None
    if not raw:
        if s.optional:
            return None
        raise ValueError(f"{s.label}: não pode ficar em branco.")
    if s.kind == "int":
        if not raw.isdigit():
            raise ValueError(f"{s.label}: use um número inteiro.")
        return int(raw)
    if s.kind in ("number", "usd"):
        try:
            float(raw.replace(",", "."))
        except ValueError:
            raise ValueError(f"{s.label}: use um número.") from None
        return raw.replace(",", ".")
    if s.kind == "choice" and raw not in s.options:
        raise ValueError(f"{s.label}: escolha uma das opções.")
    if s.kind == "text":
        names = {n for _, n, _, _ in string.Formatter().parse(raw.replace("{{", "").replace("}}", "")) if n is not None}
        if names - set(s.placeholders):
            raise ValueError(f"{s.label}: " + (f"só pode usar {', '.join('{' + p + '}' for p in s.placeholders)}." if s.placeholders else "não use chaves { } neste texto."))
    return raw


def _more_page(request: Request, user: dict, tr, f=None, errors=(), saved: str | None = None, status_code: int = 200) -> HTMLResponse:
    doc = json.loads(tr.config.model_dump_json())
    groups = []
    for g in ui.MORE_SETTINGS:
        rows = []
        for s in g["settings"]:
            current = _at(doc, s.path)
            if f is None:
                value = _shown(s, current)
            elif s.kind == "bool":
                value = s.path in f
            elif s.kind in ("map_text", "map_int"):
                value = {k: f.get(f"{s.path}:{k}", v) for k, v in (current or {}).items()}
            else:
                value = f.get(s.path, "")
            rows.append({"s": s, "value": value})
        groups.append({**g, "rows": rows})
    subs = [{"i": i, "name": s.name, "purpose": s.purpose, "country": s.country, "mechanism": (f.get(f"sub_{i}", s.transfer_mechanism) if f is not None else s.transfer_mechanism)}
            for i, s in enumerate(tr.config.subprocessors)]
    go_live = ("go_live" in f) if f is not None else bool(tr.config.operations.feature_flags.get("go_live"))
    return _page(request, "more.html", {"user": user, "groups": groups, "subs": subs, "go_live": go_live, "env": tr.config.environment, "errors": errors, "saved": saved,
                                        "TRANSFER": ui.TRANSFER, "note": f.get("note", "") if f is not None else ""}, status_code)


@router.get("/assistant/more", response_class=HTMLResponse)
async def more_page(request: Request):
    user = require(request, "tenant_owner")
    return _more_page(request, user, await _setup_runtime(_tid(user)))


@router.post("/assistant/more", response_class=HTMLResponse)
async def more_save(request: Request):
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    tr = await _setup_runtime(tid)
    f = await request.form()
    doc = json.loads(tr.config.model_dump_json())
    changes, errors = {}, []
    for g in ui.MORE_SETTINGS:
        for s in g["settings"]:
            try:
                changes[s.path] = _posted(s, f, _at(doc, s.path))
            except ValueError as e:
                errors.append(str(e))
    subs = doc["subprocessors"]
    for i, s in enumerate(subs):
        chosen = str(f.get(f"sub_{i}") or s["transfer_mechanism"])
        if chosen in ui.TRANSFER:
            s["transfer_mechanism"] = chosen
    changes["subprocessors"] = subs
    flags = dict(doc["operations"]["feature_flags"])
    flags["go_live"] = "go_live" in f
    if not flags["go_live"] and "go_live" not in doc["operations"]["feature_flags"]:
        flags.pop("go_live")
    changes["operations.feature_flags"] = flags
    vid = None
    if not errors:
        vid, errors = await _propose(tr, tid, user, changes, str(f.get("note") or "Mais ajustes").strip())
    if errors:
        return _more_page(request, user, tr, f, errors, status_code=400)
    return _to_config(vid) if vid else _more_page(request, user, tr, saved="Nada mudou.")


# ======================= follow-ups =======================
def _rule_name(cad) -> str:
    return cad.label or ui.CADENCE.get(cad.id, ui.humanize(cad.id))


def _shortest_window() -> timedelta | None:
    """A rule that waits past the channel's window would never send. The tightest window wins: the rule runs on every channel."""
    from agentplatform.runtime import get_runtime
    windows = [a.capabilities().service_window for a in get_runtime().adapters.values()]
    return min((w for w in windows if w), default=None)


def _rules_view(tr, f=None) -> list[dict]:
    """Every rule, plus a blank one to add. Each rule also shows a blank attempt while it has room for another."""
    fu, words = tr.config.followups, {**tr.package.manifest.templates, **tr.config.followups.texts}
    rules = []
    for i, c in enumerate([*fu.cadences, None]):
        said = {"delay": "", "text": ""}
        steps = [{"j": j, "delay": st.delay, "text": words.get(st.template_key or "", ""), "editable": st.content_kind == "template"}
                 for j, st in enumerate(c.steps)] if c else []
        steps += [{"j": j, **said, "editable": True} for j in range(len(steps), fu.max_steps)]
        chosen = set(trigger_stages(c.trigger)) if c else set()
        rule = {"i": i, "is_new": c is None, "name": _rule_name(c) if c else "", "enabled": c.enabled if c else True, "event": c.trigger.get("event") if c else "bot_message_unanswered"}
        if f is not None:
            rule.update(name=str(f.get(f"rule_{i}_label") or ""), enabled=f"rule_{i}_enabled" in f, event=str(f.get(f"rule_{i}_event") or rule["event"]))
            chosen = {k.rsplit("_stage_", 1)[1] for k in f if k.startswith(f"rule_{i}_stage_")}
            steps = [{**s, "delay": str(f.get(f"rule_{i}_{s['j']}_delay") or ""), "text": str(f.get(f"rule_{i}_{s['j']}_text") or "")} for s in steps]
        rules.append({**rule, "stages": chosen, "steps": steps})
    return rules


def _posted_rules(tr, f) -> tuple[list[dict], dict, list[str]]:
    """What the form says, checked. A rule or an attempt left completely blank is not there."""
    fu = tr.config.followups
    templates = tr.package.manifest.templates
    allowed = {s.name for s in tr.package.manifest.stages if not s.is_terminal}
    window, shortest = _shortest_window(), parse_delay(fu.min_delay)
    old_rules = json.loads(fu.model_dump_json())["cadences"]
    taken = {c["id"] for c in old_rules}
    cadences, errors = [], []
    overrides = dict(fu.texts)
    used = set()
    for i in range(len(old_rules) + 1):
        old = old_rules[i] if i < len(old_rules) else None
        if f"rule_{i}_remove" in f:
            continue
        label = str(f.get(f"rule_{i}_label") or "").strip()
        raw_steps = [(j, str(f.get(f"rule_{i}_{j}_delay") or "").strip(), str(f.get(f"rule_{i}_{j}_text") or "").strip()) for j in range(fu.max_steps)]
        if old is None and not label and not any(d or t for _, d, t in raw_steps):
            continue
        name = label or (_rule_name(fu.cadences[i]) if old else "Nova regra")
        if not label:
            errors.append(f"{name}: dê um nome à regra.")
        event = str(f.get(f"rule_{i}_event") or "")
        if event not in ui.FOLLOWUP_EVENT:
            errors.append(f"{name}: escolha quando a regra vale.")
        stages = [s.name for s in tr.package.manifest.stages if s.name in allowed and f"rule_{i}_stage_{s.name}" in f]
        if not stages:
            errors.append(f"{name}: escolha ao menos uma etapa.")
        steps, total = [], timedelta(0)
        rid = old["id"] if old else _new_rule_id(label, taken)
        for j, delay, said in raw_steps:
            before = old["steps"][j] if old and j < len(old["steps"]) else None
            fixed = before is not None and before["content_kind"] != "template"
            if not delay and (fixed or not said):
                continue
            try:
                wait = parse_delay(delay)
            except ValueError:
                errors.append(f"{name}: use o tempo como 30m, 4h ou 2d.")
                continue
            if wait < shortest:
                errors.append(f"{name}: o tempo mínimo é {fu.min_delay}.")
            total += wait
            if window and total >= window:
                errors.append(f"{name}: a tentativa sairia depois de {_hours(total)}, com a janela do canal já fechada. Use menos de {_hours(window)} no total.")
            if fixed:
                steps.append({**before, "delay": delay})
                continue
            if not said:
                errors.append(f"{name}: o texto não pode ficar em branco.")
                continue
            errors += [f"{name}: {ui.text_problem(p)}" for p in text_problems(tr.config, "followup", said, tr.config.comments.private_max_chars, tr.allowed_urls)]
            key = (before or {}).get("template_key") or f"{rid}_{j}"
            used.add(key)
            if said != templates.get(key):
                overrides[key] = said
            else:
                overrides.pop(key, None)
            steps.append({**(before or {"content_kind": "template"}), "delay": delay, "template_key": key})
        if not steps:
            errors.append(f"{name}: a regra precisa de ao menos uma tentativa.")
        same = old and old["trigger"].get("event") == event and sorted(trigger_stages(old["trigger"])) == sorted(stages)
        unnamed = old and not old.get("label") and fu.cadences[i] and label == _rule_name(fu.cadences[i])
        cadences.append({**(old or {}), "id": rid, "label": "" if unnamed else label, "enabled": f"rule_{i}_enabled" in f,
                         "trigger": old["trigger"] if same else {"event": event, "stages": stages}, "steps": steps, "max_attempts": len(steps) or 1})
    gone = {st["template_key"] for c in old_rules for st in c["steps"] if st.get("template_key")} - used
    return cadences, {k: v for k, v in overrides.items() if k not in gone}, errors


def _hours(d: timedelta) -> str:
    return f"{d.total_seconds() / 3600:g} h"


def _new_rule_id(label: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", unicodedata.normalize("NFKD", label.lower()).encode("ascii", "ignore").decode()).strip("_") or "regra"
    rid, n = base, 2
    while rid in taken:
        rid, n = f"{base}_{n}", n + 1
    taken.add(rid)
    return rid


async def _followups_page(request: Request, user: dict, tr, f=None, errors: list[str] | None = None, status_code: int = 200) -> HTMLResponse:
    tid = _tid(user)
    fu, m = tr.config.followups, tr.package.manifest
    shown = str(request.query_params.get("parado") or fu.stalled_after).strip()
    try:
        silence = parse_delay(shown)
    except ValueError:
        shown, silence = fu.stalled_after, parse_delay(fu.stalled_after)
    who = """join conversations c on c.id = f.conversation_id join contacts ct on ct.id = c.contact_id
             join contact_identities ci on ci.contact_id = c.contact_id"""
    people = "ci.channel <> 'simulator' and ci.external_user_id not like 'LAB-%%'"
    async with tenant_tx(tid) as conn:
        stalled = await (await conn.execute(
            f"""select c.id, c.last_reply_at, c.window_expires_at, l.stage, ci.username, ci.channel, ci.external_user_id, ct.first_name,
                       coalesce(c.window_expires_at > now(), false) as window_open,
                       (select min(r.due_at) from followup_runs r where r.conversation_id = c.id and r.status = 'scheduled') as next_due
                from conversations c join leads l on l.contact_id = c.contact_id join contacts ct on ct.id = c.contact_id
                join contact_identities ci on ci.contact_id = c.contact_id
                where {people} and c.ownership = 'BOT' and l.opted_out_at is null and not (l.stage = any(%s))
                  and c.last_inbound_at is not null and c.last_reply_at > c.last_inbound_at and c.last_reply_at < now() - %s
                order by c.last_reply_at desc limit 100""", ([s.name for s in m.stages if s.is_terminal], silence))).fetchall()
        upcoming = await (await conn.execute(
            f"""select f.cadence_id, f.due_at, c.id, ci.username, ci.channel, ci.external_user_id, ct.first_name
                from followup_runs f {who} where {people} and f.status = 'scheduled' order by f.due_at limit 50""")).fetchall()
        recent = await (await conn.execute(
            f"""select f.cadence_id, f.status, f.stop_reason, coalesce(o.sent_at, f.due_at) as at, o.status as outbox_status,
                       c.id, ci.username, ci.channel, ci.external_user_id, ct.first_name
                from followup_runs f {who} left join outbox o on o.id = f.outbox_id
                where {people} and f.status <> 'scheduled' order by coalesce(o.sent_at, f.due_at) desc limit 50""")).fetchall()
        sent = (await (await conn.execute(
            "select count(*) as n from followup_runs f join outbox o on o.id = f.outbox_id where o.status = 'sent' and o.sent_at > now() - interval '7 days'")).fetchone())["n"]
    names = {c.id: _rule_name(c) for c in fu.cadences}
    history = [{**r, "shown_status": "sent" if r["outbox_status"] == "sent" else r["status"]} for r in recent]
    return _page(request, "followups.html", {
        "user": user, "can_edit": user["role"] in ("tenant_owner", "platform_admin"), "rules": _rules_view(tr, f), "errors": errors or [], "names": names,
        "on": ("followups_enabled" in f) if f is not None else fu.enabled, "stalled": stalled, "upcoming": upcoming, "history": history, "sent": sent, "shown": shown,
        "stalled_after": (f.get("stalled_after", fu.stalled_after) if f is not None else fu.stalled_after), "stages": [s for s in m.stages if not s.is_terminal],
        "EVENT": ui.FOLLOWUP_EVENT, "STATUS": ui.FOLLOWUP_STATUS, "sending": tr.config.sending, "note": f.get("note", "") if f is not None else "",
        "min_delay": fu.min_delay, "window": _hours(w) if (w := _shortest_window()) else None, "labels": await _labels(tid)}, status_code)


@router.get("/followups", response_class=HTMLResponse)
async def followups(request: Request):
    user = require(request)
    return await _followups_page(request, user, await _setup_runtime(_tid(user)))


@router.post("/followups", response_class=HTMLResponse)
async def followups_save(request: Request):
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    tr = await _setup_runtime(tid)
    f = await request.form()
    cadences, texts, errors = _posted_rules(tr, f)
    stalled_after = str(f.get("stalled_after") or "").strip()
    try:
        parse_delay(stalled_after)
    except ValueError:
        errors.append("Contato parado: use o tempo como 30m, 4h ou 2d.")
    vid = None
    if not errors:
        vid, errors = await _propose(tr, tid, user, {"followups.enabled": "followups_enabled" in f, "followups.cadences": cadences, "followups.texts": texts,
                                                     "followups.stalled_after": stalled_after}, str(f.get("note") or "Follow-ups").strip())
    if errors:
        return await _followups_page(request, user, tr, f, errors, 400)
    return _to_config(vid)


# ======================= joining the console =======================
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def users_page(request: Request, user: dict, *, edit: dict | None = None, error: str | None = None, status_code: int = 200) -> HTMLResponse:
    """The row being edited shows its alert contacts in clear text, so the browser keeps no copy."""
    tid = _tid(user)
    raw = request.query_params.get("edit")
    if edit is None and raw:
        try:
            uid = UUID(raw)
        except ValueError:
            raise HTTPException(404) from None
        async with tenant_tx(tid) as conn:
            row = await (await conn.execute("select id, display_name, notify_whatsapp_enc, notify_email_enc from tenant_users where id=%s and tenant_id=%s and is_active", (uid, tid))).fetchone()
            if not row:
                raise HTTPException(404)
            edit = {"id": row["id"], "name": row["display_name"], "whatsapp": await decrypt_field(conn, tid, row["notify_whatsapp_enc"], "tenant_users.whatsapp") or "",
                    "email": await decrypt_field(conn, tid, row["notify_email_enc"], "tenant_users.email") or ""}
    resp = _page(request, "users.html", {"user": user, **await users_context(user), "edit": edit, "error": error}, status_code)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/users/{uid}/edit", response_class=HTMLResponse)
async def user_edit(request: Request, uid: UUID, name: str = Form(""), whatsapp: str = Form(""), email: str = Form("")):
    """How this person appears, and where the alerts of conversations waiting for them go."""
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    name, email = name.strip()[:100], email.strip().lower()[:200]
    digits = re.sub(r"[\s()+-]", "", whatsapp)
    form = {"id": uid, "name": name, "whatsapp": whatsapp, "email": email}
    error = None
    if not name:
        error = "O nome não pode ficar em branco."
    elif digits and not re.fullmatch(r"\d{10,15}", digits):
        error = "WhatsApp inválido. Use o número com DDI e DDD, como 55 11 99999-0000."
    elif email and "@" not in email:
        error = "E-mail de aviso inválido."
    if error:
        return await users_page(request, user, edit=form, error=error, status_code=400)
    async with tenant_tx(tid) as conn:
        row = await (await conn.execute(
            "update tenant_users set display_name=%s, notify_whatsapp_enc=%s, notify_email_enc=%s where id=%s and tenant_id=%s and is_active returning id",
            (name, await encrypt_field(conn, tid, digits or None, "tenant_users.whatsapp"), await encrypt_field(conn, tid, email or None, "tenant_users.email"), uid, tid))).fetchone()
        if not row:
            raise HTTPException(404)
        await _audit(conn, tid, UUID(user["id"]), "user.edit", str(uid), {"whatsapp": bool(digits), "email": bool(email)})
    return RedirectResponse("/console/users", status_code=303)


@router.post("/users/{uid}/reactivate", response_class=HTMLResponse)
async def user_reactivate(request: Request, uid: UUID):
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    async with tenant_tx(tid) as conn:
        row = await (await conn.execute("update tenant_users set is_active=true where id=%s and tenant_id=%s and not is_active returning id", (uid, tid))).fetchone()
        if not row:
            raise HTTPException(404)
        await _audit(conn, tid, UUID(user["id"]), "user.reactivate", str(uid))
    return RedirectResponse("/console/users", status_code=303)


async def users_context(user: dict) -> dict:
    tid = _tid(user)
    async with platform_tx() as conn:
        rows = await (await conn.execute(
            """select u.id, u.display_name, u.role, u.totp_enabled_at, u.locked_until, u.tenant_id, null as slug,
                      u.notify_whatsapp_enc is not null as has_whatsapp, u.notify_email_enc is not null as has_email from tenant_users u
               where u.tenant_id = %s and u.is_active order by u.display_name""", (tid,))).fetchall()
        inactive = await (await conn.execute("select id, display_name, role from tenant_users where tenant_id=%s and not is_active order by display_name", (tid,))).fetchall()
        invites = await (await conn.execute(
            """select id, display_name, role, expires_at, created_at from user_invites
               where tenant_id=%s and used_at is null and revoked_at is null and expires_at > now() order by created_at desc""", (tid,))).fetchall()
    return {"rows": rows, "invites": invites, "invite_roles": INVITE_ROLES, "inactive": inactive}


@router.post("/users/invite", response_class=HTMLResponse)
async def user_invite(request: Request, name: str = Form(""), email: str = Form(""), role: str = Form("")):
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    name, email = name.strip()[:100], email.strip().lower()[:200]
    error = None
    if not name or "@" not in email:
        error = "Preencha o nome e um e-mail válido."
    elif role not in INVITE_ROLES:
        error = "Escolha o perfil."
    tr = await _setup_runtime(tid)
    h = crypto().lookup_hash(email, kind="email") if not error else None
    async with platform_tx() as conn:
        if not error and await (await conn.execute("select 1 from tenant_users where email_hash=%s", (h,))).fetchone():
            error = "Já existe um usuário com esse e-mail."
    if error:
        return _page(request, "users.html", {"user": user, **await users_context(user), "error": error}, 400)
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=tr.config.operations.invite_valid_hours)
    async with tenant_tx(tid) as conn:
        await conn.execute(
            """insert into user_invites (tenant_id, token_hash, email_hash, email_enc, display_name, role, created_by, expires_at)
               values (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (tid, _token_hash(token), h.hex() if isinstance(h, bytes) else str(h), await encrypt_field(conn, tid, email, "user_invites.email"), name, role, UUID(user["id"]), expires))
        await _audit(conn, tid, UUID(user["id"]), "user.invite", role)
    link = get_settings().public_base_url.rstrip("/") + f"/console/invite/{token}"      # shown once; only the hash is stored
    return _page(request, "users.html", {"user": user, **await users_context(user), "invite_link": link, "invited": name})


@router.post("/users/invites/{invite_id}/revoke")
async def invite_revoke(request: Request, invite_id: UUID):
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    async with tenant_tx(tid) as conn:
        row = await (await conn.execute("update user_invites set revoked_at=now() where id=%s and tenant_id=%s and used_at is null returning id", (invite_id, tid))).fetchone()
        if not row:
            raise HTTPException(404)
        await _audit(conn, tid, UUID(user["id"]), "user.invite_revoke", str(invite_id))
    return RedirectResponse("/console/users", status_code=303)


async def _other_owner_exists(conn, tid: UUID, uid: UUID) -> bool:
    return bool(await (await conn.execute(
        "select 1 from tenant_users where tenant_id=%s and id<>%s and is_active and role='tenant_owner' limit 1", (tid, uid))).fetchone())


@router.post("/users/{uid}/deactivate", response_class=HTMLResponse)
async def user_deactivate(request: Request, uid: UUID):
    """The account stops working at once (auth.session_alive). What this person did stays attributed to them."""
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    error = None
    async with platform_tx() as conn:
        target = await (await conn.execute("select role from tenant_users where id=%s and tenant_id=%s and is_active", (uid, tid))).fetchone()
        if not target:
            raise HTTPException(404)
        if str(uid) == user["id"]:
            error = "Você não pode desativar a sua própria conta."
        elif target["role"] == "tenant_owner" and not await _other_owner_exists(conn, tid, uid):
            error = "É o único dono da conta. Convide outro dono antes."
        else:
            await conn.execute("update tenant_users set is_active=false where id=%s", (uid,))
            forget_session(str(uid))
            await _audit(conn, tid, UUID(user["id"]), "user.deactivate", str(uid), {"role": target["role"]})
    if error:
        return _page(request, "users.html", {"user": user, **await users_context(user), "error": error}, 400)
    return RedirectResponse("/console/users", status_code=303)


@router.post("/users/{uid}/role", response_class=HTMLResponse)
async def user_role(request: Request, uid: UUID, role: str = Form("")):
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    if role not in INVITE_ROLES:
        raise HTTPException(400)
    error = None
    async with platform_tx() as conn:
        target = await (await conn.execute("select role from tenant_users where id=%s and tenant_id=%s and is_active", (uid, tid))).fetchone()
        if not target:
            raise HTTPException(404)
        if target["role"] == "tenant_owner" and role != "tenant_owner" and not await _other_owner_exists(conn, tid, uid):
            error = "É o único dono da conta. Convide outro dono antes."
        elif target["role"] != role:
            await conn.execute("update tenant_users set role=%s where id=%s", (role, uid))
            forget_session(str(uid))
            await _audit(conn, tid, UUID(user["id"]), "user.role", str(uid), {"from": target["role"], "to": role})
    if error:
        return _page(request, "users.html", {"user": user, **await users_context(user), "error": error}, 400)
    return RedirectResponse("/console/users", status_code=303)


async def _open_invite(token: str) -> dict | None:
    async with platform_tx() as conn:
        return await (await conn.execute(
            """select i.id, i.tenant_id, i.display_name, i.role, i.email_hash, i.email_enc, t.display_name as tenant from user_invites i join tenants t on t.id = i.tenant_id
               where i.token_hash=%s and i.used_at is null and i.revoked_at is null and i.expires_at > now()""", (_token_hash(token),))).fetchone()


@router.get("/invite/{token}", response_class=HTMLResponse)
async def invite_page(request: Request, token: str):
    inv = await _open_invite(token)
    resp = _page(request, "invite.html", {"invite": inv, "token": token, "error": None, "min_len": MIN_PASSWORD_LEN, "two_factor": bool(inv) and totp_required(inv["role"])}, 200 if inv else 404)
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@router.post("/invite/{token}", response_class=HTMLResponse)
async def invite_accept(request: Request, token: str, password: str = Form(""), confirm: str = Form("")):
    """The link is the proof: it was given to this person, it works once and it expires. The password never passes through anyone else."""
    inv = await _open_invite(token)
    error = None
    if not inv:
        return _page(request, "invite.html", {"invite": None, "token": token, "error": None, "min_len": MIN_PASSWORD_LEN, "two_factor": False}, 404)
    if len(password) < MIN_PASSWORD_LEN:
        error = f"A senha precisa ter pelo menos {MIN_PASSWORD_LEN} caracteres."
    elif password != confirm:
        error = "As duas senhas não são iguais."
    if error:
        return _page(request, "invite.html", {"invite": inv, "token": token, "error": error, "min_len": MIN_PASSWORD_LEN, "two_factor": totp_required(inv["role"])}, 400)
    email_hash = bytes.fromhex(inv["email_hash"])
    async with platform_tx() as conn:
        claimed = await (await conn.execute("update user_invites set used_at=now() where id=%s and used_at is null returning id", (inv["id"],))).fetchone()
        if not claimed or await (await conn.execute("select 1 from tenant_users where email_hash=%s", (email_hash,))).fetchone():
            return _page(request, "invite.html", {"invite": None, "token": token, "error": None, "min_len": MIN_PASSWORD_LEN, "two_factor": False}, 404)
        from agentplatform.core.security.keys import decrypt_field
        email = await decrypt_field(conn, inv["tenant_id"], inv["email_enc"], "user_invites.email")
        email_enc = await encrypt_field(conn, inv["tenant_id"], email, "tenant_users.email")
        row = await (await conn.execute(
            """insert into tenant_users (tenant_id, email_hash, email_enc, display_name, role, password_hash, notify_email_enc)
               values (%s,%s,%s,%s,%s,%s,%s) returning id""",
            (inv["tenant_id"], email_hash, email_enc, inv["display_name"], inv["role"], hash_password(password), email_enc))).fetchone()
        await conn.execute(
            "insert into audit_log (tenant_id, actor, actor_kind, action, target, after) values (%s,%s,'user','user.joined',%s,%s)",
            (inv["tenant_id"], row["id"], str(row["id"]), json.dumps({"role": inv["role"]})))
    return RedirectResponse("/console/login", status_code=303)


# ======================= what is missing to go live =======================
async def _setup_items(tid: UUID) -> list[dict]:
    settings = get_settings()
    try:
        tr = await _setup_runtime(tid)
    except ConfigError:
        return [{"key": "config", "state": "todo", "link": "/console/config"}]
    cfg, env = tr.config, settings.app_env
    async with tenant_tx(tid) as conn:
        q = lambda sql, *a: conn.execute(sql, a)
        versions = await (await q("select status, eval_run_id from config_versions where tenant_id=%s and env=%s", tid, env)).fetchall()
        channel = await (await q("select channel, token_status, token_enc is not null as has_token from channel_accounts where tenant_id=%s and channel <> 'simulator'", tid)).fetchall()
        docs = await (await q("select status, count(*) as n from kb_documents group by 1")).fetchall()
        faqs = await (await q("select count(*) as n from faqs where status='approved'")).fetchone()
        draft_faqs = await (await q("select count(*) as n from faqs where status='draft'")).fetchone()
        questions = await (await q("select count(*) as n from curation_queue where tenant_id=%s and status='open' and cluster_id is null", tid)).fetchone()
        conflicts = await (await q(
            """select count(*) as n from kb_conflicts k join kb_documents a on a.id=k.document_id join kb_documents b on b.id=k.other_document_id
               where k.tenant_id=%s and k.status='open' and a.status='approved' and b.status='approved'""", tid)).fetchone()
        tables = await (await q("select count(*) as n from domain_datasets where tenant_id=%s and status='active'", tid)).fetchone()
        people = await (await q(      # whoever runs the platform can be the second person of someone working alone
            "select count(*) as n from tenant_users where is_active and ((tenant_id=%s and role='tenant_owner') or role='platform_admin')", tid)).fetchone()
        notify = await (await q("select count(*) as n from tenant_users where tenant_id=%s and is_active and (notify_whatsapp_enc is not null or notify_email_enc is not null)", tid)).fetchone()
        running = await (await q("select status from eval_requests where tenant_id=%s order by created_at desc limit 1", tid)).fetchone()
    doc = {r["status"]: r["n"] for r in docs}
    active = any(v["status"] == "active" for v in versions)
    evaluated = active and env != "prod" or any(v["eval_run_id"] for v in versions if v["status"] in ("draft", "active", "canary"))
    live_ok = any(c["token_status"] == "valid" and c["has_token"] for c in channel)
    pending_legal = [s.name for s in cfg.subprocessors if s.transfer_mechanism == "pending"]
    package_datasets_exist = bool(package_datasets(tr.package))
    state = lambda ok, warn=False: "done" if ok else ("warn" if warn else "todo")
    items = [
        {"key": "identity", "state": state(bool(cfg.conversation.persona.seller_display_name.strip())), "link": "/console/assistant", "detail": cfg.conversation.persona.seller_display_name},
        {"key": "provider", "state": state(_can_index()), "link": None, "detail": settings.llm_provider},
        {"key": "channel", "state": state(live_ok), "link": "/console/channels", "detail": ", ".join(sorted({c["channel"] for c in channel if c["token_status"] == "valid"}))},
        {"key": "tables", "state": state(tables["n"] > 0, warn=not package_datasets_exist), "link": "/console/datasets", "detail": str(tables["n"])},
        {"key": "material", "state": state(doc.get("approved", 0) > 0), "link": "/console/documents",
         "detail": ui.setup_docs(doc.get("approved", 0), doc.get("in_review", 0), doc.get("blocked_pii", 0))},
        {"key": "faqs", "state": state(faqs["n"] > 0), "link": "/console/faqs", "detail": ui.setup_faqs(faqs["n"], draft_faqs["n"])},
        {"key": "conflicts", "state": state(conflicts["n"] == 0), "link": "/console/documents", "detail": str(conflicts["n"])},
        {"key": "curation", "state": state(questions["n"] == 0, warn=True), "link": "/console/curation", "detail": str(questions["n"])},
        {"key": "second_person", "state": state(people["n"] >= 2), "link": "/console/users", "detail": str(people["n"])},
        {"key": "seller_notice", "state": state(notify["n"] > 0), "link": "/console/users", "detail": str(notify["n"])},
        {"key": "legal", "state": state(not pending_legal), "link": "/console/assistant/more#ar", "detail": ", ".join(pending_legal)},
        {"key": "evaluation", "state": state(bool(evaluated)), "link": "/console/config", "detail": ui.EVAL_STATUS.get(running["status"], "") if running else ""},
        {"key": "active", "state": state(active), "link": "/console/config", "detail": ""},
    ]
    if env == "prod":
        items.append({"key": "go_live", "state": state(bool(cfg.operations.feature_flags.get("go_live"))), "link": "/console/assistant/more#ar", "detail": ""})
    return items


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    user = require(request, "tenant_owner")
    items = await _setup_items(_tid(user))
    return _page(request, "setup.html", {"user": user, "items": items, "missing": sum(1 for i in items if i["state"] == "todo"), "labels": ui.SETUP_ITEM})
