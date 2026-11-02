"""End-to-end turn with a scripted gateway: inbound -> graph -> fenced commit -> outbox -> dispatcher."""
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentplatform.core.agent.turn import TurnDeps, TurnRunner
from agentplatform.core.channels.fake.adapter import FakeChannelAdapter
from agentplatform.core.config.registry import load_yaml
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.models.fake import ScriptedGateway, analysis, draft
from agentplatform.core.models.gateway import BudgetDenied
from agentplatform.core.orchestration.conversations import (
    get_or_create_conversation,
    store_inbound_message,
    transition_ownership,
)
from agentplatform.core.outbox.dispatcher import Dispatcher
from agentplatform.core.packages.contract import load_package

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]
CFG = load_yaml(ROOT / "config/tenants/dev-consortium.yaml")
PKG = load_package("consortium")
NOON = lambda: datetime.now(timezone.utc).replace(hour=15, minute=0)   # 12:00 BRT today (human hours, window open)


async def inbound(tenant, texts, igsid=None):
    igsid = igsid or f"IG-{uuid.uuid4().hex[:8]}"
    async with tenant_tx(tenant) as conn:
        ca = await (await conn.execute("select id from channel_accounts where tenant_id=%s", (tenant,))).fetchone()
        conv = await get_or_create_conversation(conn, tenant, ca["id"], "instagram", igsid, "novo")
        for t in texts:
            await store_inbound_message(conn, tenant, conv["id"], mid=f"m-{uuid.uuid4().hex[:8]}", kind="text", text=t,
                                        media=None, provider_ts=datetime.now(timezone.utc), inbound_event_id=None,
                                        service_window=timedelta(hours=24))
    return conv


def runner(script, now=NOON):
    return TurnRunner(TurnDeps(config=CFG, package=PKG, gateway=ScriptedGateway(script), knowledge=None, now=now))


async def lead_of(tenant, conv):
    async with tenant_tx(tenant) as conn:
        return await (await conn.execute("select * from leads where contact_id=%s", (conv["contact_id"],))).fetchone()


async def test_qualifying_turn_commits_and_sends(tenant):
    conv = await inbound(tenant, ["oi", "quero um carro, uma carta de 80 mil"])
    r = runner({"analyze_turn": [analysis(intents=["provide_info"], stage="qualificando", evidence=["quero um carro"],
                                          slots=[("goal", "vehicle", "quero um carro"), ("credit_amount", "80 mil", "carta de 80 mil")])],
                "respond": [draft("Olá! Sou o assistente virtual do consultor. Você tem um prazo em mente?")]})
    res = await r.run(tenant, conv["id"])
    assert res.status == "committed" and len(res.outbox_ids) == 1
    lead = await lead_of(tenant, conv)
    assert lead["stage"] == "qualificando" and lead["profile"]["credit_amount"]["value"] == "80000"
    a = FakeChannelAdapter()
    out = await Dispatcher({"instagram": a}).dispatch_conversation(tenant, conv["id"])
    assert len(out.sent) == 1 and "prazo" in a.texts()[0]


async def test_invented_number_blocked_then_retry(tenant):
    conv = await inbound(tenant, ["quanto fica a parcela?"])
    r = runner({"analyze_turn": [analysis(intents=["price_question"], stage="engajado", evidence=["parcela"])],
                "respond": [draft("A parcela fica R$ 1.234 por mês."),
                            draft("Os valores dependem da tabela oficial. Vou pedir para o consultor simular para você, combinado?")],
                "output_judge": [{"violations": [], "explanation": ""}]})
    res = await r.run(tenant, conv["id"])
    assert res.status == "committed"
    async with tenant_tx(tenant) as conn:
        g = await (await conn.execute("select rule_id from guard_results where turn_id=%s", (res.turn_id,))).fetchall()
        o = await (await conn.execute("select payload from outbox where turn_id=%s", (res.turn_id,))).fetchall()
    assert "numbers_provenance" in {x["rule_id"] for x in g}
    assert "1.220" not in o[0]["payload"]["text"]


async def test_second_block_goes_to_handoff_with_safe_message(tenant):
    conv = await inbound(tenant, ["me garante a contemplação?"])
    r = runner({"analyze_turn": [analysis(intents=["wants_fast"], stage="engajado")],
                "respond": [draft("Sim, contemplação garantida!"), draft("Com certeza, aprovação garantida.")]})
    res = await r.run(tenant, conv["id"])
    assert res.status == "handoff"
    async with tenant_tx(tenant) as conn:
        o = await (await conn.execute("select kind, payload from outbox where turn_id=%s", (res.turn_id,))).fetchall()
        c = await (await conn.execute("select ownership from conversations where id=%s", (conv["id"],))).fetchone()
    assert [x["kind"] for x in o] == ["handoff_expectation"]
    assert "garantid" not in o[0]["payload"]["text"]
    assert c["ownership"] == "AWAITING_HUMAN"


async def test_clear_opt_out_intent_is_recorded_not_just_answered(tenant):
    conv = await inbound(tenant, ["chega dessas mensagens, obrigado"])
    res = await runner({"analyze_turn": [analysis(opt_out=0.92)], "respond": [draft("nunca chamado")]}).run(tenant, conv["id"])
    assert res.status == "committed"
    lead = await lead_of(tenant, conv)
    assert lead["opted_out_at"] is not None and lead["stage"] == "opt_out"
    async with tenant_tx(tenant) as c:
        o = await (await c.execute("select payload from outbox where conversation_id=%s", (conv["id"],))).fetchone()
    assert o["payload"]["is_optout_confirmation"] is True


async def test_reply_cannot_promise_to_stop_when_nothing_was_recorded(tenant):
    conv = await inbound(tenant, ["acho que já deu, né?"])
    res = await runner({"analyze_turn": [analysis(opt_out=0.7)],
                        "respond": [draft("Tranquilo, vou parar de enviar mensagens."), draft("Você quer que eu pare de te chamar por aqui?")]}).run(tenant, conv["id"])
    assert res.status == "committed"
    lead = await lead_of(tenant, conv)
    assert lead["opted_out_at"] is None
    a = FakeChannelAdapter()
    await Dispatcher({"instagram": a}).dispatch_conversation(tenant, conv["id"])
    assert len(a.texts()) == 1 and a.texts()[0].endswith("Você quer que eu pare de te chamar por aqui?")


async def test_opt_out_while_human_records_suppression_without_sending(tenant):
    """review #1"""
    conv = await inbound(tenant, ["oi"])
    async with tenant_tx(tenant) as conn:
        await conn.execute("update messages set turn_id=gen_random_uuid() where conversation_id=%s", (conv["id"],))
        await transition_ownership(conn, tenant, conv["id"], "HUMAN", cause="echo_unknown_mid")
    await inbound(tenant, ["PARAR"], igsid=None) if False else None
    async with tenant_tx(tenant) as conn:
        await store_inbound_message(conn, tenant, conv["id"], mid=f"m-{uuid.uuid4().hex[:6]}", kind="text", text="PARAR",
                                    media=None, provider_ts=datetime.now(timezone.utc), inbound_event_id=None,
                                    service_window=timedelta(hours=24))
    g = ScriptedGateway({})
    res = await TurnRunner(TurnDeps(config=CFG, package=PKG, gateway=g, knowledge=None, now=NOON)).run(tenant, conv["id"])
    assert res.status == "committed" and res.outbox_ids == [] and g.calls == []
    lead = await lead_of(tenant, conv)
    assert lead["opted_out_at"] is not None and lead["stage"] == "opt_out"


async def test_human_owner_means_no_llm_and_no_output(tenant):
    conv = await inbound(tenant, ["oi"])
    async with tenant_tx(tenant) as conn:
        await transition_ownership(conn, tenant, conv["id"], "HUMAN", cause="console_take")
    g = ScriptedGateway({})
    res = await TurnRunner(TurnDeps(config=CFG, package=PKG, gateway=g, knowledge=None, now=NOON)).run(tenant, conv["id"])
    assert res.status == "committed" and res.outbox_ids == [] and g.calls == []


async def test_budget_exhausted_degrades_to_handoff(tenant):
    conv = await inbound(tenant, ["como funciona?"])
    r = runner({"analyze_turn": [BudgetDenied("key:total")]})
    res = await r.run(tenant, conv["id"])
    assert res.status == "handoff" and res.trace["degraded"] is True


async def test_off_hours_handoff_keeps_bot_serving(tenant):
    """review #8"""
    conv = await inbound(tenant, ["quero falar com o vendedor"])
    night = lambda: datetime.now(timezone.utc).replace(hour=2, minute=0)   # 23:00 BRT
    res = await runner({}, now=night).run(tenant, conv["id"])
    assert res.status == "handoff" and res.handoff["mode"] == "off_hours"
    async with tenant_tx(tenant) as conn:
        c = await (await conn.execute("select ownership, handoff_pending from conversations where id=%s", (conv["id"],))).fetchone()
    assert c["ownership"] == "BOT" and c["handoff_pending"] is True


async def test_superseded_turn_does_not_commit(tenant):
    """(a) new batch before commit: run_epoch bump makes the commit fail (fencing)."""
    conv = await inbound(tenant, ["oi"])

    def bump_then_answer(messages):
        return draft("Oi! Você pensa em imóvel ou veículo?")

    class Bumping(ScriptedGateway):
        async def chat(self, spec, messages, ctx, output_model=None):
            if ctx.node == "respond":
                async with tenant_tx(tenant) as conn:
                    await conn.execute("update conversations set run_epoch = run_epoch + 1 where id=%s", (conv["id"],))
            return await super().chat(spec, messages, ctx, output_model)
    g = Bumping({"analyze_turn": [analysis(stage="novo")], "respond": [draft("Oi! Você pensa em imóvel ou veículo?")]})
    res = await TurnRunner(TurnDeps(config=CFG, package=PKG, gateway=g, knowledge=None, now=NOON)).run(tenant, conv["id"])
    assert res.status == "cancelled"
    async with tenant_tx(tenant) as conn:
        n = (await (await conn.execute("select count(*) as n from outbox where conversation_id=%s", (conv["id"],))).fetchone())["n"]
        m = (await (await conn.execute("select count(*) as n from messages where conversation_id=%s and turn_id is null", (conv["id"],))).fetchone())["n"]
    assert n == 0 and m == 1      # nothing sent; the message stays pending for the combined next turn
