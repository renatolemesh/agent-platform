"""Console simulator: real pipeline on the isolated 'simulator' channel, draft configs tested without activating them,
replay with another version, and nothing leaks to customers, sellers, the inbox or the public webhook."""
import re

import httpx
import pytest

from agentplatform.core.config.registry import load_yaml, registry
from agentplatform.core.db.pool import platform_tx, tenant_tx
from agentplatform.core.handoff.notifiers import FallbackInbox, LogNotifier
from agentplatform.core.models.fake import ScriptedGateway, analysis, draft
from agentplatform.core.packages.contract import load_package
from agentplatform.runtime import Runtime, set_runtime
from tests.integration.test_console_admin import ROOT, _client, _csrf, _login, _user

pytestmark = pytest.mark.db


async def _setup(tenant):
    async with platform_tx() as conn:
        slug = (await (await conn.execute("select slug from tenants where id=%s", (tenant,))).fetchone())["slug"]
    cfg = load_yaml(ROOT / "config/tenants/dev-consortium.yaml").model_copy(update={"tenant_slug": slug})
    pkg = load_package(cfg.segment_package.name)
    conv_cfg = cfg.conversation
    draft_cfg = cfg.model_copy(update={"conversation": conv_cfg.model_copy(update={
        "persona": conv_cfg.persona.model_copy(update={"seller_display_name": "Rascunho"})})})
    async with tenant_tx(tenant) as conn:
        active = await registry.publish(conn, tenant, cfg, package_compliance=pkg.manifest.compliance, activate=True)
        draft_id = await registry.publish(conn, tenant, draft_cfg, package_compliance=pkg.manifest.compliance)
    gw = ScriptedGateway({"analyze_turn": lambda m: analysis(intents=["greeting"], stage="engajado", evidence=["oi"]),
                          "respond": lambda m: draft("Oi! Você pensa em imóvel ou veículo?"),
                          "output_judge": lambda m: {"risk": 0.0, "verdict": "pass", "reasons": []}})
    set_runtime(Runtime(adapters={}, gateway=gw, inbox=FallbackInbox([LogNotifier()])))
    return active, draft_id


async def test_simulator_chat_draft_and_replay(tenant):
    active, draft_id = await _setup(tenant)
    email, _ = await _user(tenant)
    async with _client() as c:
        await _login(c, email)
        page = await c.get("/console/simulator")
        assert page.status_code == 200 and "Simulador" in page.text
        session = re.search(r'name="s" value="(sim-[0-9a-f]{8})"', page.text).group(1)
        r = await c.post("/console/simulator/send", data={"s": session, "v": str(draft_id), "text": "oi", "csrf": _csrf(c)})
        assert r.status_code == 303
        page = await c.get(r.headers["location"])
        assert "Você pensa em imóvel ou veículo?" in page.text and "O que o assistente fez" in page.text
        r = await c.post("/console/simulator/replay", data={"s": session, "v": "active", "csrf": _csrf(c)})
        new_session = re.search(r"s=(sim-[0-9a-f]{8})", r.headers["location"]).group(1)
        assert new_session != session
        inbox = await c.get("/console/")
        assert session not in inbox.text and new_session not in inbox.text           # not in the real inbox
    async with tenant_tx(tenant) as conn:
        used = await (await conn.execute(
            """select ci.external_user_id as s, t.config_version_id as v from turns t join conversations c on c.id = t.conversation_id
               join contact_identities ci on ci.contact_id = c.contact_id where ci.channel = 'simulator'""")).fetchall()
        active_now = await (await conn.execute(
            "select id from config_versions where tenant_id=%s and status='active'", (tenant,))).fetchone()
        funnel = await (await conn.execute("select coalesce(sum(leads), 0) as n from v_funnel where tenant_id=%s", (tenant,))).fetchone()
    versions = {r["s"]: r["v"] for r in used}
    assert versions[session] == draft_id and versions[new_session] == active     # draft tested, then compared with active
    assert active_now["id"] == active                                            # the draft was never activated
    assert funnel["n"] == 0                                                      # simulated leads stay out of dashboards


async def test_simulator_channel_is_not_reachable_from_outside(tenant):
    from agentplatform.api.main import create_app
    await _setup(tenant)
    app = create_app(on_persisted=lambda created: None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/webhooks/simulator", content=b"{}", headers={"x-fake-signature": "ok"})
    assert r.status_code == 404
