"""Async Postgres access. Every business transaction runs with app.tenant_id set (RLS enforced by app_rw)."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agentplatform.settings import get_settings

_pool: AsyncConnectionPool | None = None


async def get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            get_settings().database_url, min_size=1, max_size=10, open=False,
            kwargs={"row_factory": dict_row, "autocommit": False},
        )
        await _pool.open()
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def tenant_tx(tenant_id: UUID | str) -> AsyncIterator[AsyncConnection]:
    """One transaction scoped to a tenant (set_config(..., true) == SET LOCAL)."""
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("select set_config('app.tenant_id', %s, true)", (str(tenant_id),))
            yield conn


@asynccontextmanager
async def platform_tx() -> AsyncIterator[AsyncConnection]:
    """Platform jobs (retention, token guard, cross-tenant sweeps). Audited by callers."""
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("select set_config('app.platform', 'on', true)")
            yield conn


async def advisory_xact_lock(conn: AsyncConnection, key: str) -> None:
    """Per-conversation critical section inside the current transaction (ADR-005)."""
    await conn.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
