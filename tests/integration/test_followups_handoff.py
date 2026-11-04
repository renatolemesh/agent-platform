import uuid
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentplatform.core.agent.turn import TurnResult
from agentplatform.core.config.registry import load_yaml
from agentplatform.core.config.schema import Cadence, CadenceStep
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.followups.engine import on_bot_message, on_stage_entered, tick
from agentplatform.core.handoff.alerts import apply_seller_action, run_alerts
from agentplatform.core.handoff.notifiers import FallbackInbox, LogNotifier, sign_action, verify_action
from agentplatform.core.handoff.service import create_handoff
from agentplatform.core.orchestration.conversations import stop_followups
from agentplatform.core.outbox.dispatcher import DispatchPolicy
from agentplatform.core.packages.contract import load_package
from agentplatform.core.security.keys import encrypt_field
from agentplatform.jobs import tasks
from tests.integration.helpers import make_conversation

pytestmark = pytest.mark.db
CFG = load_yaml(Path(__file__).resolve().parents[2] / "config/tenants/dev-consortium.yaml")
TPL = load_package("consortium").manifest.templates
NEVER_QUIET = DispatchPolicy(quiet_start=dtime(23, 59, 59), quiet_end=dtime(0, 0), holidays_national=False)


async def test_followup_queued_inside_window(tenant):
    conv, _ = await make_conversation(tenant)
    now = datetime.now(timezone.utc)
    async with tenant_tx(tenant) as conn:
        assert await on_stage_entered(conn, CFG, tenant, conv["id"], "pronto_para_proposta", now - timedelta(hours=5)) == 1
        r = await tick(conn, CFG, tenant, TPL, now, NEVER_QUIET)
    assert r.queued == 1


async def test_followup_skipped_when_window_closed(tenant):
    conv, _ = await make_conversation(tenant, window_open=False)
    now = datetime.now(timezone.utc)
    async with tenant_tx(tenant) as conn:
        await on_stage_entered(conn, CFG, tenant, conv["id"], "pronto_para_proposta", now - timedelta(hours=5))
        r = await tick(conn, CFG, tenant, TPL, now, NEVER_QUIET)
        st = await (await conn.execute("select status, stop_reason from followup_runs where conversation_id=%s", (conv["id"],))).fetchone()
    assert r.skipped == 1 and st["stop_reason"] == "window_closed"


async def test_followup_in_quiet_hours_skipped_if_window_ends_before_open(tenant):
    conv, _ = await make_conversation(tenant)
    now = datetime.now(timezone.utc)
    always_quiet = DispatchPolicy(quiet_start=dtime(0, 0), quiet_end=dtime(0, 0))
    async with tenant_tx(tenant) as conn:
        await conn.execute("update conversations set window_expires_at = now() + interval '10 minutes' where id=%s", (conv["id"],))
        await on_stage_entered(conn, CFG, tenant, conv["id"], "pronto_para_proposta", now - timedelta(hours=5))
        r = await tick(conn, CFG, tenant, TPL, now, always_quiet)
    assert r.skipped == 1


# ------------------------------------------------------------------ the person who stopped answering
NO_REPLY = Cadence(id="silence", trigger={"event": "bot_message_unanswered", "stages": ["novo"]},
                   steps=[CadenceStep(delay="2h", content_kind="template", template_key="no_reply_nudge")])


def _with(*cadences, **fields):
    return CFG.model_copy(update={"followups": CFG.followups.model_copy(update={"cadences": list(cadences), **fields})})


async def _run(conn, conv):
    return await (await conn.execute("select * from followup_runs where conversation_id=%s", (conv["id"],))).fetchone()


async def test_silence_waits_only_what_the_rule_says_and_needs_no_stage_change(tenant):
    conv, _ = await make_conversation(tenant)
    now = datetime.now(timezone.utc)
    async with tenant_tx(tenant) as conn:
        assert await on_bot_message(conn, _with(NO_REPLY), tenant, conv["id"], now) == 1
        run = await _run(conn, conv)
    assert run["due_at"] == now + timedelta(hours=2)


@pytest.mark.parametrize("decision", [{"applied": False, "from": "novo", "to": "novo"}, None])
async def test_a_turn_that_answered_starts_the_silence_even_without_a_stage_change(tenant, monkeypatch, decision):
    """The real job, with the turn itself faked: most turns do not move the stage, and some carry no decision at all."""
    conv, _ = await make_conversation(tenant)

    class Runner:
        async def run(self, *_):
            return TurnResult("committed", uuid.uuid4(), outbox_ids=[uuid.uuid4()], trace={"stage_decision": decision})

    class Runtime:
        async def get_checkpointer(self):
            return None

        async def runner(self, *_, **__):
            return Runner()

        async def tenant(self, _):
            return SimpleNamespace(config=_with(NO_REPLY))

    async def not_blocked(*_):
        return None

    monkeypatch.setattr(tasks, "get_runtime", Runtime)
    monkeypatch.setattr(tasks, "_turn_blocked", not_blocked)
    await tasks.run_turn(str(tenant), str(conv["id"]))
    async with tenant_tx(tenant) as conn:
        run = await _run(conn, conv)
    assert run["cadence_id"] == "silence" and run["status"] == "scheduled"


async def test_silence_outside_the_rule_stages_schedules_nothing(tenant):
    conv, _ = await make_conversation(tenant)
    rule = NO_REPLY.model_copy(update={"trigger": {"event": "bot_message_unanswered", "stages": ["educando"]}})
    async with tenant_tx(tenant) as conn:
        assert await on_bot_message(conn, _with(rule), tenant, conv["id"], datetime.now(timezone.utc)) == 0


async def test_a_reply_stops_the_reminder_and_the_next_silence_arms_it_again(tenant):
    conv, _ = await make_conversation(tenant)
    now = datetime.now(timezone.utc)
    cfg = _with(NO_REPLY)
    async with tenant_tx(tenant) as conn:
        await on_bot_message(conn, cfg, tenant, conv["id"], now)
        await stop_followups(conn, conv["id"], "inbound_message")
        assert (await _run(conn, conv))["status"] == "stopped"
        later = now + timedelta(minutes=30)
        assert await on_bot_message(conn, cfg, tenant, conv["id"], later) == 1
        run = await _run(conn, conv)
    assert run["status"] == "scheduled" and run["stop_reason"] is None and run["due_at"] == later + timedelta(hours=2)


async def test_a_new_message_from_the_assistant_starts_the_silence_over(tenant):
    conv, _ = await make_conversation(tenant)
    now = datetime.now(timezone.utc)
    cfg = _with(NO_REPLY)
    async with tenant_tx(tenant) as conn:
        await on_bot_message(conn, cfg, tenant, conv["id"], now)
        later = now + timedelta(hours=1)
        await on_bot_message(conn, cfg, tenant, conv["id"], later)
        assert (await _run(conn, conv))["due_at"] == later + timedelta(hours=2)


async def test_a_reminder_that_went_out_never_goes_out_again(tenant):
    """A person who answers the reminder and goes quiet again does not get it a second time."""
    conv, _ = await make_conversation(tenant)
    now = datetime.now(timezone.utc)
    cfg = _with(NO_REPLY)
    async with tenant_tx(tenant) as conn:
        await on_bot_message(conn, cfg, tenant, conv["id"], now - timedelta(hours=3))
        assert (await tick(conn, cfg, tenant, TPL, now, NEVER_QUIET)).queued == 1
        await conn.execute("update outbox set status='sent', sent_at=now() where conversation_id=%s and is_proactive", (conv["id"],))
        await stop_followups(conn, conv["id"], "inbound_message")
        assert await on_bot_message(conn, cfg, tenant, conv["id"], now) == 0
        assert (await _run(conn, conv))["status"] == "stopped"
        assert (await tick(conn, cfg, tenant, TPL, now + timedelta(hours=5), NEVER_QUIET)).queued == 0
        sent = await (await conn.execute("select count(*) as n from outbox where conversation_id=%s and is_proactive", (conv["id"],))).fetchone()
    assert sent["n"] == 1


async def test_a_reminder_of_a_rule_turned_off_does_not_go_out(tenant):
    conv, _ = await make_conversation(tenant)
    now = datetime.now(timezone.utc)
    off = NO_REPLY.model_copy(update={"enabled": False})
    async with tenant_tx(tenant) as conn:
        assert await on_bot_message(conn, _with(off), tenant, conv["id"], now) == 0
        await on_bot_message(conn, _with(NO_REPLY), tenant, conv["id"], now - timedelta(hours=3))
        assert (await tick(conn, _with(off), tenant, TPL, now, NEVER_QUIET)).skipped == 1
        assert (await _run(conn, conv))["stop_reason"] == "cadence_disabled"


async def test_reminders_switched_off_for_the_account_do_not_go_out(tenant):
    conv, _ = await make_conversation(tenant)
    now = datetime.now(timezone.utc)
    async with tenant_tx(tenant) as conn:
        await on_bot_message(conn, _with(NO_REPLY), tenant, conv["id"], now - timedelta(hours=3))
        assert (await tick(conn, _with(NO_REPLY, enabled=False), tenant, TPL, now, NEVER_QUIET)).queued == 0
        assert (await _run(conn, conv))["stop_reason"] == "cadence_disabled"


async def test_an_attempt_removed_from_the_rule_does_not_break_the_others(tenant):
    """A scheduled second attempt, after the rule went back to one: the rest of the batch still goes."""
    first, _ = await make_conversation(tenant)
    second, _ = await make_conversation(tenant)
    now = datetime.now(timezone.utc)
    async with tenant_tx(tenant) as conn:
        await conn.execute("insert into followup_runs (tenant_id, conversation_id, cadence_id, step, status, due_at) values (%s,%s,'silence',1,'scheduled',%s)",
                           (tenant, first["id"], now - timedelta(hours=2)))
        await on_bot_message(conn, _with(NO_REPLY), tenant, second["id"], now - timedelta(hours=3))
        r = await tick(conn, _with(NO_REPLY), tenant, TPL, now, NEVER_QUIET)
        gone = await _run(conn, first)
    assert r.queued == 1 and r.skipped == 1 and gone["stop_reason"] == "cadence_removed"


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
