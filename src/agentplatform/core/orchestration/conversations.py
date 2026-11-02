"""Contacts, conversations, messages and ownership (plan 03, 05 §5, ADR-005).

Ownership transitions bump BOTH epochs under the conversation advisory lock: run_epoch (cancels an uncommitted run)
and send_epoch (blocks any pending outbox item at dispatch).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from psycopg import AsyncConnection

from agentplatform.core.db.pool import advisory_xact_lock
from agentplatform.core.security.keys import encrypt_field
from agentplatform.core.security.pii import mask

Ownership = Literal["BOT", "AWAITING_HUMAN", "HUMAN", "PAUSED", "CLOSED"]


def conv_lock_key(conversation_id: UUID | str) -> str:
    return f"conv:{conversation_id}"


async def get_or_create_conversation(conn: AsyncConnection, tenant_id: UUID, channel_account_id: UUID, channel: str,
                                     contact_external_id: str, initial_stage: str, *, username: str | None = None,
                                     entrypoint: str | None = None) -> dict:
    ident = await (await conn.execute(
        "select contact_id from contact_identities where tenant_id = %s and channel = %s and external_user_id = %s",
        (tenant_id, channel, contact_external_id))).fetchone()
    if ident is None:
        c = await (await conn.execute("insert into contacts (tenant_id) values (%s) returning id", (tenant_id,))).fetchone()
        contact_id = c["id"]
        ins = await (await conn.execute(
            """insert into contact_identities (tenant_id, contact_id, channel, external_user_id, username)
               values (%s,%s,%s,%s,%s) on conflict (tenant_id, channel, external_user_id) do nothing returning contact_id""",
            (tenant_id, contact_id, channel, contact_external_id, username))).fetchone()
        if ins is None:   # concurrent creator won
            await conn.execute("delete from contacts where id = %s", (contact_id,))
            contact_id = (await (await conn.execute(
                "select contact_id from contact_identities where tenant_id=%s and channel=%s and external_user_id=%s",
                (tenant_id, channel, contact_external_id))).fetchone())["contact_id"]
    else:
        contact_id = ident["contact_id"]
        if username:                   # known from a comment even when the contact started in a direct
            await conn.execute(
                "update contact_identities set username = %s where tenant_id = %s and channel = %s and external_user_id = %s "
                "and username is distinct from %s", (username, tenant_id, channel, contact_external_id, username))
    await conn.execute(
        "insert into leads (tenant_id, contact_id, stage) values (%s,%s,%s) on conflict (contact_id) do nothing",
        (tenant_id, contact_id, initial_stage))
    conv = await (await conn.execute(
        """insert into conversations (tenant_id, contact_id, channel_account_id, entrypoint)
           values (%s,%s,%s,%s) on conflict (channel_account_id, contact_id) do update set entrypoint = coalesce(conversations.entrypoint, excluded.entrypoint)
           returning *""", (tenant_id, contact_id, channel_account_id, entrypoint))).fetchone()
    return conv


async def store_inbound_message(conn: AsyncConnection, tenant_id: UUID, conversation_id: UUID, *, mid: str | None,
                                kind: str, text: str | None, media: dict | None, provider_ts: datetime,
                                inbound_event_id: int | None, service_window: timedelta | None) -> UUID | None:
    masked = mask(text).text if text else None
    row = await (await conn.execute(
        """insert into messages (tenant_id, conversation_id, direction, external_mid, inbound_event_id, kind, text_masked,
                                 text_enc, media, provider_ts)
           values (%s,%s,'in',%s,%s,%s,%s,%s,%s,%s) on conflict do nothing returning id""",
        (tenant_id, conversation_id, mid, inbound_event_id, kind, masked,
         await encrypt_field(conn, tenant_id, text, "messages.text"), json.dumps(media) if media else None,
         provider_ts))).fetchone()
    expires = provider_ts + service_window if service_window else None
    await conn.execute(
        """update conversations set last_inbound_at = greatest(coalesce(last_inbound_at, %s), %s),
                  window_expires_at = greatest(coalesce(window_expires_at, %s), %s)
           where id = %s""", (provider_ts, provider_ts, expires, expires, conversation_id))
    return row["id"] if row else None


async def transition_ownership(conn: AsyncConnection, tenant_id: UUID, conversation_id: UUID, to: Ownership, *,
                               cause: str, actor: UUID | None = None, evidence: dict | None = None,
                               bump_send_epoch: bool = True) -> dict:
    await advisory_xact_lock(conn, conv_lock_key(conversation_id))
    cur = await (await conn.execute("select ownership from conversations where id = %s for update",
                                    (conversation_id,))).fetchone()
    frm = cur["ownership"]
    row = await (await conn.execute(
        """update conversations set ownership = %s, ownership_changed_at = now(), run_epoch = run_epoch + 1,
                  send_epoch = send_epoch + %s, handoff_pending = case when %s in ('AWAITING_HUMAN','HUMAN') then false else handoff_pending end
           where id = %s returning *""", (to, 1 if bump_send_epoch else 0, to, conversation_id))).fetchone()
    if to != "BOT":
        await conn.execute(
            "update outbox set status = 'cancelled', cancel_reason = %s where conversation_id = %s and status = 'pending' "
            "and kind not in ('handoff_expectation', 'hide_comment')", (f"ownership:{to}", conversation_id))
        await conn.execute(
            "update followup_runs set status = 'stopped', stop_reason = %s where conversation_id = %s and status in ('scheduled','queued')",
            (f"ownership:{to}", conversation_id))
    await conn.execute(
        "insert into ownership_events (tenant_id, conversation_id, from_state, to_state, cause, actor, evidence) values (%s,%s,%s,%s,%s,%s,%s)",
        (tenant_id, conversation_id, frm, to, cause, actor, json.dumps(mask(json.dumps(evidence)).text) if evidence else None))
    return row


async def stop_followups(conn: AsyncConnection, conversation_id: UUID, reason: str) -> int:
    cur = await conn.execute(
        "update followup_runs set status = 'stopped', stop_reason = %s where conversation_id = %s and status in ('scheduled','queued')",
        (reason, conversation_id))
    await conn.execute(
        "update outbox set status = 'cancelled', cancel_reason = %s where conversation_id = %s and status = 'pending' and is_proactive",
        (reason, conversation_id))
    return cur.rowcount


async def pending_inbound(conn: AsyncConnection, conversation_id: UUID) -> list[dict]:
    return await (await conn.execute(
        """select id, kind, text_masked, media, provider_ts, created_at from messages
           where conversation_id = %s and direction = 'in' and turn_id is null and kind not in ('comment', 'shared', 'deleted')
           order by provider_ts, created_at""", (conversation_id,))).fetchall()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
