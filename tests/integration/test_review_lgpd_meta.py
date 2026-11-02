"""What the provider really sends, and what stays stored about a customer (independent review). Payload shapes follow Meta's documentation
as of 2026-10; everything marked by the review as depending on a real account is listed in docs/runbooks/go-live.md."""
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentplatform.api.webhooks import Spool, build_router, replay_spool
from agentplatform.core.channels.events import Attachment, CanonicalEvent
from agentplatform.core.channels.instagram.adapter import InstagramGraphAdapter
from agentplatform.core.channels.ports import AccountRef, PartialParse
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.lgpd.service import erase_comment_author, erase_subject, retention_sweep
from agentplatform.core.orchestration import dm_scope
from agentplatform.core.orchestration.dm_recovery import recover_dms
from agentplatform.core.orchestration.ingress import load_event, persist_events, route_event
from agentplatform.core.security.keys import decrypt_field
from tests.integration.test_turn import CFG, inbound

pytestmark = pytest.mark.db
SECRET = "s3cret"
ADAPTER = InstagramGraphAdapter(SECRET)


def body(*messaging, standby=(), changes=(), account="IGACC"):
    return json.dumps({"object": "instagram", "entry": [
        {"id": account, "time": 1760040000, "messaging": list(messaging), "standby": list(standby), "changes": list(changes)}]}).encode()


def msg(mid="m1", sender="U1", recipient="IGACC", **message):
    return {"sender": {"id": sender}, "recipient": {"id": recipient}, "timestamp": 1760040000000, "message": {"mid": mid, **message}}


def sign(b: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), b, hashlib.sha256).hexdigest()


async def rows(tenant, sql, *args):
    async with tenant_tx(tenant) as conn:
        return await (await conn.execute(sql, args)).fetchall()


async def account_of(tenant):
    return (await rows(tenant, "select id, external_account_id from channel_accounts where tenant_id=%s", tenant))[0]


def event(acc, text="oi", contact="U1", etype="MessageReceived", mid=None, **kw):
    mid = mid or f"m-{uuid.uuid4().hex[:8]}"
    key = {"MessageEcho": f"echo:{mid}", "MessageDeleted": f"del:{mid}", "MessageEdited": f"edit:{mid}:1"}.get(etype, f"msg:{mid}")
    return CanonicalEvent(event_type=etype, channel="instagram", account_external_id=acc, contact_external_id=contact, idempotency_key=key,
                          mid=mid, text=text, provider_ts=datetime.now(timezone.utc), **kw)


# ------------------------------------------------------------------ parser
def test_ad_click_is_read_where_the_provider_puts_it():
    ev = ADAPTER.parse(body(msg(text="tenho interesse", referral={"ad_id": 123, "source": "ADS", "type": "OPEN_THREAD",
                                                                   "ads_context_data": {"ad_title": "Anúncio"}})))[0]
    assert ev.referral["ad_id"] == 123 and ev.referral["ads_context_data"]["ad_title"] == "Anúncio"


def test_link_open_without_text_counts_as_someone_arriving():
    ev = ADAPTER.parse(body({"sender": {"id": "U1"}, "recipient": {"id": "IGACC"}, "timestamp": 1760040000000,
                             "referral": {"ref": "post-7", "source": "SHORTLINK"}}))[0]
    assert ev.event_type == "Referral" and not dm_scope.is_shared_only(ev)


def test_empty_or_shared_things_still_do_not_start_a_turn():
    shared = CanonicalEvent(event_type="MessageReceived", channel="instagram", account_external_id="a", idempotency_key="k",
                            provider_ts=datetime.now(timezone.utc), attachments=[Attachment(type="share")])
    empty = shared.model_copy(update={"attachments": []})
    assert dm_scope.is_shared_only(shared) and dm_scope.is_shared_only(empty)


def test_comment_id_of_facebook_login_and_of_instagram_login():
    fb = {"field": "comments", "value": {"comment_id": "C1", "text": "quero", "from": {"id": "U1"}, "media": {"id": 55, "ad_id": 9}}}
    ig = {"field": "comments", "value": {"id": "C2", "text": "quero", "from": {"id": "U1"}, "media": {"id": "55"}}}
    none = {"field": "comments", "value": {"text": "sem id"}}
    events = ADAPTER.parse(body(changes=[fb, ig, none]))
    assert [e.comment_id for e in events] == ["C1", "C2"] and events[0].media_id == "55" and events[0].ad_id == "9"


def test_numeric_ids_do_not_break_the_delivery():
    ev = ADAPTER.parse(body(msg(reply_to={"story": {"id": 987, "url": "x"}}, text="legal")))[0]
    assert ev.story_id == "987"


def test_message_without_id_is_not_a_conversation_message():
    assert ADAPTER.parse(body({"sender": {"id": "U1"}, "recipient": {"id": "IGACC"}, "timestamp": 1, "message": {"text": "admin text"}})) == []


def test_standby_is_read():
    ev = ADAPTER.parse(body(standby=[msg(text="parar")]))[0]
    assert ev.standby and ev.text == "parar" and ev.event_type == "MessageReceived"


def test_deleted_echo_is_a_deletion_not_a_person_taking_over():
    ev = ADAPTER.parse(body(msg(sender="IGACC", recipient="U1", is_echo=True, is_deleted=True)))[0]
    assert ev.event_type == "MessageDeleted" and ev.contact_external_id == "U1"


def test_echo_carries_the_app_that_sent_it():
    assert ADAPTER.parse(body(msg(sender="IGACC", recipient="U1", is_echo=True, text="oi", app_id=777)))[0].sender_app_id == "777"


def test_one_unreadable_item_keeps_the_rest():
    bad = msg(mid="m2", text="x")
    bad["timestamp"] = "not-a-timestamp"
    with pytest.raises(PartialParse) as e:
        ADAPTER.parse(body(msg(mid="m1", text="oi"), bad))
    assert [x.mid for x in e.value.events] == ["m1"] and e.value.failures == 1


# ------------------------------------------------------------------ webhook
@pytest.fixture
def app_and_spool(tmp_path):
    spool = Spool(str(tmp_path))
    app = FastAPI()
    app.include_router(build_router({"instagram": ADAPTER}, spool))
    return app, spool


async def test_unreadable_delivery_is_set_aside_with_a_200(tenant, app_and_spool):
    app, spool = app_and_spool
    acc = (await account_of(tenant))["external_account_id"]
    bad = msg(mid="m-bad", text="x")
    bad["timestamp"] = "not-a-timestamp"
    good_mid = f"m-{tenant.hex[:8]}"
    b = body(msg(mid=good_mid, text="oi"), bad, account=acc)
    with TestClient(app) as c:
        r = c.post("/webhooks/meta", content=b, headers={"X-Hub-Signature-256": sign(b)})
        junk = b"not json at all"
        r2 = c.post("/webhooks/meta", content=junk, headers={"X-Hub-Signature-256": sign(junk)})
    assert r.status_code == 200 and r.json()["accepted"] == 1          # the good message went on
    assert r2.status_code == 200 and r2.json()["status"] == "quarantined"
    assert spool.quarantine_size() > 0 and spool.size() == 0            # not in the replay queue: it would fail forever
    assert spool.purge_quarantine(max_age_s=10_000) is False
    assert spool.purge_quarantine(max_age_s=-1) is True and spool.quarantine_size() == 0


async def test_unreadable_spooled_delivery_does_not_hold_back_the_others(tenant, tmp_path):
    acc = (await account_of(tenant))["external_account_id"]
    spool = Spool(str(tmp_path))
    spool.append("instagram", b"not json at all")
    spool.append("instagram", body(msg(mid=f"m-{tenant.hex[:8]}", text="oi"), account=acc))
    assert await replay_spool(spool, {"instagram": ADAPTER}) == 1
    assert spool.size() == 0 and spool.quarantine_size() > 0


async def test_verification_works_for_every_channel_path(app_and_spool, monkeypatch):
    from pydantic import SecretStr

    from agentplatform.settings import get_settings
    monkeypatch.setattr(get_settings(), "meta_verify_token", SecretStr("tok"))
    app, _ = app_and_spool
    with TestClient(app) as c:
        for path in ("/webhooks/meta", "/webhooks/whatsapp", "/webhooks/whatsapp-notifier"):
            r = c.get(path, params={"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "42"})
            assert r.status_code == 200 and r.text == "42"
        assert c.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "42"}).status_code == 403


# ------------------------------------------------------------------ the event log
async def test_customer_words_are_not_left_in_clear_in_the_event_log(tenant):
    acc = await account_of(tenant)
    (_, eid), = await persist_events([event(acc["external_account_id"], "meu cpf é 529.982.247-25 e tenho diabetes")])
    raw = (await rows(tenant, "select payload_canonical::text as p, payload_raw from inbound_events where id=%s", eid))[0]
    assert "529.982" not in raw["p"] and "diabetes" not in raw["p"] and raw["payload_raw"] is None
    async with tenant_tx(tenant) as conn:
        ev = await load_event(conn, tenant, eid)
    assert ev.text.startswith("meu cpf é 529.982.247-25")           # the pipeline still reads it
    await route_event(tenant, eid, ADAPTER, "novo")
    after = (await rows(tenant, "select payload_canonical from inbound_events where id=%s", eid))[0]["payload_canonical"]
    assert not {"text", "text_enc", "text_masked", "attachments"} & set(after) and after["idempotency_key"]
    m = (await rows(tenant, "select text_masked, text_enc from messages where inbound_event_id=%s", eid))[0]
    assert "[CPF]" in m["text_masked"] and "[SAUDE]" in m["text_masked"]
    async with tenant_tx(tenant) as conn:
        assert "529.982" in await decrypt_field(conn, tenant, m["text_enc"], "messages.text")


async def test_events_written_before_the_change_still_load(tenant):
    acc = await account_of(tenant)
    (_, eid), = await persist_events([event(acc["external_account_id"], None)])
    async with tenant_tx(tenant) as conn:
        await conn.execute("update inbound_events set payload_canonical = payload_canonical || '{\"text\": \"antigo\"}'::jsonb where id=%s", (eid,))
        assert (await load_event(conn, tenant, eid)).text == "antigo"


async def _route_all(tenant, created):
    out = []
    for _, eid in created:
        out.append(await route_event(tenant, eid, ADAPTER, "novo"))
    return out


async def test_message_the_customer_deleted_is_deleted_here(tenant):
    acc = (await account_of(tenant))["external_account_id"]
    mid = f"m-{uuid.uuid4().hex[:8]}"
    created = await persist_events([event(acc, "me arrependi de escrever isso", mid=mid)])
    (routed,) = await _route_all(tenant, created)
    async with tenant_tx(tenant) as conn:
        await conn.execute("update conversations set summary='resumo com o texto' where id=%s", (routed.conversation_id,))
        await conn.execute("update messages set turn_id = gen_random_uuid() where conversation_id=%s", (routed.conversation_id,))
    await _route_all(tenant, await persist_events([event(acc, None, etype="MessageDeleted", mid=mid)]))
    m = (await rows(tenant, "select kind, text_masked, text_enc, media from messages where external_mid=%s", mid))[0]
    assert m == {"kind": "deleted", "text_masked": "[mensagem apagada]", "text_enc": None, "media": None}
    assert (await rows(tenant, "select summary from conversations where id=%s", routed.conversation_id))[0]["summary"] is None
    ev = (await rows(tenant, "select payload_canonical from inbound_events where idempotency_key=%s", f"msg:{mid}"))[0]
    assert ev["payload_canonical"] == {}


async def test_message_deleted_before_the_turn_is_never_answered(tenant):
    from tests.integration.test_turn import runner
    acc = (await account_of(tenant))["external_account_id"]
    mid = f"m-{uuid.uuid4().hex[:8]}"
    (routed,) = await _route_all(tenant, await persist_events([event(acc, "esquece", mid=mid)]))
    await _route_all(tenant, await persist_events([event(acc, None, etype="MessageDeleted", mid=mid)]))
    r = runner({})
    assert (await r.run(tenant, routed.conversation_id)).status == "no_input"
    assert r.d.gateway.calls == []


async def test_edited_message_is_answered_with_what_the_customer_meant(tenant):
    acc = (await account_of(tenant))["external_account_id"]
    mid = f"m-{uuid.uuid4().hex[:8]}"
    await _route_all(tenant, await persist_events([event(acc, "quero um carro", mid=mid)]))
    await _route_all(tenant, await persist_events([event(acc, "quero uma casa", etype="MessageEdited", mid=mid)]))
    m = (await rows(tenant, "select text_masked, text_enc from messages where external_mid=%s", mid))[0]
    assert m["text_masked"] == "quero uma casa"
    async with tenant_tx(tenant) as conn:
        assert await decrypt_field(conn, tenant, m["text_enc"], "messages.text") == "quero uma casa"


@pytest.mark.parametrize("old, new, again", [
    ("uns 300 mil", "uns 400 mil", True), ("quero um barco", "quero um apartamento", True), ("quero um carrro", "quero um carro", False),
    ("como funciona o lance", "como funciona o lance?", False), ("oi", "parar", True),
])
async def test_a_correction_to_an_answered_message_gets_a_new_answer_and_a_typo_does_not(tenant, old, new, again):
    acc = (await account_of(tenant))["external_account_id"]
    mid = f"m-{uuid.uuid4().hex[:8]}"
    (routed,) = await _route_all(tenant, await persist_events([event(acc, old, mid=mid)]))
    async with tenant_tx(tenant) as conn:
        await conn.execute("update messages set turn_id = gen_random_uuid() where external_mid=%s", (mid,))
    (edited,) = await _route_all(tenant, await persist_events([event(acc, new, etype="MessageEdited", mid=mid)]))
    assert edited.action == ("debounce" if again else "none")
    m = (await rows(tenant, "select text_masked, turn_id from messages where external_mid=%s", mid))[0]
    assert m["text_masked"] == new and (m["turn_id"] is None) is again
    assert routed.conversation_id == edited.conversation_id


async def test_an_edit_of_a_message_we_never_stored_changes_nothing(tenant):
    acc = (await account_of(tenant))["external_account_id"]
    await _route_all(tenant, await persist_events([event(acc, "oi")]))
    (edited,) = await _route_all(tenant, await persist_events([event(acc, "novo", etype="MessageEdited", mid="never-seen")]))
    assert edited.action == "none"


async def test_message_in_a_thread_another_app_owns_goes_to_the_seller(tenant):
    acc = (await account_of(tenant))["external_account_id"]
    (routed,) = await _route_all(tenant, await persist_events([event(acc, "oi", standby=True)]))
    assert (await rows(tenant, "select ownership from conversations where id=%s", routed.conversation_id))[0]["ownership"] == "HUMAN"
    assert (await rows(tenant, "select cause from ownership_events where conversation_id=%s", routed.conversation_id))[0]["cause"] == "standby"


async def test_retention_empties_old_event_payloads_and_checkpoints(tenant):
    from agentplatform.core.db.pool import platform_tx
    conv = await inbound(tenant, ["oi"])
    acc = (await account_of(tenant))["external_account_id"]
    (_, eid), = await persist_events([event(acc, "antigo")])
    await route_event(tenant, eid, ADAPTER, "novo")
    (_, keep), = await persist_events([event(acc, "pendente")])
    turn_id = uuid.uuid4()
    thread = f"{tenant}:{conv['id']}:{turn_id}"
    async with tenant_tx(tenant) as conn:
        await conn.execute("update inbound_events set received_at = now() - interval '60 days' where tenant_id=%s", (tenant,))
        await conn.execute(
            """insert into turns (id, tenant_id, conversation_id, run_epoch, input_message_ids, status, lease_id, lease_expires_at, started_at, trace)
               values (%s,%s,%s,0,'{}','committed',gen_random_uuid(),now(), now() - interval '60 days','{"a":1}')""", (turn_id, tenant, conv["id"]))
    async with platform_tx() as conn:
        await conn.execute("insert into checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) values (%s,'','1','{}','{}')", (thread,))
    async with tenant_tx(tenant) as conn:
        out = await retention_sweep(conn, CFG, tenant)
    assert out["checkpoints"] == 1
    payloads = {r["id"]: r["payload_canonical"] for r in await rows(tenant, "select id, payload_canonical from inbound_events where tenant_id=%s", tenant)}
    assert payloads[eid] == {} and payloads[keep] != {}              # a message not processed yet is never wiped
    assert (await rows(tenant, "select trace from turns where id=%s", turn_id))[0]["trace"] is None
    async with platform_tx() as conn:
        assert not await (await conn.execute("select 1 from checkpoints where thread_id=%s", (thread,))).fetchone()


async def test_erasure_reaches_comment_events_and_what_turns_left_behind(tenant):
    from agentplatform.core.db.pool import platform_tx
    acc = await account_of(tenant)
    conv = await inbound(tenant, ["oi"], "U-ERASE")
    comment = CanonicalEvent(event_type="CommentReceived", channel="instagram", account_external_id=acc["external_account_id"],
                             contact_external_id="U-ERASE", idempotency_key=f"cmt:{uuid.uuid4()}", comment_id="c", text="tenho interesse",
                             author_username="alguem", provider_ts=datetime.now(timezone.utc))
    (_, eid), = await persist_events([comment])
    turn_id = uuid.uuid4()
    thread = f"{tenant}:{conv['id']}:{turn_id}"
    async with tenant_tx(tenant) as conn:
        await conn.execute(
            """insert into turns (id, tenant_id, conversation_id, run_epoch, input_message_ids, status, lease_id, lease_expires_at, trace)
               values (%s,%s,%s,0,'{}','committed',gen_random_uuid(),now(),'{"a":1}')""", (turn_id, tenant, conv["id"]))
        await conn.execute("insert into curation_queue (tenant_id, question_masked, turn_id) values (%s,'pergunta',%s)", (tenant, turn_id))
        await conn.execute("insert into guard_results (tenant_id, turn_id, guard, rule_id, verdict, detail) values (%s,%s,'g','r','block','{\"detail\":\"x\"}')", (tenant, turn_id))
        lead = await (await conn.execute("select id from leads where contact_id=%s", (conv["contact_id"],))).fetchone()
        await conn.execute(
            "insert into lead_stage_history (tenant_id, lead_id, from_stage, to_stage, proposed_by, evidence, applied) values (%s,%s,'novo','engajado','classifier','[\"frase\"]',true)",
            (tenant, lead["id"]))
    async with platform_tx() as conn:
        await conn.execute("insert into checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) values (%s,'','1','{}','{}')", (thread,))
    async with tenant_tx(tenant) as conn:
        await erase_subject(conn, tenant, conv["contact_id"], None)
    assert (await rows(tenant, "select payload_canonical from inbound_events where id=%s", eid))[0]["payload_canonical"] == {}
    assert (await rows(tenant, "select trace from turns where id=%s", turn_id))[0]["trace"] is None
    assert (await rows(tenant, "select question_masked from curation_queue where turn_id=%s", turn_id))[0]["question_masked"] == "[eliminado]"
    assert (await rows(tenant, "select detail from guard_results where turn_id=%s", turn_id))[0]["detail"] is None
    assert (await rows(tenant, "select evidence from lead_stage_history where lead_id=%s", lead["id"]))[0]["evidence"] is None
    async with platform_tx() as conn:
        assert not await (await conn.execute("select 1 from checkpoints where thread_id=%s", (thread,))).fetchone()


async def test_erasing_someone_who_only_commented(tenant):
    acc = await account_of(tenant)
    comment = CanonicalEvent(event_type="CommentReceived", channel="instagram", account_external_id=acc["external_account_id"],
                             contact_external_id="U-C", idempotency_key=f"cmt:{uuid.uuid4()}", comment_id="c", text="oi",
                             provider_ts=datetime.now(timezone.utc))
    (_, eid), = await persist_events([comment])
    async with tenant_tx(tenant) as conn:
        await erase_comment_author(conn, tenant, "U-C")
    assert (await rows(tenant, "select payload_canonical from inbound_events where id=%s", eid))[0]["payload_canonical"] == {}


# ------------------------------------------------------------------ recovery of directs
class Threads:
    """A provider with the threads of an account; records whose contents were read."""
    channel = "instagram"

    def __init__(self, threads, messages):
        self.threads, self.messages, self.read = threads, messages, []

    async def list_recent_threads(self, account, limit):
        return self.threads

    async def list_thread_messages(self, account, thread_id, limit):
        self.read.append(thread_id)
        return self.messages[thread_id]


class Rt:
    def __init__(self, config):
        self.config = config

    async def tenant(self, tid):
        return self


async def _recover(tenant, adapter, scope="origin_and_new", synced=True):
    acc = await account_of(tenant)
    async with tenant_tx(tenant) as conn:
        await conn.execute(
            """insert into dm_settings (channel_account_id, tenant_id, scope, contacts_synced_at) values (%s,%s,%s,%s)
               on conflict (channel_account_id) do update set scope=excluded.scope, contacts_synced_at=excluded.contacts_synced_at""",
            (acc["id"], tenant, scope, datetime.now(timezone.utc) if synced else None))
    ref = AccountRef(tenant, acc["id"], acc["external_account_id"], "PAGE", "tok")
    return await recover_dms(tenant, adapter, ref, Rt(CFG)), ref


def ago(minutes):
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


async def test_a_direct_the_webhook_never_delivered_comes_back_once(tenant):
    acc = (await account_of(tenant))["external_account_id"]
    a = Threads([{"id": "T1", "contact": "U-LOST", "updated_at": ago(10)}], {"T1": [
        {"mid": f"m2-{tenant.hex[:6]}", "text": "alguém aí?", "from": "U-LOST", "at": ago(10)},
        {"mid": f"m1-{tenant.hex[:6]}", "text": "quero saber", "from": "U-LOST", "at": ago(11)},
        {"mid": f"m0-{tenant.hex[:6]}", "text": "oi, já te respondi", "from": acc, "at": ago(12)},
        {"mid": f"mx-{tenant.hex[:6]}", "text": "pergunta velha", "from": "U-LOST", "at": ago(13)}]})
    created, _ = await _recover(tenant, a)
    assert len(created) == 2           # only what comes after the last thing said on our side
    routed = await _route_all(tenant, created)
    assert all(r.action == "debounce" for r in routed)
    texts = await rows(tenant, "select text_masked from messages where conversation_id=%s order by provider_ts", routed[0].conversation_id)
    assert [t["text_masked"] for t in texts] == ["quero saber", "alguém aí?"]
    again, _ = await _recover(tenant, a)
    assert again == []                 # same key as the webhook would use


async def test_contents_of_the_owners_contacts_are_never_read(tenant):
    acc = await account_of(tenant)
    async with tenant_tx(tenant) as conn:
        await dm_scope.set_flag(conn, tenant, acc["id"], "U-FRIEND", "known", "snapshot")
        await dm_scope.set_flag(conn, tenant, acc["id"], "U-BLOCK", "blocked", "console", None)
    a = Threads([{"id": "T1", "contact": "U-FRIEND", "updated_at": ago(10)}, {"id": "T2", "contact": "U-BLOCK", "updated_at": ago(10)}], {})
    created, _ = await _recover(tenant, a)
    assert created == [] and a.read == []


async def test_new_people_are_not_read_when_only_origins_are_served_or_the_list_is_not_ready(tenant):
    a = Threads([{"id": "T1", "contact": "U-NEW", "updated_at": ago(10)}], {})
    assert (await _recover(tenant, a, scope="origin_only"))[0] == [] and a.read == []
    assert (await _recover(tenant, a, scope="origin_and_new", synced=False))[0] == [] and a.read == []


async def test_threads_outside_the_window_or_just_moved_are_left_to_the_webhook(tenant):
    a = Threads([{"id": "T1", "contact": "U-A", "updated_at": ago(1)}, {"id": "T2", "contact": "U-B", "updated_at": ago(500)}], {})
    assert (await _recover(tenant, a))[0] == [] and a.read == []


async def test_nothing_new_in_a_known_conversation_means_no_read(tenant):
    conv = await inbound(tenant, ["oi"], "U-KNOWN")
    async with tenant_tx(tenant) as conn:
        await conn.execute("update conversations set last_inbound_at = now() where id=%s", (conv["id"],))
    a = Threads([{"id": "T1", "contact": "U-KNOWN", "updated_at": ago(10)}], {})
    assert (await _recover(tenant, a))[0] == [] and a.read == []


async def test_relist_restarts_the_contact_list_and_keeps_the_flags(tenant):
    acc = await account_of(tenant)
    async with tenant_tx(tenant) as conn:
        await dm_scope.set_flag(conn, tenant, acc["id"], "U-KEEP", "known", "snapshot")
        await conn.execute("insert into dm_settings (channel_account_id, tenant_id, contacts_synced_at, contacts_count) values (%s,%s,now(),3)", (acc["id"], tenant))
        await dm_scope.restart_snapshot(conn, tenant, acc["id"])
        s = await dm_scope.load_scope(conn, acc["id"])
        assert s["contacts_synced_at"] is None and s["contacts_count"] == 0
        assert await dm_scope.flag_of(conn, acc["id"], "U-KEEP") == "known"
