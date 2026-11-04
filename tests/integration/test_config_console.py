"""Changing the assistant in the console (ADR-022): options that really do something, drafts that never undo another change, evaluations that do not touch
customers, a search that never compares vectors from two models, and people joining and leaving. Material is synthetic and the provider is a fake."""
import asyncio
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from agentplatform.api import console, console_setup
from agentplatform.api.main import create_app
from agentplatform.cli import _ensure_kb_index
from agentplatform.core.agent.guards import OutputContext, failed, output_guard
from agentplatform.core.agent.turn import TurnDeps, TurnRunner
from agentplatform.core.channels.fake.adapter import FakeChannelAdapter
from agentplatform.core.config.registry import ConfigError, load_yaml, merge_changes, registry, resolve_with_package
from agentplatform.core.db.pool import platform_tx, tenant_tx
from agentplatform.core.followups import engine
from agentplatform.core.knowledge import admin as K
from agentplatform.core.knowledge.service import KnowledgeService, question_similarity, vec_literal
from agentplatform.core.models.fake import ScriptedGateway, analysis, draft
from agentplatform.core.models.gateway import CallContext
from agentplatform.core.outbox.dispatcher import Dispatcher, DispatchPolicy
from agentplatform.core.packages.contract import load_package
from agentplatform.lab import gate as G
from agentplatform.settings import get_settings
from tests.integration.helpers import make_conversation
from tests.integration.test_console_admin import PW, _user
from tests.integration.test_turn import NOON, inbound

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]
PKG = load_package("consortium")
RAW = load_yaml(ROOT / "config/tenants/dev-consortium.yaml")
CFG = resolve_with_package(RAW, PKG.manifest.compliance)
CTX = CallContext(None, "knowledge", purpose="ingest")
ANY_DAY = DispatchPolicy(holidays_national=False)


def U(**over):
    from agentplatform.core.agent.state import TurnUnderstanding
    return TurnUnderstanding(**{"intents": [], "questions": [], "slot_updates": [], "objections": [], "sentiment": "neutral", "asks_for_human": False,
                                "out_of_scope": False, "opt_out_intent": 0.0, **over})


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(console.router)
    app.include_router(console_setup.router)
    return app


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://t")


async def publish(tenant, cfg=RAW):
    async with tenant_tx(tenant) as conn:
        return await registry.publish(conn, tenant, cfg, package_compliance=PKG.manifest.compliance, activate=True)


async def signed_in(c, tenant, role="tenant_owner", name="Dona"):
    email, uid = await _user(tenant, role=role, name=name)
    await c.post("/console/login", data={"email": email, "password": PW})
    return uid


async def csrf(c, path="/console/setup"):
    return re.search(r'name="csrf" value="([^"]+)"', (await c.get(path)).text).group(1)


async def version(tenant, vid):
    async with tenant_tx(tenant) as conn:
        return await (await conn.execute("select * from config_versions where id=%s", (vid,))).fetchone()


async def active(tenant):
    async with tenant_tx(tenant) as conn:
        return await (await conn.execute("select * from config_versions where tenant_id=%s and status='active'", (tenant,))).fetchone()


def changed(cfg, **rag):
    return cfg.model_copy(update={"rag": cfg.rag.model_copy(update=rag)})


# ------------------------------------------------------------------ quoting guidance-only material
GUIDANCE = "O bônus fictício da equipe zebrafina é pago em três etapas depois da venda confirmada pela empresa no sistema oficial"


def guard(text, guidance=(GUIDANCE,)):
    ctx = OutputContext(CFG.compliance, [text], set(), [], 1000, guidance_texts=guidance, guidance_max_ngram=CFG.output_guard.guidance_only_max_ngram)
    return {v.rule_id for v in failed(output_guard(text, ctx))}


def test_a_long_quote_of_guidance_only_material_is_blocked():
    assert "guidance_quoted" in guard("Olha: o bônus fictício da equipe zebrafina é pago em três etapas depois da venda confirmada pela empresa, combinado?")


def test_saying_it_in_other_words_passes():
    assert "guidance_quoted" not in guard("Quem cuida do pagamento é a empresa, depois da venda, tudo pelo sistema oficial.")


def test_the_quote_check_ignores_accents_and_case():
    assert "guidance_quoted" in guard("O BONUS FICTICIO DA EQUIPE ZEBRAFINA E PAGO EM TRES ETAPAS DEPOIS DA VENDA CONFIRMADA PELA EMPRESA.")


def test_citable_passages_can_be_quoted():
    assert "guidance_quoted" not in guard(f"{GUIDANCE}.", guidance=())


def _no_basis_rules(text):
    ctx = OutputContext(CFG.compliance, [text], set(), [], 1000, no_basis=True, no_basis_claims=tuple(CFG.output_guard.no_basis_claim_patterns))
    return {v.rule_id for v in failed(output_guard(text, ctx))}


def test_with_no_basis_a_reply_cannot_open_by_settling_the_question():
    """Found by the evaluation: a model opened with "Pode sim", with nothing to base it on."""
    for reply in ("Pode sim, a carta vale em outro estado.", "Sim, dá para usar.", "Não tem multa nenhuma.", "Claro! Funciona assim."):
        assert "answered_without_basis" in _no_basis_rules(reply), reply
    for reply in ("Vou confirmar com o consultor e já te falo.", "Não tenho essa informação agora, vou confirmar.", "Boa pergunta: vou ver isso para você."):
        assert "answered_without_basis" not in _no_basis_rules(reply), reply


def test_with_a_basis_the_same_opening_is_fine():
    ctx = OutputContext(CFG.compliance, ["sim"], set(), [], 1000, no_basis=False, no_basis_claims=tuple(CFG.output_guard.no_basis_claim_patterns))
    assert "answered_without_basis" not in {v.rule_id for v in failed(output_guard("Sim, dá para usar.", ctx))}


async def test_a_turn_retries_and_then_hands_off_when_it_keeps_quoting(tenant):
    """Real turn: the guard gets the passages marked guidance_only, not the citable ones."""
    conv = await inbound(tenant, ["como funciona o bônus?"])
    quote = f"Então: {GUIDANCE}."

    class Search:
        async def retrieve(self, conn, tenant_id, q, ctx, degraded=False):
            from agentplatform.core.agent.state import RetrievalResult, RetrievedChunk
            return RetrievalResult(chunks=[RetrievedChunk(id=str(uuid.uuid4()), text=GUIDANCE, score=1.0, visibility="guidance_only", document="ficticio.pdf")])
    g = ScriptedGateway({"analyze_turn": [analysis(intents=["product_question"], stage="educando", evidence=["bônus"], questions=["como funciona o bônus?"])],
                         "respond": [draft(quote), draft(quote)], "output_judge": [{"violations": [], "explanation": ""}] * 2})
    res = await TurnRunner(TurnDeps(config=CFG, package=PKG, gateway=g, knowledge=Search(), now=NOON)).run(tenant, conv["id"])
    async with tenant_tx(tenant) as conn:
        rules = {r["rule_id"] for r in await (await conn.execute("select rule_id from guard_results where turn_id=%s", (res.turn_id,))).fetchall()}
    assert "guidance_quoted" in rules and res.status == "handoff"


# ------------------------------------------------------------------ a draft never undoes another change
async def test_approving_a_draft_made_from_another_version_is_refused(tenant):
    a, _ = await _user(tenant)
    b_uid = (await _user(tenant, name="Outra"))[1]
    a_uid = (await _user(tenant, name="Terceira"))[1]
    await publish(tenant)
    async with tenant_tx(tenant) as conn:
        prompt = await registry.propose(conn, tenant, changed(RAW, top_k=7), package_compliance=PKG.manifest.compliance, created_by=a_uid)
        budget = await registry.propose(conn, tenant, changed(RAW, min_score=0.4), package_compliance=PKG.manifest.compliance, created_by=a_uid)
        await registry.approve(conn, tenant, prompt, b_uid)
    with pytest.raises(ConfigError) as e:
        async with tenant_tx(tenant) as conn:
            await registry.approve(conn, tenant, budget, b_uid)
    assert e.value.code == "stale_draft"
    assert (await active(tenant))["document"]["rag"]["top_k"] == 7, "the first change is still there"


async def test_bringing_a_draft_up_to_date_keeps_both_changes(tenant):
    a_uid = (await _user(tenant, name="A"))[1]
    b_uid = (await _user(tenant, name="B"))[1]
    await publish(tenant)
    async with tenant_tx(tenant) as conn:
        first = await registry.propose(conn, tenant, changed(RAW, top_k=7), package_compliance=PKG.manifest.compliance, created_by=a_uid)
        second = await registry.propose(conn, tenant, changed(RAW, min_score=0.4), package_compliance=PKG.manifest.compliance, created_by=a_uid, note="busca")
        await registry.approve(conn, tenant, first, b_uid)
    async with tenant_tx(tenant) as conn:
        new = await registry.rebase(conn, tenant, second, b_uid)
    assert (await version(tenant, second))["status"] == "discarded"
    row = await version(tenant, new)
    assert row["created_by"] == a_uid and row["note"] == "busca" and row["eval_run_id"] is None, "same author, and it needs its own evaluation"
    assert row["document"]["rag"]["top_k"] == 7 and row["document"]["rag"]["min_score"] == 0.4
    async with tenant_tx(tenant) as conn:
        assert await registry.approve(conn, tenant, new, b_uid) == "active"
    now = await active(tenant)
    assert now["document"]["rag"]["top_k"] == 7 and now["document"]["rag"]["min_score"] == 0.4


async def test_two_changes_to_the_same_field_need_a_person(tenant):
    a_uid = (await _user(tenant, name="A"))[1]
    b_uid = (await _user(tenant, name="B"))[1]
    await publish(tenant)
    async with tenant_tx(tenant) as conn:
        first = await registry.propose(conn, tenant, changed(RAW, top_k=7), package_compliance=PKG.manifest.compliance, created_by=a_uid)
        second = await registry.propose(conn, tenant, changed(RAW, top_k=9), package_compliance=PKG.manifest.compliance, created_by=a_uid)
        await registry.approve(conn, tenant, first, b_uid)
    with pytest.raises(ConfigError) as e:
        async with tenant_tx(tenant) as conn:
            await registry.rebase(conn, tenant, second, b_uid)
    assert e.value.code == "merge_conflict" and "rag.top_k" in e.value.detail


def test_merge_applies_only_what_the_draft_changed():
    base = {"a": 1, "b": {"x": 1, "y": 2}, "c": [1]}
    merged, conflicts = merge_changes(base, {"a": 1, "b": {"x": 5, "y": 2}, "c": [1]}, {"a": 9, "b": {"x": 1, "y": 3}, "c": [1, 2]})
    assert merged == {"a": 9, "b": {"x": 5, "y": 3}, "c": [1, 2]} and not conflicts


async def test_rolling_back_makes_drafts_on_the_undone_version_stale(tenant):
    a_uid = (await _user(tenant, name="A"))[1]
    b_uid = (await _user(tenant, name="B"))[1]
    await publish(tenant)
    async with tenant_tx(tenant) as conn:
        first = await registry.propose(conn, tenant, changed(RAW, top_k=7), package_compliance=PKG.manifest.compliance, created_by=a_uid)
        await registry.approve(conn, tenant, first, b_uid)
        pending = await registry.propose(conn, tenant, changed(RAW, top_k=7, min_score=0.4), package_compliance=PKG.manifest.compliance, created_by=a_uid)
        await registry.rollback(conn, tenant, RAW.environment)
    with pytest.raises(ConfigError) as e:
        async with tenant_tx(tenant) as conn:
            await registry.approve(conn, tenant, pending, b_uid)
    assert e.value.code == "stale_draft"


async def test_the_config_screen_explains_what_a_draft_changes_and_refuses_in_words(tenant):
    await publish(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        token = await csrf(c, "/console/config")
        r = await c.post("/console/assistant", data={"csrf": token, **_assistant_form(tone="formal", business_instructions="Atendo por videochamada.")})
        assert r.status_code == 303
        page = (await c.get(r.headers["location"])).text
    assert "Tom" in page and "formal" in page and "Instruções do negócio" in page and "Atendo por videochamada." in page
    assert "Aguardando outra pessoa aprovar" in page, "who proposed cannot approve"


async def test_a_form_opened_right_after_an_approval_does_not_undo_it(tenant):
    """Found in the real console: the runtime keeps a config for 30 seconds, and a form built from that copy reverted what had just been approved."""
    from agentplatform.runtime import Runtime, reset_scoped_runtime, scoped_runtime
    rt = Runtime(adapters={"instagram": FakeChannelAdapter()}, gateway=None)
    token = scoped_runtime(rt)
    try:
        await publish(tenant)
        await rt.tenant(tenant)                                        # the process now has this version in memory
        a = (await _user(tenant, name="A"))[1]
        b = (await _user(tenant, name="B"))[1]
        async with tenant_tx(tenant) as conn:
            first = await registry.propose(conn, tenant, changed(RAW, top_k=7), package_compliance=PKG.manifest.compliance, created_by=a)
            await registry.approve(conn, tenant, first, b)
        assert (await rt.tenant(tenant)).config.rag.top_k != 7, "what a turn may keep using for a few seconds"
        async with client() as c:
            await signed_in(c, tenant)
            token_c = await csrf(c, "/console/assistant")
            page = (await c.get("/console/assistant")).text
            r = await c.post("/console/assistant", data={"csrf": token_c, **_assistant_form(tone="formal")})
        assert r.status_code == 303 and "Instruções do negócio" in page
        d = (await version(tenant, uuid.UUID(r.headers["location"].split("proposta=")[1])))["document"]
        assert d["rag"]["top_k"] == 7 and d["conversation"]["persona"]["tone"] == "formal"
    finally:
        reset_scoped_runtime(token)


async def test_old_documents_do_not_look_like_changes(tenant):
    """Versions stored before an option was removed or added: only what a person changed appears."""
    from agentplatform.api.ui import config_changes
    from agentplatform.core.config.registry import normalized
    old = json.loads(RAW.model_dump_json())
    old["agent"]["analyze_mode"] = "merged"
    old["budget"]["day_boundary_tz"] = "UTC"
    del old["agent"]["max_reply_attempts"]
    new = json.loads(RAW.model_dump_json())
    new["conversation"]["persona"]["tone"] = "formal"
    assert [c["label"] for c in config_changes(normalized(old), normalized(new))] == ["Tom"]


# ------------------------------------------------------------------ forms
def _assistant_form(**over):
    base = {"seller_display_name": "Ana", "assistant_name": "Assistente da Ana", "company_name": "", "allowed_urls": "", "tone": "friendly", "disclose": "on", "business_instructions": "",
            "waiting_message": CFG.conversation.waiting_message, "first_message_disclosure": CFG.conversation.first_message_disclosure, "safe_failure_message": CFG.conversation.safe_failure_message,
            "opt_out_confirmation": CFG.conversation.opt_out_confirmation, "expectation_in": CFG.handoff.expectation_message_in_hours,
            "expectation_off": CFG.handoff.expectation_message_off_hours, "send_expectation": "on", "sla_minutes": "30", "realert_min": "30",
            "off_hours_mode": "bot_keeps_serving", "notify_off_hours": "at_open", "quiet_start": "22:00", "quiet_end": "08:00", "timezone": "America/Sao_Paulo",
            "holidays_national": "on", "holidays_local": "", "cap_day": "2", "cap_week": "4", "followups_enabled": "on", "note": "teste"}
    return {**base, **over}


async def test_the_assistant_form_creates_a_draft_and_changes_nothing_for_customers(tenant):
    v0 = await publish(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        token = await csrf(c, "/console/assistant")
        form = _assistant_form(tone="formal", business_instructions="Prefiro explicar o crédito primeiro.", holidays_local="05-20\n2027-06-03", cap_week="3")
        r = await c.post("/console/assistant", data={**form, "csrf": token, "example_customer": "oi", "example_reply": "Oi! Tudo bem?"})
    assert r.status_code == 303
    draft_id = uuid.UUID(r.headers["location"].split("proposta=")[1])
    row = await version(tenant, draft_id)
    d = row["document"]
    assert row["status"] == "draft" and row["base_version_id"] == v0 and row["note"] == "teste"
    assert d["conversation"]["persona"]["tone"] == "formal" and d["conversation"]["business_instructions"].startswith("Prefiro")
    assert d["conversation"]["reply_examples"] == [{"customer": "oi", "reply": "Oi! Tudo bem?"}]
    assert d["sending"]["holidays_local"] == ["05-20", "2027-06-03"] and d["sending"]["frequency_cap_per_contact_week"] == 3
    assert (await active(tenant))["id"] == v0


async def test_the_assistant_form_refuses_a_forbidden_promise_in_a_message(tenant):
    await publish(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        token = await csrf(c, "/console/assistant")
        r = await c.post("/console/assistant", data={"csrf": token, **_assistant_form(safe_failure_message="Fica tranquilo, sua contemplação garantida em poucos meses!")})
    assert r.status_code == 400 and "Mensagem quando algo dá errado" in r.text


async def test_the_assistant_form_explains_an_invalid_value(tenant):
    await publish(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        token = await csrf(c, "/console/assistant")
        r = await c.post("/console/assistant", data={"csrf": token, **_assistant_form(quiet_start="tarde", holidays_local="dia 5")})
    assert r.status_code == 400 and "Início do silêncio" in r.text


async def test_saving_without_changes_creates_nothing(tenant):
    await publish(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        token = await csrf(c, "/console/knowledge/test")
        r = await c.post("/console/knowledge/settings", data={"csrf": token})
        assert r.status_code == 303 and "igual=1" in r.headers["location"]
    async with tenant_tx(tenant) as conn:
        assert (await (await conn.execute("select count(*) as n from config_versions where tenant_id=%s", (tenant,))).fetchone())["n"] == 1


async def test_only_the_owner_changes_the_assistant(tenant):
    await publish(tenant)
    async with client() as c:
        await signed_in(c, tenant, role="reviewer", name="R")
        assert (await c.get("/console/assistant")).status_code == 403
        assert (await c.get("/console/assistant/rules")).status_code == 403
        assert (await c.get("/console/knowledge/test")).status_code == 200, "a reviewer can test the search"


async def test_models_form_keeps_the_family_rule_and_only_offers_priced_models(tenant, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "openrouter")
    await publish(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        page = await c.get("/console/assistant/models")
        assert "deepseek/deepseek-v4-flash" in page.text
        token = await csrf(c, "/console/assistant/models")
        bad = await c.post("/console/assistant/models", data={"csrf": token, "respond_primary": "unknown/model"})
        assert bad.status_code == 400
        ok = await c.post("/console/assistant/models", data={"csrf": token, "respond_primary": "deepseek/deepseek-v4-flash", "respond_fallback": "google/gemini-3.1-flash-lite",
                                                                  "transcription": "openai/whisper-large-v3-turbo"})
    assert ok.status_code == 303
    d = (await version(tenant, uuid.UUID(ok.headers["location"].split("proposta=")[1])))["document"]
    assert d["models"]["respond"]["primary"] == "deepseek/deepseek-v4-flash" and d["models"]["respond"]["fallbacks"] == ["google/gemini-3.1-flash-lite"]


async def test_the_test_key_only_offers_its_own_models(tenant, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "openai")
    await publish(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        page = (await c.get("/console/assistant/models")).text
    assert "deepseek/deepseek-v4-flash · " not in page and "chave de testes" in page


# ------------------------------------------------------------------ rules
async def test_owner_can_turn_a_guideline_off_and_add_their_own(tenant):
    await publish(tenant)
    target = PKG.manifest.guidelines[0]
    others = {f"g_on_{g.id}": "on" for g in PKG.manifest.guidelines[1:]}
    reasons = {f"reason_{r}": "on" for r in ("frustration", "no_progress")}
    async with client() as c:
        await signed_in(c, tenant)
        token = await csrf(c, "/console/assistant/rules")
        r = await c.post("/console/assistant/rules", data={
            "csrf": token, **others, **reasons, "c_action": "Explique que atendo por videochamada", "c_words": "visita, presencial", "c_intents": "", "c_stages": "",
            "own_phrases": "aproveite só hoje\n"})
    assert r.status_code == 303
    d = (await version(tenant, uuid.UUID(r.headers["location"].split("proposta=")[1])))["document"]
    assert {"id": target.id, "enabled": False, "priority": None, "action": None} in d["guidelines"]["overrides"]
    assert d["guidelines"]["custom"][0]["when_keywords"] == ["visita", "presencial"]
    assert "aproveite só hoje" in d["compliance"]["forbidden_phrases"]
    assert set(PKG.manifest.compliance.forbidden_phrases) <= set(d["compliance"]["forbidden_phrases"]), "the package's stay"


def test_a_disabled_guideline_is_not_selected_and_a_custom_one_is():
    from agentplatform.core.agent.policy import select_guidelines
    from agentplatform.core.config.schema import CustomGuideline, GuidelineOverride, GuidelinesConfig
    u = U(intents=["bid_question"])
    on = select_guidelines(PKG.manifest, "educando", u, "como funciona o lance?", 0, GuidelinesConfig())
    bid = next(g for g in PKG.manifest.guidelines if g.id == "bid_question")
    assert bid.action in on
    off = select_guidelines(PKG.manifest, "educando", u, "como funciona o lance?", 0, GuidelinesConfig(overrides=[GuidelineOverride(id="bid_question", enabled=False)]))
    assert bid.action not in off
    own = CustomGuideline(id="own", action="Fale da videochamada", when_keywords=["visita"])
    assert "Fale da videochamada" in select_guidelines(PKG.manifest, "educando", U(), "posso fazer uma visita?", 0, GuidelinesConfig(custom=[own]))


def test_a_locked_handoff_reason_cannot_be_turned_off_but_an_open_one_can():
    from agentplatform.core.agent.guards import input_guard
    from agentplatform.core.agent.policy import can_turn_off, handoff_reason
    m = PKG.manifest
    assert "legal" not in can_turn_off(m) and "hot_lead" in can_turn_off(m)
    legal = U(intents=["legal"])
    assert handoff_reason(m, legal, False, "educando", 0, None, ["legal"]) == "legal"
    hot = U(intents=["ready_to_buy"])
    assert handoff_reason(m, hot, False, "educando", 0) == "hot_lead"
    assert handoff_reason(m, hot, False, "educando", 0, None, ["hot_lead"]) is None
    assert input_guard("quero falar com uma pessoa", CFG.input_guard, m, ["explicit_request"]).handoff_reason == "explicit_request"
    assert input_guard("vou processar vocês", CFG.input_guard, m, ["legal"]).handoff_reason == "legal"
    assert input_guard("quero fechar agora", CFG.input_guard, m, ["hot_lead"]).handoff_reason is None


def test_the_owner_can_change_how_long_a_stage_waits():
    from agentplatform.core.agent.policy import handoff_reason, stage_limit
    from agentplatform.core.config.schema import FunnelConfig
    st = next(s for s in PKG.manifest.stages if s.max_turns_without_progress)
    funnel = FunnelConfig(max_turns_without_progress_override={st.name: 1})
    assert stage_limit(PKG.manifest, st.name, funnel) == 1
    assert handoff_reason(PKG.manifest, U(), False, st.name, 2, funnel) == "no_progress"
    assert handoff_reason(PKG.manifest, U(), False, st.name, 2, funnel, ["no_progress"]) is None


# ------------------------------------------------------------------ the turn's limits
class SlowGateway(ScriptedGateway):
    def __init__(self, script, delay):
        super().__init__(script)
        self.delay = delay

    async def chat(self, spec, messages, ctx, output_model=None):
        await asyncio.sleep(self.delay)
        return await super().chat(spec, messages, ctx, output_model)


def timed(slow_after=None, timeout=None, waiting=None):
    agent = {k: v for k, v in (("slow_after_s", slow_after), ("turn_timeout_s", timeout)) if v is not None}
    conversation = {"waiting_message": waiting} if waiting is not None else {}
    return CFG.model_copy(update={"agent": CFG.agent.model_copy(update=agent), "conversation": CFG.conversation.model_copy(update=conversation)})


async def test_a_slow_turn_tells_the_customer_once_through_the_outbox(tenant):
    conv = await inbound(tenant, ["oi"])
    a = FakeChannelAdapter()
    d = Dispatcher({"instagram": a})
    g = SlowGateway({"analyze_turn": [analysis(stage="novo")], "respond": [draft("Oi! Você pensa em imóvel ou veículo?")]}, 0.15)
    res = await TurnRunner(TurnDeps(config=timed(slow_after=0.05), package=PKG, gateway=g, knowledge=None, now=NOON, dispatch_now=d.dispatch_conversation)).run(tenant, conv["id"])
    assert res.status == "committed"
    assert a.texts() == [CFG.conversation.waiting_message], "out before the reply, on its own"
    await d.dispatch_conversation(tenant, conv["id"])
    assert a.texts()[-1].startswith("Oi!") and len(a.texts()) == 2
    async with tenant_tx(tenant) as conn:
        rows = await (await conn.execute("select effect_key from outbox where conversation_id=%s and effect_key like %s", (conv["id"], "%:wait"))).fetchall()
    assert len(rows) == 1


async def test_a_fast_turn_says_nothing_extra(tenant):
    conv = await inbound(tenant, ["oi"])
    a = FakeChannelAdapter()
    d = Dispatcher({"instagram": a})
    g = ScriptedGateway({"analyze_turn": [analysis(stage="novo")], "respond": [draft("Oi! Você pensa em imóvel ou veículo?")]})
    await TurnRunner(TurnDeps(config=timed(slow_after=5), package=PKG, gateway=g, knowledge=None, now=NOON, dispatch_now=d.dispatch_conversation)).run(tenant, conv["id"])
    await d.dispatch_conversation(tenant, conv["id"])
    assert len(a.texts()) == 1


async def test_a_conversation_a_person_took_gets_no_waiting_message(tenant):
    from agentplatform.core.orchestration.conversations import transition_ownership
    conv = await inbound(tenant, ["oi"])
    a = FakeChannelAdapter()
    d = Dispatcher({"instagram": a})

    class TakenMeanwhile(SlowGateway):
        async def chat(self, spec, messages, ctx, output_model=None):
            async with tenant_tx(tenant) as conn:
                await transition_ownership(conn, tenant, conv["id"], "HUMAN", cause="seller_took")
            return await super().chat(spec, messages, ctx, output_model)
    g = TakenMeanwhile({"analyze_turn": [analysis(stage="novo")], "respond": [draft("Oi!")]}, 0.15)
    await TurnRunner(TurnDeps(config=timed(slow_after=0.05), package=PKG, gateway=g, knowledge=None, now=NOON, dispatch_now=d.dispatch_conversation)).run(tenant, conv["id"])
    assert a.texts() == []


async def test_a_turn_past_its_limit_ends_like_a_model_outage(tenant):
    conv = await inbound(tenant, ["oi"])
    g = SlowGateway({"analyze_turn": [analysis(stage="novo")], "respond": [draft("Oi!")]}, 0.5)
    res = await TurnRunner(TurnDeps(config=timed(timeout=0.1, slow_after=5), package=PKG, gateway=g, knowledge=None, now=NOON)).run(tenant, conv["id"])
    assert res.status == "handoff"
    async with tenant_tx(tenant) as conn:
        t = await (await conn.execute("select status, cancel_reason from turns where id=%s", (res.turn_id,))).fetchone()
    assert t["status"] == "failed" and t["cancel_reason"] == "TimeoutError"


async def test_someone_flooding_gets_no_turn_until_it_calms_down_but_a_stop_request_counts(tenant, monkeypatch):
    from agentplatform.jobs import tasks as T
    conv = await inbound(tenant, [f"msg {i}" for i in range(4)])

    class Rt:
        async def tenant(self, tid):
            return type("TR", (), {"config": timed().model_copy(update={"input_guard": CFG.input_guard.model_copy(update={"per_contact_messages_per_min": 3})}),
                                   "policy": lambda self: type("P", (), {"live": True})()})()
    monkeypatch.setattr(T, "get_runtime", lambda: Rt())
    assert await T._turn_blocked(tenant, conv["id"]) == "contact_flood"
    async with tenant_tx(tenant) as conn:
        await conn.execute("update messages set text_masked='pode parar' where id=(select id from messages where conversation_id=%s limit 1)", (conv["id"],))
    assert await T._turn_blocked(tenant, conv["id"]) is None


async def test_bubbles_wait_between_them_and_every_check_runs_again(tenant, monkeypatch):
    from tests.integration.helpers import put
    conv, ig = await make_conversation(tenant)
    await put(tenant, conv, ig, "primeira")
    await put(tenant, conv, ig, "segunda")
    naps = []
    real = asyncio.sleep

    async def nap(s):
        naps.append(s)
        await real(0)
    monkeypatch.setattr("agentplatform.core.outbox.dispatcher.asyncio.sleep", nap)
    a = FakeChannelAdapter()

    async def policy(_):
        return DispatchPolicy(bubble_delay_s=1.2)
    d = Dispatcher({"instagram": a}, policy_for=policy)
    await d.dispatch_conversation(tenant, conv["id"])
    assert a.texts() == ["primeira", "segunda"] and naps == [1.2], "only between two messages"


async def test_a_closed_conversation_can_stay_closed_when_the_customer_writes(tenant):
    from agentplatform.core.channels.events import CanonicalEvent
    from agentplatform.core.orchestration.conversations import transition_ownership
    from agentplatform.core.orchestration.ingress import persist_events, route_event
    conv, ig = await make_conversation(tenant)
    async with tenant_tx(tenant) as conn:
        await transition_ownership(conn, tenant, conv["id"], "CLOSED", cause="seller_closed")
        acc = await (await conn.execute("select external_account_id from channel_accounts where id=%s", (conv["channel_account_id"],))).fetchone()

    async def write(**kw):
        ev = CanonicalEvent(event_type="MessageReceived", channel="instagram", account_external_id=acc["external_account_id"], contact_external_id=ig,
                            idempotency_key=f"t:{uuid.uuid4().hex}", mid=uuid.uuid4().hex, provider_ts=datetime.now(timezone.utc), text="oi de novo")
        (_, event_id), = await persist_events([ev])
        return await route_event(tenant, event_id, FakeChannelAdapter(), "novo", **kw)
    assert (await write(reopen_closed=False)).action == "none"
    async with tenant_tx(tenant) as conn:
        assert (await (await conn.execute("select ownership from conversations where id=%s", (conv["id"],))).fetchone())["ownership"] == "CLOSED"
        assert (await (await conn.execute("select count(*) as n from messages where conversation_id=%s and text_masked='oi de novo'", (conv["id"],))).fetchone())["n"] == 1
    assert (await write()).action == "debounce"


async def test_a_tool_can_be_turned_off_or_limited(tenant):
    from agentplatform.core.config.schema import ToolOverride
    spec = PKG.tools()[0]
    conv = await inbound(tenant, ["quanto fica a parcela pra um carro de 80 mil em 60 meses?"])
    flags = {f"{PKG.manifest.name}.simulation_enabled": True, **({spec.feature_flag: True} if spec.feature_flag else {})}
    base = CFG.model_copy(update={"operations": CFG.operations.model_copy(update={"feature_flags": flags}), "tools": {spec.name: ToolOverride(enabled=False)}})
    g = ScriptedGateway({"analyze_turn": [analysis(intents=["price_question"], stage="qualificando", evidence=["carro"], slots=[("goal", "vehicle", "carro")])],
                         "respond": [draft("Posso ver com o vendedor os valores para você, combinado?")], "output_judge": [{"violations": [], "explanation": ""}]})
    res = await TurnRunner(TurnDeps(config=base, package=PKG, gateway=g, knowledge=None, now=NOON)).run(tenant, conv["id"])
    assert spec.name not in (res.trace or {}).get("tools", [])


# ------------------------------------------------------------------ follow-ups
async def test_followups_respect_the_weekly_cap_across_the_persons_channels_and_are_not_all_at_once(tenant):
    from agentplatform.core.config.schema import Cadence, CadenceStep
    cad = Cadence(id="c1", trigger={"event": "x"}, steps=[CadenceStep(delay="0m", content_kind="template", template_key="t")])
    cfg = CFG.model_copy(update={"followups": CFG.followups.model_copy(update={"cadences": [cad]}),
                                 "sending": CFG.sending.model_copy(update={"frequency_cap_per_contact_day": 9, "frequency_cap_per_contact_week": 1, "proactive_jitter_s": (30, 300)})})
    now = NOON()
    conv, ig = await make_conversation(tenant)
    async with tenant_tx(tenant) as conn:
        await conn.execute("insert into followup_runs (tenant_id, conversation_id, cadence_id, step, status, due_at) values (%s,%s,'c1',0,'scheduled',%s)", (tenant, conv["id"], now))
        r = await engine.tick(conn, cfg, tenant, {"t": "Tudo certo por aí?"}, now, policy=ANY_DAY)
        row = await (await conn.execute("select not_before from outbox where conversation_id=%s and is_proactive", (conv["id"],))).fetchone()
    assert r.queued == 1 and timedelta(seconds=30) <= row["not_before"] - now <= timedelta(seconds=300)
    async with tenant_tx(tenant) as conn:
        await conn.execute("update outbox set status='sent', sent_at=%s where conversation_id=%s and is_proactive", (now - timedelta(days=3), conv["id"]))
        await conn.execute("delete from followup_runs where conversation_id=%s", (conv["id"],))
        await conn.execute("insert into followup_runs (tenant_id, conversation_id, cadence_id, step, status, due_at) values (%s,%s,'c1',0,'scheduled',%s)", (tenant, conv["id"], now))
        r = await engine.tick(conn, cfg, tenant, {"t": "Tudo certo por aí?"}, now, policy=ANY_DAY)
        st = await (await conn.execute("select status, stop_reason from followup_runs where conversation_id=%s", (conv["id"],))).fetchone()
    assert r.queued == 0 and st["stop_reason"] == "frequency_cap"


# ------------------------------------------------------------------ the embedding model and the index
async def test_a_config_with_another_embedding_model_is_refused(tenant):
    _ensure_kb_index(CFG)
    await publish(tenant)
    other = CFG.models.embedding.model_copy(update={"model": "baai/bge-m3"})
    bad = RAW.model_copy(update={"models": RAW.models.model_copy(update={"embedding": other})})
    uid = (await _user(tenant))[1]
    with pytest.raises(ConfigError) as e:
        async with tenant_tx(tenant) as conn:
            await registry.propose(conn, tenant, bad, package_compliance=PKG.manifest.compliance, created_by=uid)
    assert e.value.code == "embedding_mismatch"


class FakeEmbed:
    def __init__(self, value=0.1, fail=None):
        self.calls, self.value, self.fail = [], value, fail

    async def embed(self, spec, texts, ctx):
        if self.fail:
            raise self.fail
        self.calls.append(list(texts))
        return [[self.value] * spec.dims for _ in texts]


async def _approved_doc(tenant, text="Passagem sobre a palavra zebrafina com 12 parcelas.", name="a.pdf", index=None):
    idx_name = index or CFG.rag.active_index_id
    async with tenant_tx(tenant) as conn:
        src = await (await conn.execute("insert into kb_sources (tenant_id, scope, title, kind) values (%s,'tenant',%s,'pdf') returning id", (tenant, name))).fetchone()
        doc = await (await conn.execute(
            "insert into kb_documents (tenant_id, source_id, file_name, file_sha256, status, visibility, audience) values (%s,%s,%s,%s,'approved','citable','customer') returning id",
            (tenant, src["id"], name, uuid.uuid4().hex))).fetchone()
        idx = await (await conn.execute("select id from kb_indexes where name=%s", (idx_name,))).fetchone()
        await conn.execute("insert into kb_chunks (tenant_id, index_id, document_id, ordinal, text, token_count, embedding) values (%s,%s,%s,0,%s,10,%s::vector)",
                           (tenant, idx["id"], doc["id"], text, vec_literal([0.1] * CFG.models.embedding.dims)))
    return doc["id"]


async def _approved_faq(tenant, question="Como funciona?"):
    async with tenant_tx(tenant) as conn:
        f = await (await conn.execute("insert into faqs (tenant_id, canonical_question, answer, status) values (%s,%s,'Assim.','approved') returning id", (tenant, question))).fetchone()
        v = await (await conn.execute("insert into faq_variants (tenant_id, faq_id, question) values (%s,%s,%s) returning id", (tenant, f["id"], question))).fetchone()
        idx = await (await conn.execute("select id from kb_indexes where name=%s", (CFG.rag.active_index_id,))).fetchone()
        await conn.execute("insert into faq_variant_vectors (variant_id, index_id, tenant_id, embedding) values (%s,%s,%s,%s::vector)",
                           (v["id"], idx["id"], tenant, vec_literal([0.1] * CFG.models.embedding.dims)))
    return f["id"]


async def test_search_with_a_config_from_another_model_never_compares_the_vectors(tenant):
    _ensure_kb_index(CFG)
    await _approved_doc(tenant)
    wrong = CFG.models.embedding.model_copy(update={"model": "baai/bge-m3"})
    g = FakeEmbed()
    async with tenant_tx(tenant) as conn:
        r = await KnowledgeService(g, wrong, CFG.rag, "consortium").retrieve(conn, tenant, "zebrafina", CallContext(None, "t", purpose="turn"))
    assert r.no_basis and not r.chunks and not g.calls


async def test_replacing_the_model_builds_beside_the_current_one_and_a_person_activates_it(tenant, monkeypatch):
    _ensure_kb_index(CFG)
    await publish(tenant)
    doc = await _approved_doc(tenant)
    faq = await _approved_faq(tenant)
    draft_doc = await _approved_doc(tenant, name="rascunho.pdf", text="Trecho ainda em revisão, não pode sair do servidor.")
    async with tenant_tx(tenant) as conn:
        await conn.execute("update kb_documents set status='in_review' where id=%s", (draft_doc,))
    uid = (await _user(tenant))[1]
    new_model = "baai/bge-m3"
    async with tenant_tx(tenant) as conn:
        run = await K.start_reindex(conn, tenant, "t", uid, new_model, CFG.rag.active_index_id)
        row = await (await conn.execute("select to_index from kb_reindex_runs where id=%s", (run,))).fetchone()
        idx = await (await conn.execute("select name, status, dims from kb_indexes where id=%s", (row["to_index"],))).fetchone()
    assert idx["status"] == "building" and idx["dims"] == 1024
    new_spec = CFG.models.embedding.model_copy(update={"model": new_model, "dims": 1024})
    g = FakeEmbed()
    assert await K.run_reindex(tenant, run, g, new_spec, CTX, 64) == "ready"
    sent = [t for call in g.calls for t in call]
    assert "Passagem sobre a palavra zebrafina com 12 parcelas." in sent and "Como funciona?" in sent
    assert not any("em revisão" in t for t in sent), "only what is in use goes to the provider"
    async with tenant_tx(tenant) as conn:
        copied = await (await conn.execute("select count(*) as n from kb_chunks where index_id=%s and tenant_id=%s", (row["to_index"], tenant))).fetchone()
        old = await (await conn.execute("select count(*) as n from kb_chunks c join kb_indexes i on i.id=c.index_id where i.name=%s and c.document_id=%s and c.embedding is not null",
                                        (CFG.rag.active_index_id, doc))).fetchone()
    assert copied["n"] == 2 and old["n"] == 1, "the search keeps working on the current index"
    moved = RAW.model_copy(update={"models": RAW.models.model_copy(update={"embedding": new_spec}), "rag": RAW.rag.model_copy(update={"active_index_id": idx["name"]})})
    async with tenant_tx(tenant) as conn:
        await registry.propose(conn, tenant, moved, package_compliance=PKG.manifest.compliance, created_by=uid)
    g2 = FakeEmbed()
    async with tenant_tx(tenant) as conn:
        r = await KnowledgeService(g2, new_spec, moved.rag.model_copy(update={"faq_threshold": 0.5, "top_k": 2}), "consortium").retrieve(conn, tenant, "como funciona?", CallContext(None, "t", purpose="turn"))
    assert r.faq_id == str(faq)
    assert (await K.run_reindex(tenant, run, g, new_spec, CTX, 64)) == "ready", "running it again changes nothing"


async def test_a_failed_replacement_keeps_what_is_done_and_can_continue(tenant):
    from agentplatform.core.models.gateway import ModelCallError
    _ensure_kb_index(CFG)
    await _approved_doc(tenant)
    await _approved_doc(tenant, name="b.pdf", text="Outro trecho")
    uid = (await _user(tenant))[1]
    async with tenant_tx(tenant) as conn:
        run = await K.start_reindex(conn, tenant, "t", uid, "baai/bge-m3", CFG.rag.active_index_id)
    spec = CFG.models.embedding.model_copy(update={"model": "baai/bge-m3", "dims": 1024})

    class FailsSecond(FakeEmbed):
        async def embed(self, spec, texts, ctx):
            if self.calls:
                raise ModelCallError("down")
            return await super().embed(spec, texts, ctx)
    assert await K.run_reindex(tenant, run, FailsSecond(), spec, CTX, 1) == "failed"
    async with tenant_tx(tenant) as conn:
        state = await (await conn.execute("select status, error, done, total from kb_reindex_runs where id=%s", (run,))).fetchone()
        await K.retry_reindex(conn, tenant, run)
    assert state["status"] == "failed" and state["error"] == "provider" and state["done"] == 1
    g = FakeEmbed()
    assert await K.run_reindex(tenant, run, g, spec, CTX, 64) == "ready"
    assert sum(len(c) for c in g.calls) == 1, "only the part that was missing"


async def test_only_one_replacement_at_a_time_and_only_for_a_priced_model(tenant):
    _ensure_kb_index(CFG)
    uid = (await _user(tenant))[1]
    async with tenant_tx(tenant) as conn:
        with pytest.raises(K.Refused) as e:
            await K.start_reindex(conn, tenant, "t", uid, "unknown/model", CFG.rag.active_index_id)
        assert e.value.reason == "unknown_model"
        with pytest.raises(K.Refused) as e:
            await K.start_reindex(conn, tenant, "t", uid, CFG.models.embedding.model, CFG.rag.active_index_id)
        assert e.value.reason == "same_model"
        await K.start_reindex(conn, tenant, "t", uid, "baai/bge-m3", CFG.rag.active_index_id)
        with pytest.raises(K.Refused) as e:
            await K.start_reindex(conn, tenant, "t", uid, "openai/text-embedding-3-large", CFG.rag.active_index_id)
        assert e.value.reason == "reindex_running"


async def test_a_document_approved_while_the_new_index_waited_is_caught_up_by_the_sweep(tenant):
    """Otherwise it would silently stop being found after the switch."""
    _ensure_kb_index(CFG)
    await _approved_doc(tenant)
    async with platform_tx() as conn:
        idx = await (await conn.execute("insert into kb_indexes (name, strategy, embedding_model, dims, status) values (%s,'{}','baai/bge-m3',1024,'ready') returning id, name", (f"kb-{uuid.uuid4().hex[:8]}",))).fetchone()
    async with tenant_tx(tenant) as conn:
        jobs = await K.sweep(conn, tenant, CFG.operations.knowledge, idx["name"])
    assert [j[0] for j in jobs] == ["knowledge_index"]
    async with tenant_tx(tenant) as conn:
        assert (await (await conn.execute("select count(*) as n from kb_chunks where index_id=%s", (idx["id"],))).fetchone())["n"] == 1


# ------------------------------------------------------------------ conflicts between documents
async def test_two_passages_on_the_same_thing_with_other_numbers_are_flagged(tenant):
    _ensure_kb_index(CFG)
    a = await _approved_doc(tenant, name="a.pdf", text="A taxa do plano é de 15% ao ano.")
    b = await _approved_doc(tenant, name="b.pdf", text="A taxa do plano é de 18% ao ano.")
    same = await _approved_doc(tenant, name="c.pdf", text="A taxa do plano é de 15% ao ano, como informado.")
    async with tenant_tx(tenant) as conn:
        idx = await (await conn.execute("select id from kb_indexes where name=%s", (CFG.rag.active_index_id,))).fetchone()
        found = await K.find_conflicts(conn, tenant, b, idx["id"], CFG.models.embedding.dims, CFG.rag.conflict_similarity)
        again = await K.find_conflicts(conn, tenant, b, idx["id"], CFG.models.embedding.dims, CFG.rag.conflict_similarity)
        none = await K.find_conflicts(conn, tenant, a, idx["id"], CFG.models.embedding.dims, 1.5)
        rows = await (await conn.execute("select id, other_document_id from kb_conflicts where document_id=%s", (b,))).fetchall()
    assert found == 2 and again == 0 and none == 0
    assert {r["other_document_id"] for r in rows} == {a, same}
    async with tenant_tx(tenant) as conn:
        await K.dismiss_conflict(conn, tenant, rows[0]["id"], (await _user(tenant))[1])
        with pytest.raises(K.Refused):
            await K.dismiss_conflict(conn, tenant, rows[0]["id"], (await _user(tenant))[1])


async def test_the_document_screen_shows_the_conflict_and_lets_a_person_dismiss_it(tenant):
    _ensure_kb_index(CFG)
    await publish(tenant)
    a = await _approved_doc(tenant, name="a.pdf", text="A taxa do plano é de 15% ao ano.")
    b = await _approved_doc(tenant, name="b.pdf", text="A taxa do plano é de 18% ao ano.")
    async with tenant_tx(tenant) as conn:
        idx = await (await conn.execute("select id from kb_indexes where name=%s", (CFG.rag.active_index_id,))).fetchone()
        await K.find_conflicts(conn, tenant, b, idx["id"], CFG.models.embedding.dims, CFG.rag.conflict_similarity)
        kid = (await (await conn.execute("select id from kb_conflicts where document_id=%s", (b,))).fetchone())["id"]
    async with client() as c:
        await signed_in(c, tenant)
        listing = (await c.get("/console/documents")).text
        assert "Pode contradizer outro" in listing
        page = (await c.get(f"/console/documents/{a}")).text
        assert "Pode contradizer outro documento" in page and "18%" in page, "from either side"
        token = await csrf(c, f"/console/documents/{a}")
        r = await c.post(f"/console/documents/conflicts/{kid}/dismiss", data={"csrf": token, "back": f"/console/documents/{a}"})
        assert r.status_code == 303
        assert "Pode contradizer outro documento" not in (await c.get(f"/console/documents/{a}")).text


# ------------------------------------------------------------------ grouping unanswered questions
def test_alike_questions_are_recognized_locally():
    assert question_similarity("qual o valor da parcela do carro?", "valor da parcela de um carro") >= CFG.rag.curation_group_similarity
    assert question_similarity("qual o valor da parcela?", "vocês atendem em Campinas?") < CFG.rag.curation_group_similarity


async def test_alike_questions_are_grouped_answered_together_and_can_be_separated(tenant, monkeypatch):
    _ensure_kb_index(CFG)
    await publish(tenant)
    service = KnowledgeService(None, CFG.models.embedding, CFG.rag, "consortium")
    async with tenant_tx(tenant) as conn:
        await service.report_gap(conn, tenant, "qual o valor da parcela do carro?", None)
        await service.report_gap(conn, tenant, "qual o valor da parcela do carro?", None)
        await service.report_gap(conn, tenant, "valor da parcela de um carro", None)
        await service.report_gap(conn, tenant, "vocês atendem em Campinas?", None)
        rows = await (await conn.execute("select id, question_masked, frequency, cluster_id from curation_queue where tenant_id=%s order by question_masked", (tenant,))).fetchall()
    leader = rows[0]
    assert leader["frequency"] == 2 and rows[1]["cluster_id"] == leader["id"] and rows[2]["cluster_id"] is None
    monkeypatch.setattr(get_settings(), "llm_provider", "openrouter")
    from agentplatform.jobs import app as J

    async def defer(nxt):
        pass
    monkeypatch.setattr(J, "defer_jobs", defer)
    async with client() as c:
        await signed_in(c, tenant)
        page = (await c.get("/console/curation")).text
        assert "Parecidas, respondidas junto (1)" in page and "Perguntaram 3 vezes" in page
        token = await csrf(c, "/console/curation")
        r = await c.post(f"/console/curation/{leader['id']}/faq", data={"csrf": token, "question": leader["question_masked"], "answer": "Depende da tabela."})
        assert r.status_code == 303
    async with tenant_tx(tenant) as conn:
        states = {x["question_masked"]: x["status"] for x in await (await conn.execute("select question_masked, status from curation_queue where tenant_id=%s", (tenant,))).fetchall()}
        variants = {x["question"] for x in await (await conn.execute("select question from faq_variants where tenant_id=%s", (tenant,))).fetchall()}
    assert states["valor da parcela de um carro"] == "resolved" and states["vocês atendem em Campinas?"] == "open"
    assert "valor da parcela de um carro" in variants, "asked that way, it is answered next time"


async def test_a_question_grouped_by_mistake_can_be_separated(tenant):
    service = KnowledgeService(None, CFG.models.embedding, CFG.rag, "consortium")
    async with tenant_tx(tenant) as conn:
        await service.report_gap(conn, tenant, "qual o valor da parcela do carro?", None)
        await service.report_gap(conn, tenant, "valor da parcela de um carro", None)
        lead = await (await conn.execute("select id from curation_queue where tenant_id=%s and cluster_id is null", (tenant,))).fetchone()
        await K.ungroup_question(conn, tenant, lead["id"], "valor da parcela de um carro")
        n = await (await conn.execute("select count(*) as n from curation_queue where tenant_id=%s and cluster_id is null", (tenant,))).fetchone()
    assert n["n"] == 2


# ------------------------------------------------------------------ testing the base
async def test_testing_the_base_shows_what_it_finds_and_records_nothing(tenant, monkeypatch):
    _ensure_kb_index(CFG)
    await publish(tenant)
    await _approved_doc(tenant)
    await _approved_faq(tenant, "Como funciona o pagamento?")
    fake = FakeEmbed()

    class Rt:
        gateway = fake

        async def tenant_for_setup(self, tid):
            from agentplatform.runtime import TenantRuntime
            return TenantRuntime(tid, CFG, None, PKG, None)
    monkeypatch.setattr("agentplatform.runtime.get_runtime", lambda: Rt())
    monkeypatch.setattr("agentplatform.api.console_setup._setup_runtime", Rt().tenant_for_setup)
    async with client() as c:
        await signed_in(c, tenant)
        token = await csrf(c, "/console/knowledge/test")
        faq = await c.post("/console/knowledge/test", data={"csrf": token, "question": "como funciona o pagamento? meu email é a@b.com"})
        assert faq.status_code == 200 and 'data-outcome="faq"' in faq.text and "Como funciona o pagamento?" in faq.text
        async with tenant_tx(tenant) as conn:
            await conn.execute("update faqs set status='retired' where tenant_id=%s", (tenant,))
        chunks = await c.post("/console/knowledge/test", data={"csrf": token, "question": "zebrafina", "min_score": "0.1", "top_k": "2"})
        assert 'data-outcome="chunks"' in chunks.text and "a.pdf" in chunks.text
        async with tenant_tx(tenant) as conn:
            await conn.execute("update kb_documents set status='do_not_use' where tenant_id=%s", (tenant,))
        none = await c.post("/console/knowledge/test", data={"csrf": token, "question": "zebrafina"})
        assert 'data-outcome="no_basis"' in none.text
    assert "a@b.com" not in fake.calls[0][0], "masked before leaving the server"
    async with tenant_tx(tenant) as conn:
        assert (await (await conn.execute("select count(*) as n from curation_queue where tenant_id=%s", (tenant,))).fetchone())["n"] == 0


async def test_a_preset_becomes_a_normal_draft(tenant):
    from agentplatform.core.config.presets import RAG_PRESETS
    await publish(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        token = await csrf(c, "/console/knowledge/test")
        r = await c.post("/console/knowledge/settings", data={"csrf": token, "preset": "strict"})
    d = (await version(tenant, uuid.UUID(r.headers["location"].split("proposta=")[1])))["document"]
    assert d["rag"]["min_score"] == RAG_PRESETS["strict"]["min_score"] and d["rag"]["top_k"] == RAG_PRESETS["strict"]["top_k"]


# ------------------------------------------------------------------ evaluations from the console
async def test_a_fast_evaluation_from_the_console_runs_and_shows_the_result(tenant, monkeypatch):
    from agentplatform.jobs import app as J
    uid = (await _user(tenant, name="A"))[1]
    await publish(tenant)
    async with tenant_tx(tenant) as conn:
        draft_id = await registry.propose(conn, tenant, changed(RAW, top_k=7), package_compliance=PKG.manifest.compliance, created_by=uid)
    queued = []

    async def defer(nxt):
        queued.extend(nxt)
    monkeypatch.setattr(J, "defer_jobs", defer)
    async with client() as c:
        await signed_in(c, tenant, name="B")
        token = await csrf(c, "/console/config")
        assert (await c.post(f"/console/config/{draft_id}/evaluate", data={"csrf": token, "level": "fast"})).status_code == 303
        again = await c.post(f"/console/config/{draft_id}/evaluate", data={"csrf": token, "level": "fast"})
        assert again.status_code == 409 and "Já tem uma avaliação rodando" in again.text
        assert len(queued) == 1 and queued[0][0] == "config_gate"
        status = await G.run_requested(tenant, uuid.UUID(queued[0][1]["request_id"]), None)
        assert status == "passed"
        page = (await c.get("/console/config")).text
    assert 'data-evaluation="passed"' in page and "Barra o que é proibido" in page
    assert (await version(tenant, draft_id))["eval_run_id"] is None, "only the complete one unlocks production"


async def test_a_complete_evaluation_that_passes_unlocks_only_that_draft(tenant, monkeypatch):
    uid = (await _user(tenant))[1]
    await publish(tenant)
    async with tenant_tx(tenant) as conn:
        draft_id = await registry.propose(conn, tenant, changed(RAW, top_k=7), package_compliance=PKG.manifest.compliance, created_by=uid)
        req = await G.request_evaluation(conn, tenant, draft_id, "full", uid)

    async def fake_gate(cfg, pkg, slug, level, gateway=None, **kw):
        return G.GateResult(scores=[], failed=[], cost=Decimal("0.01"), experiment_id=str(uuid.uuid4()))
    monkeypatch.setattr(G, "run_gate", fake_gate)
    assert await G.run_requested(tenant, req, None) == "passed"
    assert (await version(tenant, draft_id))["eval_run_id"] is not None


async def test_a_failed_or_broken_evaluation_never_unlocks(tenant, monkeypatch):
    uid = (await _user(tenant))[1]
    await publish(tenant)
    async with tenant_tx(tenant) as conn:
        draft_id = await registry.propose(conn, tenant, changed(RAW, top_k=7), package_compliance=PKG.manifest.compliance, created_by=uid)
        req = await G.request_evaluation(conn, tenant, draft_id, "full", uid)

    async def failing(cfg, pkg, slug, level, gateway=None, **kw):
        return G.GateResult(scores=[], failed=["scenarios"], experiment_id=str(uuid.uuid4()))
    monkeypatch.setattr(G, "run_gate", failing)
    assert await G.run_requested(tenant, req, None) == "failed"
    assert (await version(tenant, draft_id))["eval_run_id"] is None
    async with tenant_tx(tenant) as conn:
        req = await G.request_evaluation(conn, tenant, draft_id, "full", uid)

    async def broken(cfg, pkg, slug, level, gateway=None, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(G, "run_gate", broken)
    assert await G.run_requested(tenant, req, None) == "error"
    assert (await version(tenant, draft_id))["eval_run_id"] is None


async def test_a_lost_evaluation_is_asked_again_and_a_stuck_one_is_reported(tenant):
    uid = (await _user(tenant))[1]
    v0 = await publish(tenant)
    async with tenant_tx(tenant) as conn:
        req = await G.request_evaluation(conn, tenant, v0, "fast", uid)
        await conn.execute("update eval_requests set created_at=now() - interval '10 minutes' where id=%s", (req,))
        jobs = await G.sweep_requests(conn, tenant, 60)
        assert [j[1]["request_id"] for j in jobs] == [str(req)]
        await conn.execute("update eval_requests set status='running', started_at=now() - interval '2 hours' where id=%s", (req,))
        await G.sweep_requests(conn, tenant, 60)
        st = await (await conn.execute("select status, error from eval_requests where id=%s", (req,))).fetchone()
    assert st["status"] == "error" and st["error"] == "timeout"


# ------------------------------------------------------------------ joining and leaving
async def _invite(c, email=None, name="Nova Pessoa", role="reviewer"):
    email = email or f"novo-{uuid.uuid4().hex[:8]}@example.com"
    token = await csrf(c, "/console/users")
    r = await c.post("/console/users/invite", data={"csrf": token, "name": name, "email": email, "role": role})
    return r, re.search(r'data-invite-link[^>]*|value="(http[^"]+/console/invite/[^"]+)"', r.text)


async def test_someone_joins_through_a_link_that_works_once(tenant):
    await publish(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        email = f"novo-{uuid.uuid4().hex[:8]}@example.com"
        r, m = await _invite(c, email)
        assert r.status_code == 200 and m
        link = m.group(1).split("http://t")[-1].split("/console/invite/")[1] if m.group(1) else None
    assert link
    async with tenant_tx(tenant) as conn:
        row = await (await conn.execute("select token_hash, email_enc from user_invites where tenant_id=%s", (tenant,))).fetchone()
    assert link not in row["token_hash"], "only the hash is stored"
    async with client() as visitor:
        assert (await visitor.get(f"/console/invite/{link}")).status_code == 200
        weak = await visitor.post(f"/console/invite/{link}", data={"password": "curta", "confirm": "curta"})
        assert weak.status_code == 400
        other = await visitor.post(f"/console/invite/{link}", data={"password": "s3nha-forte-123", "confirm": "outra-senha-123"})
        assert other.status_code == 400
        ok = await visitor.post(f"/console/invite/{link}", data={"password": "s3nha-forte-123", "confirm": "s3nha-forte-123"})
        assert ok.status_code == 303
        again = await visitor.post(f"/console/invite/{link}", data={"password": "s3nha-forte-123", "confirm": "s3nha-forte-123"})
        assert again.status_code == 404
        assert (await visitor.get(f"/console/invite/{link}")).status_code == 404
        login = await visitor.post("/console/login", data={"email": email, "password": "s3nha-forte-123"})
        assert login.status_code == 303
        assert (await visitor.get("/console/assistant")).status_code == 403, "joined as what was asked"
        assert (await visitor.get("/console/knowledge/test")).status_code == 200


async def test_an_expired_or_cancelled_invite_does_not_work(tenant):
    await publish(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        _, m = await _invite(c)
        link = m.group(1).split("/console/invite/")[1]
        async with tenant_tx(tenant) as conn:
            await conn.execute("update user_invites set expires_at=now() - interval '1 minute' where tenant_id=%s", (tenant,))
        assert (await c.get(f"/console/invite/{link}")).status_code == 404
        _, m = await _invite(c)
        link = m.group(1).split("/console/invite/")[1]
        async with tenant_tx(tenant) as conn:
            iid = (await (await conn.execute("select id from user_invites where tenant_id=%s and revoked_at is null and expires_at > now()", (tenant,))).fetchone())["id"]
        token = await csrf(c, "/console/users")
        assert (await c.post(f"/console/users/invites/{iid}/revoke", data={"csrf": token})).status_code == 303
        assert (await c.get(f"/console/invite/{link}")).status_code == 404


async def test_an_invite_cannot_make_a_platform_admin_or_reuse_an_email(tenant):
    await publish(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        r, _ = await _invite(c, role="platform_admin")
        assert r.status_code == 400
        existing, _ = await _user(tenant, name="Já existe")
        r, _ = await _invite(c, existing)
        assert r.status_code == 400 and "Já existe um usuário" in r.text


async def test_a_seller_cannot_invite(tenant):
    await publish(tenant)
    async with client() as c:
        await signed_in(c, tenant, role="seller", name="V")
        r = await c.post("/console/users/invite", data={"csrf": "x", "name": "a", "email": "a@example.com", "role": "tenant_owner"})
        assert r.status_code == 403


async def _full_app_client():
    app = create_app(on_persisted=None)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_a_deactivated_person_is_out_at_once_even_with_an_open_session(tenant):
    await publish(tenant)
    email, uid = await _user(tenant, role="reviewer", name="Sai")
    async with await _full_app_client() as gone, await _full_app_client() as owner:
        await gone.post("/console/login", data={"email": email, "password": PW})
        assert (await gone.get("/console/knowledge/test")).status_code == 200
        await signed_in(owner, tenant, name="Dona")
        token = await csrf(owner, "/console/users")
        assert (await owner.post(f"/console/users/{uid}/deactivate", data={"csrf": token})).status_code == 303
        r = await gone.get("/console/knowledge/test")
        assert r.status_code == 303 and r.headers["location"] == "/console/login"
        assert (await gone.post("/console/login", data={"email": email, "password": PW})).status_code == 401


async def test_a_role_change_ends_the_old_session(tenant):
    await publish(tenant)
    email, uid = await _user(tenant, role="tenant_owner", name="Era dona")
    async with await _full_app_client() as person, await _full_app_client() as owner:
        await person.post("/console/login", data={"email": email, "password": PW})
        assert (await person.get("/console/assistant")).status_code == 200
        await signed_in(owner, tenant, name="Dona")
        token = await csrf(owner, "/console/users")
        assert (await owner.post(f"/console/users/{uid}/role", data={"csrf": token, "role": "seller"})).status_code == 303
        assert (await person.get("/console/assistant")).status_code == 303


async def test_the_last_owner_cannot_be_removed_and_nobody_removes_themselves(tenant):
    await publish(tenant)
    async with client() as c:
        me = await signed_in(c, tenant)
        token = await csrf(c, "/console/users")
        r = await c.post(f"/console/users/{me}/deactivate", data={"csrf": token})
        assert r.status_code == 400 and "sua própria conta" in r.text
        r = await c.post(f"/console/users/{me}/role", data={"csrf": token, "role": "seller"})
        assert r.status_code == 400 and "único dono" in r.text


async def test_people_from_another_tenant_are_out_of_reach(tenant, db):
    await publish(tenant)
    other = uuid.uuid4()
    async with platform_tx() as conn:
        await conn.execute("insert into tenants (id, slug, display_name, segment_package, segment_package_version) values (%s,%s,'x','consortium','0.1.0')", (other, f"o-{other.hex[:6]}"))
    _, stranger = await _user(other, name="De fora")
    async with client() as c:
        await signed_in(c, tenant)
        token = await csrf(c, "/console/users")
        assert (await c.post(f"/console/users/{stranger}/deactivate", data={"csrf": token})).status_code == 404
        assert (await c.post(f"/console/users/{stranger}/role", data={"csrf": token, "role": "seller"})).status_code == 404


async def test_a_name_cannot_inject_a_script_in_the_users_screen(tenant):
    await publish(tenant)
    await _user(tenant, role="seller", name="O'Brien');alert(1)//")
    async with client() as c:
        await signed_in(c, tenant)
        page = (await c.get("/console/users")).text
    assert "alert(1)//')" not in page and "this.dataset.name" in page and "confirm('Desativar O" not in page


# ------------------------------------------------------------------ preparing before the first activation
async def test_prompts_budget_and_preparation_work_with_only_a_draft_in_production(tenant, monkeypatch):
    from agentplatform.runtime import Runtime, reset_scoped_runtime, scoped_runtime
    async with tenant_tx(tenant) as conn:
        await registry.publish(conn, tenant, load_yaml(ROOT / "config/tenants/prod-consortium.example.yaml"), package_compliance=PKG.manifest.compliance)
    monkeypatch.setattr(get_settings(), "app_env", "prod")
    token = scoped_runtime(Runtime(adapters={"instagram": FakeChannelAdapter()}, gateway=None))
    try:
        async with client() as c:
            await signed_in(c, tenant)
            for path in ("/console/prompts", "/console/budget", "/console/setup", "/console/assistant", "/console/assistant/rules"):
                assert (await c.get(path)).status_code == 200, path
    finally:
        reset_scoped_runtime(token)


async def test_a_tenant_with_no_configuration_gets_a_sentence_not_an_error(tenant, monkeypatch):
    monkeypatch.setattr(get_settings(), "app_env", "prod")
    from agentplatform.runtime import Runtime, reset_scoped_runtime, scoped_runtime
    token = scoped_runtime(Runtime(adapters={"instagram": FakeChannelAdapter()}, gateway=None))
    try:
        async with await _full_app_client() as c:
            await signed_in(c, tenant)
            r = await c.get("/console/assistant")
            assert r.status_code == 409 and "ainda não tem configuração" in r.text
            assert (await c.get("/console/setup")).status_code == 200
    finally:
        reset_scoped_runtime(token)


async def test_the_preparation_list_reads_the_real_state(tenant, monkeypatch):
    _ensure_kb_index(CFG)
    await publish(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        page = (await c.get("/console/setup")).text
    state = dict(re.findall(r'data-setup="(\w+)" data-state="(\w+)"', page))
    assert state["material"] == "todo" and state["active"] == "done"
    await _approved_doc(tenant)
    await _user(tenant, name="Segunda")
    async with client() as c:
        await signed_in(c, tenant, name="Terceira")
        state = dict(re.findall(r'data-setup="(\w+)" data-state="(\w+)"', (await c.get("/console/setup")).text))
    assert state["material"] == "done" and state["second_person"] == "done"


# ------------------------------------------------------------------ why it said that
async def test_a_person_can_read_why_the_assistant_said_that(tenant):
    await publish(tenant)
    faq = await _approved_faq(tenant, "Como funciona?")
    conv = await inbound(tenant, ["como funciona?"])

    class Search:
        async def retrieve(self, conn, tenant_id, q, ctx, degraded=False):
            from agentplatform.core.agent.state import RetrievalResult
            return RetrievalResult(faq_answer="Assim.", faq_id=str(faq))
    g = ScriptedGateway({"analyze_turn": [analysis(intents=["product_question"], stage="educando", evidence=["como funciona"], questions=["como funciona?"])],
                         "respond": [draft("Funciona assim.")], "output_judge": [{"violations": [], "explanation": ""}]})
    res = await TurnRunner(TurnDeps(config=CFG, package=PKG, gateway=g, knowledge=Search(), now=NOON)).run(tenant, conv["id"])
    assert res.status in ("committed", "handoff"), res
    async with client() as c:
        await signed_in(c, tenant)
        page = (await c.get(f"/console/conversations/{conv['id']}")).text
    assert "Por que respondeu isso" in page and 'data-explain-source="faq"' in page and "Como funciona?" in page and CFG.models.respond.prompt_version in page


async def test_an_answer_without_basis_says_so(tenant):
    await publish(tenant)
    conv = await inbound(tenant, ["tem algo sobre zebrafina?"])

    class Search:
        async def retrieve(self, conn, tenant_id, q, ctx, degraded=False):
            from agentplatform.core.agent.state import RetrievalResult
            return RetrievalResult(no_basis=True)
    g = ScriptedGateway({"analyze_turn": [analysis(intents=["product_question"], stage="educando", evidence=["zebrafina"], questions=["zebrafina?"])],
                         "respond": [draft("Vou confirmar com o vendedor, combinado?")], "output_judge": [{"violations": [], "explanation": ""}]})
    await TurnRunner(TurnDeps(config=CFG, package=PKG, gateway=g, knowledge=Search(), now=NOON)).run(tenant, conv["id"])
    async with client() as c:
        await signed_in(c, tenant)
        page = (await c.get(f"/console/conversations/{conv['id']}")).text
    assert 'data-explain-source="no_basis"' in page


# ------------------------------------------------------------------ the first message says what it is
async def test_a_first_reply_that_forgets_to_identify_itself_is_completed_by_code(tenant):
    """Found by the evaluation: the prompt asks, and a real model does not always do it."""
    conv = await inbound(tenant, ["oi"])
    a = FakeChannelAdapter()
    g = ScriptedGateway({"analyze_turn": [analysis(stage="novo")], "respond": [draft("Você pensa em imóvel ou veículo?")]})
    await TurnRunner(TurnDeps(config=CFG, package=PKG, gateway=g, knowledge=None, now=NOON)).run(tenant, conv["id"])
    await Dispatcher({"instagram": a}).dispatch_conversation(tenant, conv["id"])
    assert "assistente virtual" in a.texts()[0] and a.texts()[0].endswith("Você pensa em imóvel ou veículo?")


async def test_a_first_reply_that_already_says_it_stays_as_it_is(tenant):
    conv = await inbound(tenant, ["oi"])
    a = FakeChannelAdapter()
    reply = "Oi! Sou o assistente virtual do consultor. Você pensa em imóvel ou veículo?"
    g = ScriptedGateway({"analyze_turn": [analysis(stage="novo")], "respond": [draft(reply)]})
    await TurnRunner(TurnDeps(config=CFG, package=PKG, gateway=g, knowledge=None, now=NOON)).run(tenant, conv["id"])
    await Dispatcher({"instagram": a}).dispatch_conversation(tenant, conv["id"])
    assert a.texts() == [reply]


def test_the_disclosure_goes_right_after_a_greeting():
    from agentplatform.core.agent.guards import ensure_identified
    say = lambda t: ensure_identified(t, "Aqui é o assistente virtual de Ana.", ["assistente"], CFG.conversation.greeting_pattern)
    assert say("Oi! Consórcio é uma compra em grupo.") == "Oi! Aqui é o assistente virtual de Ana. Consórcio é uma compra em grupo."
    assert say("Boa tarde, tudo bem?") == "Boa tarde, Aqui é o assistente virtual de Ana. tudo bem?"
    assert say("Você pensa em imóvel?") == "Aqui é o assistente virtual de Ana. Você pensa em imóvel?"
    assert say("Oi!") == "Oi! Aqui é o assistente virtual de Ana."
    assert say("Sou o assistente da Ana.") == "Sou o assistente da Ana."


async def test_the_second_reply_does_not_repeat_it(tenant):
    ig = f"IG-{uuid.uuid4().hex[:8]}"
    conv = await inbound(tenant, ["oi"], ig)
    a = FakeChannelAdapter()
    d = Dispatcher({"instagram": a})
    g = ScriptedGateway({"analyze_turn": [analysis(stage="novo"), analysis(stage="novo")], "respond": [draft("Oi! Sou o assistente virtual. Tudo bem?"), draft("Certo. Você pensa em imóvel ou veículo?")]})
    runner = TurnRunner(TurnDeps(config=CFG, package=PKG, gateway=g, knowledge=None, now=NOON))
    await runner.run(tenant, conv["id"])
    await d.dispatch_conversation(tenant, conv["id"])
    await inbound(tenant, ["tudo"], ig)
    await runner.run(tenant, conv["id"])
    await d.dispatch_conversation(tenant, conv["id"])
    assert a.texts()[-1] == "Certo. Você pensa em imóvel ou veículo?"


# ------------------------------------------------------------------ monitoring reads only what someone should hear about
async def test_budget_alerts_and_series_leave_out_per_turn_scopes_and_past_days(tenant):
    from agentplatform.core.ops.observability import collect_metrics, evaluate_alerts
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    async with tenant_tx(tenant) as conn:
        for scope in (f"turn:{uuid.uuid4()}", f"conversation:{uuid.uuid4()}", "day:2020-01-01", f"day:{today}"):
            await conn.execute("insert into budget_balances (tenant_id, scope, cap_usd, spent_usd) values (%s,%s,1,1)", (tenant, scope))
        alerts = await evaluate_alerts(conn, tenant, CFG.operations.alerts, [1.0])
    keys = [a.key for a in alerts if a.key.startswith("budget:")]
    assert len(keys) == 1 and f"day:{today}" in keys[0]
    async with platform_tx() as conn:
        body = (await collect_metrics(conn, {"agent_spool_bytes": 0.0})).decode()
    mine = [line for line in body.splitlines() if line.startswith("agent_budget_used_ratio") and str(tenant) in line]
    assert len(mine) == 1 and f"day:{today}" in mine[0]


async def test_the_disk_is_watched(db):
    from agentplatform.core.ops.observability import disk_used_ratio, evaluate_platform_alerts
    assert 0 <= disk_used_ratio() <= 1
    limits = CFG.operations.alerts
    async with platform_tx() as conn:
        full = await evaluate_platform_alerts(conn, limits, 0, [1.0], disk_used=0.91)
        fine = await evaluate_platform_alerts(conn, limits, 0, [1.0], disk_used=0.5)
    assert "disk" in [a.key for a in full] and "disk" not in [a.key for a in fine]


async def test_finished_turns_do_not_leave_a_budget_row_forever(tenant):
    from agentplatform.core.lgpd.service import retention_sweep
    old = f"turn:{uuid.uuid4()}"
    async with tenant_tx(tenant) as conn:
        await conn.execute("insert into budget_balances (tenant_id, scope, cap_usd, spent_usd) values (%s,%s,1,0.1)", (tenant, old))
        await conn.execute("insert into budget_balances (tenant_id, scope, cap_usd, spent_usd) values (%s,'conversation:x',1,0.1)", (tenant,))
        await retention_sweep(conn, CFG, tenant)
        left = {r["scope"] for r in await (await conn.execute("select scope from budget_balances where tenant_id=%s", (tenant,))).fetchall()}
    assert old not in left and "conversation:x" in left, "a conversation's cap is cumulative"


# ------------------------------------------------------------------ public code: its development defaults must not be able to run in production
@pytest.mark.parametrize("name", ["CONSOLE_SESSION_SECRET", "KEK_B64", "HASH_PEPPER_B64", "META_APP_SECRET"])
def test_production_refuses_to_start_without_a_secret_or_with_an_empty_one(monkeypatch, name):
    from pydantic import ValidationError

    from agentplatform.settings import Settings
    good = {"CONSOLE_SESSION_SECRET": "a" * 32, "KEK_B64": "a" * 44, "HASH_PEPPER_B64": "a" * 44, "META_APP_SECRET": "a" * 32}
    for k, v in good.items():
        monkeypatch.setenv(k, v)
    base = {"app_env": "prod", "llm_provider": "openrouter", "ops_alert_email": "ops@x.test"}
    assert Settings(**base).app_env == "prod"
    monkeypatch.setenv(name, "   ")
    with pytest.raises(ValidationError, match=name):
        Settings(**base)


# ------------------------------------------------------------------ what belongs to an account, not to the package
def test_the_persona_mentions_the_company_only_when_the_account_has_one():
    assert "consultor(a) Empresa X." in PKG.manifest.persona_text("Ana", "Empresa X")
    assert "consultor(a)." in PKG.manifest.persona_text("Ana")


async def test_an_account_can_allow_its_own_site_and_only_that(tenant):
    from agentplatform.core.config.schema import ConversationConfig
    await publish(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        token = await csrf(c, "/console/assistant")
        bad = await c.post("/console/assistant", data={"csrf": token, **_assistant_form(allowed_urls="http://inseguro.example.com")})
        assert bad.status_code == 400
        r = await c.post("/console/assistant", data={"csrf": token, **_assistant_form(company_name="Empresa X", allowed_urls="https://www.exemplo.com.br")})
    d = (await version(tenant, uuid.UUID(r.headers["location"].split("proposta=")[1])))["document"]
    assert d["conversation"]["persona"]["company_name"] == "Empresa X" and d["conversation"]["allowed_urls"] == ["https://www.exemplo.com.br"]
    conv = await inbound(tenant, ["tem site?"])
    cfg = CFG.model_copy(update={"conversation": ConversationConfig.model_validate({**json.loads(CFG.conversation.model_dump_json()), "allowed_urls": ["https://www.exemplo.com.br"]})})
    g = ScriptedGateway({"analyze_turn": [analysis(stage="novo")], "respond": [draft("Sou o assistente virtual. O site é https://www.exemplo.com.br")]})
    res = await TurnRunner(TurnDeps(config=cfg, package=PKG, gateway=g, knowledge=None, now=NOON)).run(tenant, conv["id"])
    assert res.status == "committed"


def test_an_account_can_forbid_its_own_files(tmp_path):
    (tmp_path / "Lista de Clientes (1).pdf").write_bytes(b"%PDF-1.4 x")
    load, skipped = K.material_to_load(tmp_path, [K.forbidden_name_hash("lista de clientes.pdf")])
    assert not load and skipped[0]["result"] == "never_import"


def test_a_forbidden_file_is_recognized_however_it_was_downloaded():
    h = [K.forbidden_name_hash("arquivo proibido de teste.pdf")]
    for name in ("Arquivo Proibido de Teste.pdf", "ARQUIVO PROIBIDO DE TESTE (2).pdf", "C:\\Users\\x\\arquivo proibido de teste.pdf"):
        assert K.is_forbidden_file(name, h), name
    assert not K.is_forbidden_file("arquivo proibido de teste versao 2.pdf", h)


# ------------------------------------------------------------------ prompts see the new options
def test_the_new_prompt_uses_name_tone_and_instructions():
    from agentplatform.core.agent.prompts import render
    msgs = render("respond@v9", nonce="n", persona="Você é o assistente de Ana.", seller_name="Ana", first_message=False, disclose=True, stage="novo", stage_goal="x",
                  guidelines=[], next_question=None, fix_reasons="guidance_quoted", profile="-", require_with={}, reply_examples=[{"customer": "oi", "reply": "Oi!"}],
                  summary="-", recent="-", origin=[], turn_input="- oi", knowledge="no_basis", tool_results="(nenhum)", assistant_name="Bia",
                  tone_instruction="Tom mais formal.", business_instructions="Atendo por videochamada.")
    system = msgs[0]["content"]
    assert "você é Bia" in system and "Tom mais formal." in system and "Atendo por videochamada." in system
    assert "nunca valem mais que as regras" in system and "guidance_only: diga com as suas palavras" in system


def test_older_prompts_still_render_with_the_new_variables():
    from agentplatform.core.agent.prompts import render
    assert render("respond@v8", nonce="n", persona="p", seller_name="A", first_message=False, disclose=True, stage="novo", stage_goal="x", guidelines=[], next_question=None,
                  fix_reasons=None, profile="-", require_with={}, reply_examples=[], summary="-", recent="-", origin=[], turn_input="- oi", knowledge="no_basis",
                  tool_results="(nenhum)", assistant_name="Bia", tone_instruction="x", business_instructions="y")


@pytest.mark.parametrize("limits, unlocks", [({}, True), ({"repeats": 1}, False), ({"max_scenarios": 10}, False)])
async def test_only_a_complete_evaluation_unlocks_activation(db, monkeypatch, limits, unlocks):
    """The command line used to run 10 of the scenarios by default, and that passed as approval for production."""
    from agentplatform.lab import gate as G

    async def no_spend(cfg, pkg, slug, gateway, res, max_scenarios, repeats, verbose, output):
        return None
    monkeypatch.setattr(G, "_live_levels", no_spend)
    res = await G.run_gate(RAW, PKG, "t", "full", object(), **limits)
    assert res.passed, "the scores themselves are fine"
    async with platform_tx() as conn:
        exp = await (await conn.execute("select variants from experiments where id=%s", (res.experiment_id,))).fetchone()
    assert exp["variants"]["passed"] is unlocks and res.partial is not unlocks

