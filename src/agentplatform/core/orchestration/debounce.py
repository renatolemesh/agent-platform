"""Burst debounce backed by Postgres (pending inbound messages ARE the buffer; Redis loss cannot drop messages).

due = min(last_received + window, first_received + max_wait); audio being transcribed holds the batch up to media_max_wait.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from psycopg import AsyncConnection


@dataclass
class DebounceState:
    ready: bool
    due: datetime | None
    pending: int


async def debounce_state(conn: AsyncConnection, conversation_id: UUID, now: datetime, *, window_s: float,
                         max_wait_s: float, media_max_wait_s: float) -> DebounceState:
    row = await (await conn.execute(
        """select count(*) as n, min(created_at) as first_at, max(created_at) as last_at,
                  bool_or(coalesce((media->>'pending')::boolean, false)) as media_pending
           from messages where conversation_id = %s and direction = 'in' and turn_id is null and kind not in ('comment', 'shared', 'deleted')""",
        (conversation_id,))).fetchone()
    if row["n"] == 0:
        return DebounceState(False, None, 0)
    due = min(row["last_at"] + timedelta(seconds=window_s), row["first_at"] + timedelta(seconds=max_wait_s))
    if row["media_pending"]:
        due = max(due, min(now, row["first_at"] + timedelta(seconds=media_max_wait_s)))
        if now < row["first_at"] + timedelta(seconds=media_max_wait_s):
            return DebounceState(False, max(due, now + timedelta(seconds=1)), row["n"])
    return DebounceState(now >= due, due, row["n"])
