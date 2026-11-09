import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentplatform.core.channels.events import CanonicalEvent
from agentplatform.core.channels.fake.adapter import FakeChannelAdapter
from agentplatform.core.channels.ports import AccountRef, MediaInfo
from agentplatform.core.comments.automation import process_comment
from agentplatform.core.comments.catalog import poll_account, sync_account
from agentplatform.core.config.registry import load_yaml, resolve_with_package
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.lgpd.service import erase_comment_author, erase_subject, find_comment_author, retention_sweep
from agentplatform.core.orchestration.conversations import pending_inbound, transition_ownership
from agentplatform.core.orchestration.debounce import debounce_state
from agentplatform.core.orchestration.human_detector import handle_echo
from agentplatform.core.outbox.dispatcher import Dispatcher
from agentplatform.core.packages.contract import load_package
from agentplatform.jobs import tasks

pytestmark = pytest.mark.db
PKG = load_package("consortium")
CFG = resolve_with_package(load_yaml(Path(__file__).resolve().parents[2] / "config/tenants/dev-consortium.yaml"), PKG.manifest.compliance)
OFFER = PKG.manifest.comment_templates[0]
REPEAT = ["Já te chamei no direct!", "Te respondo por lá, no direct!"]
# The time windows, as before one reply per person became the default
WINDOW = CFG.model_copy(update={"comments": CFG.comments.model_copy(update={"once_per_person": False})})
NOW = lambda: datetime.now(timezone.utc)     # noqa: E731


def ev(text, author="IGC1", media="media1", cid=None, parent=None, ts=None, **kw):
    cid = cid or f"c-{uuid.uuid4().hex[:8]}"
    return CanonicalEvent(event_type="CommentReceived", channel="instagram", account_external_id="acc",
                          contact_external_id=author, idempotency_key=f"cmt:{cid}", provider_ts=ts or NOW(),
                          text=text, comment_id=cid, media_id=media, parent_comment_id=parent, **kw)


async def account(tenant):
    async with tenant_tx(tenant) as conn:
        return await (await conn.execute("select id, external_account_id from channel_accounts where tenant_id=%s", (tenant,))).fetchone()


async def activate(tenant, *, active=True, mode="both", trigger="keyword", since=None, **over):
    """The account default, as the console saves it."""
    ca = await account(tenant)
    async with tenant_tx(tenant) as conn:
        await conn.execute(
            """insert into comment_settings (channel_account_id, tenant_id, default_active, default_mode, trigger_kind, keywords,
                                             public_variants, private_variants, repeat_variants, hide_spam, active_since)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (ca["id"], tenant, active, mode, trigger, over.get("keywords", OFFER.keywords), over.get("public", OFFER.public_variants),
             json.dumps(over.get("private", OFFER.private_variants)), over.get("repeat", REPEAT),
             over.get("hide_spam", False), since or NOW() - timedelta(days=30)))


async def post(tenant, media="media1", mode="both", **over):
    """A per-post choice, as the console saves it."""
    ca = await account(tenant)
    async with tenant_tx(tenant) as conn:
        m = await (await conn.execute(
            "insert into channel_media (tenant_id, channel_account_id, external_media_id, caption_masked) values (%s,%s,%s,%s) "
            "on conflict (tenant_id, external_media_id) do update set caption_masked = excluded.caption_masked returning id",
            (tenant, ca["id"], media, over.get("caption")))).fetchone()
        await conn.execute(
            """insert into media_automations (media_id, tenant_id, mode, keywords, public_variants, private_variants, active_since,
                                              backfill_since, context_note_masked, context_slots, valid_to)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (m["id"], tenant, mode, over.get("keywords", []), over.get("public", []), json.dumps(over.get("private", {})),
             over.get("since", NOW() - timedelta(days=30)), over.get("backfill"), over.get("note"), json.dumps(over.get("slots", {})),
             over.get("valid_to")))
    return m["id"]


async def run(tenant, e, cfg=CFG, **kw):
    ca = await account(tenant)
    async with tenant_tx(tenant) as conn:
        return await process_comment(conn, cfg, PKG.manifest, tenant_id=tenant, channel_account_id=ca["id"],
                                     account_external_id=ca["external_account_id"], ev=e, now=NOW(), **kw)


async def action(tenant, e):
    async with tenant_tx(tenant) as conn:
        return await (await conn.execute("select * from comment_actions where comment_id=%s", (e.comment_id,))).fetchone()


# ------------------------------------------------------------------ which posts answer
async def test_nothing_is_sent_until_someone_activates_and_the_comment_is_still_listed(tenant):
    e = ev("quero saber o valor")
    d = await run(tenant, e)
    assert d.decision == "inactive" and not d.public_outbox_id and not d.private_outbox_id and d.conversation_id is None
    row = await action(tenant, e)
    assert row["text_masked"] == "quero saber o valor" and row["decision"] == "inactive"


async def test_account_default_active_answers_every_post(tenant):
    await activate(tenant)
    d = await run(tenant, ev("quero saber mais, como funciona?"))
    assert d.decision == "acted" and d.public_outbox_id and d.private_outbox_id and d.rule_id == "account"


async def test_post_choice_overrides_the_default_in_both_directions(tenant):
    await activate(tenant)
    await post(tenant, "family", mode="off")
    assert (await run(tenant, ev("quero ir nesse churrasco", media="family"))).decision == "inactive"


async def test_only_the_chosen_action_goes_out(tenant):
    await activate(tenant, active=False)
    await post(tenant, "dm-only", mode="private")
    await post(tenant, "public-only", mode="public")
    a = await run(tenant, ev("quero", author="A1", media="dm-only"))
    assert a.private_outbox_id and not a.public_outbox_id and a.rule_id == "post"
    b = await run(tenant, ev("quero", author="A2", media="public-only"))
    assert b.public_outbox_id and not b.private_outbox_id


async def test_post_texts_and_words_replace_the_default(tenant):
    await activate(tenant)
    await post(tenant, "car", keywords=["acelera"], private={"A": "Oi, sou o assistente virtual do {seller}. Vi seu comentário: é para trocar de carro?"})
    assert (await run(tenant, ev("quero", author="B1", media="car"))).decision == "no_rule"
    d = await run(tenant, ev("acelera", author="B2", media="car"))
    async with tenant_tx(tenant) as conn:
        o = await (await conn.execute("select payload from outbox where id=%s", (d.private_outbox_id,))).fetchone()
    assert o["payload"]["text"].endswith("é para trocar de carro?") and "{seller}" not in o["payload"]["text"]
    assert CFG.conversation.persona.seller_display_name in o["payload"]["text"]


async def test_post_outside_its_dates_is_inactive(tenant):
    await activate(tenant)
    await post(tenant, "old-offer", valid_to=NOW() - timedelta(days=1))
    assert (await run(tenant, ev("quero", media="old-offer"))).decision == "inactive"


async def test_register_only_creates_the_lead_for_the_seller_and_sends_nothing(tenant):
    await activate(tenant, active=False)
    await post(tenant, "proof", mode="register")
    e = ev("parabéns, quero também", media="proof")
    d = await run(tenant, e)
    assert d.decision == "registered" and d.conversation_id and not d.public_outbox_id and not d.private_outbox_id
    assert (await action(tenant, e))["attention"] is True


# ------------------------------------------------------------------ old comments
async def test_comments_older_than_the_activation_are_never_answered(tenant):
    await activate(tenant, since=NOW())
    e = ev("quero", ts=NOW() - timedelta(hours=2))
    assert (await run(tenant, e)).decision == "before_activation"


async def test_backfill_answers_old_comments_only_when_the_seller_asks(tenant):
    await activate(tenant, active=False)
    await post(tenant, "late", since=NOW(), backfill=NOW() - timedelta(days=7))
    assert (await run(tenant, ev("quero", author="L1", media="late", ts=NOW() - timedelta(days=2)))).decision == "acted"
    assert (await run(tenant, ev("quero", author="L2", media="late", ts=NOW() - timedelta(days=8)))).decision == "before_activation"


async def test_backfill_job_reprocesses_only_comments_not_acted_on(tenant):
    from agentplatform.core.orchestration.ingress import persist_events
    ca = await account(tenant)
    await activate(tenant, active=False)
    async with tenant_tx(tenant) as conn:
        await conn.execute("update channel_accounts set external_account_id=%s where id=%s", (f"acc-{tenant.hex[:8]}", ca["id"]))
    e = ev("quero", author="L3", media="later", ts=NOW() - timedelta(days=1)).model_copy(update={"account_external_id": f"acc-{tenant.hex[:8]}"})
    created = await persist_events([e])
    assert (await run(tenant, e)).decision == "inactive"
    await post(tenant, "later", since=NOW(), backfill=NOW() - timedelta(days=7))
    nxt = await tasks.comment_backfill(str(tenant), "later")
    assert [(n, k["event_id"]) for n, k, _ in nxt] == [("comment", created[0][1])]
    assert await action(tenant, e) is None          # the next pass records the new decision
    assert (await run(tenant, e)).decision == "acted"


# ------------------------------------------------------------------ real customer behavior
async def test_duplicate_comment_single_action(tenant):
    await activate(tenant)
    e = ev("quero", cid=f"dup-{uuid.uuid4().hex[:6]}")
    d1, d2 = await run(tenant, e), await run(tenant, e)
    assert d1.decision == "acted" and d2.decision == "duplicate"


async def test_one_private_reply_per_person_and_media_even_in_parallel(tenant):
    await activate(tenant)
    author = f"IGP-{uuid.uuid4().hex[:6]}"
    media = f"m-{uuid.uuid4().hex[:6]}"
    ds = await asyncio.gather(*[run(tenant, ev("quero info", author=author, media=media)) for _ in range(4)])
    assert sum(1 for d in ds if d.private_outbox_id) == 1


async def test_one_reply_and_one_direct_per_person_however_many_comments(tenant):
    await activate(tenant, trigger="any")
    first = await run(tenant, ev("que legal", author="ONCE", media="post-a"))
    assert first.decision == "acted" and first.public_outbox_id and first.private_outbox_id
    for media, text in (("post-a", "quero saber mais"), ("post-b", "quanto fica?"), ("post-c", "top")):
        e = ev(text, author="ONCE", media=media)
        d = await run(tenant, e)
        assert d.decision == "already_answered" and not d.public_outbox_id and not d.private_outbox_id, media
        assert (await action(tenant, e))["attention"] is ("?" in text)           # still listed; a question stands out
    async with tenant_tx(tenant) as conn:
        n = await (await conn.execute("select count(*) n from outbox where conversation_id=%s", (first.conversation_id,))).fetchone()
    assert n["n"] == 2


async def test_one_public_reply_per_person_on_posts_without_a_direct(tenant):
    await activate(tenant, active=False)
    await post(tenant, "pub-a", mode="public")
    await post(tenant, "pub-b", mode="public")
    assert (await run(tenant, ev("quero", author="PUB", media="pub-a"))).public_outbox_id
    assert (await run(tenant, ev("quero", author="PUB", media="pub-b"))).decision == "already_answered"


async def test_someone_already_talked_to_gets_no_reply_to_a_comment(tenant):
    await activate(tenant, active=False)
    await post(tenant, "listed", mode="register")
    await post(tenant, "offer")
    first = await run(tenant, ev("quero", author="TALK", media="listed"))
    async with tenant_tx(tenant) as conn:
        await conn.execute("insert into messages (tenant_id, conversation_id, direction, external_mid, kind, provider_ts) "
                           "values (%s,%s,'out_human','m-talk','text',now())", (tenant, first.conversation_id))
    assert (await run(tenant, ev("quero", author="TALK", media="offer"))).decision == "already_answered"


async def test_parallel_comments_of_one_person_get_one_reply_and_one_direct(tenant):
    await activate(tenant)
    author = f"IGO-{uuid.uuid4().hex[:6]}"
    ds = await asyncio.gather(*[run(tenant, ev("quero info", author=author, media=f"m{i}")) for i in range(4)])
    assert sum(1 for d in ds if d.private_outbox_id) == 1 and sum(1 for d in ds if d.public_outbox_id) == 1


async def test_with_the_windows_comments_on_several_posts_get_one_opener_and_the_reply_does_not_promise_another(tenant):
    await activate(tenant)
    first = await run(tenant, ev("quero", author="SAME", media="post-a"), WINDOW)
    second = await run(tenant, ev("quero", author="SAME", media="post-b"), WINDOW)
    assert first.private_outbox_id and not second.private_outbox_id and second.decision == "acted"
    async with tenant_tx(tenant) as conn:
        o = await (await conn.execute("select payload from outbox where id=%s", (second.public_outbox_id,))).fetchone()
    assert o["payload"]["text"] in REPEAT


async def test_with_the_windows_no_repeat_text_means_no_public_reply(tenant):
    await activate(tenant, repeat=[])
    await run(tenant, ev("quero", author="SAME2", media="post-a"), WINDOW)
    assert (await run(tenant, ev("quero", author="SAME2", media="post-b"), WINDOW)).decision == "already_in_contact"


def test_post_templates_have_no_text_for_a_second_reply():
    assert CFG.comments.once_per_person and all(not t.repeat_variants for t in PKG.manifest.comment_templates)


async def test_customer_with_the_seller_gets_no_automatic_reply_and_the_seller_sees_it(tenant):
    await activate(tenant)
    first = await run(tenant, ev("quero", author="HUM", media="post-a"))
    async with tenant_tx(tenant) as conn:
        await transition_ownership(conn, tenant, first.conversation_id, "HUMAN", cause="test")
    e = ev("quero", author="HUM", media="post-b")
    d = await run(tenant, e)
    assert d.decision == "with_human" and not d.public_outbox_id and (await action(tenant, e))["attention"] is True


async def test_existing_customer_gets_no_sales_opener(tenant):
    await activate(tenant)
    first = await run(tenant, ev("oi quero", author="WON", media="post-a"))
    async with tenant_tx(tenant) as conn:
        await conn.execute("update leads set stage=%s where contact_id=(select contact_id from conversations where id=%s)",
                           (PKG.manifest.won_stage, first.conversation_id))
    assert (await run(tenant, ev("quero", author="WON", media="post-b"))).decision == "existing_customer"


async def test_who_asked_not_to_be_contacted_is_left_alone(tenant):
    await activate(tenant)
    first = await run(tenant, ev("quero", author="OUT", media="post-a"))
    async with tenant_tx(tenant) as conn:
        await conn.execute("update leads set opted_out_at=now() where contact_id=(select contact_id from conversations where id=%s)",
                           (first.conversation_id,))
    assert (await run(tenant, ev("quero", author="OUT", media="post-b"))).decision == "opted_out"


async def test_closed_conversation_reopens_for_a_new_comment(tenant):
    await activate(tenant)
    first = await run(tenant, ev("quero", author="CLO", media="post-a"), WINDOW)
    async with tenant_tx(tenant) as conn:
        await transition_ownership(conn, tenant, first.conversation_id, "CLOSED", cause="test")
        c = await (await conn.execute("select send_epoch from conversations where id=%s", (first.conversation_id,))).fetchone()
    d = await run(tenant, ev("quero", author="CLO", media="post-b"), WINDOW)
    assert d.decision == "acted"
    async with tenant_tx(tenant) as conn:
        now_c = await (await conn.execute("select ownership, send_epoch from conversations where id=%s", (first.conversation_id,))).fetchone()
        o = await (await conn.execute("select send_epoch from outbox where id=%s", (d.public_outbox_id,))).fetchone()
    assert now_c["ownership"] == "BOT" and now_c["send_epoch"] > c["send_epoch"] and o["send_epoch"] == now_c["send_epoch"]


@pytest.mark.parametrize("text,decision", [
    ("sigam meu perfil https://x.y", "spam"),
    ("isso é golpe, vou no procon", "human_queue"),
    ("que foto linda", "no_rule"),
])
async def test_filters(tenant, text, decision):
    await activate(tenant)
    assert (await run(tenant, ev(text))).decision == decision


async def test_complaint_reaches_the_seller_even_on_an_inactive_post(tenant):
    e = ev("isso é golpe, vou no procon")
    d = await run(tenant, e)
    assert d.decision == "human_queue" and d.conversation_id and (await action(tenant, e))["attention"] is True
    async with tenant_tx(tenant) as conn:
        h = await (await conn.execute("select reason, summary from handoffs where conversation_id=%s", (d.conversation_id,))).fetchone()
        c = await (await conn.execute("select ownership from conversations where id=%s", (d.conversation_id,))).fetchone()
    assert h["reason"] == "complaint_or_abuse" and "golpe" in h["summary"]["last_message"] and c["ownership"] == "AWAITING_HUMAN"


async def test_question_without_the_words_is_flagged_for_the_seller(tenant):
    await activate(tenant)
    e = ev("dá pra usar FGTS?")
    assert (await run(tenant, e)).decision == "no_rule"
    assert (await action(tenant, e))["attention"] is True
    plain = ev("que foto linda")
    await run(tenant, plain)
    assert (await action(tenant, plain))["attention"] is False


async def test_any_comment_skips_emoji_and_friend_tags(tenant):
    await activate(tenant, trigger="any")
    assert (await run(tenant, ev("🔥🔥", author="T1"))).decision == "no_rule"
    assert (await run(tenant, ev("@fulano", author="T2"))).decision == "no_rule"
    assert (await run(tenant, ev("achei interessante", author="T3"))).decision == "acted"


@pytest.mark.parametrize("intent,decision", [(0.9, "acted"), (0.2, "no_rule"), (None, "acted")])
async def test_intent_trigger_falls_back_to_the_words_without_a_model_answer(tenant, intent, decision):
    await activate(tenant, trigger="intent")
    assert (await run(tenant, ev("quero", author=f"I{intent}"), intent=intent)).decision == decision


async def test_own_account_ignored(tenant):
    await activate(tenant)
    acc = (await account(tenant))["external_account_id"]
    assert (await run(tenant, ev("quero", author=acc))).decision == "ignored_self"


async def test_paused_comments_stay_listed_for_later(tenant):
    await activate(tenant)
    e = ev("quero")
    assert (await run(tenant, e, paused=True)).decision == "paused"
    assert (await action(tenant, e))["attention"] is True


async def test_public_reply_is_only_for_top_level_comments_and_not_on_lives(tenant):
    await activate(tenant)
    nested = await run(tenant, ev("quero", author="N1", parent="c-parent"))
    assert nested.private_outbox_id and not nested.public_outbox_id
    live = await run(tenant, ev("quero", author="N2", media="live1", media_product_type="LIVE"))
    assert live.private_outbox_id and not live.public_outbox_id


# ------------------------------------------------------------------ conversation side effects
async def test_comment_is_history_not_a_turn_input_and_opens_no_message_window(tenant):
    await activate(tenant)
    d = await run(tenant, ev("quero saber"))
    async with tenant_tx(tenant) as conn:
        assert await pending_inbound(conn, d.conversation_id) == []
        st = await debounce_state(conn, d.conversation_id, NOW(), window_s=1, max_wait_s=1, media_max_wait_s=1)
        c = await (await conn.execute("select window_expires_at, last_inbound_at from conversations where id=%s", (d.conversation_id,))).fetchone()
        m = await (await conn.execute("select kind, text_masked from messages where conversation_id=%s", (d.conversation_id,))).fetchone()
    assert st.pending == 0 and c["window_expires_at"] is None and c["last_inbound_at"] is None
    assert m["kind"] == "comment" and m["text_masked"] == "quero saber"


async def test_post_subject_prefills_the_profile_with_low_confidence(tenant):
    await activate(tenant, active=False)
    await post(tenant, "car2", slots={"goal": "vehicle"})
    d = await run(tenant, ev("quero", media="car2"))
    async with tenant_tx(tenant) as conn:
        lead = await (await conn.execute(
            "select profile from leads where contact_id=(select contact_id from conversations where id=%s)", (d.conversation_id,))).fetchone()
    assert lead["profile"]["goal"]["value"] == "vehicle" and lead["profile"]["goal"]["confidence"] < CFG.funnel.slot_confidence_threshold


async def test_username_from_a_comment_reaches_a_contact_that_started_in_a_direct(tenant):
    await activate(tenant)
    first = await run(tenant, ev("quero", author="U1", media="post-a"))
    await run(tenant, ev("quero", author="U1", media="post-b", author_username="fulano"))
    async with tenant_tx(tenant) as conn:
        u = await (await conn.execute(
            "select username from contact_identities where contact_id=(select contact_id from conversations where id=%s)", (first.conversation_id,))).fetchone()
    assert u["username"] == "fulano"


async def test_dispatch_records_public_reply_apart_from_the_direct(tenant):
    await activate(tenant)
    d = await run(tenant, ev("quero"))
    a = FakeChannelAdapter()
    out = await Dispatcher({"instagram": a}).dispatch_conversation(tenant, d.conversation_id)
    assert len(out.sent) == 2
    async with tenant_tx(tenant) as conn:
        kinds = {r["kind"] for r in await (await conn.execute("select kind from messages where conversation_id=%s and direction='out_bot'",
                                                              (d.conversation_id,))).fetchall()}
    assert kinds == {"comment_reply", "private_reply"}


async def test_rate_limited_send_says_when_to_come_back(tenant):
    """The private reply bucket starts empty after an idle hour: the retry comes when due, not at the minute sweep."""
    class Empty:
        async def try_acquire(self, key, bucket, cost=1.0):
            return 6.0
    await activate(tenant)
    d = await run(tenant, ev("quero"))
    out = await Dispatcher({"instagram": FakeChannelAdapter()}, limiter=Empty()).dispatch_conversation(tenant, d.conversation_id)
    assert out.retry_in == 6.0 and out.rescheduled and not out.sent


async def test_spam_can_be_hidden_through_the_outbox(tenant):
    await activate(tenant, hide_spam=True)
    cid = f"spam-{uuid.uuid4().hex[:8]}"
    d = await run(tenant, ev("sigam meu perfil https://x.y", cid=cid))
    assert d.decision == "spam" and d.hide_outbox_id
    a = FakeChannelAdapter()
    out = await Dispatcher({"instagram": a}).dispatch_conversation(tenant, d.conversation_id)
    assert len(out.sent) == 1 and a.sent[0].msg.kind == "hide_comment" and a.sent[0].msg.comment_id == cid
    async with tenant_tx(tenant) as conn:
        assert not await (await conn.execute("select 1 from messages where conversation_id=%s and direction='out_bot'",
                                             (d.conversation_id,))).fetchone()


async def test_hiding_spam_survives_the_conversation_going_to_the_seller(tenant):
    """Same person: spam and a complaint in the same poll. Moderation is not a message, so the handoff does not cancel it."""
    await activate(tenant, hide_spam=True)
    d = await run(tenant, ev("sigam meu perfil", author="SP1", cid=f"spam-{uuid.uuid4().hex[:8]}"))
    assert (await run(tenant, ev("quero fazer uma reclamação", author="SP1"))).decision == "human_queue"
    a = FakeChannelAdapter()
    out = await Dispatcher({"instagram": a}).dispatch_conversation(tenant, d.conversation_id)
    assert len(out.sent) == 1 and a.sent[0].msg.kind == "hide_comment"


async def test_spam_is_only_hidden_when_the_seller_chose_it(tenant):
    await activate(tenant)
    d = await run(tenant, ev("sigam meu perfil https://x.y"))
    assert d.decision == "spam" and not d.hide_outbox_id and d.conversation_id is None


async def test_seller_text_is_kept_masked_for_the_panel_and_the_assistant(tenant):
    await activate(tenant)
    d = await run(tenant, ev("quero"))
    assert await handle_echo(tenant, d.conversation_id, "mid-seller", "meu fone é 11 99988-7766, combinado amanhã",
                             reconcile_window_s=120, rechecked=True) == "human"
    async with tenant_tx(tenant) as conn:
        m = await (await conn.execute("select text_masked, text_enc from messages where external_mid='mid-seller'")).fetchone()
    assert "combinado amanhã" in m["text_masked"] and "99988" not in m["text_masked"] and m["text_enc"]


# ------------------------------------------------------------------ ads
async def test_ad_comment_follows_the_choice_of_the_post_it_promotes(tenant):
    await activate(tenant, active=False)
    await post(tenant, "organic", mode="private")
    d = await run(tenant, ev("quero", media="ad-media", ad_id="A1", ad_title="Oferta", original_media_id="organic"))
    assert d.private_outbox_id and d.media_created
    async with tenant_tx(tenant) as conn:
        m = await (await conn.execute("select media_kind, ad_title from channel_media where external_media_id='ad-media'")).fetchone()
    assert m["media_kind"] == "ad" and m["ad_title"] == "Oferta"


# ------------------------------------------------------------------ catalog and polling
def _catalog(tenant, ca, **kw):
    return AccountRef(tenant, ca["id"], ca["external_account_id"], "page", "tok"), FakeChannelAdapter(**kw)


async def test_sync_lists_posts_with_masked_captions_and_keeps_the_seller_choice(tenant):
    ca = await account(tenant)
    acc, a = _catalog(tenant, ca, media=[
        MediaInfo("p1", "post", "IMAGE", "Fale comigo: 11 99988-7766", "https://i/p1", "https://c/p1", NOW(), 3),
        MediaInfo("s1", "story", "IMAGE", None, None, None, NOW(), None)])
    assert await sync_account(tenant, a, acc, CFG.comments) == 2
    async with tenant_tx(tenant) as conn:
        r = await (await conn.execute("select caption_masked, media_kind, comments_count from channel_media where external_media_id='p1'")).fetchone()
        s = await (await conn.execute("select media_kind from channel_media where external_media_id='s1'")).fetchone()
    assert "99988" not in r["caption_masked"] and r["media_kind"] == "post" and r["comments_count"] == 3 and s["media_kind"] == "story"


async def test_polled_comment_and_webhook_are_one_event_whichever_comes_first(tenant):
    from agentplatform.core.orchestration.ingress import persist_events
    ca = await account(tenant)
    own = ca["external_account_id"]
    e = ev("quero", cid=f"both-{uuid.uuid4().hex[:6]}", media="p2").model_copy(update={"account_external_id": own})
    self_reply = ev("Te chamei", author=own, media="p2").model_copy(update={"account_external_id": own})
    acc, a = _catalog(tenant, ca, media=[MediaInfo("p2", "post", posted_at=NOW(), comments_count=2)], comments={"p2": [e, self_reply]})
    await sync_account(tenant, a, acc, CFG.comments)
    polled = await poll_account(tenant, a, acc, CFG.comments)
    assert len(polled) == 1                                       # our own reply is not an event
    assert await persist_events([e]) == []                        # the late webhook is a duplicate
    assert await poll_account(tenant, a, acc, CFG.comments) == []  # same count: the post is not read again


async def test_old_posts_are_not_polled(tenant):
    ca = await account(tenant)
    acc, a = _catalog(tenant, ca, media=[MediaInfo("p3", "post", posted_at=NOW() - timedelta(days=30), comments_count=5)],
                      comments={"p3": [ev("quero", media="p3")]})
    await sync_account(tenant, a, acc, CFG.comments)
    assert await poll_account(tenant, a, acc, CFG.comments) == []


# ------------------------------------------------------------------ LGPD
async def test_erasure_covers_comments_with_and_without_a_contact(tenant):
    await activate(tenant)
    only = ev("só comentei", author="ONLY", author_username="soh.comentei")
    await run(tenant, only)
    async with tenant_tx(tenant) as conn:
        found = await find_comment_author(conn, tenant, "@soh.comentei")
        assert found["author_external_id"] == "ONLY" and found["comments"] == 1
        assert await erase_comment_author(conn, tenant, "ONLY") == 1
    row = await action(tenant, only)
    assert row["text_masked"] == "[eliminado]" and row["author_username"] is None and row["author_external_id"].startswith("erased:")

    e = ev("quero", author="SUBJ", author_username="sujeito")
    d = await run(tenant, e)
    async with tenant_tx(tenant) as conn:
        c = await (await conn.execute("select contact_id from conversations where id=%s", (d.conversation_id,))).fetchone()
        await erase_subject(conn, tenant, c["contact_id"], None)
        assert not await (await conn.execute("select 1 from private_reply_claims where author_external_id='SUBJ'")).fetchone()
    row = await action(tenant, e)
    assert row["text_masked"] == "[eliminado]" and row["author_username"] is None


async def test_retention_clears_old_comment_texts(tenant):
    await activate(tenant)
    e = ev("quero", author="OLD")
    await run(tenant, e)
    async with tenant_tx(tenant) as conn:
        await conn.execute("update comment_actions set created_at = now() - interval '400 days' where comment_id=%s", (e.comment_id,))
        await retention_sweep(conn, CFG, tenant)
    assert (await action(tenant, e))["text_masked"] is None


async def test_story_reply_tells_the_agent_where_it_came_from(tenant):
    from agentplatform.core.comments.origin import load_origin
    from agentplatform.core.orchestration.ingress import persist_events, route_event
    ca = await account(tenant)
    await_ev = CanonicalEvent(event_type="StoryReply", channel="instagram", account_external_id=ca["external_account_id"],
                              contact_external_id="ST1", idempotency_key=f"msg:{uuid.uuid4().hex}", provider_ts=NOW(), mid=uuid.uuid4().hex,
                              text="quanto custa?", story_id="story-1")
    (_, event_id), = await persist_events([await_ev])
    routed = await route_event(tenant, event_id, FakeChannelAdapter(), "novo")
    assert routed.action == "debounce" and routed.media_to_hydrate == "story-1" and routed.username_missing
    async with tenant_tx(tenant) as conn:
        m = await (await conn.execute("select id, media_kind from channel_media where external_media_id='story-1'")).fetchone()
        await conn.execute("insert into media_automations (media_id, tenant_id, context_note_masked) values (%s,%s,'Promoção da semana')", (m["id"], tenant))
        lead = await (await conn.execute("select l.id from leads l join conversations c on c.contact_id=l.contact_id where c.id=%s",
                                         (routed.conversation_id,))).fetchone()
        origin = await load_origin(conn, routed.conversation_id, lead["id"], CFG.comments)
    assert m["media_kind"] == "story" and origin[0]["kind"] == "story_reply" and origin[0]["note"] == "Promoção da semana"
