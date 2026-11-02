"""F1-OBS-01: /metrics from the database, operator alerts with dedupe/cooldown, seller action links from e-mail."""
import uuid

import httpx
import pytest
from pydantic import SecretStr

from agentplatform.core.config.schema import OpsAlertsConfig
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.handoff.notifiers import FallbackInbox, LogNotifier, sign_action
from agentplatform.core.models.fake import ScriptedGateway
from agentplatform.core.ops.observability import OpsNotifier, evaluate_alerts, record_sent, should_send
from agentplatform.runtime import Runtime, set_runtime
from agentplatform.settings import get_settings
from tests.integration.helpers import make_conversation, put
from tests.integration.test_pipeline import fast_cfg

pytestmark = pytest.mark.db


def _app(monkeypatch, **settings):
    for k, v in settings.items():
        monkeypatch.setattr(get_settings(), k, v)
    set_runtime(Runtime(adapters={}, gateway=ScriptedGateway({}), inbox=FallbackInbox([LogNotifier()])))
    from agentplatform.api.main import create_app
    return create_app(on_persisted=lambda created: None)


async def test_metrics_requires_token_and_reports_outbox(tenant, monkeypatch):
    conv, igsid = await make_conversation(tenant)
    await put(tenant, conv, igsid, "olá")
    app = _app(monkeypatch, metrics_token=SecretStr("m-token"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/metrics")).status_code == 404
        r = await c.get("/metrics", headers={"authorization": "Bearer m-token"})
    assert r.status_code == 200
    assert f'agent_outbox_items{{status="pending",tenant="{tenant}"}}' in r.text
    for name in ("agent_turn_p95_seconds", "agent_budget_used_ratio", "agent_jobs", "agent_spool_bytes"):
        assert name in r.text


async def test_alerts_outbox_stuck_budget_and_cooldown(tenant):
    conv, igsid = await make_conversation(tenant)
    oid = await put(tenant, conv, igsid, "olá")
    cfg = OpsAlertsConfig(outbox_stuck_minutes=10, cooldown_minutes=60)
    async with tenant_tx(tenant) as conn:
        await conn.execute("update outbox set created_at = now() - interval '30 minutes', not_before = now() - interval '30 minutes' where id=%s", (oid,))
        await conn.execute("insert into budget_balances (tenant_id, scope, cap_usd, spent_usd) values (%s, 'tenant:total', 1, 0.85)", (tenant,))
        alerts = await evaluate_alerts(conn, tenant, cfg, [0.5, 0.8, 1.0])
        keys = {a.key.split(":")[0]: a for a in alerts}
        assert "outbox_stuck" in keys and "budget" in keys
        assert keys["budget"].key.endswith(":0.8") and keys["budget"].sticky
        first = [await should_send(conn, a, cfg.cooldown_minutes) for a in alerts]
        for a in alerts:
            await record_sent(conn, a)
        again = [await should_send(conn, a, cfg.cooldown_minutes) for a in alerts]
    assert all(first) and not any(again)          # deduplicated within the cooldown / sticky
    notifier = OpsNotifier()                         # no channel configured -> log, never raises
    assert await notifier.send(alerts[0]) and notifier.sent == [alerts[0]]


async def test_seller_action_link_get_is_safe_post_applies(tenant, monkeypatch):
    from agentplatform.core.handoff.service import create_handoff
    from agentplatform.core.orchestration.conversations import utcnow
    conv, _ = await make_conversation(tenant)
    async with tenant_tx(tenant) as conn:
        h = await create_handoff(conn, fast_cfg(), tenant_id=tenant, conversation_id=conv["id"], lead_id=None,
                                 reason="wants_human", summary={}, now=utcnow())
    app = _app(monkeypatch, console_session_secret=SecretStr("s3cret"))
    payload = sign_action(b"s3cret", "took", h["handoff_id"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get(f"/actions/took:{uuid.uuid4()}:deadbeef")).status_code == 404
        page = await c.get(f"/actions/{payload}")
        assert page.status_code == 200 and "<form method=\"post\">" in page.text
        async with tenant_tx(tenant) as conn:
            assert (await (await conn.execute("select ownership from conversations where id=%s", (conv["id"],))).fetchone())["ownership"] != "HUMAN"
        done = await c.post(f"/actions/{payload}")
    assert done.status_code == 200
    async with tenant_tx(tenant) as conn:
        assert (await (await conn.execute("select ownership from conversations where id=%s", (conv["id"],))).fetchone())["ownership"] == "HUMAN"
