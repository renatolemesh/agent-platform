"""Periodic jobs against the real procrastinate app and the dev database (they run every minute in the worker)."""
import pytest

from agentplatform.core.db.pool import platform_tx, tenant_tx
from agentplatform.jobs import tasks

pytestmark = pytest.mark.db


async def test_retry_stalled_runs_against_the_real_job_manager(db):
    from agentplatform.jobs.app import app
    async with app.open_async():
        assert await tasks.retry_stalled(app) == []


async def test_ingress_for_tenant_without_config_keeps_event_retryable(tenant):
    async with platform_tx() as conn:
        ca = await (await conn.execute("select id from channel_accounts where tenant_id=%s", (tenant,))).fetchone()
        ev = await (await conn.execute(
            """insert into inbound_events (tenant_id, channel_account_id, idempotency_key, event_type, provider_ts,
                                           payload_canonical) values (%s,%s,'k-noconfig','MessageReceived',now(),'{}')
               returning id""", (tenant, ca["id"]))).fetchone()
    assert await tasks.ingress(str(tenant), ev["id"]) == []
    async with tenant_tx(tenant) as conn:
        row = await (await conn.execute("select status, last_error from inbound_events where id=%s", (ev["id"],))).fetchone()
    assert (row["status"], row["last_error"]) == ("failed", "no_active_config")
