"""Posts and comments screens (ADR-018): the account default starts inactive, the seller edits without a second approval, texts
are validated on save, and the agent receives where the conversation came from."""
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentplatform.core.agent.turn import TurnDeps, TurnRunner
from agentplatform.core.channels.events import CanonicalEvent
from agentplatform.core.comments.automation import process_comment
from agentplatform.core.comments.settings import validate
from agentplatform.core.config.registry import load_yaml, registry, resolve_with_package
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.models.fake import ScriptedGateway, analysis, draft
from agentplatform.core.orchestration.conversations import store_inbound_message
from agentplatform.core.orchestration.human_detector import handle_echo
from agentplatform.core.packages.contract import load_package
from agentplatform.jobs import tasks
from tests.integration.test_console_admin import PW, _client, _user

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]
PKG = load_package("consortium")
CFG = resolve_with_package(load_yaml(ROOT / "config/tenants/dev-consortium.yaml"), PKG.manifest.compliance)
OFFER = PKG.manifest.comment_templates[0]
NOW = lambda: datetime.now(timezone.utc)     # noqa: E731
NOON = lambda: datetime.now(timezone.utc).replace(hour=15, minute=0)     # noqa: E731


async def _publish(tenant):
    async with tenant_tx(tenant) as conn:
        await registry.publish(conn, tenant, load_yaml(ROOT / "config/tenants/dev-consortium.yaml"),
                               package_compliance=PKG.manifest.compliance, activate=True)


async def _login(c, email):
    r = await c.post("/console/login", data={"email": email, "password": PW})
    assert r.status_code in (200, 303)


async def _csrf(c, path="/console/posts"):
    r = await c.get(path)
    assert r.status_code == 200, r.text[:200]
    return re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)


async def _seller(tenant, c):
    await _publish(tenant)
    email, _ = await _user(tenant, role="seller", name="Vendedor")
    await _login(c, email)


def _default_form(csrf, **over):
    return {"csrf": csrf, "default_active": "1", "default_mode": "both", "trigger_kind": "keyword", "keywords": ", ".join(OFFER.keywords),
            "public_variants": "\n".join(OFFER.public_variants), "private_a": OFFER.private_variants["A"], "private_b": "",
            "repeat_variants": "\n".join(OFFER.repeat_variants), "save": "1", **over}


async def _media(tenant, ext="m1", caption="Carta para carro"):
    async with tenant_tx(tenant) as conn:
        ca = await (await conn.execute("select id from channel_accounts where tenant_id=%s", (tenant,))).fetchone()
        return (await (await conn.execute(
            "insert into channel_media (tenant_id, channel_account_id, external_media_id, caption_masked, posted_at, media_kind) "
            "values (%s,%s,%s,%s,now(),'post') returning id", (tenant, ca["id"], ext, caption))).fetchone())["id"], ca["id"]


async def test_default_starts_inactive_and_seller_turns_it_on_without_second_approval(tenant):
    async with _client() as c:
        await _seller(tenant, c)
        r = await c.get("/console/posts")
        assert r.status_code == 200 and "Inativo" in r.text and OFFER.public_variants[0] in r.text     # the package's texts, ready to review
        async with tenant_tx(tenant) as conn:
            assert not await (await conn.execute("select 1 from comment_settings")).fetchone()    # seeing the page changes nothing
        r = await c.post("/console/posts/settings", data=_default_form(await _csrf(c)))
        assert r.status_code == 303
    async with tenant_tx(tenant) as conn:
        s = await (await conn.execute("select default_active, active_since, updated_by from comment_settings")).fetchone()
        a = await (await conn.execute("select action from audit_log where action='comments.default'")).fetchone()
    assert s["default_active"] is True and s["active_since"] is not None and s["updated_by"] and a


@pytest.mark.parametrize("field,value,message", [
    ("public_variants", "Parcelas de R$ 300!\nTe chamei\nOlha o direct", "Não use valores"),
    ("public_variants", "Te chamei", "Cadastre mais respostas"),
    ("private_a", "Oi! Vi seu comentário, é para carro?", "diga que é o assistente virtual"),
    ("private_a", "Sou o assistente virtual, contemplação garantida", "promessa"),
    ("private_a", "Sou o assistente virtual. Qual o seu CPF?", "dados pessoais"),
    ("private_a", "Sou o assistente virtual. É para carro? E para quando?", "só uma pergunta"),
    ("private_a", "Sou o assistente virtual {nome}", "Só {seller}"),
    ("keywords", "", "palavras que acionam"),
])
async def test_texts_are_validated_on_save_and_nothing_is_stored(tenant, field, value, message):
    async with _client() as c:
        await _seller(tenant, c)
        r = await c.post("/console/posts/settings", data=_default_form(await _csrf(c), **{field: value}))
        assert r.status_code == 400 and message in r.text.replace("&#34;", '"').replace("&#39;", "'")
    async with tenant_tx(tenant) as conn:
        assert not await (await conn.execute("select 1 from comment_settings")).fetchone()


async def test_template_only_fills_the_form(tenant):
    async with _client() as c:
        await _seller(tenant, c)
        r = await c.post("/console/posts/settings", data={"csrf": await _csrf(c), "use_template": PKG.manifest.comment_templates[1].id})
        assert r.status_code == 200 and PKG.manifest.comment_templates[1].public_variants[0] in r.text
    async with tenant_tx(tenant) as conn:
        assert not await (await conn.execute("select 1 from comment_settings")).fetchone()


async def test_post_choice_saved_and_inactive_default_still_applies_elsewhere(tenant):
    mid, ca = await _media(tenant)
    other, _ = await _media(tenant, "m2", "Família no domingo")
    async with _client() as c:
        await _seller(tenant, c)
        csrf = await _csrf(c)
        r = await c.post(f"/console/posts/{mid}", data={"csrf": csrf, "mode": "private", "keywords": "acelera",
                                                        "private_a": "Sou o assistente virtual do {seller}. Vi seu comentário: é para trocar de carro?",
                                                        "context_note": "Chamada para trocar de carro, fone 11 99988-7766", "slot_goal": "vehicle",
                                                        "valid_to": "2099-06-30", "save": "1"})
        assert r.status_code == 303
        page = await c.get("/console/posts")
        assert "Só direct" in page.text and "Família no domingo" in page.text
        assert (await c.get(f"/console/posts/{mid}")).status_code == 200
    async with tenant_tx(tenant) as conn:
        a = await (await conn.execute("select * from media_automations where media_id=%s", (mid,))).fetchone()
        assert not await (await conn.execute("select 1 from media_automations where media_id=%s", (other,))).fetchone()
    assert a["mode"] == "private" and a["keywords"] == ["acelera"] and a["context_slots"] == {"goal": "vehicle"}
    assert "99988" not in a["context_note_masked"] and a["active_since"] and a["valid_to"].year == 2099


async def test_post_set_to_answer_needs_valid_texts_even_when_inheriting(tenant):
    mid, _ = await _media(tenant)
    async with _client() as c:
        await _seller(tenant, c)
        r = await c.post(f"/console/posts/{mid}", data={"csrf": await _csrf(c), "mode": "both", "save": "1"})
        assert r.status_code == 400 and "palavras que acionam" in r.text
    async with tenant_tx(tenant) as conn:
        assert not await (await conn.execute("select 1 from media_automations")).fetchone()


async def test_activation_moment_only_moves_when_turned_on(tenant):
    mid, _ = await _media(tenant)
    data = {"mode": "public", "keywords": "quero", "public_variants": "um\ndois\ntres", "save": "1"}
    async with _client() as c:
        await _seller(tenant, c)
        await c.post(f"/console/posts/{mid}", data={"csrf": await _csrf(c), **data})
        async with tenant_tx(tenant) as conn:
            first = (await (await conn.execute("select active_since from media_automations")).fetchone())["active_since"]
        await c.post(f"/console/posts/{mid}", data={"csrf": await _csrf(c), **data, "keywords": "quero, valor"})
        async with tenant_tx(tenant) as conn:
            same = (await (await conn.execute("select active_since from media_automations")).fetchone())["active_since"]
        await c.post(f"/console/posts/{mid}", data={"csrf": await _csrf(c), "mode": "off", "save": "1"})
        await c.post(f"/console/posts/{mid}", data={"csrf": await _csrf(c), **data})
        async with tenant_tx(tenant) as conn:
            later = (await (await conn.execute("select active_since from media_automations")).fetchone())["active_since"]
    assert first == same and later > first


async def test_comments_screen_filters_and_resolves(tenant):
    await _publish(tenant)
    async with tenant_tx(tenant) as conn:
        for cid, text, decision, attention in (("c1", "dá pra usar FGTS?", "no_rule", True), ("c2", "que foto linda", "inactive", False)):
            await conn.execute(
                """insert into comment_actions (tenant_id, comment_id, media_id, author_external_id, decision, text_masked, attention, author_username, comment_ts)
                   values (%s,%s,'m1','A',%s,%s,%s,'fulano',now())""", (tenant, cid, decision, text, attention))
        rid = (await (await conn.execute("select id from comment_actions where comment_id='c1'")).fetchone())["id"]
    email, _ = await _user(tenant, role="seller", name="V")
    async with _client() as c:
        await _login(c, email)
        r = await c.get("/console/comments")
        assert r.status_code == 200 and "FGTS" in r.text and "foto linda" not in r.text and "@fulano" in r.text
        assert "foto linda" in (await c.get("/console/comments?f=all")).text
        csrf = re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)
        assert (await c.post(f"/console/comments/{rid}/resolve", data={"csrf": csrf})).status_code == 303
        assert "FGTS" not in (await c.get("/console/comments")).text


async def test_inbox_hides_lab_conversations_and_warns_when_nobody_was_notified(tenant):
    await _publish(tenant)
    async with tenant_tx(tenant) as conn:
        ca = await (await conn.execute("select id from channel_accounts where tenant_id=%s", (tenant,))).fetchone()
        from agentplatform.core.orchestration.conversations import get_or_create_conversation
        await get_or_create_conversation(conn, tenant, ca["id"], "instagram", "LAB-s1-abc", "novo")
        real = await get_or_create_conversation(conn, tenant, ca["id"], "instagram", "IG-real", "novo", username="cliente.real", entrypoint="comment")
        lead = await (await conn.execute("select id from leads where contact_id=%s", (real["contact_id"],))).fetchone()
        await conn.execute("insert into handoffs (tenant_id, conversation_id, lead_id, reason, priority, summary, notify_at, sla_due_at, notified_at, notified_via) "
                           "values (%s,%s,%s,'explicit_request','normal','{}',now(),now(),now(),'log')", (tenant, real["id"], lead["id"]))
    email, _ = await _user(tenant, role="seller", name="V")
    async with _client() as c:
        await _login(c, email)
        r = await c.get("/console/")
    assert r.status_code == 200 and "@cliente.real" in r.text and "Comentário" in r.text and "LAB-s1" not in r.text.replace("…", "")
    assert "sem aviso ao vendedor" in r.text


def test_validate_accepts_the_package_templates():
    for t in PKG.manifest.comment_templates:
        assert validate(CFG, mode="both", trigger_kind="keyword", keywords=t.keywords, public=t.public_variants, private=t.private_variants,
                        repeat=t.repeat_variants, allowed_urls=PKG.manifest.allowed_urls) == []


def test_public_text_needs_no_disclosure_but_direct_does():
    problems = validate(CFG, mode="both", trigger_kind="any", keywords=[], public=["a", "b", "c"], private={"A": "Oi!"}, repeat=[], allowed_urls=[])
    assert [p.rule for p in problems] == ["missing_disclosure"]


# ------------------------------------------------------------------ what the agent knows
async def _comment_conversation(tenant, text="quero saber o valor"):
    async with tenant_tx(tenant) as conn:
        ca = await (await conn.execute("select id, external_account_id from channel_accounts where tenant_id=%s", (tenant,))).fetchone()
        await conn.execute(
            """insert into comment_settings (channel_account_id, tenant_id, default_active, keywords, public_variants, private_variants, repeat_variants, active_since)
               values (%s,%s,true,%s,%s,%s,%s,now() - interval '1 day')""",
            (ca["id"], tenant, OFFER.keywords, OFFER.public_variants, json.dumps(OFFER.private_variants), OFFER.repeat_variants))
        m = await (await conn.execute(
            "insert into channel_media (tenant_id, channel_account_id, external_media_id, caption_masked, media_kind) values (%s,%s,'car','Carta para trocar de carro por R$ 777','post') returning id",
            (tenant, ca["id"]))).fetchone()
        await conn.execute("insert into media_automations (media_id, tenant_id, mode, context_note_masked, active_since) values (%s,%s,'default','Oferta de troca de carro',now())",
                           (m["id"], tenant))
        ev = CanonicalEvent(event_type="CommentReceived", channel="instagram", account_external_id=ca["external_account_id"],
                            contact_external_id=f"IG-{uuid.uuid4().hex[:8]}", idempotency_key="cmt:x", provider_ts=NOW(), text=text,
                            comment_id=f"x-{uuid.uuid4().hex[:6]}", media_id="car")
        d = await process_comment(conn, CFG, PKG.manifest, tenant_id=tenant, channel_account_id=ca["id"],
                                  account_external_id=ca["external_account_id"], ev=ev, now=NOW())
    return d


async def _reply_in_direct(tenant, conv_id, text):
    async with tenant_tx(tenant) as conn:
        await store_inbound_message(conn, tenant, conv_id, mid=f"m-{uuid.uuid4().hex[:8]}", kind="text", text=text, media=None,
                                    provider_ts=NOW(), inbound_event_id=None, service_window=timedelta(hours=24))


class CapturingGateway(ScriptedGateway):
    def __init__(self, script):
        super().__init__(script)
        self.prompts: dict[str, str] = {}

    async def chat(self, spec, messages, ctx, out_model=None, **kw):
        self.prompts[ctx.node] = "\n".join(m["content"] for m in messages)
        return await super().chat(spec, messages, ctx, out_model, **kw)


async def _send_sent(tenant, d):
    from agentplatform.core.channels.fake.adapter import FakeChannelAdapter
    from agentplatform.core.outbox.dispatcher import Dispatcher
    await Dispatcher({"instagram": FakeChannelAdapter()}).dispatch_conversation(tenant, d.conversation_id)


async def test_agent_knows_the_comment_the_post_and_its_own_opener(tenant):
    d = await _comment_conversation(tenant)
    await _send_sent(tenant, d)
    await _reply_in_direct(tenant, d.conversation_id, "para casa")
    gw = CapturingGateway({"analyze_turn": [analysis(intents=["provide_info"], stage="engajado", evidence=["para casa"])],
                           "respond": [draft("Boa! Você tem um valor em mente para a casa?")]})
    res = await TurnRunner(TurnDeps(config=CFG, package=PKG, gateway=gw, knowledge=None, now=NOON)).run(tenant, d.conversation_id)
    assert res.status == "committed"
    p = gw.prompts["respond"]
    assert "quero saber o valor" in p and "Carta para trocar de carro" in p and "Oferta de troca de carro" in p
    assert "assistente virtual do" in p.split("mensagens recentes:")[1]                    # the opener it sent
    assert OFFER.public_variants[0] not in p.split("mensagens recentes:")[1]               # a post reply is not part of the direct
    assert "- cliente: quero saber" not in p                                               # the comment is origin, not a direct message
    assert "<origin>" in gw.prompts["analyze_turn"]


async def test_number_in_the_caption_is_not_a_source_but_the_customers_comment_is(tenant):
    d = await _comment_conversation(tenant, "quero a de 90 mil, qual o valor?")
    await _send_sent(tenant, d)
    await _reply_in_direct(tenant, d.conversation_id, "para casa")
    gw = ScriptedGateway({"analyze_turn": [analysis(intents=["provide_info"], stage="engajado", evidence=["para casa"])],
                          "respond": [draft("Sai por R$ 777 por mês."), draft("Boa! A carta de 90 mil é uma boa base, é para morar?")]})
    res = await TurnRunner(TurnDeps(config=CFG, package=PKG, gateway=gw, knowledge=None, now=NOON)).run(tenant, d.conversation_id)
    assert res.status == "committed" and any(g["rule_id"] == "numbers_provenance" for g in res.trace.get("guard", [])) is False
    async with tenant_tx(tenant) as conn:
        guards = [r["rule_id"] for r in await (await conn.execute(
            "select rule_id from guard_results where turn_id=%s and verdict='block'", (res.turn_id,))).fetchall()]
        sent = await (await conn.execute("select payload from outbox where turn_id=%s", (res.turn_id,))).fetchone()
    assert "numbers_provenance" in guards and "90 mil" in sent["payload"]["text"] and "777" not in sent["payload"]["text"]


async def test_spam_is_never_the_origin_of_a_conversation(tenant):
    from agentplatform.core.comments.origin import load_origin
    d = await _comment_conversation(tenant)
    async with tenant_tx(tenant) as conn:
        await conn.execute(
            """insert into comment_actions (tenant_id, comment_id, media_id, author_external_id, decision, text_masked, conversation_id)
               values (%s,%s,'car','A','spam','sigam meu perfil',%s)""", (tenant, f"s-{uuid.uuid4().hex[:6]}", d.conversation_id))
        lead = await (await conn.execute(
            "select l.id from leads l join conversations c on c.contact_id=l.contact_id where c.id=%s", (d.conversation_id,))).fetchone()
        origin = await load_origin(conn, d.conversation_id, lead["id"], CFG.comments)
    assert [o["comment"] for o in origin] == ["quero saber o valor"]


async def test_conversation_page_says_when_the_customer_asked_to_stop(tenant):
    await _publish(tenant)
    d = await _comment_conversation(tenant)
    async with tenant_tx(tenant) as conn:
        await conn.execute("update leads set opted_out_at=now() where contact_id=(select contact_id from conversations where id=%s)", (d.conversation_id,))
    email, _ = await _user(tenant, role="seller", name="V")
    async with _client() as c:
        await _login(c, email)
        r = await c.get(f"/console/conversations/{d.conversation_id}")
    assert r.status_code == 200 and "pediu para não receber mensagens" in r.text and "Respondendo" not in r.text


async def test_assistant_sees_what_the_seller_said_after_taking_back(tenant):
    d = await _comment_conversation(tenant)
    await handle_echo(tenant, d.conversation_id, "mid-s1", "Combinado, te ligo amanhã", reconcile_window_s=120, rechecked=True)
    async with tenant_tx(tenant) as conn:
        from agentplatform.core.orchestration.conversations import transition_ownership
        await transition_ownership(conn, tenant, d.conversation_id, "BOT", cause="test")
    await _reply_in_direct(tenant, d.conversation_id, "oi de novo")
    gw = CapturingGateway({"analyze_turn": [analysis(intents=["greeting"], stage="engajado", evidence=["oi"])],
                           "respond": [draft("Oi! Consigo ajudar até o consultor te ligar?")]})
    await TurnRunner(TurnDeps(config=CFG, package=PKG, gateway=gw, knowledge=None, now=NOON)).run(tenant, d.conversation_id)
    assert "- vendedor: Combinado, te ligo amanhã" in gw.prompts["respond"]


async def test_first_turn_after_a_public_only_reply_still_introduces_itself(tenant):
    async with tenant_tx(tenant) as conn:
        ca = await (await conn.execute("select id, external_account_id from channel_accounts where tenant_id=%s", (tenant,))).fetchone()
        await conn.execute(
            """insert into comment_settings (channel_account_id, tenant_id, default_active, default_mode, keywords, public_variants, active_since)
               values (%s,%s,true,'public',%s,%s,now() - interval '1 day')""", (ca["id"], tenant, OFFER.keywords, OFFER.public_variants))
        ev = CanonicalEvent(event_type="CommentReceived", channel="instagram", account_external_id=ca["external_account_id"],
                            contact_external_id=f"IG-{uuid.uuid4().hex[:8]}", idempotency_key="cmt:y", provider_ts=NOW(), text="quero",
                            comment_id=f"y-{uuid.uuid4().hex[:6]}", media_id="any")
        d = await process_comment(conn, CFG, PKG.manifest, tenant_id=tenant, channel_account_id=ca["id"],
                                  account_external_id=ca["external_account_id"], ev=ev, now=NOW())
    await _send_sent(tenant, d)
    await _reply_in_direct(tenant, d.conversation_id, "oi")
    gw = CapturingGateway({"analyze_turn": [analysis(intents=["greeting"], stage="engajado", evidence=["oi"])],
                           "respond": [draft("Oi! Sou o assistente virtual do consultor. É para casa ou carro?")]})
    await TurnRunner(TurnDeps(config=CFG, package=PKG, gateway=gw, knowledge=None, now=NOON)).run(tenant, d.conversation_id)
    assert "Esta é a primeira mensagem da conversa" in gw.prompts["respond"]


def test_jobs_registered():
    assert callable(tasks.comment_backfill) and callable(tasks.media_sync) and callable(tasks.hydrate_media) and callable(tasks.fetch_username)
