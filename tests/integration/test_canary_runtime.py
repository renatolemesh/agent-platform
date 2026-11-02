"""Canary (F2-CON-04) reaches the turn: conversations in the canary slice run with the canary version, the others keep
the active one, and the turn records which version it used."""
import uuid

import pytest

from agentplatform.core.config.registry import canary_bucket, registry
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.handoff.notifiers import FallbackInbox, LogNotifier
from agentplatform.core.models.fake import ScriptedGateway
from agentplatform.core.packages.contract import load_package
from agentplatform.runtime import Runtime
from tests.integration.test_pipeline import fast_cfg

pytestmark = pytest.mark.db


async def test_runner_uses_canary_for_its_slice(tenant, monkeypatch):
    pkg = load_package("consortium")
    base = fast_cfg()
    conv_cfg = base.conversation
    canary_cfg = base.model_copy(update={"conversation": conv_cfg.model_copy(update={
        "persona": conv_cfg.persona.model_copy(update={"seller_display_name": "Canário"})})})
    proposer, approver = uuid.uuid4(), uuid.uuid4()
    async with tenant_tx(tenant) as conn:
        v1 = await registry.publish(conn, tenant, base, package_compliance=pkg.manifest.compliance, activate=True)
        v2 = await registry.propose(conn, tenant, canary_cfg, package_compliance=pkg.manifest.compliance,
                                    created_by=proposer, canary_percent=30)
        assert await registry.approve(conn, tenant, v2, approver) == "canary"
    monkeypatch.setattr(Runtime, "_build_limiter", lambda self: None)
    rt = Runtime(adapters={}, gateway=ScriptedGateway({}), inbox=FallbackInbox([LogNotifier()]))
    monkeypatch.setattr(rt.settings, "app_env", base.environment)
    inside = next(u for u in (uuid.uuid4() for _ in range(1000)) if canary_bucket(u) < 30)
    outside = next(u for u in (uuid.uuid4() for _ in range(1000)) if canary_bucket(u) >= 30)
    r_in = await rt.runner(tenant, conversation_id=inside)
    r_out = await rt.runner(tenant, conversation_id=outside)
    assert r_in.d.config_version_id == v2 and r_in.d.config.conversation.persona.seller_display_name == "Canário"
    assert r_out.d.config_version_id == v1 and r_out.d.config.conversation.persona.seller_display_name != "Canário"
    assert r_in.d.config.compliance.forbidden_phrases == r_out.d.config.compliance.forbidden_phrases  # package union kept
