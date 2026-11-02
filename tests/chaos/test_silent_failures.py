"""Failures that used to leave a customer without an answer and nobody told (independent review, plan 07): the queue, the turn and the
outbox each must end in an answer, in a retry, or in a person being told."""
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import psycopg
import pytest
from procrastinate.exceptions import UniqueViolation
from procrastinate.jobs import Status

from agentplatform.core.channels.fake.adapter import FakeChannelAdapter
from agentplatform.core.channels.instagram.adapter import InstagramGraphAdapter
from agentplatform.core.channels.ports import (
    AccountRef,
    AmbiguousSendError,
    RetryableSendError,
    SendError,
    TokenInvalidError,
)
from agentplatform.core.config.registry import registry
from agentplatform.core.db.pool import platform_tx, tenant_tx
from agentplatform.core.models.fake import ScriptedGateway, analysis, draft
from agentplatform.core.orchestration.conversations import transition_ownership
from agentplatform.core.orchestration.human_detector import handle_echo
from agentplatform.core.outbox.dispatcher import Dispatcher, DispatchPolicy
from agentplatform.jobs import tasks
from agentplatform.settings import get_settings
from tests.integration.helpers import make_conversation, put, status
from tests.integration.test_turn import CFG, NOON, PKG, inbound, runner

pytestmark = pytest.mark.db


def chat(script=None, knowledge=None):
    from agentplatform.core.agent.turn import TurnDeps, TurnRunner
    return TurnRunner(TurnDeps(config=CFG, package=PKG, gateway=ScriptedGateway(script or {}), knowledge=knowledge, now=NOON))


async def conv_row(tenant, cid):
    async with tenant_tx(tenant) as conn:
        return await (await conn.execute("select * from conversations where id=%s", (cid,))).fetchone()


async def rows(tenant, sql, *args):
    async with tenant_tx(tenant) as conn:
        return await (await conn.execute(sql, args)).fetchall()


async def committed_reply(tenant, text="uma resposta"):
    """A real turn committed with its reply in the outbox, not yet sent."""
    conv = await inbound(tenant, ["quero saber"])
    r = runner({"analyze_turn": [analysis(intents=["greeting"], stage="novo", evidence=["quero"])], "respond": [draft(text)]})
    res = await r.run(tenant, conv["id"])
    return conv, res


# ------------------------------------------------------------------ queue
def test_only_jobs_without_a_queueing_lock_are_retried_by_the_queue():
    from agentplatform.jobs import app as J
    for name in ("close_batch", "fetch_username", "summarize"):
        assert J.TASKS[name].retry_strategy is None
    for name in ("run_turn", "ingress", "dispatch", "reconcile", "echo", "comment"):
        assert J.TASKS[name].retry_strategy is not None


async def test_retry_of_a_failed_turn_with_the_next_turn_queued(db):
    """The collision reproduced in the review, with turns as they are now (conversation lock only). Runs inside a rolled back transaction."""
    q = f"__test_{uuid.uuid4().hex[:8]}"
    lock = f"conv:{uuid.uuid4()}"
    with psycopg.connect(get_settings().migration_database_url) as conn:
        worker = conn.execute("select worker_id from procrastinate_register_worker_v1()").fetchone()[0]

        def defer():
            return conn.execute(
                "select unnest(procrastinate_defer_jobs_v1(array[row(%s,'probe',0,%s,null,'{}'::jsonb,null)::procrastinate_job_to_defer_v1]))",
                (q, lock)).fetchone()[0]
        a = defer()
        assert conn.execute("select id from procrastinate_fetch_job_v2(array[%s], %s)", (q, worker)).fetchone()[0] == a
        b = defer()
        conn.execute("select procrastinate_retry_job_v2(%s, now(), null, null, null)", (a,))    # raised duplicate key before
        fetched = conn.execute("select id from procrastinate_fetch_job_v2(array[%s], %s)", (q, worker)).fetchone()[0]
        assert fetched == a and b != a          # the conversation keeps moving, in order
        conn.rollback()


async def test_stalled_sweep_survives_a_job_it_cannot_retry():
    class JM:
        finished, retried = [], []

        async def get_stalled_jobs(self, seconds_since_heartbeat):
            return [type("J", (), {"id": i, "task_name": "close_batch"}) for i in (1, 2, 3)]

        async def retry_job_by_id_async(self, job_id, retry_at):
            if job_id == 1:
                raise UniqueViolation(constraint_name="procrastinate_jobs_queueing_lock_idx_v1", queueing_lock="cb:x")
            if job_id == 2:
                raise OSError("connection lost")
            self.retried.append(job_id)

        async def finish_job_by_id_async(self, job_id, status, delete_job):
            self.finished.append((job_id, status))
    jm = JM()
    await tasks.retry_stalled(type("App", (), {"job_manager": jm}))
    assert jm.retried == [3] and jm.finished == [(1, Status.FAILED)]


async def test_sweep_picks_up_a_message_nobody_is_working_on(tenant):
    conv = await inbound(tenant, ["oi"])
    cfg = CFG

    async def sweep():
        async with tenant_tx(tenant) as conn:
            return await tasks.sweep_pending_turns(tenant, conn, cfg)
    assert await sweep() == []                  # too recent: the normal path is still on it
    async with tenant_tx(tenant) as conn:
        await conn.execute("update messages set created_at = now() - interval '5 minutes' where conversation_id=%s", (conv["id"],))
    jobs = await sweep()
    assert [j[0] for j in jobs] == ["close_batch"] and jobs[0][2] == {"queueing_lock": f"cb:{conv['id']}"}

    async with tenant_tx(tenant) as conn:       # a live turn has it
        await conn.execute(
            """insert into turns (tenant_id, conversation_id, run_epoch, input_message_ids, status, lease_id, lease_expires_at)
               values (%s,%s,0,'{}','running',gen_random_uuid(), now() + interval '1 minute')""", (tenant, conv["id"]))
    assert await sweep() == []
    async with tenant_tx(tenant) as conn:       # that worker died: the lease expired
        await conn.execute("update turns set lease_expires_at = now() - interval '1 minute' where conversation_id=%s", (conv["id"],))
    assert len(await sweep()) == 1

    async with tenant_tx(tenant) as conn:       # the account cannot send: waits, and comes back when it can
        await conn.execute("update channel_accounts set token_status='invalid' where tenant_id=%s", (tenant,))
    assert await sweep() == []


async def test_dead_inbound_event_becomes_a_dead_letter(tenant):
    from agentplatform.core.channels.events import CanonicalEvent
    from agentplatform.core.orchestration.ingress import persist_events
    async with tenant_tx(tenant) as conn:
        acc = (await (await conn.execute("select external_account_id from channel_accounts where tenant_id=%s", (tenant,))).fetchone())["external_account_id"]
    (_, eid), = await persist_events([CanonicalEvent(event_type="MessageReceived", channel="instagram", account_external_id=acc,
                                                     contact_external_id="IG-x", idempotency_key=f"msg:{uuid.uuid4()}", mid="m",
                                                     text="oi", provider_ts=datetime.now(timezone.utc))])
    async with tenant_tx(tenant) as conn:
        await conn.execute("update inbound_events set attempts=5, last_error='boom' where id=%s", (eid,))
    await tasks.sweep_inbound()
    assert (await rows(tenant, "select status from inbound_events where id=%s", eid))[0]["status"] == "dead"
    async with platform_tx() as conn:
        dl = await (await conn.execute("select source, error from dead_letters where ref=%s", (str(eid),))).fetchone()
    assert dl == {"source": "inbound_event", "error": "boom"}


async def test_failing_job_is_recorded_only_as_a_class_name(tenant):
    ref = str(uuid.uuid4())
    await tasks.record_dead_letter("run_turn", {"tenant_id": str(tenant), "conversation_id": ref}, RuntimeError("customer said a secret"))
    async with platform_tx() as conn:
        dl = await (await conn.execute("select error from dead_letters where ref=%s", (ref,))).fetchone()
    assert dl["error"] == "RuntimeError"


# ------------------------------------------------------------------ turn
async def test_unexpected_error_reaches_a_person(tenant):
    conv = await inbound(tenant, ["quero saber o valor"])
    r = chat({"analyze_turn": [RuntimeError("provider returned something odd")]})
    res = await r.run(tenant, conv["id"])
    assert res.status == "handoff" and len(res.outbox_ids) == 1
    assert (await conv_row(tenant, conv["id"]))["ownership"] == "AWAITING_HUMAN"
    h = await rows(tenant, "select reason from handoffs where conversation_id=%s", conv["id"])
    assert [x["reason"] for x in h] == ["technical_failure"]
    t = (await rows(tenant, "select status, cancel_reason from turns where conversation_id=%s", conv["id"]))[0]
    assert t == {"status": "failed", "cancel_reason": "RuntimeError"}            # what the turn_failures alert counts
    assert not await rows(tenant, "select 1 from messages where conversation_id=%s and turn_id is null and direction='in'", conv["id"])
    out = (await rows(tenant, "select kind, payload from outbox where id=%s", res.outbox_ids[0]))[0]
    assert out["kind"] == "handoff_expectation" and "responder" in out["payload"]["text"]


async def _boom(*a, **k):
    raise RuntimeError("graph failure")


async def test_unexpected_error_with_a_person_on_the_conversation_stays_silent(tenant, monkeypatch):
    conv = await inbound(tenant, ["oi de novo"])
    async with tenant_tx(tenant) as conn:
        await transition_ownership(conn, tenant, conv["id"], "HUMAN", cause="test")
    r = chat()
    monkeypatch.setattr(r.graph, "ainvoke", _boom)
    res = await r.run(tenant, conv["id"])
    assert res.outbox_ids == []
    assert not await rows(tenant, "select 1 from handoffs where conversation_id=%s", conv["id"])
    assert (await conv_row(tenant, conv["id"]))["ownership"] == "HUMAN"


async def test_unexpected_error_still_honors_a_stop_request(tenant, monkeypatch):
    conv = await inbound(tenant, ["parar"])
    r = chat()
    monkeypatch.setattr(r.graph, "ainvoke", _boom)
    await r.run(tenant, conv["id"])
    lead = (await rows(tenant, "select opted_out_at from leads where contact_id=%s", conv["contact_id"]))[0]
    assert lead["opted_out_at"] is not None
    assert not await rows(tenant, "select 1 from handoffs where conversation_id=%s", conv["id"])


async def test_error_while_failing_safely_goes_back_to_the_queue(tenant, monkeypatch):
    """Database down in the middle: nothing is written, the exception reaches the queue, and the job's retry runs the turn again."""
    conv = await inbound(tenant, ["oi"])
    r = chat()
    monkeypatch.setattr(r.graph, "ainvoke", _boom)
    monkeypatch.setattr(r, "n_commit", _boom)
    with pytest.raises(RuntimeError):
        await r.run(tenant, conv["id"])
    assert await rows(tenant, "select 1 from messages where conversation_id=%s and turn_id is null and direction='in'", conv["id"])


async def test_knowledge_search_outage_hands_off_instead_of_retrying_forever(tenant):
    from agentplatform.core.agent.state import RetrievalResult

    class Search:
        async def retrieve(self, conn, tid, q, ctx, degraded=False):
            if not degraded:
                raise httpx.ConnectError("embedding provider down")
            return RetrievalResult(no_basis=True)
    conv = await inbound(tenant, ["como funciona o lance?"])
    a = analysis(intents=["product_question"], stage="novo", evidence=["lance"])
    a["understanding"]["questions"] = ["como funciona o lance?"]
    res = await chat({"analyze_turn": [a]}, knowledge=Search()).run(tenant, conv["id"])
    assert res.status == "handoff"
    assert (await rows(tenant, "select reason from handoffs where conversation_id=%s", conv["id"]))[0]["reason"] == "technical_failure"


# ------------------------------------------------------------------ the turn is not run when its reply could not go out
async def _make_runtime(monkeypatch, tenant, gw):
    from agentplatform.core.handoff.notifiers import FallbackInbox, LogNotifier
    from agentplatform.runtime import Runtime, TenantRuntime, set_runtime
    rt = Runtime(adapters={"instagram": FakeChannelAdapter()}, gateway=gw, inbox=FallbackInbox([LogNotifier()]))
    rt._tenants[str(tenant)] = (1e18, TenantRuntime(tenant, CFG, None, PKG, None))
    rt.use_checkpointer = False
    set_runtime(rt)
    return rt


async def test_invalid_token_skips_the_turn_and_keeps_the_message(tenant, monkeypatch):
    gw = ScriptedGateway({})
    await _make_runtime(monkeypatch, tenant, gw)
    conv = await inbound(tenant, ["quanto custa?"])
    async with tenant_tx(tenant) as conn:
        await conn.execute("update channel_accounts set token_status='invalid' where tenant_id=%s", (tenant,))
    assert await tasks.run_turn(str(tenant), str(conv["id"])) == []
    assert gw.calls == []
    assert await rows(tenant, "select 1 from messages where conversation_id=%s and turn_id is null and direction='in'", conv["id"])


async def test_stop_request_is_recorded_even_when_the_account_cannot_send(tenant, monkeypatch):
    gw = ScriptedGateway({})
    await _make_runtime(monkeypatch, tenant, gw)
    conv = await inbound(tenant, ["parar"])
    async with tenant_tx(tenant) as conn:
        await conn.execute("update channel_accounts set token_status='invalid' where tenant_id=%s", (tenant,))
    await tasks.run_turn(str(tenant), str(conv["id"]))
    assert gw.calls == []
    assert (await rows(tenant, "select opted_out_at from leads where contact_id=%s", conv["contact_id"]))[0]["opted_out_at"]


# ------------------------------------------------------------------ outbox
async def test_echo_confirmed_while_the_call_was_in_flight_stays_sent(tenant):
    conv, ig = await make_conversation(tenant)
    oid = await put(tenant, conv, ig, "olá")

    class Slow(FakeChannelAdapter):
        async def send(self, account, msg):
            assert await handle_echo(tenant, conv["id"], "mid-echo", msg.text, reconcile_window_s=120, rechecked=False) == "ours"
            raise AmbiguousSendError("timeout after delivery")
    out = await Dispatcher({"instagram": Slow()}).dispatch_conversation(tenant, conv["id"])
    st = await status(tenant, oid)
    assert st["status"] == "sent" and st["external_mid"] == "mid-echo"
    assert out.unknown == [oid]        # its reconcile finds the item already sent
    hist = await rows(tenant, "select text_masked, direction from messages where conversation_id=%s and direction='out_bot'", conv["id"])
    assert hist == [{"text_masked": "olá", "direction": "out_bot"}]     # an echo confirmation also lands in the history


async def test_unexpected_send_error_is_ambiguous_and_never_resent(tenant):
    conv, ig = await make_conversation(tenant)
    oid = await put(tenant, conv, ig, "olá")
    a = FakeChannelAdapter()
    a.fail_next.append(RuntimeError("bug in the adapter"))
    d = Dispatcher({"instagram": a})
    out = await d.dispatch_conversation(tenant, conv["id"])
    assert out.unknown == [oid] and (await status(tenant, oid))["status"] == "unknown"
    await d.dispatch_conversation(tenant, conv["id"])
    assert a.sent == []


async def test_a_send_the_worker_lost_is_reconciled_not_resent(tenant):
    conv, ig = await make_conversation(tenant)
    oid = await put(tenant, conv, ig, "olá")
    async with tenant_tx(tenant) as conn:
        await conn.execute("update outbox set status='sending', attempt_registered_at = now() - interval '10 minutes' where id=%s", (oid,))
        jobs = await tasks.sweep_orphan_sends(tenant, conn, CFG)
    assert jobs == [("reconcile", {"tenant_id": str(tenant), "outbox_id": str(oid)}, {})]
    assert (await status(tenant, oid))["status"] == "unknown"
    told = []

    async def on_failure(t, c, reason):
        told.append(reason)
    a = FakeChannelAdapter()
    assert await Dispatcher({"instagram": a}, on_failure=on_failure).reconcile_unknown(tenant, oid) == "failed"
    assert told == ["ambiguous_send_unconfirmed"] and a.sent == []


async def test_a_delayed_bubble_holds_back_the_rest_of_its_reply(tenant):
    conv, res = await committed_reply(tenant, "Primeira parte da resposta. " + "x" * 1100)
    assert len(res.outbox_ids) >= 2
    a = FakeChannelAdapter()
    a.fail_next.append(RetryableSendError("connect"))
    await Dispatcher({"instagram": a}).dispatch_conversation(tenant, conv["id"])
    assert a.sent == []                # the second part did not jump ahead
    items = await rows(tenant, "select status, not_before from outbox where conversation_id=%s order by effect_key", conv["id"])
    assert {i["status"] for i in items} == {"pending"} and all(i["not_before"] >= items[0]["not_before"] for i in items)


async def test_refusal_about_the_account_stops_the_channel_and_keeps_the_question(tenant):
    conv, res = await committed_reply(tenant)
    told = []

    async def on_failure(t, c, reason):
        told.append(reason)
    a = FakeChannelAdapter()
    a.fail_next.append(SendError("HTTP 400 code=200 subcode=2534041", scope="account"))
    await Dispatcher({"instagram": a}, on_failure=on_failure).dispatch_conversation(tenant, conv["id"])
    try:
        assert told == []              # one problem of the account is not a handoff per conversation
        async with platform_tx() as conn:
            ks = await (await conn.execute(
                "select is_on, reason from kill_switches where tenant_id=%s and scope='channel'", (tenant,))).fetchone()
        assert ks == {"is_on": True, "reason": "channel_error"}
        assert await rows(tenant, "select 1 from messages where conversation_id=%s and turn_id is null and direction='in'", conv["id"])
    finally:
        async with platform_tx() as conn:
            await conn.execute("delete from kill_switches where tenant_id=%s", (tenant,))
        registry.invalidate_kill_switches()


async def test_invalid_token_is_not_a_handoff_per_conversation(tenant):
    conv, res = await committed_reply(tenant)
    told = []

    async def on_failure(t, c, reason):
        told.append(reason)
    a = FakeChannelAdapter()
    a.fail_next.append(TokenInvalidError("190/460"))
    await Dispatcher({"instagram": a}, on_failure=on_failure).dispatch_conversation(tenant, conv["id"])
    assert told == []
    assert (await rows(tenant, "select token_status from channel_accounts where tenant_id=%s", tenant))[0]["token_status"] == "invalid"
    assert await rows(tenant, "select 1 from messages where conversation_id=%s and turn_id is null and direction='in'", conv["id"])


@pytest.mark.parametrize("error, kind, tells", [
    (SendError("code=551", scope="recipient"), "dm_text", False),
    (SendError("comment gone", scope="object"), "private_reply", False),
    (SendError("hide failed"), "hide_comment", False),
    (SendError("reply failed"), "public_comment_reply", False),
    (SendError("not the owner", scope="thread_owner"), "dm_text", True),
    (SendError("something else"), "dm_text", True),
])
async def test_who_hears_about_a_refused_send(tenant, error, kind, tells):
    conv, ig = await make_conversation(tenant)
    await put(tenant, conv, ig, "olá", kind=kind, comment_id="c1" if kind != "dm_text" else None)
    told = []

    async def on_failure(t, c, reason):
        told.append(reason)
    a = FakeChannelAdapter()
    a.fail_next.append(error)
    await Dispatcher({"instagram": a}, on_failure=on_failure).dispatch_conversation(tenant, conv["id"])
    assert bool(told) is tells


async def test_reply_too_late_for_the_window_tells_the_seller(tenant):
    conv, ig = await make_conversation(tenant)
    await put(tenant, conv, ig, "olá")
    async with tenant_tx(tenant) as conn:
        await conn.execute("update conversations set window_expires_at = now() - interval '1 hour' where id=%s", (conv["id"],))
    told = []

    async def on_failure(t, c, reason):
        told.append(reason)
    await Dispatcher({"instagram": FakeChannelAdapter()}, on_failure=on_failure).dispatch_conversation(tenant, conv["id"])
    assert told == ["window_closed"]


async def test_before_go_live_nothing_reaches_a_customer(tenant):
    conv, res = await committed_reply(tenant)
    a = FakeChannelAdapter()

    async def not_live(_):
        return DispatchPolicy(live=False)
    await Dispatcher({"instagram": a}, policy_for=not_live).dispatch_conversation(tenant, conv["id"])
    assert a.sent == []
    assert (await rows(tenant, "select cancel_reason from outbox where id=%s", res.outbox_ids[0]))[0]["cancel_reason"] == "kill_switch:not_live"
    assert await rows(tenant, "select 1 from messages where conversation_id=%s and turn_id is null and direction='in'", conv["id"])


# ------------------------------------------------------------------ reconciliation against the provider
def _ig(handler) -> tuple[InstagramGraphAdapter, AccountRef]:
    return (InstagramGraphAdapter("s", transport=httpx.MockTransport(handler)),
            AccountRef(uuid.uuid4(), uuid.uuid4(), "IG1", "PAGE1", "tok"))


async def test_same_words_sent_before_the_attempt_do_not_confirm_it():
    attempt = datetime.now(timezone.utc)

    def handler(request):
        old = (attempt - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        new = attempt.strftime("%Y-%m-%dT%H:%M:%S+0000")
        at = old if "first" in str(request.url) else new
        return httpx.Response(200, json={"data": [{"messages": {"data": [
            {"id": "m1", "message": "Vou pedir para te responder", "from": {"id": "IG1"}, "created_time": at}]}}]})
    a, acc = _ig(handler)
    assert await a.find_sent_message(acc, "first", "Vou pedir para te responder", sent_after=attempt) is None
    assert await a.find_sent_message(acc, "second", "Vou pedir para te responder", sent_after=attempt) == "m1"


async def test_private_reply_cannot_be_confirmed_by_conversation():
    def handler(request):
        raise AssertionError("no call: there is no user id to look for")
    a, acc = _ig(handler)
    assert await a.find_sent_message(acc, "", "olá") is None


@pytest.mark.parametrize("response, expected", [
    (httpx.Response(502, text="<html>bad gateway</html>"), AmbiguousSendError),
    (httpx.Response(404, text="<html>not json</html>"), SendError),
    (httpx.Response(200, json={}), AmbiguousSendError),
])
async def test_answers_that_are_not_what_the_api_promises(response, expected):
    from agentplatform.core.channels.ports import OutboundMessage
    a, acc = _ig(lambda request: response)
    with pytest.raises(expected):
        await a.send(acc, OutboundMessage(kind="dm_text", recipient_external_id="u", text="oi"))


async def test_a_write_cut_in_the_middle_is_ambiguous():
    from agentplatform.core.channels.ports import OutboundMessage

    def handler(request):
        raise httpx.WriteError("connection reset")
    a, acc = _ig(handler)
    with pytest.raises(AmbiguousSendError):
        await a.send(acc, OutboundMessage(kind="dm_text", recipient_external_id="u", text="oi"))


@pytest.mark.parametrize("code, sub, scope", [
    (200, 2534041, "account"), (10, 2534077, "account"), (100, 2534013, "account"), (551, 1545041, "recipient"),
    (100, 2534025, "object"), (100, 2534037, "thread_owner"), (10, 2534022, "window"), (1, 99, None),
])
async def test_refusals_say_what_they_are_about(code, sub, scope):
    from agentplatform.core.channels.ports import OutboundMessage
    a, acc = _ig(lambda request: httpx.Response(400, json={"error": {"code": code, "error_subcode": sub}}))
    with pytest.raises(SendError) as e:
        await a.send(acc, OutboundMessage(kind="dm_text", recipient_external_id="u", text="oi"))
    assert e.value.scope == scope and f"subcode={sub}" in str(e.value)
