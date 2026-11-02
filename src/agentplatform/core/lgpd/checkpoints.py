"""Agent checkpoints hold what the turn read (encrypted, but still the customer's words): they follow deletion and retention too.
The saver's tables are keyed by thread_id = "<tenant>:<conversation>:<turn>" (core/agent/turn.py)."""
from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection

_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")


async def delete_checkpoints(conn: AsyncConnection, thread_id: str) -> None:
    for table in _TABLES:
        await conn.execute(f"delete from {table} where thread_id = %s", (thread_id,))


async def delete_old_checkpoints(conn: AsyncConnection, tenant_id: UUID, days: int) -> int:
    rows = await (await conn.execute(
        """select tenant_id || ':' || conversation_id || ':' || id as thread_id from turns
           where tenant_id = %s and status <> 'running' and started_at < now() - make_interval(days => %s)
             and exists (select 1 from checkpoints c where c.thread_id = tenant_id || ':' || conversation_id || ':' || turns.id)
           limit 5000""", (tenant_id, days))).fetchall()
    for r in rows:
        await delete_checkpoints(conn, r["thread_id"])
    return len(rows)
