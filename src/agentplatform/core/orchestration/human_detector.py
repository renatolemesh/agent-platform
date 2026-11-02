"""Human detection by echo (ADR-002). Runs WITHOUT the conversation queue lock (a long turn must not delay it)."""
from __future__ import annotations

import logging
from uuid import UUID

from agentplatform.core.db.pool import advisory_xact_lock, tenant_tx
from agentplatform.core.orchestration.conversations import conv_lock_key, transition_ownership
from agentplatform.core.outbox.store import record_sent_message
from agentplatform.core.security.crypto import content_hash
from agentplatform.core.security.keys import encrypt_field
from agentplatform.core.security.pii import mask

log = logging.getLogger(__name__)


async def handle_echo(tenant_id: UUID, conversation_id: UUID, mid: str | None, text: str | None, *,
                      reconcile_window_s: float, rechecked: bool, known_sender_app: bool = False) -> str:
    """Returns: 'ours' | 'recheck' | 'human' | 'ignored'."""
    h = content_hash(text or "")
    async with tenant_tx(tenant_id) as conn:
        await advisory_xact_lock(conn, conv_lock_key(conversation_id))
        if mid:
            if await (await conn.execute("select 1 from outbox where external_mid = %s", (mid,))).fetchone():
                return "ours"
        # echo before the API returned the mid, or a late echo of an ambiguous (unknown) send
        row = await (await conn.execute(
            """select * from outbox where conversation_id = %s and content_hash = %s and status in ('sending','unknown')
               and attempt_registered_at > now() - make_interval(secs => %s)
               order by attempt_registered_at limit 1 for update""",
            (conversation_id, h, reconcile_window_s))).fetchone()
        if row:
            await conn.execute(
                "update outbox set status = 'sent', external_mid = coalesce(external_mid, %s), sent_at = coalesce(sent_at, now()) where id = %s",
                (mid, row["id"]))
            await record_sent_message(conn, row, mid or row["external_mid"])
            return "ours"
        if known_sender_app:
            return "ignored"
        if not rechecked:
            return "recheck"
        conv = await (await conn.execute("select ownership from conversations where id = %s", (conversation_id,))).fetchone()
        await conn.execute(
            """insert into messages (tenant_id, conversation_id, direction, external_mid, kind, text_masked, text_enc, provider_ts)
               values (%s,%s,'out_human',%s,'text',%s,%s,now()) on conflict do nothing""",
            (tenant_id, conversation_id, mid, mask(text).text if text else None,
             await encrypt_field(conn, tenant_id, text, "messages.text")))
        await conn.execute("update conversations set last_reply_at = now() where id = %s", (conversation_id,))
        await conn.execute("update handoffs set taken_at = coalesce(taken_at, now()) where conversation_id = %s and taken_at is null",
                           (conversation_id,))
        if conv["ownership"] != "HUMAN":
            await transition_ownership(conn, tenant_id, conversation_id, "HUMAN", cause="echo_unknown_mid",
                                       evidence={"mid": mid})
        return "human"
