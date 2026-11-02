"""Recovery of directs the provider gave up delivering (ADR-019 still applies). The webhook is the main path: after about an hour of
failures the provider stops retrying and unsubscribes the app, and nothing replays what was missed. Every few minutes this looks at which
threads moved, by participants and time only, and reads the messages of a thread only when the assistant may read it and something in it
is newer than what we have. Messages get the webhook's idempotency key, so whichever arrives first wins."""
from __future__ import annotations

import logging
from datetime import timedelta
from uuid import UUID

from agentplatform.core.channels.events import CanonicalEvent
from agentplatform.core.channels.ports import AccountRef, RetryableSendError, SendError
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.orchestration import dm_scope
from agentplatform.core.orchestration.conversations import utcnow
from agentplatform.core.orchestration.ingress import persist_events

log = logging.getLogger(__name__)
WEBHOOK_GRACE = timedelta(minutes=2)     # the webhook usually gets here first
THREAD_PAGE = 20


async def _may_read(conn, account: AccountRef, channel: str, contact: str, updated_at) -> bool:
    """Has a conversation with something unseen, or would be served as a new person. Never the owner's known or blocked contacts."""
    flag = await dm_scope.flag_of(conn, account.channel_account_id, contact)
    if flag == "blocked":
        return False
    conv = await (await conn.execute(
        """select greatest(c.last_inbound_at, c.last_reply_at) as seen from conversations c
           join contact_identities ci on ci.contact_id = c.contact_id
           where c.channel_account_id=%s and ci.channel=%s and ci.external_user_id=%s""",
        (account.channel_account_id, channel, contact))).fetchone()
    if conv:
        return conv["seen"] is None or updated_at > conv["seen"]
    s = await dm_scope.load_scope(conn, account.channel_account_id)
    if flag == "allowed" or s["scope"] == "all":
        return True
    return bool(s["contacts_synced_at"]) and flag != "known" and s["scope"] == "origin_and_new"


def _unanswered(messages: list[dict], own: set[str]) -> list[dict]:
    """Customer messages after the last thing said on our side, oldest first. What someone already answered needs no answer."""
    out = []
    for m in messages:                   # newest first
        if m["from"] in own:
            break
        if (m["text"] or "").strip():
            out.append(m)
    return list(reversed(out))


async def recover_dms(tenant_id: UUID, adapter, account: AccountRef, rt) -> list[tuple[UUID, int]]:
    cfg = (await rt.tenant(tenant_id)).config.operations
    now = utcnow()
    since, latest = now - timedelta(minutes=cfg.dm_recovery_lookback_minutes), now - WEBHOOK_GRACE
    own = {account.external_account_id, account.external_page_id}
    created: list[tuple[UUID, int]] = []
    try:
        threads = await adapter.list_recent_threads(account, cfg.dm_recovery_max_threads)
        for t in threads:
            if not since <= t["updated_at"] <= latest:
                continue
            async with tenant_tx(tenant_id) as conn:
                if not await _may_read(conn, account, adapter.channel, t["contact"], t["updated_at"]):
                    continue
            messages = await adapter.list_thread_messages(account, t["id"], THREAD_PAGE)     # network outside the transaction
            events = [CanonicalEvent(event_type="MessageReceived", channel=adapter.channel, account_external_id=account.external_account_id,
                                     contact_external_id=t["contact"], idempotency_key=f"msg:{m['mid']}", mid=m["mid"], text=m["text"],
                                     provider_ts=m["at"])
                      for m in _unanswered(messages, own) if since <= m["at"] <= latest]
            if events:
                new = await persist_events(events)
                if new:
                    log.warning("recovered %s direct(s) the webhook never delivered, tenant %s", len(new), tenant_id)
                created += new
    except (SendError, RetryableSendError) as e:       # next run tries again; the polling of comments must go on
        log.warning("direct recovery skipped for tenant %s: %s", tenant_id, type(e).__name__)
    return created
