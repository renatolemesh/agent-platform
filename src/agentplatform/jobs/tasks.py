"""Job bodies (plain async functions, testable without a queue). The procrastinate app in jobs/app.py wraps them.

Scheduling contract: every function returns a list of follow-up jobs as (task_name, kwargs, opts) so that the queue
layer stays thin and tests can run the whole pipeline synchronously.
"""
from __future__ import annotations

import json
import logging
import math
import secrets
from datetime import datetime, timezone
from uuid import UUID

import httpx
from procrastinate.exceptions import UniqueViolation
from procrastinate.jobs import Status

from agentplatform.core.config.registry import ConfigError, registry
from agentplatform.core.db.pool import advisory_xact_lock, platform_tx, tenant_tx
from agentplatform.core.orchestration.conversations import conv_lock_key, utcnow
from agentplatform.core.orchestration.debounce import debounce_state
from agentplatform.core.orchestration.human_detector import handle_echo
from agentplatform.core.orchestration.ingress import load_event, route_event, scrub_event
from agentplatform.runtime import get_runtime

log = logging.getLogger(__name__)
Next = list[tuple[str, dict, dict]]


async def ingress(tenant_id: str, event_id: int) -> Next:
    rt = get_runtime()
    tid = UUID(tenant_id)
    try:
        tr = await rt.tenant(tid)
    except ConfigError:
        # Never drop the message: the event stays retryable (sweep_inbound, up to 5 attempts) and the operator gets
        # the inbound_backlog alert. A queue-level retry loop would only add noise.
        log.warning("tenant %s has no active config; inbound event %s kept for retry", tid, event_id)
        async with tenant_tx(tid) as conn:
            await conn.execute("update inbound_events set status='failed', last_error='no_active_config' where id=%s",
                               (event_id,))
        return []
    routed = await route_event(tid, event_id, rt.adapters, tr.package.manifest.initial_stage, compliance=tr.config.compliance,
                               edit_similarity=tr.config.conversation.edit_reanswer_similarity,
                               reopen_closed=tr.config.handoff.closed_reopens_on_inbound)
    if routed.action == "debounce":
        nxt: Next = [("close_batch", {"tenant_id": tenant_id, "conversation_id": str(routed.conversation_id)},
                      {"queueing_lock": f"cb:{routed.conversation_id}",
                       "schedule_in": {"seconds": tr.config.concurrency.debounce_window_s}})]
        if routed.has_pending_media:
            nxt.append(("transcribe_pending", {"tenant_id": tenant_id, "conversation_id": str(routed.conversation_id)}, {}))
        if routed.media_to_hydrate:
            nxt.append(("hydrate_media", {"tenant_id": tenant_id, "media_id": routed.media_to_hydrate}, {}))
        if routed.username_missing:
            nxt.append(("fetch_username", {"tenant_id": tenant_id, "conversation_id": str(routed.conversation_id)},
                        {"queueing_lock": f"user:{routed.conversation_id}"}))
        return nxt
    if routed.action == "echo":
        return [("echo", {"tenant_id": tenant_id, "conversation_id": str(routed.conversation_id), "event_id": event_id,
                          "rechecked": False}, {})]
    if routed.action == "comment":
        return [("comment", {"tenant_id": tenant_id, "event_id": event_id}, {})]
    return []


async def close_batch(tenant_id: str, conversation_id: str) -> Next:
    """Debounce check + ADR-012 (cancel an uncommitted run when a new batch arrives, under the advisory lock)."""
    rt = get_runtime()
    tid, cid = UUID(tenant_id), UUID(conversation_id)
    cfg = (await rt.tenant(tid)).config.concurrency
    now = utcnow()
    async with tenant_tx(tid) as conn:
        await advisory_xact_lock(conn, conv_lock_key(cid))
        st = await debounce_state(conn, cid, now, window_s=cfg.debounce_window_s, max_wait_s=cfg.debounce_max_wait_s,
                                  media_max_wait_s=cfg.media_max_wait_s)
        if st.pending == 0:
            return []
        if not st.ready:
            delay = max(0.5, (st.due - now).total_seconds())
            return [("close_batch", {"tenant_id": tenant_id, "conversation_id": conversation_id},
                     {"schedule_in": {"seconds": delay}})]
        running = await (await conn.execute(
            "select id from turns where conversation_id=%s and status='running'", (cid,))).fetchone()
        conv = await (await conn.execute("select consecutive_restarts from conversations where id=%s", (cid,))).fetchone()
        if running and cfg.new_message_policy == "cancel_if_unsent_else_next_turn" \
                and conv["consecutive_restarts"] < cfg.max_consecutive_restarts:
            await conn.execute(
                "update conversations set run_epoch = run_epoch + 1, consecutive_restarts = consecutive_restarts + 1 where id=%s",
                (cid,))
            await conn.execute("update turns set status='cancelled', cancel_reason='new_batch' where id=%s", (running["id"],))
    # `lock` only: with a queueing lock too, the retry of a failed turn collides with the next turn already queued and the
    # conversation freezes. Two queued turns are harmless: they run one at a time, and the second finds nothing to do.
    return [("run_turn", {"tenant_id": tenant_id, "conversation_id": conversation_id}, {"lock": f"conv:{conversation_id}"})]


async def _paused(tid: UUID, feature: str) -> str | None:
    """Global/tenant/capability kill switch (the dispatcher re-checks before every send; this only avoids the work)."""
    async with tenant_tx(tid) as conn:
        return await registry.kill_switch_on(conn, tenant_id=tid, feature=feature)


async def _turn_blocked(tid: UUID, cid: UUID) -> str | None:
    """Why a reply could not go out right now. The turn is not run (no spending for a reply that would be cancelled); the messages
    stay pending and the sweep picks them up as soon as the account can send. A stop request is the exception: it is recorded anyway."""
    from agentplatform.core.agent.guards import privacy_gate
    async with tenant_tx(tid) as conn:
        acc = await (await conn.execute(
            """select ca.channel, ca.token_status from conversations c join channel_accounts ca on ca.id = c.channel_account_id
               where c.id=%s""", (cid,))).fetchone()
        why = await registry.kill_switch_on(conn, tenant_id=tid, feature="agent", channel=acc["channel"] if acc else None)
        if not why and acc and acc["token_status"] == "invalid":
            why = "token_invalid"
        recent = (await (await conn.execute(
            "select count(*) as n from messages where conversation_id=%s and direction='in' and created_at > now() - interval '1 minute'",
            (cid,))).fetchone())["n"]
        pending = await (await conn.execute(
            """select text_masked from messages where conversation_id=%s and direction='in' and turn_id is null
               and kind not in ('comment','shared','deleted')""", (cid,))).fetchall()
    if why and not pending:
        return why
    tr = await get_runtime().tenant(tid)
    if not why and recent > tr.config.input_guard.per_contact_messages_per_min:
        why = "contact_flood"
    why = why or (None if tr.policy().live or (acc and acc["channel"] == "simulator") else "not_live")
    if not why:
        return None
    return None if privacy_gate([p["text_masked"] or "" for p in pending], tr.config.compliance).opt_out else why


async def run_turn(tenant_id: str, conversation_id: str) -> Next:
    tid, cid = UUID(tenant_id), UUID(conversation_id)
    if await _turn_blocked(tid, cid):
        log.info("tenant %s cannot send right now: turn skipped (messages stay pending)", tid)
        return []
    rt = get_runtime()
    runner = await rt.runner(tid, checkpointer=await rt.get_checkpointer(), conversation_id=cid)
    res = await runner.run(tid, cid)
    nxt: Next = []
    if res.outbox_ids:
        nxt.append(("dispatch", {"tenant_id": tenant_id, "conversation_id": conversation_id},
                    {"lock": f"dispatch:{conversation_id}"}))
    if res.status in ("committed", "handoff"):
        # messages that arrived during the run are picked up by a new batch check
        nxt.append(("close_batch", {"tenant_id": tenant_id, "conversation_id": conversation_id},
                    {"queueing_lock": f"cb:{conversation_id}", "schedule_in": {"seconds": 1}}))
    if res.status in ("committed", "handoff"):
        nxt.append(("summarize", {"tenant_id": tenant_id, "conversation_id": conversation_id},
                    {"queueing_lock": f"sum:{conversation_id}", "schedule_in": {"seconds": 5}}))
    if res.status == "committed" and res.trace:
        sd = res.trace.get("stage_decision") or {}
        tr = await rt.tenant(tid)
        from agentplatform.core.followups.engine import on_bot_message, on_stage_entered
        async with tenant_tx(tid) as conn:
            if sd.get("applied"):
                await on_stage_entered(conn, tr.config, tid, cid, sd["to"], utcnow())
            if res.outbox_ids:
                await on_bot_message(conn, tr.config, tid, cid, utcnow())
    return nxt


async def dispatch(tenant_id: str, conversation_id: str) -> Next:
    rt = get_runtime()
    tid, cid = UUID(tenant_id), UUID(conversation_id)
    out = await rt.dispatcher.dispatch_conversation(tid, cid)
    window = (await rt.tenant(tid)).config.concurrency.echo_reconcile_window_s
    nxt: Next = [("reconcile", {"tenant_id": tenant_id, "outbox_id": str(oid)}, {"schedule_in": {"seconds": window}})
                 for oid in out.unknown]
    if out.retry_in is not None:         # back right when it is due, instead of at the next minute sweep
        nxt.append(("dispatch", {"tenant_id": tenant_id, "conversation_id": conversation_id},
                    {"lock": f"dispatch:{conversation_id}", "schedule_in": {"seconds": math.ceil(out.retry_in) + 1}}))
    return nxt


async def reconcile(tenant_id: str, outbox_id: str) -> Next:
    await get_runtime().dispatcher.reconcile_unknown(UUID(tenant_id), UUID(outbox_id))
    return []


async def echo(tenant_id: str, conversation_id: str, event_id: int, rechecked: bool) -> Next:
    rt = get_runtime()
    tid = UUID(tenant_id)
    cfg = (await rt.tenant(tid)).config.concurrency
    async with tenant_tx(tid) as conn:
        ev = await load_event(conn, tid, event_id)
    r = await handle_echo(tid, UUID(conversation_id), ev.mid, ev.text,
                          reconcile_window_s=cfg.echo_reconcile_window_s, rechecked=rechecked,
                          known_sender_app=ev.sender_app_id in (rt.settings.automation_app_ids or ()))
    if r == "recheck":
        return [("echo", {"tenant_id": tenant_id, "conversation_id": conversation_id, "event_id": event_id, "rechecked": True},
                 {"schedule_in": {"seconds": cfg.echo_grace_s}})]
    async with tenant_tx(tid) as conn:
        await scrub_event(conn, event_id)
    return []


async def _comment_intent(rt, tr, tid: UUID, text: str) -> float | None:
    """Cheap model, only for posts set to use it; no budget or an outage falls back to the post's keywords."""
    from pydantic import BaseModel, Field

    from agentplatform.core.agent.prompts import render
    from agentplatform.core.models.gateway import BudgetDenied, ModelCallError, tenant_context
    from agentplatform.core.security.pii import mask

    class IntentOut(BaseModel):
        interest: float = Field(ge=0, le=1)

    spec = tr.config.models.comment_intent
    if spec is None or rt.gateway is None:
        return None
    try:
        msgs = render(spec.prompt_version, nonce=secrets.token_hex(4), scope=tr.package.manifest.scope or "o negócio do cliente",
                      comment=mask(text).text)
        r = await rt.gateway.chat(spec, msgs, tenant_context(tr.config.budget, tid, "comment_intent"), IntentOut)
    except (BudgetDenied, ModelCallError):
        return None
    return r.parsed.interest


async def comment(tenant_id: str, event_id: int) -> Next:
    from agentplatform.core.comments.automation import plan_intent, process_comment
    rt = get_runtime()
    tid = UUID(tenant_id)
    tr = await rt.tenant(tid)
    paused = bool(await _paused(tid, "comments")) or not tr.policy().live
    async with tenant_tx(tid) as conn:
        row = await (await conn.execute(
            """select ca.id as ca_id, ca.external_account_id from inbound_events e
               join channel_accounts ca on ca.id = e.channel_account_id where e.id=%s""", (event_id,))).fetchone()
        ev = await load_event(conn, tid, event_id)
        use_model = not paused and await plan_intent(conn, tr.config, row["ca_id"], ev, utcnow())
    intent = await _comment_intent(rt, tr, tid, ev.text or "") if use_model else None     # never inside a transaction
    async with tenant_tx(tid) as conn:
        d = await process_comment(conn, tr.config, tr.package.manifest, tenant_id=tid, channel_account_id=row["ca_id"],
                                  account_external_id=row["external_account_id"], ev=ev, now=utcnow(), intent=intent, paused=paused,
                                  mixed_inbox=rt.adapters[ev.channel].capabilities().mixed_inbox)
    nxt: Next = []
    if d.media_created and ev.media_id:
        nxt.append(("hydrate_media", {"tenant_id": tenant_id, "media_id": ev.media_id}, {}))
    if d.conversation_id and (d.public_outbox_id or d.private_outbox_id or d.hide_outbox_id):
        nxt.append(("dispatch", {"tenant_id": tenant_id, "conversation_id": str(d.conversation_id)},
                    {"lock": f"dispatch:{d.conversation_id}"}))
    return nxt


async def comment_backfill(tenant_id: str, media_id: str) -> Next:
    """The seller chose to answer comments already made on this post: those not acted on go through the automation again."""
    tid = UUID(tenant_id)
    async with tenant_tx(tid) as conn:
        rows = await (await conn.execute(
            """delete from comment_actions a using inbound_events e, media_automations au, channel_media m
               where a.tenant_id=%s and a.media_id=%s and a.decision in ('inactive','before_activation','paused','no_rule','known_contact','contacts_pending')
                 and e.tenant_id = a.tenant_id and e.idempotency_key = 'cmt:' || a.comment_id
                 and m.tenant_id = a.tenant_id and m.external_media_id = a.media_id and au.media_id = m.id
                 and a.comment_ts >= au.backfill_since
               returning e.id""", (tid, media_id))).fetchall()
    return [("comment", {"tenant_id": tenant_id, "event_id": r["id"]}, {}) for r in rows]


async def hydrate_media(tenant_id: str, media_id: str) -> Next:
    """Details of a post first seen in an event (ad, story, live, or a post newer than the last sync)."""
    from agentplatform.core.comments.catalog import CATALOG_ERRORS, hydrate
    rt = get_runtime()
    tid = UUID(tenant_id)
    async with tenant_tx(tid) as conn:
        row = await (await conn.execute(
            """select ca.channel from channel_media m join channel_accounts ca on ca.id = m.channel_account_id
               where m.tenant_id=%s and m.external_media_id=%s""", (tid, media_id))).fetchone()
    adapter = rt.adapters.get(row["channel"]) if row else None
    if adapter is None or not hasattr(adapter, "get_media"):
        return []
    try:
        await hydrate(tid, adapter, media_id)
    except CATALOG_ERRORS as e:
        log.info("media details unavailable for tenant %s: %s", tid, type(e).__name__)
    return []


async def fetch_username(tenant_id: str, conversation_id: str) -> Next:
    """The @ of someone who started in a direct (the panel shows it). Tried once per contact: a refusal is not retried."""
    from agentplatform.core.channels.ports import AccountRef
    from agentplatform.core.comments.catalog import CATALOG_ERRORS
    from agentplatform.core.security.keys import decrypt_field
    rt = get_runtime()
    tid = UUID(tenant_id)
    async with tenant_tx(tid) as conn:
        r = await (await conn.execute(
            """select ci.id, ci.external_user_id, ca.channel, ca.id as ca_id, ca.external_account_id, ca.external_page_id, ca.token_enc
               from conversations c join contact_identities ci on ci.contact_id = c.contact_id
               join channel_accounts ca on ca.id = c.channel_account_id and ca.channel = ci.channel
               where c.id=%s and ci.username is null and ci.username_checked_at is null""", (UUID(conversation_id),))).fetchone()
        token = await decrypt_field(conn, tid, r["token_enc"], "channel_accounts.token") if r and r["token_enc"] else None
    adapter = rt.adapters.get(r["channel"]) if r else None
    if adapter is None or not hasattr(adapter, "get_username"):
        return []
    username = None
    try:
        username = await adapter.get_username(AccountRef(tid, r["ca_id"], r["external_account_id"], r["external_page_id"], token),
                                              r["external_user_id"])
    except CATALOG_ERRORS as e:
        log.info("username unavailable for tenant %s: %s", tid, type(e).__name__)
    async with tenant_tx(tid) as conn:
        await conn.execute("update contact_identities set username=coalesce(%s, username), username_checked_at=now() where id=%s",
                           (username, r["id"]))
    return []


async def media_sync() -> Next:
    """Every 5 minutes, per tenant: refresh the posts and read new comments of recent posts. Webhooks are the main path; this
    covers lost deliveries and works before the provider's review. One failing tenant never blocks the others."""
    from agentplatform.core.comments.catalog import CATALOG_ERRORS, accounts, poll_account, sync_account
    from agentplatform.core.orchestration.dm_recovery import recover_dms
    from agentplatform.core.orchestration.dm_scope import snapshot_contacts
    rt = get_runtime()

    async def recover_dms_enabled(tid: UUID, adapter) -> bool:
        return (await rt.tenant(tid)).config.operations.dm_recovery_enabled and hasattr(adapter, "list_recent_threads")

    async with platform_tx() as conn:
        tenants = await (await conn.execute("select id from tenants where status='active'")).fetchall()
    nxt: Next = []
    for t in tenants:
        tid = t["id"]
        try:
            cfg = (await rt.tenant(tid)).config.comments
            polling = cfg.enabled and cfg.polling_enabled and not await _paused(tid, "comments")
            for channel, account in await accounts(tid):
                adapter = rt.adapters.get(channel)
                if adapter is None:
                    continue
                if adapter.capabilities().mixed_inbox:      # read-only and needed before anyone new is served, even with comments paused
                    try:
                        await snapshot_contacts(tid, adapter, account, cfg)
                    except CATALOG_ERRORS as e:         # the list resumes next time; it must not stop comments from being read
                        log.warning("contact list not read for tenant %s: %s", tid, type(e).__name__)
                if await recover_dms_enabled(tid, adapter):
                    nxt += [("ingress", {"tenant_id": str(x), "event_id": e}, {}) for x, e in await recover_dms(tid, adapter, account, rt)]
                if not polling or not hasattr(adapter, "list_media"):
                    continue
                await sync_account(tid, adapter, account, cfg)
                nxt += [("ingress", {"tenant_id": str(x), "event_id": e}, {}) for x, e in await poll_account(tid, adapter, account, cfg)]
        except ConfigError:
            continue
        except CATALOG_ERRORS as e:
            log.info("media sync skipped for tenant %s: %s", tid, type(e).__name__)
        except Exception:
            log.exception("media sync failed for %s", tid)
    return nxt


async def transcribe_pending(tenant_id: str, conversation_id: str) -> Next:
    """Pending media of the batch -> masked text (core/media/processor.py), then let the batch close. Audio is transcribed
    through the ModelGateway (tenant budget); images/PDFs are read locally. Never blocks the batch on a media failure."""
    from agentplatform.core.media.processor import download, process_attachment
    from agentplatform.core.models.gateway import tenant_context
    from agentplatform.core.security.keys import decrypt_field
    rt = get_runtime()
    tid, cid = UUID(tenant_id), UUID(conversation_id)
    tr = await rt.tenant(tid)
    async with tenant_tx(tid) as conn:
        rows = await (await conn.execute(
            "select id, media from messages where conversation_id=%s and (media->>'pending')::boolean", (cid,))).fetchall()
        acc = await (await conn.execute(
            """select ca.token_enc from conversations c join channel_accounts ca on ca.id = c.channel_account_id
               where c.id=%s""", (cid,))).fetchone()
        token = await decrypt_field(conn, tid, acc["token_enc"], "channel_accounts.token") if acc and acc["token_enc"] else None

    async def fetch(url: str) -> bytes:
        return await download(url, token, rt.settings.meta_graph_version, tr.config.media.max_download_mb * 1024 * 1024)

    async def transcribe(audio: bytes, seconds: float) -> str:
        if rt.gateway is None:
            raise RuntimeError("no model gateway")
        return await rt.gateway.transcribe(tr.config.models.transcription, audio, "audio.ogg", seconds,
                                           tenant_context(tr.config.budget, tid, "transcribe", conversation_id=cid),
                                           language=tr.config.media.transcription_language)

    for r in rows:
        media = dict(r["media"])
        texts, notes = [], []
        for att in media.get("attachments", []):
            if att.get("type") not in ("audio", "image", "file"):
                continue
            res = await process_attachment(att, tr.config, fetch=fetch, transcribe=transcribe)
            texts += [res.transcript] if res.transcript else []
            notes += [res.note] if res.note else []
        media.update(pending=False, transcript=" ".join(texts) or None, note=notes[0] if notes else None)
        async with tenant_tx(tid) as conn:
            # the transcript becomes the message's text: later turns, the summary and the seller read it like any other message
            await conn.execute("update messages set media=%s, text_masked=coalesce(text_masked, %s) where id=%s",
                               (json.dumps(media), media["transcript"], r["id"]))
    return [("close_batch", {"tenant_id": tenant_id, "conversation_id": conversation_id},
             {"queueing_lock": f"cb:{conversation_id}"})]


# ----------------------------------------------------------------- periodic
async def sweep_inbound() -> Next:
    """Catch events whose notify/defer was lost (pending > 30 s). One that keeps failing becomes a dead letter, which alerts the
    operator; it stays in the table with its encrypted text so it can be reprocessed."""
    async with platform_tx() as conn:
        dead = await (await conn.execute(
            """update inbound_events set status='dead' where status in ('pending','failed') and attempts >= 5
               returning tenant_id, id, event_type, last_error""")).fetchall()
        for d in dead:
            await conn.execute(
                "insert into dead_letters (tenant_id, source, ref, error, payload_masked) values (%s,'inbound_event',%s,%s,%s)",
                (d["tenant_id"], str(d["id"]), d["last_error"] or "repeated_failure", json.dumps({"event_type": d["event_type"]})))
        rows = await (await conn.execute(
            "select e.tenant_id, e.id from inbound_events e join tenants t on t.id = e.tenant_id "
            "where e.status in ('pending','failed') and e.received_at < now() - interval '30 seconds' "
            "and e.attempts < 5 and t.status <> 'offboarded' order by e.received_at limit 200")).fetchall()
        for r in rows:
            await conn.execute("update inbound_events set attempts = attempts + 1 where id=%s", (r["id"],))
    return [("ingress", {"tenant_id": str(r["tenant_id"]), "event_id": r["id"]}, {}) for r in rows]


async def record_dead_letter(source: str, kwargs: dict, error: Exception) -> None:
    """A job that gave up. Job arguments are ids only, and only the error's class is kept: a message may quote customer text."""
    tid = kwargs.get("tenant_id")
    ref = kwargs.get("conversation_id") or kwargs.get("event_id") or kwargs.get("outbox_id") or ""
    try:
        async with platform_tx() as conn:
            await conn.execute("insert into dead_letters (tenant_id, source, ref, error, payload_masked) values (%s,%s,%s,%s,%s)",
                               (tid, source, str(ref), type(error).__name__, json.dumps(kwargs, default=str)))
    except Exception:
        log.exception("could not record dead letter for %s", source)


async def sweep_pending_turns(tid: UUID, conn, cfg) -> Next:
    """Customer messages nobody is working on (the job was lost between the commit and the queue, the worker died, the agent was
    paused and resumed, the account is back): they go to the normal batch check. Part of the per-tenant tick."""
    if await registry.kill_switch_on(conn, tenant_id=tid, feature="agent"):
        return []
    rows = await (await conn.execute(
        """select distinct m.conversation_id from messages m join conversations c on c.id = m.conversation_id
           join channel_accounts ca on ca.id = c.channel_account_id
           where m.tenant_id=%s and m.direction='in' and m.turn_id is null and m.kind not in ('comment','shared','deleted')
             and m.created_at < now() - make_interval(secs => %s) and ca.token_status <> 'invalid'
             and not exists (select 1 from turns t where t.conversation_id = m.conversation_id and t.status='running'
                             and t.lease_expires_at > now())
           limit 200""", (tid, cfg.concurrency.pending_sweep_after_s))).fetchall()
    return [("close_batch", {"tenant_id": str(tid), "conversation_id": str(r["conversation_id"])},
             {"queueing_lock": f"cb:{r['conversation_id']}"}) for r in rows]


async def sweep_orphan_sends(tid: UUID, conn, cfg) -> Next:
    """A send with no outcome: the worker died mid-call, or the reconcile job was lost. Never resent: it goes to reconciliation
    (echo, then the conversations API, else failed + handoff)."""
    limit = cfg.concurrency.sending_orphan_after_s
    await conn.execute(
        """update outbox set status='unknown', last_error=coalesce(last_error, 'worker_lost')
           where tenant_id=%s and status='sending' and attempt_registered_at < now() - make_interval(secs => %s)""", (tid, limit))
    rows = await (await conn.execute(
        "select id from outbox where tenant_id=%s and status='unknown' and attempt_registered_at < now() - make_interval(secs => %s) limit 100",
        (tid, limit))).fetchall()
    return [("reconcile", {"tenant_id": str(tid), "outbox_id": str(r["id"])}, {}) for r in rows]


async def dispatch_due() -> Next:
    async with platform_tx() as conn:
        rows = await (await conn.execute(
            "select distinct o.tenant_id, o.conversation_id from outbox o join tenants t on t.id = o.tenant_id "
            "where o.status='pending' and o.not_before <= now() and t.status <> 'offboarded' limit 200")).fetchall()
    return [("dispatch", {"tenant_id": str(r["tenant_id"]), "conversation_id": str(r["conversation_id"])},
             {"lock": f"dispatch:{r['conversation_id']}"}) for r in rows]


async def knowledge_extract(tenant_id: str, document_id: str) -> Next:
    from agentplatform.core.knowledge.admin import extract_document
    tid = UUID(tenant_id)
    tr = await get_runtime().tenant_for_setup(tid)
    await extract_document(tid, UUID(document_id), tr.config.operations.knowledge, tr.config.rag.active_index_id)
    return []


async def knowledge_index(tenant_id: str, document_id: str) -> Next:
    from agentplatform.core.knowledge.admin import index_document
    from agentplatform.core.models.gateway import tenant_context
    tid = UUID(tenant_id)
    rt = get_runtime()
    tr = await rt.tenant_for_setup(tid)
    await index_document(tid, UUID(document_id), rt.gateway, tr.config.models.embedding,
                         tenant_context(tr.config.budget, tid, "knowledge", purpose="ingest"), tr.config.operations.knowledge.embed_batch_size,
                         tr.config.rag.active_index_id, tr.config.rag.conflict_similarity)
    return []


async def knowledge_index_faq(tenant_id: str, faq_id: str) -> Next:
    from agentplatform.core.knowledge.admin import index_faq
    from agentplatform.core.models.gateway import tenant_context
    tid = UUID(tenant_id)
    rt = get_runtime()
    tr = await rt.tenant_for_setup(tid)
    await index_faq(tid, UUID(faq_id), rt.gateway, tr.config.models.embedding, tenant_context(tr.config.budget, tid, "knowledge", purpose="ingest"),
                    tr.config.rag.active_index_id)
    return []


async def knowledge_reindex(tenant_id: str, run_id: str) -> Next:
    """Builds the new index, then proposes the change as a normal draft: a person approves it, after an evaluation in production."""
    from agentplatform.core.config.schema import ModelRef
    from agentplatform.core.knowledge.admin import run_reindex
    from agentplatform.core.models.gateway import tenant_context
    tid, rid = UUID(tenant_id), UUID(run_id)
    rt = get_runtime()
    tr = await rt.tenant_for_change(tid)
    async with tenant_tx(tid) as conn:
        run = await (await conn.execute("select model, dims, to_index, created_by from kb_reindex_runs where id=%s", (rid,))).fetchone()
    if not run:
        return []
    new_model = ModelRef(model=run["model"], dims=run["dims"], provider=tr.config.models.embedding.provider, timeout_s=tr.config.models.embedding.timeout_s)
    status = await run_reindex(tid, rid, rt.gateway, new_model, tenant_context(tr.config.budget, tid, "knowledge", purpose="ingest"),
                               tr.config.operations.knowledge.embed_batch_size)
    if status != "ready":
        return []
    async with tenant_tx(tid) as conn:
        done = await (await conn.execute("select draft_version_id from kb_reindex_runs where id=%s", (rid,))).fetchone()
        if done["draft_version_id"]:
            return []
        name = (await (await conn.execute("select name from kb_indexes where id=%s", (run["to_index"],))).fetchone())["name"]
        cfg = tr.config.model_copy(update={
            "models": tr.config.models.model_copy(update={"embedding": new_model}),
            "rag": tr.config.rag.model_copy(update={"active_index_id": name})})
        draft = await registry.propose(conn, tid, cfg, package_compliance=tr.package.manifest.compliance, created_by=run["created_by"],
                                       note=f"Troca do modelo de busca para {run['model']}")
        await conn.execute("update kb_reindex_runs set draft_version_id=%s where id=%s", (draft, rid))
    return []


async def config_gate(tenant_id: str, request_id: str) -> Next:
    from agentplatform.lab.gate import run_requested
    await run_requested(UUID(tenant_id), UUID(request_id), get_runtime().gateway)
    return []


async def post_publish(tenant_id: str, post_id: str) -> Next:
    """One step of an approved post (ADR-024). The minute tick defers it again while the post is on the way, so the queue never retries."""
    from agentplatform.core.comments.catalog import accounts
    from agentplatform.core.posts.publisher import advance
    rt = get_runtime()
    tid = UUID(tenant_id)
    tr = await rt.tenant(tid)
    found = await accounts(tid)
    return await advance(tid, UUID(post_id), tr.config, rt.adapters, {a.channel_account_id: a for _, a in found}, rt.settings.public_base_url,
                         [*tr.package.manifest.allowed_urls, *tr.config.conversation.allowed_urls], utcnow())


async def tenants_tick() -> Next:
    """Per tenant: follow-ups, handoff alerts and campaigns. One failing tenant never blocks the others."""
    from agentplatform.core.campaigns.engine import tick as campaign_tick
    from agentplatform.core.followups.engine import tick
    from agentplatform.core.handoff.alerts import auto_return_to_bot, run_alerts
    from agentplatform.core.knowledge.admin import sweep as sweep_knowledge
    from agentplatform.core.posts.publisher import due_posts
    from agentplatform.lab.gate import sweep_requests
    rt = get_runtime()
    async with platform_tx() as conn:
        tenants = await (await conn.execute("select id from tenants where status='active'")).fetchall()
    nxt: Next = []
    for t in tenants:
        tid = t["id"]
        try:
            tr = await rt.tenant(tid)
            async with tenant_tx(tid) as conn:
                if not await registry.kill_switch_on(conn, tenant_id=tid, feature="followups"):
                    await tick(conn, tr.config, tid, tr.package.manifest.templates, utcnow(), tr.policy())
                await run_alerts(conn, tr.config, tid, rt.inbox, utcnow(), rt.settings.public_base_url + "/console")
                for cid in await auto_return_to_bot(conn, tr.config, tid, utcnow()):
                    log.info("conversation %s back with the assistant after a failure handoff", cid)
                if tr.policy().live:
                    nxt += await sweep_pending_turns(tid, conn, tr.config)
                nxt += await sweep_orphan_sends(tid, conn, tr.config)
                nxt += await sweep_knowledge(conn, tid, tr.config.operations.knowledge, tr.config.rag.active_index_id)
                nxt += await sweep_requests(conn, tid, tr.config.operations.evaluation_stuck_after_min)
                nxt += await due_posts(conn, tid, tr.config.operations.posts)
                camps = await (await conn.execute(
                    "select id from campaigns where tenant_id=%s and status in ('scheduled','running')", (tid,))).fetchall()
                campaigns_paused = await registry.kill_switch_on(conn, tenant_id=tid, feature="campaigns")
                for c in camps if not campaigns_paused else []:
                    if not await registry.kill_switch_on(conn, tenant_id=tid, campaign=str(c["id"])):
                        await campaign_tick(conn, tid, c["id"], utcnow(), is_quiet=tr.policy().is_quiet(utcnow()))
        except ConfigError:
            continue                       # tenant without an active config: nothing to run
        except Exception:
            log.exception("tenant tick failed for %s", tid)
    nxt += await dispatch_due()
    return nxt


async def ops_alerts() -> Next:
    """Operator alerts (core/ops/observability.py): per-tenant checks with the tenant's thresholds + platform checks."""
    from agentplatform.api.webhooks import Spool
    from agentplatform.core.config.schema import OpsAlertsConfig
    from agentplatform.core.ops.observability import (
        disk_used_ratio,
        evaluate_alerts,
        evaluate_platform_alerts,
        record_sent,
        should_send,
    )
    rt = get_runtime()
    async with platform_tx() as conn:
        tenants = await (await conn.execute("select id from tenants where status='active'")).fetchall()
    pending = []
    for t in tenants:
        tid = t["id"]
        try:
            tr = await rt.tenant(tid)
            cfg = tr.config.operations.alerts
            async with tenant_tx(tid) as conn:
                for a in await evaluate_alerts(conn, tid, cfg, tr.config.budget.alert_thresholds):
                    if await should_send(conn, a, cfg.cooldown_minutes):
                        pending.append(a)
        except ConfigError:
            continue
        except Exception:
            log.exception("ops alerts failed for tenant %s", tid)
    base = OpsAlertsConfig()
    try:
        spool = Spool(rt.settings.spool_dir)                   # only meaningful where the API spool is mounted
        spool_bytes, unreadable = spool.size(), spool.quarantine_size()
    except OSError:
        spool_bytes, unreadable = 0, 0
    async with platform_tx() as conn:
        for a in await evaluate_platform_alerts(conn, base, spool_bytes, [0.5, 0.8, 1.0], unreadable,
                                                restore_tests_required=rt.settings.app_env == "prod", disk_used=disk_used_ratio()):
            if await should_send(conn, a, base.cooldown_minutes):
                pending.append(a)
    for a in pending:                      # network calls outside the transactions
        if await rt.ops.send(a):
            async with platform_tx() as conn:
                await record_sent(conn, a)
    return []


async def heartbeat() -> None:
    """Dead man's switch: an outside service alerts when these pings stop (machine off, database down, workers stuck), which is
    exactly when nothing in here can send an alert."""
    url = get_runtime().settings.heartbeat_url
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.get(url.get_secret_value())
    except httpx.HTTPError as e:
        log.warning("heartbeat not delivered: %s", type(e).__name__)


async def budget_sweeper() -> Next:
    rt = get_runtime()
    if rt.gateway:
        await rt.gateway.budget.release_expired()
    return []


async def retry_stalled(app) -> Next:
    """Jobs in `doing` whose worker stopped heart-beating are retried (review #6); the turn lease fences the old one."""
    jm = app.job_manager
    for job in await jm.get_stalled_jobs(seconds_since_heartbeat=60):          # async in procrastinate 3.x
        log.warning("retrying stalled job %s (%s)", job.id, job.task_name)
        try:
            await jm.retry_job_by_id_async(job.id, retry_at=datetime.now(timezone.utc))
        except UniqueViolation:        # an equivalent job is already queued and takes over; this one must not hold the others back
            await jm.finish_job_by_id_async(job.id, Status.FAILED, delete_job=False)
        except Exception:              # one bad job never stops the rest of the sweep
            log.exception("could not retry stalled job %s", job.id)
    return []


async def summarize(tenant_id: str, conversation_id: str) -> Next:
    """Rolling summary every N turns, async (off the critical path), masked, via the gateway (budget)."""
    from pydantic import BaseModel

    from agentplatform.core.agent.prompts import render
    from agentplatform.core.models.gateway import BudgetDenied, ModelCallError, tenant_context
    from agentplatform.core.security.pii import mask

    class SummaryOut(BaseModel):
        summary: str

    rt = get_runtime()
    tid, cid = UUID(tenant_id), UUID(conversation_id)
    tr = await rt.tenant(tid)
    every = tr.config.agent.summary_every_n_turns
    async with tenant_tx(tid) as conn:
        n = (await (await conn.execute("select count(*) as n from turns where conversation_id=%s and status in ('committed','handoff')",
                                       (cid,))).fetchone())["n"]
        if n == 0 or n % every != 0 or rt.gateway is None:
            return []
        conv = await (await conn.execute("select summary from conversations where id=%s", (cid,))).fetchone()
        msgs = await (await conn.execute(
            "select direction, text_masked from messages where conversation_id=%s and text_masked is not null and kind <> 'comment_reply' "
            "order by provider_ts desc limit 12",
            (cid,))).fetchall()
    speaker = {"in": "cliente", "out_human": "vendedor"}
    text = "\n".join(f"- {speaker.get(m['direction'], 'assistente')}: {m['text_masked']}" for m in reversed(msgs))
    try:
        r = await rt.gateway.chat(tr.config.models.summarize, render(tr.config.models.summarize.prompt_version,
                                  previous=conv["summary"] or "-", messages=text),
                                  tenant_context(tr.config.budget, tid, "summarize", purpose="summary", conversation_id=cid),
                                  SummaryOut)
    except (BudgetDenied, ModelCallError):
        return []
    async with tenant_tx(tid) as conn:
        await conn.execute("update conversations set summary=%s where id=%s", (mask(r.parsed.summary).text[:600], cid))
    return []


async def webhook_subscriptions() -> Next:
    """Connected pages stay subscribed to the app's webhooks (checked every 10 min, re-subscribed when dropped)."""
    from agentplatform.core.ops.maintenance import ensure_subscriptions, missing_app_fields
    from agentplatform.core.ops.observability import OpsAlert, record_sent, should_send
    rt = get_runtime()
    s = rt.settings
    fields = {f.strip() for f in s.meta_page_subscribed_fields.split(",") if f.strip()}
    if not (s.meta_app_id and fields):
        return []
    required = {f.strip() for f in s.meta_app_required_fields.split(",") if f.strip()}
    missing = await missing_app_fields(s.meta_app_id, s.meta_app_secret.get_secret_value(), required, s.meta_graph_version) if s.meta_app_secret else None
    if missing:
        alert = OpsAlert("app_webhook_fields", None, "Campos de webhook desligados no app da Meta",
                         f"Estes eventos nunca chegam: {', '.join(sorted(missing))}. Ligue-os no painel do app, em Webhooks > Instagram.")
        async with platform_tx() as conn:
            due = await should_send(conn, alert, 24 * 60)
        if due and await rt.ops.send(alert):
            async with platform_tx() as conn:
                await record_sent(conn, alert)
    async with platform_tx() as conn:
        tenants = await (await conn.execute("select id from tenants where status='active'")).fetchall()
    for t in tenants:
        async with tenant_tx(t["id"]) as conn:
            await ensure_subscriptions(conn, t["id"], fields, s.meta_app_id, s.meta_graph_version)
    return []


async def daily_maintenance() -> Next:
    """Retention sweep (LGPD), token guard, per tenant. Separate transactions and error handling: a provider outage during the
    token check never costs the retention of that day."""
    from agentplatform.core.lgpd.service import retention_sweep
    from agentplatform.core.ops.maintenance import models_without_route, token_guard, zero_retention_models
    from agentplatform.core.ops.observability import OpsAlert, record_sent, should_send
    rt = get_runtime()
    s = rt.settings
    routes = await zero_retention_models() if s.llm_provider == "openrouter" else None
    async with platform_tx() as conn:
        tenants = await (await conn.execute("select id from tenants where status='active'")).fetchall()
    for t in tenants:
        try:
            tr = await rt.tenant(t["id"])
        except Exception:
            continue
        try:
            async with tenant_tx(t["id"]) as conn:
                await retention_sweep(conn, tr.config, t["id"])
        except Exception:
            log.exception("retention sweep failed for %s", t["id"])
        try:
            async with tenant_tx(t["id"]) as conn:
                await token_guard(conn, t["id"], s.meta_app_id, s.meta_app_secret.get_secret_value() if s.meta_app_secret else None,
                                  s.meta_graph_version)
        except Exception:
            log.exception("token guard failed for %s", t["id"])
        lost = models_without_route(tr.config.models, routes) if routes else None
        if lost:
            alert = OpsAlert(f"model_route:{t['id']}", t["id"], "Modelo de IA sem rota com retenção zero",
                             f"Toda chamada a estes modelos é recusada: {', '.join(sorted(lost))}. Onde há reserva, ela responde por enquanto. Troque em Modelos.")
            async with platform_tx() as conn:
                due = await should_send(conn, alert, 24 * 60)
            if due and await rt.ops.send(alert):
                async with platform_tx() as conn:
                    await record_sent(conn, alert)
    return []
