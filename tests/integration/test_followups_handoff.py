from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path

import pytest

from agentplatform.core.config.registry import load_yaml
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.followups.engine import on_stage_entered, tick
from agentplatform.core.handoff.alerts import apply_seller_action, run_alerts
from agentplatform.core.handoff.notifiers import FallbackInbox, LogNotifier, sign_action, verify_action
from agentplatform.core.handoff.service import create_handoff
from agentplatform.core.outbox.dispatcher import DispatchPolicy
from agentplatform.core.packages.contract import load_package
from agentplatform.core.security.keys import encrypt_field
from tests.integration.helpers import make_conversation

pytestmark = pytest.mark.db
CFG = load_yaml(Path(__file__).resolve().parents[2] / "config/tenants/dev-consortium.yaml")
TPL = load_package("consortium").manifest.templates
NEVER_QUIET = DispatchPolicy(quiet_start=dtime(23, 59, 59), quiet_end=dtime(0, 0), holidays_national=False)


async def test_followup_queued_inside_window(tenant):
    conv, _ = await make_conversation(tenant)
    now = datetime.now(timezone.utc)
    async with tenant_tx(tenant) as conn:
        assert await on_stage_entered(conn, CFG, tenant, conv["id"], "pre_cadastro_enviado", now - timedelta(hours=5)) == 1
        r = await tick(conn, CFG, tenant, TPL, now, NEVER_QUIET)
    assert r.queued == 1


async def test_followup_skipped_when_window_closed(tenant):
    conv, _ = await make_conversation(tenant, window_open=False)
    now = datetime.now(timezone.utc)
    async with tenant_tx(tenant) as conn:
        await on_stage_entered(conn, CFG, tenant, conv["id"], "pre_cadastro_enviado", now - timedelta(hours=5))
        r = await tick(conn, CFG, tenant, TPL, now, NEVER_QUIET)
        st = await (await conn.execute("select status, stop_reason from followup_runs where conversation_id=%s", (conv["id"],))).fetchone()
    assert r.skipped == 1 and st["stop_reason"] == "window_closed"


async def test_followup_in_quiet_hours_skipped_if_window_ends_before_open(tenant):
    conv, _ = await make_conversation(tenant)
    now = datetime.now(timezone.utc)
    always_quiet = DispatchPolicy(quiet_start=dtime(0, 0), quiet_end=dtime(0, 0))
    async with tenant_tx(tenant) as conn:
        await conn.execute("update conversations set window_expires_at = now() + interval '10 minutes' where id=%s", (conv["id"],))
        await on_stage_entered(conn, CFG, tenant, conv["id"], "pre_cadastro_enviado", now - timedelta(hours=5))
        r = await tick(conn, CFG, tenant, TPL, now, always_quiet)
    assert r.skipped == 1


async def test_signed_actions():
    import uuid
    hid = uuid.uuid4()
    p = sign_action(b"k", "took", hid)
    assert verify_action(b"k", p) == ("took", hid)
    assert verify_action(b"other", p) is None
    assert verify_action(b"k", p.replace("took", "return")) is None


async def test_alert_sent_and_took_button_moves_to_human(tenant):
    conv, _ = await make_conversation(tenant)
    noon = datetime.now(timezone.utc).replace(hour=15, minute=0)
    notifier = LogNotifier()
    async with tenant_tx(tenant) as conn:
        await conn.execute(
            "insert into tenant_users (tenant_id, email_hash, email_enc, display_name, role, notify_whatsapp_enc) values (%s, %s, %s, 'Vendedor', 'seller', %s)",
            (tenant, b"h", b"e", await encrypt_field(conn, tenant, "+5511999990000", "tenant_users.whatsapp")))
        h = await create_handoff(conn, CFG, tenant_id=tenant, conversation_id=conv["id"], lead_id=None, reason="hot_lead",
                                 summary={"first_name": "Ana", "profile": {"goal": "vehicle"}}, now=noon)
        stats = await run_alerts(conn, CFG, tenant, FallbackInbox([notifier]), noon, "https://console.local")
        assert stats["sent"] == 1 and notifier.sent[0][0].whatsapp == "+5511999990000"
        assert "Ana" == notifier.sent[0][1].first_name and "/conversations/" in notifier.sent[0][1].console_url
        assert await apply_seller_action(conn, tenant, "took", h["handoff_id"], None) == "HUMAN"
        c = await (await conn.execute("select ownership from conversations where id=%s", (conv["id"],))).fetchone()
    assert c["ownership"] == "HUMAN"


async def test_off_hours_pending_promoted_at_opening(tenant):
    conv, _ = await make_conversation(tenant)
    night = datetime.now(timezone.utc).replace(hour=2, minute=0)
    noon = datetime.now(timezone.utc).replace(hour=15, minute=0)
    async with tenant_tx(tenant) as conn:
        h = await create_handoff(conn, CFG, tenant_id=tenant, conversation_id=conv["id"], lead_id=None, reason="explicit_request",
                                 summary={"first_name": None}, now=night)
        assert h["mode"] == "off_hours"
        stats = await run_alerts(conn, CFG, tenant, FallbackInbox([LogNotifier()]), noon, "https://c")
        c = await (await conn.execute("select ownership, handoff_pending from conversations where id=%s", (conv["id"],))).fetchone()
    assert stats["promoted"] == 1 and c["ownership"] == "AWAITING_HUMAN" and c["handoff_pending"] is False
