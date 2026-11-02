"""Every business table with tenant_id is isolated by RLS for the app role (plan 03 §7)."""
import uuid

import pytest

from agentplatform.core.db.pool import platform_tx, tenant_tx

pytestmark = pytest.mark.db

# Tables that are intentionally not RLS-protected (documented exceptions, plan 03 §3)
EXCEPTIONS = {"kb_indexes", "eval_datasets", "experiments", "eval_results", "alembic_version"}


async def test_every_tenant_table_has_rls(db):
    async with platform_tx() as conn:
        cur = await conn.execute("""
            select c.relname, c.relrowsecurity, c.relforcerowsecurity
            from pg_class c join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'public' and c.relkind = 'r'
              and exists (select 1 from information_schema.columns
                          where table_schema='public' and table_name=c.relname and column_name='tenant_id')""")
        rows = await cur.fetchall()
    missing = [r["relname"] for r in rows if r["relname"] not in EXCEPTIONS and not (r["relrowsecurity"] and r["relforcerowsecurity"])]
    assert not missing, f"tables without forced RLS: {missing}"


async def test_tenant_cannot_see_other_tenant(tenant):
    other = uuid.uuid4()
    async with platform_tx() as conn:
        await conn.execute("insert into tenants (id, slug, display_name, segment_package, segment_package_version) values (%s,%s,'o','consortium','0.1.0')",
                           (other, f"o-{other.hex[:8]}"))
        await conn.execute("insert into contacts (tenant_id, first_name) values (%s, 'Ana')", (other,))
        await conn.execute("insert into contacts (tenant_id, first_name) values (%s, 'Bia')", (tenant,))
    async with tenant_tx(tenant) as conn:
        names = [r["first_name"] for r in await (await conn.execute("select first_name from contacts")).fetchall()]
        assert names == ["Bia"]
        with pytest.raises(Exception):
            await conn.execute("insert into contacts (tenant_id, first_name) values (%s, 'X')", (other,))


async def test_no_tenant_context_sees_nothing(db):
    from agentplatform.core.db.pool import get_pool
    pool = await get_pool()
    async with pool.connection() as conn:
        n = (await (await conn.execute("select count(*) as n from contacts")).fetchone())["n"]
    assert n == 0
