"""Interleavings (a)-(j) of plan 07 §3 that live in the outbox/ownership/echo layer."""
import asyncio
from datetime import time as dtime

import pytest

from agentplatform.core.channels.fake.adapter import FakeChannelAdapter
from agentplatform.core.channels.ports import AmbiguousSendError
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.orchestration.conversations import transition_ownership
from agentplatform.core.orchestration.human_detector import handle_echo
from agentplatform.core.outbox.dispatcher import Dispatcher, DispatchPolicy
from tests.integration.helpers import make_conversation, put, status

pytestmark = pytest.mark.db


def dispatcher(adapter, **kw):
    return Dispatcher({"instagram": adapter}, **kw)


async def test_happy_path_sends_in_order(tenant):
    conv, ig = await make_conversation(tenant)
    a = FakeChannelAdapter()
    ids = [await put(tenant, conv, ig, f"bolha {i}", key=f"{conv['id']}:t1:{i}") for i in range(3)]
    out = await dispatcher(a).dispatch_conversation(tenant, conv["id"])
    assert out.sent == ids and a.texts() == ["bolha 0", "bolha 1", "bolha 2"]


async def test_new_batch_between_bubbles_does_not_cut_reply(tenant):
    """(c) a new customer message after commit must not cancel bubbles 2..n (send_epoch unchanged)."""
    conv, ig = await make_conversation(tenant)
    a = FakeChannelAdapter()
    for i in range(2):
        await put(tenant, conv, ig, f"b{i}", key=f"{conv['id']}:t2:{i}")
    async with tenant_tx(tenant) as conn:   # new batch arrives: run_epoch bump only (turn cancel semantics)
        await conn.execute("update conversations set run_epoch = run_epoch + 1 where id=%s", (conv["id"],))
    out = await dispatcher(a).dispatch_conversation(tenant, conv["id"])
    assert len(out.sent) == 2


async def test_seller_takes_over_between_bubbles(tenant):
    """(e) human echo between bubbles: remaining bubbles are blocked."""
    conv, ig = await make_conversation(tenant)
    sent_once = asyncio.Event()

    class Hook(FakeChannelAdapter):
        async def send(self, account, msg):
            r = await super().send(account, msg)
            if not sent_once.is_set():
                sent_once.set()
                async with tenant_tx(tenant) as conn:
                    await transition_ownership(conn, tenant, conv["id"], "HUMAN", cause="echo_unknown_mid")
            return r
    a = Hook()
    ids = [await put(tenant, conv, ig, f"b{i}", key=f"{conv['id']}:t3:{i}") for i in range(3)]
    out = await dispatcher(a).dispatch_conversation(tenant, conv["id"])
    assert out.sent == ids[:1]
    assert (await status(tenant, ids[1]))["status"] == "cancelled"


async def test_superseded_epoch_is_cancelled(tenant):
    conv, ig = await make_conversation(tenant)
    oid = await put(tenant, conv, ig, "velho", send_epoch=conv["send_epoch"] - 1)
    out = await dispatcher(FakeChannelAdapter()).dispatch_conversation(tenant, conv["id"])
    assert out.cancelled[oid] == "superseded"


async def test_window_closed(tenant):
    conv, ig = await make_conversation(tenant, window_open=False)
    oid = await put(tenant, conv, ig, "oi")
    out = await dispatcher(FakeChannelAdapter()).dispatch_conversation(tenant, conv["id"])
    assert out.cancelled[oid] == "window_closed"


async def test_opt_out_blocks_except_confirmation(tenant):
    """(f) opt-out between bubbles."""
    conv, ig = await make_conversation(tenant)
    o1 = await put(tenant, conv, ig, "promo")
    o2 = await put(tenant, conv, ig, "Pronto, não vou mais te mandar mensagens.", extra={"is_optout_confirmation": True})
    async with tenant_tx(tenant) as conn:
        await conn.execute("update leads set opted_out_at = now() where contact_id=%s", (conv["contact_id"],))
    out = await dispatcher(FakeChannelAdapter()).dispatch_conversation(tenant, conv["id"])
    assert out.cancelled[o1] == "opt_out" and out.sent == [o2]


async def test_proactive_cancelled_when_customer_spoke(tenant):
    """review #10: customer writes after the follow-up was created."""
    conv, ig = await make_conversation(tenant)
    oid = await put(tenant, conv, ig, "lembrete", is_proactive=True)
    async with tenant_tx(tenant) as conn:
        await conn.execute(
            "insert into messages (tenant_id, conversation_id, direction, kind, text_masked, provider_ts) values (%s,%s,'in','text','oi',now() + interval '1 second')",
            (tenant, conv["id"]))
    async def never_quiet(_):
        return DispatchPolicy(quiet_start=dtime(23, 59, 59), quiet_end=dtime(0, 0), holidays_national=False)
    out = await dispatcher(FakeChannelAdapter(), policy_for=never_quiet).dispatch_conversation(tenant, conv["id"])
    assert out.cancelled[oid] == "customer_spoke"


async def test_proactive_rescheduled_in_quiet_hours(tenant):
    conv, ig = await make_conversation(tenant)
    oid = await put(tenant, conv, ig, "lembrete", is_proactive=True)
    async def always_quiet(_):
        return DispatchPolicy(quiet_start=dtime(0, 0), quiet_end=dtime(0, 0))
    out = await dispatcher(FakeChannelAdapter(), policy_for=always_quiet).dispatch_conversation(tenant, conv["id"])
    assert out.rescheduled == [oid] and (await status(tenant, oid))["status"] == "pending"


async def test_echo_before_mid_does_not_pause_bot(tenant):
    """(echo race) our own echo arrives before the API returns the mid."""
    conv, ig = await make_conversation(tenant)
    results = []

    async def deliver_echo(rec):
        results.append(await handle_echo(tenant, conv["id"], rec.mid, rec.msg.text, reconcile_window_s=120, rechecked=True))
    a = FakeChannelAdapter(on_echo=deliver_echo, echo_before_return=True)
    await put(tenant, conv, ig, "Oi! Tudo bem?")
    await dispatcher(a).dispatch_conversation(tenant, conv["id"])
    assert results == ["ours"]
    async with tenant_tx(tenant) as conn:
        c = await (await conn.execute("select ownership from conversations where id=%s", (conv["id"],))).fetchone()
    assert c["ownership"] == "BOT"


async def test_ambiguous_send_late_echo_then_no_resend(tenant):
    """(i) timeout after delivery; echo arrives late; no resend, bot not paused."""
    conv, ig = await make_conversation(tenant)
    a = FakeChannelAdapter(ambiguous_but_delivered=1)
    oid = await put(tenant, conv, ig, "Texto único")
    d = dispatcher(a)
    out = await d.dispatch_conversation(tenant, conv["id"])
    assert out.unknown == [oid]
    r = await handle_echo(tenant, conv["id"], a.sent[0].mid, "Texto único", reconcile_window_s=120, rechecked=True)
    assert r == "ours"
    assert (await status(tenant, oid))["status"] == "sent"
    assert len(a.sent) == 1


async def test_ambiguous_send_without_echo_reconciles_via_api_or_fails(tenant):
    conv, ig = await make_conversation(tenant)
    failures = []

    async def on_failure(t, c, reason):
        failures.append(reason)
    a = FakeChannelAdapter(fail_next=[AmbiguousSendError("timeout before delivery")])
    oid = await put(tenant, conv, ig, "Mensagem X")
    d = dispatcher(a, on_failure=on_failure)
    await d.dispatch_conversation(tenant, conv["id"])
    assert await d.reconcile_unknown(tenant, oid) == "failed"            # not found at provider -> failed + handoff
    assert failures == ["ambiguous_send_unconfirmed"] and a.sent == []   # never resent


async def test_unknown_echo_from_seller_pauses_bot(tenant):
    """(d) unknown echo -> recheck -> HUMAN; pending outbox cancelled."""
    conv, ig = await make_conversation(tenant)
    oid = await put(tenant, conv, ig, "resposta do bot")
    first = await handle_echo(tenant, conv["id"], "mid.seller.1", "Oi, aqui é o vendedor", reconcile_window_s=120, rechecked=False)
    assert first == "recheck"
    second = await handle_echo(tenant, conv["id"], "mid.seller.1", "Oi, aqui é o vendedor", reconcile_window_s=120, rechecked=True)
    assert second == "human"
    st = await status(tenant, oid)
    assert st["status"] == "cancelled" and st["cancel_reason"] == "ownership:HUMAN"


async def test_kill_switch_blocks(tenant):
    """(g) kill switch."""
    from agentplatform.core.config.registry import registry
    from agentplatform.core.db.pool import platform_tx
    conv, ig = await make_conversation(tenant)
    oid = await put(tenant, conv, ig, "x")
    async with platform_tx() as conn:
        await conn.execute("insert into kill_switches (scope, scope_ref, is_on, reason) values ('tenant', %s, true, 'test')", (str(tenant),))
    registry.invalidate_kill_switches()
    try:
        out = await dispatcher(FakeChannelAdapter()).dispatch_conversation(tenant, conv["id"])
        assert out.cancelled[oid] == "kill_switch:tenant"
    finally:
        async with platform_tx() as conn:
            await conn.execute("delete from kill_switches where scope_ref = %s", (str(tenant),))
        registry.invalidate_kill_switches()


async def test_tampered_text_is_not_sent(tenant):
    """review #3: what is sent must be byte-for-byte what was approved."""
    conv, ig = await make_conversation(tenant)
    oid = await put(tenant, conv, ig, "aprovado")
    async with tenant_tx(tenant) as conn:
        await conn.execute("update outbox set payload = jsonb_set(payload, '{text}', '\"meu CPF 529.982.247-25\"') where id=%s", (oid,))
    out = await dispatcher(FakeChannelAdapter()).dispatch_conversation(tenant, conv["id"])
    assert out.cancelled[oid] == "tampered"


async def test_capability_kill_switch_blocks_only_that_capability(tenant):
    """Feature kill switch 'followups' cancels follow-up items; agent replies of the same conversation still go out."""
    from agentplatform.core.config.registry import registry
    from agentplatform.core.db.pool import platform_tx
    from agentplatform.core.outbox.dispatcher import feature_of
    assert (feature_of("fu:1"), feature_of("cmt:9:private"), feature_of("camp:x:y"), feature_of("turn:0")) == \
        ("followups", "comments", "campaigns", "agent")
    conv, ig = await make_conversation(tenant)
    reply = await put(tenant, conv, ig, "resposta do agente")
    fu = await put(tenant, conv, ig, "follow-up", key=f"fu:{conv['id']}")
    async with platform_tx() as conn:
        await conn.execute("insert into kill_switches (tenant_id, scope, scope_ref, is_on, reason) values (%s,'feature','followups',true,'test')", (tenant,))
    registry.invalidate_kill_switches()
    try:
        out = await dispatcher(FakeChannelAdapter()).dispatch_conversation(tenant, conv["id"])
        assert reply in out.sent and out.cancelled[fu] == "kill_switch:feature:followups"
    finally:
        async with platform_tx() as conn:
            await conn.execute("delete from kill_switches where tenant_id = %s", (tenant,))
        registry.invalidate_kill_switches()


async def test_paused_agent_skips_turn_without_llm(tenant, monkeypatch):
    from agentplatform.core.config.registry import registry
    from agentplatform.core.db.pool import platform_tx
    from agentplatform.jobs import tasks
    conv, _ = await make_conversation(tenant)
    async with platform_tx() as conn:
        await conn.execute("insert into kill_switches (tenant_id, scope, scope_ref, is_on, reason) values (%s,'feature','agent',true,'test')", (tenant,))
    registry.invalidate_kill_switches()

    def boom():
        raise AssertionError("runtime must not be used when the agent is paused")
    monkeypatch.setattr(tasks, "get_runtime", boom)
    try:
        assert await tasks.run_turn(str(tenant), str(conv["id"])) == []
    finally:
        async with platform_tx() as conn:
            await conn.execute("delete from kill_switches where tenant_id = %s", (tenant,))
        registry.invalidate_kill_switches()
