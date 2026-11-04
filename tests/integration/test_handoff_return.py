"""Who gets the conversation back, who is told, and what a person giving it back means (ADR-020)."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from agentplatform.core.channels.ports import SellerContact
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.handoff.alerts import auto_return_to_bot, return_to_bot, run_alerts, sellers_for
from agentplatform.core.handoff.notifiers import FallbackInbox, LogNotifier
from agentplatform.core.handoff.service import create_notice
from agentplatform.core.models.fake import ScriptedGateway, analysis, draft
from agentplatform.core.models.gateway import BudgetDenied
from agentplatform.core.orchestration.conversations import transition_ownership
from tests.integration.test_turn import CFG, NOON, inbound, lead_of, runner

pytestmark = pytest.mark.db
MIDNIGHT = lambda: datetime.now(timezone.utc).replace(hour=5, minute=0)      # 02:00 BRT


async def rows(tenant, sql, *args):
    async with tenant_tx(tenant) as conn:
        return await (await conn.execute(sql, args)).fetchall()


async def failed_turn(tenant, igsid=None, error=None):
    conv = await inbound(tenant, ["quanto custa?"], igsid)
    await runner({"analyze_turn": [error or RuntimeError("boom")]}).run(tenant, conv["id"])
    return conv


async def age_handoffs(tenant, conv, minutes=60):
    async with tenant_tx(tenant) as conn:
        await conn.execute("update handoffs set created_at = now() - make_interval(mins => %s) where conversation_id=%s", (minutes, conv["id"]))


async def test_failure_handoff_goes_back_and_the_question_is_answered(tenant):
    conv = await failed_turn(tenant)
    async with tenant_tx(tenant) as conn:
        assert await auto_return_to_bot(conn, CFG, tenant, datetime.now(timezone.utc)) == []      # too soon
    await age_handoffs(tenant, conv)
    async with tenant_tx(tenant) as conn:
        assert await auto_return_to_bot(conn, CFG, tenant, datetime.now(timezone.utc)) == [conv["id"]]
    c = (await rows(tenant, "select ownership from conversations where id=%s", conv["id"]))[0]
    assert c["ownership"] == "BOT"
    assert await rows(tenant, "select 1 from messages where conversation_id=%s and direction='in' and turn_id is null", conv["id"])
    assert (await rows(tenant, "select taken_at from handoffs where conversation_id=%s", conv["id"]))[0]["taken_at"]
    res = await runner({"analyze_turn": [analysis(intents=["greeting"], stage="novo", evidence=["custa"])],
                        "respond": [draft("Depende do valor da carta.")]}).run(tenant, conv["id"])
    assert res.status == "committed" and res.outbox_ids


async def test_notice_sent_while_failing_does_not_count_as_the_answer(tenant):
    """The short "someone will answer" went out after the question: the question is still unanswered."""
    from agentplatform.core.channels.fake.adapter import FakeChannelAdapter
    from agentplatform.core.outbox.dispatcher import Dispatcher
    conv = await failed_turn(tenant)
    await Dispatcher({"instagram": FakeChannelAdapter()}).dispatch_conversation(tenant, conv["id"])
    sent = await rows(tenant, "select kind from messages where conversation_id=%s and direction='out_bot'", conv["id"])
    assert [m["kind"] for m in sent] == ["handoff_notice"]
    await age_handoffs(tenant, conv)
    async with tenant_tx(tenant) as conn:
        await auto_return_to_bot(conn, CFG, tenant, datetime.now(timezone.utc))
    assert await rows(tenant, "select 1 from messages where conversation_id=%s and direction='in' and turn_id is null", conv["id"])


async def test_a_conversation_a_seller_took_never_comes_back_alone(tenant):
    conv = await failed_turn(tenant)
    await age_handoffs(tenant, conv)
    async with tenant_tx(tenant) as conn:
        await conn.execute("update handoffs set taken_at = now() where conversation_id=%s", (conv["id"],))
        assert await auto_return_to_bot(conn, CFG, tenant, datetime.now(timezone.utc)) == []


async def test_seller_replied_means_it_stays_with_the_seller(tenant):
    conv = await failed_turn(tenant)
    await age_handoffs(tenant, conv)
    async with tenant_tx(tenant) as conn:
        await conn.execute(
            """insert into messages (tenant_id, conversation_id, direction, kind, text_masked, provider_ts)
               values (%s,%s,'out_human','text','oi',now())""", (tenant, conv["id"]))
        assert await auto_return_to_bot(conn, CFG, tenant, datetime.now(timezone.utc)) == []


async def test_a_request_for_a_person_is_never_taken_back(tenant):
    conv = await inbound(tenant, ["quero falar com um vendedor"])
    await runner({}).run(tenant, conv["id"])
    assert (await rows(tenant, "select reason from handoffs where conversation_id=%s", conv["id"]))[0]["reason"] == "explicit_request"
    await age_handoffs(tenant, conv)
    async with tenant_tx(tenant) as conn:
        assert await auto_return_to_bot(conn, CFG, tenant, datetime.now(timezone.utc)) == []


async def test_daily_limit_of_returns(tenant):
    conv = await failed_turn(tenant)
    await age_handoffs(tenant, conv)
    async with tenant_tx(tenant) as conn:
        for _ in range(CFG.handoff.auto_return_max_per_day):
            await conn.execute(
                "insert into ownership_events (tenant_id, conversation_id, from_state, to_state, cause) values (%s,%s,'AWAITING_HUMAN','BOT','auto_return')",
                (tenant, conv["id"]))
        assert await auto_return_to_bot(conn, CFG, tenant, datetime.now(timezone.utc)) == []


async def test_off_hours_the_pending_alert_of_a_healed_failure_is_dropped(tenant):
    conv = await inbound(tenant, ["oi"])
    await runner({"analyze_turn": [RuntimeError("boom")]}, now=MIDNIGHT).run(tenant, conv["id"])
    c = (await rows(tenant, "select ownership, handoff_pending from conversations where id=%s", conv["id"]))[0]
    assert c == {"ownership": "BOT", "handoff_pending": True}
    await age_handoffs(tenant, conv)
    async with tenant_tx(tenant) as conn:
        assert await auto_return_to_bot(conn, CFG, tenant, datetime.now(timezone.utc)) == []
    c = (await rows(tenant, "select ownership, handoff_pending from conversations where id=%s", conv["id"]))[0]
    assert c == {"ownership": "BOT", "handoff_pending": False}
    h = (await rows(tenant, "select notified_at, notified_via from handoffs where conversation_id=%s", conv["id"]))[0]
    assert h["notified_at"] and h["notified_via"] == "auto_return"          # the seller is not woken up about it in the morning


async def test_conversation_cap_only_comes_back_when_a_person_gives_it_back(tenant):
    conv = await inbound(tenant, ["oi"])
    await runner({"analyze_turn": [BudgetDenied(f"conversation:{conv['id']}")]}).run(tenant, conv["id"])
    assert (await rows(tenant, "select reason from handoffs where conversation_id=%s", conv["id"]))[0]["reason"] == "conversation_budget"
    await age_handoffs(tenant, conv)
    async with tenant_tx(tenant) as conn:
        assert await auto_return_to_bot(conn, CFG, tenant, datetime.now(timezone.utc)) == []
        await conn.execute(
            "insert into budget_balances (tenant_id, scope, cap_usd, spent_usd) values (%s,%s,0.05,0.05)",
            (tenant, f"conversation:{conv['id']}"))
        await return_to_bot(conn, tenant, conv["id"], cause="console:bot", actor=None)
    bal = (await rows(tenant, "select spent_usd from budget_balances where scope=%s", f"conversation:{conv['id']}"))[0]
    assert bal["spent_usd"] == Decimal(0)
    assert (await rows(tenant, "select ownership from conversations where id=%s", conv["id"]))[0]["ownership"] == "BOT"
    assert await rows(tenant, "select 1 from messages where conversation_id=%s and direction='in' and turn_id is null", conv["id"])


async def test_giving_back_answers_only_what_nobody_answered(tenant):
    conv = await inbound(tenant, ["pergunta antiga"])
    async with tenant_tx(tenant) as conn:
        await conn.execute("update messages set turn_id = gen_random_uuid() where conversation_id=%s", (conv["id"],))
        await conn.execute(
            """insert into messages (tenant_id, conversation_id, direction, kind, text_masked, provider_ts)
               values (%s,%s,'out_human','text','respondi',now())""", (tenant, conv["id"]))
        await transition_ownership(conn, tenant, conv["id"], "HUMAN", cause="test")
    newer = await inbound(tenant, ["pergunta nova"], await _igsid(tenant, conv))
    async with tenant_tx(tenant) as conn:
        await conn.execute("update messages set turn_id = gen_random_uuid() where conversation_id=%s", (newer["id"],))
        await return_to_bot(conn, tenant, conv["id"], cause="console:bot", actor=None)
    pending = await rows(tenant, "select text_masked from messages where conversation_id=%s and direction='in' and turn_id is null", conv["id"])
    assert [p["text_masked"] for p in pending] == ["pergunta nova"]


async def test_giving_back_does_not_repeat_a_request_for_a_person(tenant):
    """The seller clicks "Retomar" after "quero falar com um vendedor": the assistant must not call the seller all over again."""
    conv = await inbound(tenant, ["quero falar com um vendedor"])
    res = await runner({}).run(tenant, conv["id"])
    assert res.status == "handoff"
    async with tenant_tx(tenant) as conn:
        await return_to_bot(conn, tenant, conv["id"], cause="console:bot", actor=None)
    assert not await rows(tenant, "select 1 from messages where conversation_id=%s and direction='in' and turn_id is null", conv["id"])
    # what the customer said while waiting is still answered
    await inbound(tenant, ["ainda tô aqui, e o lance?"], await _igsid(tenant, conv))
    assert len(await rows(tenant, "select 1 from messages where conversation_id=%s and direction='in' and turn_id is null", conv["id"])) == 1


async def _igsid(tenant, conv):
    return (await rows(tenant, "select external_user_id from contact_identities where contact_id=%s", conv["contact_id"]))[0]["external_user_id"]


# ------------------------------------------------------------------ notices: the seller hears, the conversation stays
async def test_question_without_an_approved_answer_reaches_the_seller_without_taking_the_conversation(tenant):
    from agentplatform.core.agent.state import RetrievalResult
    from agentplatform.core.agent.turn import TurnDeps, TurnRunner
    from tests.integration.test_turn import PKG

    class Empty:
        async def retrieve(self, conn, tid, q, ctx, degraded=False):
            return RetrievalResult(no_basis=True)

    def turn(script):
        return TurnRunner(TurnDeps(config=CFG, package=PKG, gateway=ScriptedGateway(script), knowledge=Empty(), now=NOON))

    def script():
        a = analysis(intents=["product_question"], stage="novo", evidence=["lance"])
        a["understanding"]["questions"] = ["e o lance?"]
        return {"analyze_turn": [a], "respond": [draft("Vou confirmar com o consultor e te falo.")]}
    conv = await inbound(tenant, ["e o lance?"])
    res = await turn(script()).run(tenant, conv["id"])
    assert res.status == "committed"
    assert (await rows(tenant, "select ownership from conversations where id=%s", conv["id"]))[0]["ownership"] == "BOT"
    h = await rows(tenant, "select reason, is_notice, notified_at, taken_at from handoffs where conversation_id=%s", conv["id"])
    assert len(h) == 1 and h[0]["reason"] == "knowledge_gap" and h[0]["is_notice"] and h[0]["notified_at"] is None and h[0]["taken_at"]
    inbox = FallbackInbox([LogNotifier()])
    async with tenant_tx(tenant) as conn:
        await _add_user(conn, tenant, "seller")
        await run_alerts(conn, CFG, tenant, inbox, NOON(), "http://x/console")
    assert [a.reason for _, a in inbox.notifiers[0].sent] == ["knowledge_gap"]
    # a second question the same day does not bother the seller again, and does not count as "the consultant was already called"
    conv2 = await inbound(tenant, ["e o sorteio?"], await _igsid(tenant, conv))
    await turn(script()).run(tenant, conv2["id"])
    assert len(await rows(tenant, "select 1 from handoffs where conversation_id=%s", conv["id"])) == 1
    third = await inbound(tenant, ["quero falar com um vendedor"], await _igsid(tenant, conv))
    res = await turn({}).run(tenant, third["id"])
    assert res.status == "handoff"


async def test_someone_who_asked_to_stop_and_writes_again_reaches_the_seller_not_the_assistant(tenant):
    conv = await inbound(tenant, ["parar"])
    await runner({}).run(tenant, conv["id"])
    assert (await lead_of(tenant, conv))["opted_out_at"]
    igsid = await _igsid(tenant, conv)
    await inbound(tenant, ["oi, mudei de ideia"], igsid)
    r = runner({})
    res = await r.run(tenant, conv["id"])
    assert res.outbox_ids == [] and r.d.gateway.calls == []
    h = await rows(tenant, "select reason, is_notice from handoffs where conversation_id=%s", conv["id"])
    assert h == [{"reason": "opted_out_returned", "is_notice": True}]
    assert (await lead_of(tenant, conv))["opted_out_at"]            # still suppressed: nothing proactive goes out


async def test_the_stop_confirmation_goes_out_right_away(tenant):
    """Found in the real test: without the id the dispatch job was not created, and the confirmation waited for the minute sweep."""
    conv = await inbound(tenant, ["parar"])
    res = await runner({}).run(tenant, conv["id"])
    assert len(res.outbox_ids) == 1
    assert "não vou mais" in (await rows(tenant, "select payload from outbox where id=%s", res.outbox_ids[0]))[0]["payload"]["text"]


async def test_stop_request_while_a_seller_is_on_the_conversation_is_told_to_the_seller(tenant):
    conv = await inbound(tenant, ["parar"])
    async with tenant_tx(tenant) as conn:
        await transition_ownership(conn, tenant, conv["id"], "HUMAN", cause="test")
    await runner({}).run(tenant, conv["id"])
    assert (await rows(tenant, "select reason from handoffs where conversation_id=%s", conv["id"]))[0]["reason"] == "opt_out"


async def test_off_hours_asking_again_repeats_nothing(tenant):
    conv = await inbound(tenant, ["quero falar com um vendedor"])
    await runner({}, now=MIDNIGHT).run(tenant, conv["id"])
    igsid = await _igsid(tenant, conv)
    await inbound(tenant, ["cadê o vendedor? quero falar com um vendedor"], igsid)
    res = await runner({"analyze_turn": [analysis(intents=["wants_human"], stage="novo", evidence=["vendedor"])],
                        "respond": [draft("Seu pedido já está registrado e ele responde assim que puder.")]}, now=MIDNIGHT).run(tenant, conv["id"])
    assert res.status == "committed"
    assert len(await rows(tenant, "select 1 from handoffs where conversation_id=%s", conv["id"])) == 1


async def test_notice_dedupe_is_per_reason(tenant):
    conv = await inbound(tenant, ["oi"])
    async with tenant_tx(tenant) as conn:
        kw = dict(tenant_id=tenant, conversation_id=conv["id"], lead_id=None, summary={}, now=NOON())
        assert await create_notice(conn, CFG, reason="a", **kw)
        assert await create_notice(conn, CFG, reason="a", **kw) is None
        assert await create_notice(conn, CFG, reason="b", **kw)


# ------------------------------------------------------------------ reaching a seller
async def _add_user(conn, tenant, role, whatsapp=None, email=None, name=None):
    from agentplatform.core.security.keys import encrypt_field
    uid = uuid.uuid4()
    await conn.execute(
        """insert into tenant_users (id, tenant_id, email_hash, email_enc, display_name, role, notify_whatsapp_enc, notify_email_enc)
           values (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (uid, tenant, uid.bytes, await encrypt_field(conn, tenant, f"{uid}@x.test", "tenant_users.email"), name or role, role,
         await encrypt_field(conn, tenant, whatsapp, "tenant_users.whatsapp") if whatsapp else None,
         await encrypt_field(conn, tenant, email, "tenant_users.email") if email else None))
    return uid


class Notifier:
    name = "real"

    def __init__(self, reachable=(), raises=False):
        self.reachable, self.raises, self.sent = set(reachable), raises, []

    async def send_alert(self, seller: SellerContact, alert) -> bool:
        if self.raises:
            raise RuntimeError("provider down")
        self.sent.append((seller.seller_id, alert.kind))
        return seller.seller_id in self.reachable


async def test_a_seller_who_cannot_be_reached_is_not_counted_as_told(tenant):
    conv = await failed_turn(tenant)
    n = Notifier(reachable=())
    async with tenant_tx(tenant) as conn:
        await _add_user(conn, tenant, "seller")
        stats = await run_alerts(conn, CFG, tenant, FallbackInbox([n]), NOON(), "http://x/console")
    assert stats["failed"] == 1 and stats["sent"] == 0
    h = (await rows(tenant, "select notified_at from handoffs where conversation_id=%s", conv["id"]))[0]
    assert h["notified_at"] is None               # tried again at the next tick; the operator alert counts these
    assert not await rows(tenant, "select 1 from audit_log where action='handoff.routed' and tenant_id=%s", tenant)


async def test_the_next_person_is_tried_when_the_first_cannot_be_reached(tenant):
    await failed_turn(tenant)
    async with tenant_tx(tenant) as conn:
        await _add_user(conn, tenant, "seller")
        owner = await _add_user(conn, tenant, "tenant_owner")
        stats = await run_alerts(conn, CFG, tenant, FallbackInbox([Notifier(reachable={owner})]), NOON(), "http://x/console")
    assert stats["sent"] == 1
    assert (await rows(tenant, "select notified_via from handoffs"))[0]["notified_via"] == "real"


async def test_a_notifier_that_raises_does_not_stop_the_chain(tenant):
    from agentplatform.core.channels.ports import Alert
    seller = SellerContact(uuid.uuid4(), None, None)
    alert = Alert("handoff", None, None, "r", "s", "u", None)
    assert await FallbackInbox([Notifier(raises=True), LogNotifier()]).notify_handoff(seller, alert) == "log"
    assert await FallbackInbox([Notifier(raises=True)]).notify_handoff(seller, alert) is None


async def test_a_log_line_only_counts_when_nobody_else_can_be_tried(tenant):
    await failed_turn(tenant)

    class Inbox:
        async def notify_handoff(self, seller, alert):
            return "real" if seller.seller_id == owner else "log"
    async with tenant_tx(tenant) as conn:
        await _add_user(conn, tenant, "seller")
        owner = await _add_user(conn, tenant, "tenant_owner")
        await run_alerts(conn, CFG, tenant, Inbox(), NOON(), "http://x/console")
    assert (await rows(tenant, "select notified_via from handoffs"))[0]["notified_via"] == "real"


def test_production_does_not_consider_a_log_line_a_delivered_alert():
    from agentplatform.runtime import Runtime
    from agentplatform.settings import Settings
    rt = object.__new__(Runtime)
    rt.settings = Settings(app_env="prod", llm_provider="openrouter", ops_alert_email="ops@x.test")
    assert not any(isinstance(n, LogNotifier) for n in rt._build_inbox().notifiers)
    rt.settings = Settings(app_env="dev")
    assert any(isinstance(n, LogNotifier) for n in rt._build_inbox().notifiers)


async def test_seller_comes_before_owner_and_routing_names_win(tenant):
    from agentplatform.core.config.schema import RoutingRule
    async with tenant_tx(tenant) as conn:
        owner = await _add_user(conn, tenant, "tenant_owner")
        s1 = await _add_user(conn, tenant, "seller")
        s2 = await _add_user(conn, tenant, "seller")
        ordered = [s.seller_id for s in await sellers_for(conn, tenant, CFG)]
        assert set(ordered[:2]) == {s1, s2} and ordered[2] == owner
        cfg = CFG.model_copy(update={"handoff": CFG.handoff.model_copy(update={"routing": [RoutingRule(seller_ids=[str(s2)])]})})
        assert (await sellers_for(conn, tenant, cfg))[0].seller_id == s2


async def test_owner_hears_once_when_a_customer_keeps_waiting(tenant):
    conv = await failed_turn(tenant)
    n = Notifier(reachable=None or ())
    async with tenant_tx(tenant) as conn:
        await _add_user(conn, tenant, "seller")
        owner = await _add_user(conn, tenant, "tenant_owner")
        n.reachable = {owner}
        await conn.execute("update conversations set last_inbound_at = %s::timestamptz - interval '5 hours', last_reply_at = null where id=%s", (NOON(), conv["id"]))
        await conn.execute("update handoffs set created_at = %s::timestamptz - interval '5 hours', notified_at = now(), taken_at = null where conversation_id=%s", (NOON(), conv["id"]))
        stats = await run_alerts(conn, CFG, tenant, FallbackInbox([n]), NOON(), "http://x/console")
        assert stats["escalated"] == 1
        assert await run_alerts(conn, CFG, tenant, FallbackInbox([n]), NOON(), "http://x/console")
    assert [k for sid, k in n.sent if sid == owner].count("escalation") == 1


async def test_a_simulated_conversation_does_not_come_back_by_itself(tenant):
    """Nobody is waiting on the simulator, and an evaluation that failed kept spending after it had ended."""
    conv = await failed_turn(tenant)
    await age_handoffs(tenant, conv)
    async with tenant_tx(tenant) as conn:
        await conn.execute("""update contact_identities set channel='simulator' where contact_id=(select contact_id from conversations where id=%s)""", (conv["id"],))
        assert await auto_return_to_bot(conn, CFG, tenant, datetime.now(timezone.utc)) == []
