"""Direct scope (ADR-019): an account that mixes customers with the owner's personal life. Fails closed, and keeps nothing of people out of scope."""
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentplatform.core.agent.turn import TurnDeps, TurnRunner
from agentplatform.core.channels.events import Attachment, CanonicalEvent
from agentplatform.core.channels.fake.adapter import FakeChannelAdapter
from agentplatform.core.channels.ports import AccountRef, RateLimitedError
from agentplatform.core.comments.automation import process_comment
from agentplatform.core.config.registry import load_yaml, registry, resolve_with_package
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.lgpd.service import erase_comment_author
from agentplatform.core.models.fake import ScriptedGateway
from agentplatform.core.orchestration.conversations import pending_inbound
from agentplatform.core.orchestration.dm_scope import flag_of, set_flag, snapshot_contacts
from agentplatform.core.orchestration.ingress import persist_events, route_event
from agentplatform.core.outbox.dispatcher import Dispatcher
from agentplatform.core.packages.contract import load_package
from tests.integration.test_console_admin import PW, _client, _user

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]
PKG = load_package("consortium")
CFG = resolve_with_package(load_yaml(ROOT / "config/tenants/dev-consortium.yaml"), PKG.manifest.compliance)
OFFER = PKG.manifest.comment_templates[0]
NOW = lambda: datetime.now(timezone.utc)     # noqa: E731
GATED = FakeChannelAdapter(mixed_inbox=True)


async def account(tenant):
    async with tenant_tx(tenant) as conn:
        return await (await conn.execute("select id, external_account_id from channel_accounts where tenant_id=%s", (tenant,))).fetchone()


async def scope(tenant, value="origin_only", synced=True):
    ca = await account(tenant)
    async with tenant_tx(tenant) as conn:
        await conn.execute("insert into dm_settings (channel_account_id, tenant_id, scope, contacts_synced_at) values (%s,%s,%s,%s)",
                           (ca["id"], tenant, value, NOW() if synced else None))


async def flag(tenant, who, status="known", source="snapshot"):
    ca = await account(tenant)
    async with tenant_tx(tenant) as conn:
        await set_flag(conn, tenant, ca["id"], who, status, source)


async def inbound(tenant, who=None, *, event_type="MessageReceived", text="oi, quero saber", **kw):
    ca = await account(tenant)
    who = who or f"U-{uuid.uuid4().hex[:8]}"
    ev = CanonicalEvent(event_type=event_type, channel="instagram", account_external_id=ca["external_account_id"], contact_external_id=who,
                        idempotency_key=f"msg:{uuid.uuid4().hex}", provider_ts=NOW(), mid=uuid.uuid4().hex, text=text, **kw)
    (_, event_id), = await persist_events([ev])
    routed = await route_event(tenant, event_id, GATED, "novo", compliance=CFG.compliance)
    return routed, event_id, who


async def stored(tenant, event_id, who):
    async with tenant_tx(tenant) as conn:
        e = await (await conn.execute("select status, payload_raw, payload_canonical from inbound_events where id=%s", (event_id,))).fetchone()
        c = await (await conn.execute("select 1 from contact_identities where external_user_id=%s", (who,))).fetchone()
    return e, c


async def assert_forgotten(tenant, routed, event_id, who):
    e, contact = await stored(tenant, event_id, who)
    assert routed.action == "none" and e["status"] == "ignored" and e["payload_raw"] is None and e["payload_canonical"] == {} and contact is None


# ------------------------------------------------------------------ first message in the direct
async def test_nothing_is_served_or_kept_until_the_contact_list_is_complete(tenant):
    await scope(tenant, "origin_and_new", synced=False)
    await assert_forgotten(tenant, *await inbound(tenant))
    routed, e, who = await inbound(tenant, event_type="StoryReply", story_id="s1")
    await assert_forgotten(tenant, routed, e, who)


async def test_account_without_settings_is_closed_by_default(tenant):
    await assert_forgotten(tenant, *await inbound(tenant))


async def test_option_1_serves_who_answered_the_business_not_strangers(tenant):
    await scope(tenant, "origin_only")
    await assert_forgotten(tenant, *await inbound(tenant))
    assert (await inbound(tenant, event_type="StoryReply", story_id="s1"))[0].action == "debounce"
    assert (await inbound(tenant, event_type="Referral", referral={"ad_id": "A1"}))[0].action == "debounce"
    assert (await inbound(tenant, event_type="Postback", postback_payload="q1", text="Como funciona?"))[0].action == "debounce"


async def test_option_2_also_serves_new_contacts_but_never_who_already_talks_to_the_account(tenant):
    await scope(tenant, "origin_and_new")
    assert (await inbound(tenant))[0].action == "debounce"
    await flag(tenant, "FRIEND")
    await assert_forgotten(tenant, *await inbound(tenant, "FRIEND"))
    await assert_forgotten(tenant, *await inbound(tenant, "FRIEND", event_type="StoryReply", story_id="s1", text="top!!"))


async def test_blocked_is_never_served_even_with_all(tenant):
    await scope(tenant, "all")
    await flag(tenant, "NOPE", "blocked", "console")
    await assert_forgotten(tenant, *await inbound(tenant, "NOPE"))
    await flag(tenant, "OLD")
    assert (await inbound(tenant, "OLD"))[0].action == "debounce"


async def test_seller_can_allow_an_old_contact(tenant):
    await scope(tenant, "origin_only")
    await flag(tenant, "OLD2")
    await flag(tenant, "OLD2", "allowed", "console")
    assert (await inbound(tenant, "OLD2"))[0].action == "debounce"


async def test_automatic_source_never_undoes_a_seller_choice(tenant):
    await flag(tenant, "KEEP", "allowed", "console")
    await flag(tenant, "KEEP", "known", "snapshot")
    ca = await account(tenant)
    async with tenant_tx(tenant) as conn:
        assert await flag_of(conn, ca["id"], "KEEP") == "allowed"


async def test_only_a_hash_of_the_contact_is_kept(tenant):
    await flag(tenant, "SECRET-ID")
    async with tenant_tx(tenant) as conn:
        row = await (await conn.execute("select * from contact_flags where tenant_id=%s", (tenant,))).fetchone()
    assert b"SECRET-ID" not in bytes(row["identifier_hash"]) and "SECRET-ID" not in str(dict(row))


# ------------------------------------------------------------------ the owner, and things that are not messages
async def test_owner_writing_to_someone_makes_them_known_and_stores_nothing(tenant):
    await scope(tenant, "origin_and_new")
    routed, e, who = await inbound(tenant, "PAL", event_type="MessageEcho", text="e aí, vamos no churrasco?")
    await assert_forgotten(tenant, routed, e, who)
    await assert_forgotten(tenant, *await inbound(tenant, "PAL"))


@pytest.mark.parametrize("event_type", ["Reaction", "Read", "MessageEdited", "MessageDeleted"])
async def test_events_of_people_out_of_scope_create_nothing(tenant, event_type):
    await scope(tenant, "origin_and_new")
    await assert_forgotten(tenant, *await inbound(tenant, event_type=event_type, text=None))


async def test_shared_reel_and_story_mention_are_not_answered(tenant):
    await scope(tenant, "origin_and_new")
    await assert_forgotten(tenant, *await inbound(tenant, text=None, attachments=[Attachment(type="share", url="https://x/r")]))
    await assert_forgotten(tenant, *await inbound(tenant, event_type="StoryMention", text=None, attachments=[Attachment(type="story_mention")]))


async def test_shared_reel_inside_a_served_conversation_is_history_not_a_turn(tenant):
    await scope(tenant, "origin_and_new")
    first, _, who = await inbound(tenant)
    async with tenant_tx(tenant) as conn:
        await conn.execute("update messages set turn_id=gen_random_uuid() where conversation_id=%s", (first.conversation_id,))
    routed, _, _ = await inbound(tenant, who, text=None, attachments=[Attachment(type="share", url="https://x/r")])
    assert routed.action == "none" and routed.conversation_id == first.conversation_id
    async with tenant_tx(tenant) as conn:
        assert await pending_inbound(conn, first.conversation_id) == []
        kinds = [r["kind"] for r in await (await conn.execute("select kind from messages where conversation_id=%s order by provider_ts", (first.conversation_id,))).fetchall()]
    assert kinds == ["text", "shared"]


async def test_audio_from_someone_in_scope_still_gets_a_turn(tenant):
    await scope(tenant, "origin_and_new")
    routed, _, _ = await inbound(tenant, text=None, attachments=[Attachment(type="audio", url="https://x/a")])
    assert routed.action == "debounce" and routed.has_pending_media


async def test_existing_conversation_keeps_going_whatever_the_flag(tenant):
    await scope(tenant, "origin_and_new")
    _, _, who = await inbound(tenant)
    await flag(tenant, who)
    assert (await inbound(tenant, who))[0].action == "debounce"


async def test_after_never_serve_nothing_more_of_that_person_is_kept(tenant):
    await scope(tenant, "origin_and_new")
    first, _, who = await inbound(tenant)
    await flag(tenant, who, "blocked", "console")
    routed, e, _ = await inbound(tenant, who, text="mensagem pessoal")
    echo, e2, _ = await inbound(tenant, who, event_type="MessageEcho", text="resposta do dono")
    assert routed.action == "none" and echo.action == "none"
    async with tenant_tx(tenant) as conn:
        for ev_id in (e, e2):
            row = await (await conn.execute("select status, payload_canonical from inbound_events where id=%s", (ev_id,))).fetchone()
            assert row["status"] == "ignored" and row["payload_canonical"] == {}
        texts = [r["text_masked"] for r in await (await conn.execute("select text_masked from messages where conversation_id=%s", (first.conversation_id,))).fetchall()]
    assert "mensagem pessoal" not in texts and "resposta do dono" not in texts


async def test_blocked_person_asking_to_stop_is_still_recorded(tenant):
    await scope(tenant, "origin_and_new")
    first, _, who = await inbound(tenant)
    await flag(tenant, who, "blocked", "console")
    routed, _, _ = await inbound(tenant, who, text="parar")
    assert routed.action == "debounce" and routed.conversation_id == first.conversation_id


# ------------------------------------------------------------------ privacy gate
async def test_request_to_stop_from_someone_out_of_scope_is_recorded_without_a_reply(tenant):
    await scope(tenant, "origin_only")
    routed, _, who = await inbound(tenant, text="não quero mais receber mensagens")
    assert routed.action == "debounce"
    async with tenant_tx(tenant) as conn:
        c = await (await conn.execute("select ownership from conversations where id=%s", (routed.conversation_id,))).fetchone()
    assert c["ownership"] == "HUMAN"
    gw = ScriptedGateway({})
    res = await TurnRunner(TurnDeps(config=CFG, package=PKG, gateway=gw, knowledge=None)).run(tenant, routed.conversation_id)
    assert res.status == "committed" and res.outbox_ids == [] and gw.calls == []
    async with tenant_tx(tenant) as conn:
        lead = await (await conn.execute(
            "select opted_out_at from leads where contact_id=(select contact_id from conversations where id=%s)", (routed.conversation_id,))).fetchone()
    assert lead["opted_out_at"] is not None


# ------------------------------------------------------------------ comments
async def comment(tenant, who="C1", text="quero", media="m1", **kw):
    ca = await account(tenant)
    async with tenant_tx(tenant) as conn:
        await conn.execute(
            """insert into comment_settings (channel_account_id, tenant_id, default_active, keywords, public_variants, private_variants, repeat_variants, active_since)
               values (%s,%s,true,%s,%s,%s,%s,%s) on conflict (channel_account_id) do nothing""",
            (ca["id"], tenant, OFFER.keywords, OFFER.public_variants, json.dumps(OFFER.private_variants), OFFER.repeat_variants, NOW() - timedelta(days=1)))
        e = CanonicalEvent(event_type="CommentReceived", channel="instagram", account_external_id=ca["external_account_id"], contact_external_id=who,
                           idempotency_key=f"cmt:{uuid.uuid4().hex}", provider_ts=NOW(), text=text, comment_id=f"c-{uuid.uuid4().hex[:8]}", media_id=media)
        d = await process_comment(conn, CFG, PKG.manifest, tenant_id=tenant, channel_account_id=ca["id"],
                                  account_external_id=ca["external_account_id"], ev=e, now=NOW(), mixed_inbox=True, **kw)
        a = await (await conn.execute("select attention from comment_actions where comment_id=%s", (e.comment_id,))).fetchone()
    return d, a


async def test_comments_wait_for_the_contact_list(tenant):
    await scope(tenant, "origin_only", synced=False)
    d, a = await comment(tenant)
    assert d.decision == "contacts_pending" and d.conversation_id is None and a["attention"] is True


async def test_comment_from_someone_new_is_served_under_option_1(tenant):
    await scope(tenant, "origin_only")
    assert (await comment(tenant))[0].decision == "acted"


async def test_comment_from_an_old_contact_goes_to_the_seller_without_opening_a_conversation(tenant):
    await scope(tenant, "origin_only")
    await flag(tenant, "PAL2")
    d, a = await comment(tenant, "PAL2")
    assert d.decision == "known_contact" and d.conversation_id is None and not d.private_outbox_id and a["attention"] is True


async def test_comment_from_an_allowed_contact_is_served(tenant):
    await scope(tenant, "origin_only")
    await flag(tenant, "PAL3", "allowed", "console")
    assert (await comment(tenant, "PAL3"))[0].decision == "acted"


async def test_comment_from_a_blocked_contact_gets_nothing_not_even_with_a_conversation(tenant):
    await scope(tenant, "origin_only")
    first, _ = await comment(tenant, "PAL4")
    await flag(tenant, "PAL4", "blocked", "console")
    d, a = await comment(tenant, "PAL4", media="m2")
    assert first.decision == "acted" and d.decision == "blocked_contact" and a["attention"] is False


async def test_complaint_from_an_old_contact_still_reaches_the_seller(tenant):
    await scope(tenant, "origin_only")
    await flag(tenant, "PAL5")
    d, _ = await comment(tenant, "PAL5", text="isso é golpe")
    assert d.decision == "human_queue"


async def test_spam_record_never_lets_the_assistant_talk_to_the_spammer(tenant):
    ca = await account(tenant)
    await scope(tenant, "origin_and_new")
    async with tenant_tx(tenant) as conn:
        await conn.execute("insert into comment_settings (channel_account_id, tenant_id, hide_spam) values (%s,%s,true)", (ca["id"], tenant))
    d, _ = await comment(tenant, "SPAMMER", text="sigam meu perfil")
    assert d.hide_outbox_id
    a = FakeChannelAdapter()
    assert len((await Dispatcher({"instagram": a}).dispatch_conversation(tenant, d.conversation_id)).sent) == 1
    assert a.sent[0].msg.kind == "hide_comment"
    routed, _, _ = await inbound(tenant, "SPAMMER")
    res = await TurnRunner(TurnDeps(config=CFG, package=PKG, gateway=ScriptedGateway({}), knowledge=None)).run(tenant, routed.conversation_id)
    assert res.outbox_ids == []


# ------------------------------------------------------------------ the contact list
async def _ref(tenant):
    ca = await account(tenant)
    return AccountRef(tenant, ca["id"], ca["external_account_id"], "page", "tok")


async def test_snapshot_is_resumable_and_marks_complete_only_at_the_end(tenant):
    ref = await _ref(tenant)
    a = FakeChannelAdapter(contact_pages=[["A", "B"], RateLimitedError("80002"), ["C"]])
    with pytest.raises(RateLimitedError):
        await snapshot_contacts(tenant, a, ref, CFG.comments)
    async with tenant_tx(tenant) as conn:
        s = await (await conn.execute("select contacts_synced_at, contacts_cursor, contacts_count from dm_settings where tenant_id=%s", (tenant,))).fetchone()
        assert s["contacts_synced_at"] is None and s["contacts_cursor"] == "1" and s["contacts_count"] == 2
        assert await flag_of(conn, ref.channel_account_id, "A") == "known"
    a.contact_pages[1] = []
    assert await snapshot_contacts(tenant, a, ref, CFG.comments) is True
    async with tenant_tx(tenant) as conn:
        s = await (await conn.execute("select contacts_synced_at, contacts_cursor, contacts_count from dm_settings where tenant_id=%s", (tenant,))).fetchone()
        assert await flag_of(conn, ref.channel_account_id, "C") == "known"
    assert s["contacts_synced_at"] and s["contacts_cursor"] is None and s["contacts_count"] == 3
    a.contact_pages = [RateLimitedError("not called again")]
    assert await snapshot_contacts(tenant, a, ref, CFG.comments) is True


async def test_snapshot_keeps_the_chosen_scope_and_the_seller_flags(tenant):
    await scope(tenant, "origin_and_new", synced=False)
    await flag(tenant, "A", "allowed", "console")
    ref = await _ref(tenant)
    await snapshot_contacts(tenant, FakeChannelAdapter(contact_pages=[["A", "B"]]), ref, CFG.comments)
    async with tenant_tx(tenant) as conn:
        assert (await (await conn.execute("select scope from dm_settings where tenant_id=%s", (tenant,))).fetchone())["scope"] == "origin_and_new"
        assert await flag_of(conn, ref.channel_account_id, "A") == "allowed"


async def test_erasure_removes_the_flag(tenant):
    await flag(tenant, "GONE")
    ca = await account(tenant)
    async with tenant_tx(tenant) as conn:
        await erase_comment_author(conn, tenant, "GONE")
        assert await flag_of(conn, ca["id"], "GONE") is None


# ------------------------------------------------------------------ console
async def _publish(tenant):
    async with tenant_tx(tenant) as conn:
        await registry.publish(conn, tenant, load_yaml(ROOT / "config/tenants/dev-consortium.yaml"), package_compliance=PKG.manifest.compliance, activate=True)


async def _login(tenant, c):
    await _publish(tenant)
    email, _ = await _user(tenant, role="seller", name="V")
    await c.post("/console/login", data={"email": email, "password": PW})


async def _csrf(c, path="/console/posts"):
    return re.search(r'name="csrf" value="([^"]+)"', (await c.get(path)).text).group(1)


async def test_panel_warns_while_the_list_is_incomplete_and_saves_the_choice(tenant):
    async with _client() as c:
        await _login(tenant, c)
        r = await c.get("/console/posts")
        assert "ainda não atende ninguém novo" in r.text and "Também contatos novos" in r.text
        assert (await c.post("/console/posts/direct", data={"csrf": await _csrf(c), "scope": "origin_and_new"})).status_code == 303
        assert (await c.post("/console/posts/direct", data={"csrf": await _csrf(c), "scope": "bad"})).status_code == 400
    async with tenant_tx(tenant) as conn:
        s = await (await conn.execute("select scope, contacts_synced_at, updated_by from dm_settings where tenant_id=%s", (tenant,))).fetchone()
        a = await (await conn.execute("select 1 from audit_log where action='dm.scope'")).fetchone()
    assert s["scope"] == "origin_and_new" and s["contacts_synced_at"] is None and s["updated_by"] and a


async def test_never_serve_takes_the_assistant_out_and_can_be_undone(tenant):
    await scope(tenant, "origin_and_new")
    routed, _, who = await inbound(tenant)
    cid = routed.conversation_id
    ca = await account(tenant)
    async with _client() as c:
        await _login(tenant, c)
        page = await c.get(f"/console/conversations/{cid}")
        assert "Nunca atender esta pessoa" in page.text
        csrf = await _csrf(c, f"/console/conversations/{cid}")
        assert (await c.post(f"/console/conversations/{cid}/contact", data={"csrf": csrf, "status": "blocked"})).status_code == 303
        async with tenant_tx(tenant) as conn:
            assert await flag_of(conn, ca["id"], who) == "blocked"
            assert (await (await conn.execute("select ownership from conversations where id=%s", (cid,))).fetchone())["ownership"] == "HUMAN"
        page = await c.get(f"/console/conversations/{cid}")
        assert "Marcado para não atender" in page.text and "Retomar assistente" not in page.text
        await c.post(f"/console/conversations/{cid}/contact", data={"csrf": csrf, "status": "allowed"})
        async with tenant_tx(tenant) as conn:
            assert await flag_of(conn, ca["id"], who) == "allowed"


async def test_allow_from_the_comments_screen_answers_that_comment(tenant, monkeypatch):
    await scope(tenant, "origin_only")
    await flag(tenant, "OLD3")
    ca = await account(tenant)
    async with tenant_tx(tenant) as conn:
        await conn.execute("insert into channel_media (tenant_id, channel_account_id, external_media_id) values (%s,%s,'m1')", (tenant, ca["id"]))
    ev = CanonicalEvent(event_type="CommentReceived", channel="instagram", account_external_id=ca["external_account_id"], contact_external_id="OLD3",
                        idempotency_key="cmt:allow-" + tenant.hex[:8], provider_ts=NOW(), text="quero", comment_id="allow-" + tenant.hex[:8], media_id="m1")
    (_, event_id), = await persist_events([ev])
    async with tenant_tx(tenant) as conn:
        await conn.execute("""insert into comment_settings (channel_account_id, tenant_id, default_active, keywords, public_variants, private_variants, active_since)
                              values (%s,%s,true,%s,%s,%s,%s)""", (ca["id"], tenant, OFFER.keywords, OFFER.public_variants, json.dumps(OFFER.private_variants), NOW() - timedelta(days=1)))
        d = await process_comment(conn, CFG, PKG.manifest, tenant_id=tenant, channel_account_id=ca["id"], account_external_id=ca["external_account_id"], ev=ev, now=NOW(), mixed_inbox=True)
        rid = (await (await conn.execute("select id from comment_actions where comment_id=%s", (ev.comment_id,))).fetchone())["id"]
    assert d.decision == "known_contact"
    deferred = []

    async def fake_defer(nxt):
        deferred.extend(nxt)
    import agentplatform.jobs.app as jobs_app
    monkeypatch.setattr(jobs_app, "defer_jobs", fake_defer)
    async with _client() as c:
        await _login(tenant, c)
        page = await c.get("/console/comments?f=attention")
        assert "Atender esta pessoa" in page.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        assert (await c.post(f"/console/comments/{rid}/allow", data={"csrf": csrf})).status_code == 303
    assert deferred == [("comment", {"tenant_id": str(tenant), "event_id": event_id}, {})]
    async with tenant_tx(tenant) as conn:
        assert await flag_of(conn, ca["id"], "OLD3") == "allowed"
        d2 = await process_comment(conn, CFG, PKG.manifest, tenant_id=tenant, channel_account_id=ca["id"], account_external_id=ca["external_account_id"], ev=ev, now=NOW(), mixed_inbox=True)
    assert d2.decision == "acted" and d2.private_outbox_id


async def test_ingress_job_turns_the_gate_on_for_real_instagram(tenant):
    from agentplatform.core.channels.instagram.adapter import InstagramGraphAdapter
    assert InstagramGraphAdapter(None).capabilities().mixed_inbox is True
