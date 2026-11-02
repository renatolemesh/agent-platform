"""Transactional outbox writes. Nodes never send: they enqueue here inside the commit transaction (principle 8)."""
from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

from psycopg import AsyncConnection

from agentplatform.core.outbox.bubbles import split_bubbles
from agentplatform.core.security.crypto import content_hash

PRIORITY = {"conversation": 0, "comment": 1, "followup": 2, "campaign": 3}
# How an automatic action shows up in the conversation history (a public reply is not part of the direct; a handoff notice
# does not answer what the customer asked, so it never counts as the reply to it).
MESSAGE_KIND = {"public_comment_reply": "comment_reply", "private_reply": "private_reply", "handoff_expectation": "handoff_notice"}


async def record_sent_message(conn: AsyncConnection, item: dict, mid: str | None) -> None:
    """The history and the "last reply" clock, whichever path confirmed the send (API answer, echo or reconciliation)."""
    if item["kind"] != "hide_comment":
        await conn.execute(
            """insert into messages (tenant_id, conversation_id, direction, external_mid, kind, text_masked, provider_ts, turn_id)
               values (%s,%s,'out_bot',%s,%s,%s,now(),%s) on conflict do nothing""",
            (item["tenant_id"], item["conversation_id"], mid, MESSAGE_KIND.get(item["kind"], "text"), item["payload"].get("text"),
             item["turn_id"]))
    if item["kind"] not in ("hide_comment", "public_comment_reply"):
        await conn.execute("update conversations set last_reply_at = now() where id = %s", (item["conversation_id"],))


async def enqueue(conn: AsyncConnection, *, tenant_id: UUID, conversation_id: UUID, effect_key: str, kind: str,
                  text: str | None, recipient: str | None, send_epoch: int, priority: int, max_staleness_s: int,
                  is_proactive: bool = False, turn_id: UUID | None = None, comment_id: str | None = None,
                  source_inbound_at: datetime | None = None, not_before: datetime | None = None,
                  extra: dict | None = None) -> UUID | None:
    payload = {"text": text, "recipient": recipient, "comment_id": comment_id, **(extra or {})}
    row = await (await conn.execute(
        """insert into outbox (tenant_id, conversation_id, turn_id, effect_key, kind, is_proactive, priority, payload,
                               content_hash, send_epoch, source_inbound_at, not_before, max_staleness_s)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,coalesce(%s, now()),%s)
           on conflict (effect_key) do nothing returning id""",
        (tenant_id, conversation_id, turn_id, effect_key, kind, is_proactive, priority, json.dumps(payload),
         content_hash(text or ""), send_epoch, source_inbound_at, not_before, max_staleness_s))).fetchone()
    return row["id"] if row else None


async def enqueue_reply(conn: AsyncConnection, *, tenant_id: UUID, conversation_id: UUID, turn_id: UUID, text: str,
                        recipient: str, send_epoch: int, max_bytes: int, max_staleness_s: int,
                        source_inbound_at: datetime | None, suffix: str = "") -> list[UUID]:
    ids = []
    for i, bubble in enumerate(split_bubbles(text, max_bytes)):
        oid = await enqueue(conn, tenant_id=tenant_id, conversation_id=conversation_id, effect_key=f"{turn_id}{suffix}:{i}",
                            kind="dm_text", text=bubble, recipient=recipient, send_epoch=send_epoch,
                            priority=PRIORITY["conversation"], max_staleness_s=max_staleness_s, turn_id=turn_id,
                            source_inbound_at=source_inbound_at)
        if oid:
            ids.append(oid)
    return ids
