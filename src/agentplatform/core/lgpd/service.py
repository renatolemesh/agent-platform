"""LGPD operations (plan 03 §8, 11 L5/L6; F2-LGPD-01/02): retention sweeper and data-subject rights
(access/export, erasure by anonymization) with audit. Runs per tenant; retention days come from PlatformConfig."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

from psycopg import AsyncConnection

from agentplatform.core.agent.policy import _norm_value
from agentplatform.core.config.schema import PlatformConfig
from agentplatform.core.imports.leads import normalize_phone
from agentplatform.core.lgpd.checkpoints import delete_checkpoints, delete_old_checkpoints
from agentplatform.core.security.keys import crypto, decrypt_field, encrypt_field
from agentplatform.core.security.pii import mask


class CorrectionRefused(Exception):
    """A value that cannot be stored; `field` says which one, `reason` is invalid or in_use."""

    def __init__(self, field: str, reason: str):
        super().__init__(f"{field}: {reason}")
        self.field, self.reason = field, reason


async def retention_sweep(conn: AsyncConnection, cfg: PlatformConfig, tenant_id: UUID) -> dict:
    d = cfg.operations.retention_days
    out = {}

    async def run(name: str, sql: str, days: int):
        cur = await conn.execute(sql, (tenant_id, days))
        out[name] = cur.rowcount

    await run("inbound_raw", "update inbound_events set payload_raw = null where tenant_id=%s and payload_raw is not null "
              "and received_at < now() - make_interval(days => %s)", d.get("inbound_raw", 30))
    await run("inbound_payloads", "update inbound_events set payload_canonical = '{}'::jsonb where tenant_id=%s and payload_canonical <> '{}'::jsonb "
              "and status not in ('pending', 'failed') and received_at < now() - make_interval(days => %s)", d.get("inbound_raw", 30))
    await run("messages_original", "update messages set text_enc = null where tenant_id=%s and text_enc is not null "
              "and created_at < now() - make_interval(days => %s)", d.get("messages_original", 365))
    await run("media", "update messages set media = media - 'attachments' where tenant_id=%s and media ? 'attachments' "
              "and created_at < now() - make_interval(days => %s)", d.get("media", 30))
    await run("ownership_evidence", "update ownership_events set evidence = null where tenant_id=%s and evidence is not null "
              "and at < now() - make_interval(days => %s)", d.get("ownership_evidence", 90))
    await run("tool_call_args", "update tool_calls set args_masked = '{}'::jsonb where tenant_id=%s "
              "and created_at < now() - make_interval(days => %s)", d.get("tool_call_args", 90))
    await run("handoff_summaries", "update handoffs set summary = '{}'::jsonb where tenant_id=%s "
              "and created_at < now() - make_interval(days => %s)", d.get("handoff_summaries", 365))
    await run("dead_letters", "delete from dead_letters where tenant_id=%s and at < now() - make_interval(days => %s)",
              d.get("dead_letters", 30))
    await run("comments", "update comment_actions set text_masked = null, author_username = null where tenant_id=%s "
              "and text_masked is not null and created_at < now() - make_interval(days => %s)", d.get("comments", 365))
    await run("lead_forms", "update lead_forms set fields_enc = null where tenant_id=%s and fields_enc is not null "
              "and coalesce(submitted_at, expires_at) < now() - make_interval(days => %s)", d.get("lead_forms", 730))
    await run("reviews", "update conversation_reviews set note_masked = null where tenant_id=%s and note_masked is not null "
              "and created_at < now() - make_interval(days => %s)", d.get("reviews", 365))
    await run("budget_turns", "delete from budget_balances b where b.tenant_id=%s and b.scope like 'turn:%%%%' and b.reserved_usd = 0 and not exists ("
              "select 1 from turns t where 'turn:' || t.id::text = b.scope and t.started_at > now() - make_interval(days => %s))", d.get("checkpoints", 30))
    await run("invites", "delete from user_invites where tenant_id=%s and greatest(expires_at, coalesce(used_at, expires_at), coalesce(revoked_at, expires_at)) "
              "< now() - make_interval(days => %s)", d.get("invites", 30))
    await run("llm_calls", "delete from llm_calls where tenant_id=%s and created_at < now() - make_interval(days => %s)", d.get("llm_calls", 365))
    await run("turn_traces", "update turns set trace = null where tenant_id=%s and trace is not null "
              "and started_at < now() - make_interval(days => %s)", d.get("checkpoints", 30))
    out["checkpoints"] = await delete_old_checkpoints(conn, tenant_id, d.get("checkpoints", 30))
    await conn.execute(
        "insert into audit_log (tenant_id, actor_kind, action, target, after) values (%s,'system','lgpd.retention','tenant',%s)",
        (tenant_id, json.dumps(out)))
    return out


async def find_subject(conn: AsyncConnection, tenant_id: UUID, *, phone: str | None = None, email: str | None = None,
                       channel_user_id: str | None = None) -> UUID | None:
    c = crypto()
    if channel_user_id:
        r = await (await conn.execute("select contact_id from contact_identities where tenant_id=%s and external_user_id=%s",
                                      (tenant_id, channel_user_id))).fetchone()
        if r:
            return r["contact_id"]
    if phone:
        r = await (await conn.execute("select id from contacts where tenant_id=%s and phone_hash=%s",
                                      (tenant_id, c.lookup_hash(phone, kind="phone")))).fetchone()
        if r:
            return r["id"]
    if email:
        r = await (await conn.execute("select id from contacts where tenant_id=%s and email_hash=%s",
                                      (tenant_id, c.lookup_hash(email, kind="email")))).fetchone()
        if r:
            return r["id"]
    return None


async def export_subject(conn: AsyncConnection, tenant_id: UUID, contact_id: UUID, actor: UUID | None) -> dict:
    """Right of access/portability: everything we hold about the subject, decrypted, as JSON."""
    ct = await (await conn.execute("select * from contacts where id=%s", (contact_id,))).fetchone()
    lead = await (await conn.execute("select stage, profile, created_at, opted_out_at from leads where contact_id=%s",
                                     (contact_id,))).fetchone()
    msgs = await (await conn.execute(
        """select m.direction, m.text_enc, m.text_masked, m.provider_ts from messages m join conversations c on c.id=m.conversation_id
           where c.contact_id=%s order by m.provider_ts""", (contact_id,))).fetchall()
    consents = await (await conn.execute("select purpose, legal_basis, status, at from consents where contact_id=%s",
                                         (contact_id,))).fetchall()
    comments = await (await conn.execute(
        """select a.media_id, a.text_masked, a.comment_ts from comment_actions a
           join contact_identities ci on ci.external_user_id = a.author_external_id where ci.contact_id=%s""", (contact_id,))).fetchall()
    reviews = await (await conn.execute(
        """select r.verdict, r.category, r.note_masked, r.created_at from conversation_reviews r join conversations c on c.id = r.conversation_id
           where c.contact_id=%s order by r.created_at""", (contact_id,))).fetchall()
    data = {
        "contact": {"first_name": ct["first_name"],
                    "name": await decrypt_field(conn, tenant_id, ct["display_name_enc"], "contacts.display_name"),
                    "phone": await decrypt_field(conn, tenant_id, ct["phone_enc"], "contacts.phone"),
                    "email": await decrypt_field(conn, tenant_id, ct["email_enc"], "contacts.email")},
        "lead": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in (lead or {}).items()},
        "messages": [{"direction": m["direction"], "at": m["provider_ts"].isoformat(),
                      "text": (await decrypt_field(conn, tenant_id, m["text_enc"], "messages.text")) if m["text_enc"] else m["text_masked"]}
                     for m in msgs],
        "consents": [{**c, "at": c["at"].isoformat()} for c in consents],
        "comments": [{"post": c["media_id"], "text": c["text_masked"], "at": c["comment_ts"].isoformat() if c["comment_ts"] else None}
                     for c in comments],
        "reviews": [{"verdict": r["verdict"], "category": r["category"], "note": r["note_masked"], "at": r["created_at"].isoformat()}
                    for r in reviews],
    }
    await conn.execute("insert into audit_log (tenant_id, actor, actor_kind, action, target) values (%s,%s,'user','lgpd.export',%s)",
                       (tenant_id, actor, str(contact_id)))
    return data


async def subject_details(conn: AsyncConnection, tenant_id: UUID, contact_id: UUID) -> dict | None:
    """What the correction form shows, in clear text: only the account owner reaches it, and the search is audited."""
    ct = await (await conn.execute(
        """select ct.first_name, ct.display_name_enc, ct.phone_enc, ct.email_enc, l.profile
           from contacts ct left join leads l on l.contact_id = ct.id where ct.id=%s""", (contact_id,))).fetchone()
    if not ct:
        return None
    return {"first_name": ct["first_name"] or "",
            "name": await decrypt_field(conn, tenant_id, ct["display_name_enc"], "contacts.display_name") or "",
            "phone": await decrypt_field(conn, tenant_id, ct["phone_enc"], "contacts.phone") or "",
            "email": await decrypt_field(conn, tenant_id, ct["email_enc"], "contacts.email") or "",
            "profile": {k: v.get("value") for k, v in (ct["profile"] or {}).items() if isinstance(v, dict)}}


async def _set_identifier(conn: AsyncConnection, tenant_id: UUID, contact_id: UUID, kind: str, value: str) -> None:
    h = crypto().lookup_hash(value, kind=kind) if value else None
    if h and await (await conn.execute(
            f"select 1 from contacts where tenant_id=%s and {kind}_hash=%s and id<>%s and merged_into is null", (tenant_id, h, contact_id))).fetchone():
        raise CorrectionRefused(kind, "in_use")       # merging two people is a different decision, taken by a person
    await conn.execute(f"update contacts set {kind}_hash=%s, {kind}_enc=%s where id=%s",
                       (h, await encrypt_field(conn, tenant_id, value or None, f"contacts.{kind}"), contact_id))


async def correct_subject(conn: AsyncConnection, tenant_id: UUID, contact_id: UUID, actor: UUID | None, *, first_name: str, name: str,
                          phone: str, email: str, profile: dict[str, str], slots: list) -> list[str]:
    """Right of correction. Old messages stay as the record of what was said (erasure is the way to remove them); the summary is dropped
    so the old value does not come back through it. Returns the changed fields; the audit log gets their names, never their values."""
    now = await subject_details(conn, tenant_id, contact_id)
    if now is None:
        raise CorrectionRefused("contact", "invalid")
    changed: list[str] = []
    first_name, name, email = first_name.strip(), name.strip(), email.strip().lower()
    if first_name != now["first_name"]:
        await conn.execute("update contacts set first_name=%s where id=%s", (first_name or None, contact_id))
        changed.append("first_name")
    if name != now["name"]:
        await conn.execute("update contacts set display_name_enc=%s where id=%s",
                           (await encrypt_field(conn, tenant_id, name or None, "contacts.display_name"), contact_id))
        changed.append("name")
    if phone.strip() != now["phone"]:
        new_phone = normalize_phone(phone) if phone.strip() else ""
        if new_phone is None:
            raise CorrectionRefused("phone", "invalid")
        if new_phone != now["phone"]:
            await _set_identifier(conn, tenant_id, contact_id, "phone", new_phone)
            if now["phone"]:      # the number is also the contact's address on that channel
                await conn.execute(
                    """update contact_identities set external_user_id = coalesce(nullif(%s, ''), 'erased:'||id::text)
                       where contact_id=%s and channel='whatsapp' and external_user_id=%s""", (new_phone, contact_id, now["phone"]))
            changed.append("phone")
    if email != now["email"]:
        if email and "@" not in email:
            raise CorrectionRefused("email", "invalid")
        await _set_identifier(conn, tenant_id, contact_id, "email", email)
        changed.append("email")
    specs = {s.name: s for s in slots}
    current = await (await conn.execute("select profile from leads where contact_id=%s", (contact_id,))).fetchone()
    new_profile = dict((current or {}).get("profile") or {})
    for slot, raw in profile.items():
        spec = specs.get(slot)
        if spec is None:
            continue
        text = mask(raw.strip()).text
        if text == str(now["profile"].get(slot, "") if now["profile"].get(slot) is not None else ""):
            continue
        if not text:
            new_profile.pop(slot, None)
        else:
            value = _norm_value(spec.type, text, spec.values, spec.synonyms)
            if value is None:
                raise CorrectionRefused(slot, "invalid")
            new_profile[slot] = {"value": value, "source_message_id": None, "confidence": 1.0, "source": "correction",
                                 "at": datetime.now(timezone.utc).isoformat()}
        changed.append(f"profile.{slot}")
    if any(c.startswith("profile.") for c in changed):
        await conn.execute("update leads set profile=%s where contact_id=%s", (json.dumps(new_profile), contact_id))
    if changed:
        await conn.execute("update conversations set summary=null where contact_id=%s", (contact_id,))
        await conn.execute("insert into audit_log (tenant_id, actor, actor_kind, action, target, after) values (%s,%s,'user','lgpd.correct',%s,%s)",
                           (tenant_id, actor, str(contact_id), json.dumps({"fields": changed})))
    return changed


async def find_comment_author(conn: AsyncConnection, tenant_id: UUID, channel_user_id: str) -> dict | None:
    """Someone who only commented has no contact record: their comments are still their personal data. Accepts the @ too."""
    ident = channel_user_id.strip().lstrip("@")
    return await (await conn.execute(
        """select author_external_id, count(*) as comments from comment_actions
           where tenant_id=%s and (author_external_id=%s or author_username=%s) group by 1 limit 1""",
        (tenant_id, ident, ident))).fetchone()


async def erase_comment_author(conn: AsyncConnection, tenant_id: UUID, external_user_id: str) -> int:
    await conn.execute("delete from private_reply_claims where tenant_id=%s and author_external_id=%s", (tenant_id, external_user_id))
    await conn.execute("delete from contact_flags where tenant_id=%s and identifier_hash=%s",
                       (tenant_id, crypto().lookup_hash(external_user_id, kind="igsid")))
    await conn.execute(      # comment events have no conversation: found by their author
        """update inbound_events set payload_raw = null, payload_canonical = '{}'::jsonb
           where tenant_id=%s and event_type = 'CommentReceived' and payload_canonical->>'contact_external_id' = %s""",
        (tenant_id, external_user_id))
    return (await conn.execute(
        """update comment_actions set text_masked='[eliminado]', author_username=null, author_external_id='erased:'||id::text
           where tenant_id=%s and author_external_id=%s""", (tenant_id, external_user_id))).rowcount


async def erase_subject(conn: AsyncConnection, tenant_id: UUID, contact_id: UUID, actor: UUID | None) -> dict:
    """Right of erasure: anonymize personal data in cascade; keep a hashed suppression entry so imports never bring the
    subject back; keep non-personal aggregates (stage history, counts) for statistics."""
    ids = await (await conn.execute("select external_user_id from contact_identities where contact_id=%s", (contact_id,))).fetchall()
    ct = await (await conn.execute("select phone_hash, email_hash from contacts where id=%s", (contact_id,))).fetchone()
    for h in [ct["phone_hash"], ct["email_hash"], *[crypto().lookup_hash(i["external_user_id"], kind="igsid") for i in ids]]:
        if h:
            await conn.execute("insert into suppression_list (tenant_id, identifier_hash, reason) values (%s,%s,'erasure') on conflict do nothing",
                               (tenant_id, h))
    for i in ids:
        await erase_comment_author(conn, tenant_id, i["external_user_id"])
    convs = [r["id"] for r in await (await conn.execute("select id from conversations where contact_id=%s", (contact_id,))).fetchall()]
    n_msgs = (await conn.execute("update messages set text_enc=null, text_masked='[eliminado]', media=null where conversation_id = any(%s)",
                                 (convs,))).rowcount
    await conn.execute("update conversations set summary=null where id = any(%s)", (convs,))
    await conn.execute("update conversation_reviews set note_masked='[eliminado]' where conversation_id = any(%s) and note_masked is not null", (convs,))
    turns = await (await conn.execute("select id, conversation_id from turns where conversation_id = any(%s)", (convs,))).fetchall()
    for t in turns:
        await delete_checkpoints(conn, f"{tenant_id}:{t['conversation_id']}:{t['id']}")
    turn_ids = [t["id"] for t in turns]
    await conn.execute("update turns set trace = null where id = any(%s)", (turn_ids,))
    await conn.execute("update guard_results set detail = null where turn_id = any(%s)", (turn_ids,))
    await conn.execute("update tool_calls set args_masked = '{}'::jsonb, result_digest = null where turn_id = any(%s)", (turn_ids,))
    await conn.execute("update curation_queue set question_masked = '[eliminado]' where turn_id = any(%s)", (turn_ids,))
    await conn.execute("update lead_stage_history set evidence = null where lead_id in (select id from leads where contact_id=%s)", (contact_id,))
    await conn.execute("update inbound_events set payload_raw=null, payload_canonical='{}'::jsonb where conversation_id = any(%s)", (convs,))
    await conn.execute("update outbox set payload='{}'::jsonb where conversation_id = any(%s)", (convs,))
    await conn.execute("update handoffs set summary='{}'::jsonb where conversation_id = any(%s)", (convs,))
    await conn.execute("update leads set profile='{}'::jsonb where contact_id=%s", (contact_id,))
    await conn.execute("update lead_forms set fields_enc=null where lead_id in (select id from leads where contact_id=%s)", (contact_id,))
    await conn.execute("update contacts set first_name=null, display_name_enc=null, phone_enc=null, email_enc=null, phone_hash=null, email_hash=null where id=%s",
                       (contact_id,))
    await conn.execute("update contact_identities set username=null, external_user_id='erased:'||id::text where contact_id=%s",
                       (contact_id,))
    await conn.execute("insert into audit_log (tenant_id, actor, actor_kind, action, target, after) values (%s,%s,'user','lgpd.erase',%s,%s)",
                       (tenant_id, actor, str(contact_id), json.dumps({"messages": n_msgs, "conversations": len(convs)})))
    return {"messages": n_msgs, "conversations": len(convs)}
