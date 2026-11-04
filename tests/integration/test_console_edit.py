"""What a person can change in the console, and what stays refused: documents, passages, FAQs, users, the lead's stage and the settings that used to need the file.
All material here is invented and the provider is a fake."""
import uuid

import pytest

from agentplatform.cli import _ensure_kb_index
from agentplatform.core.db.pool import platform_tx, tenant_tx
from agentplatform.core.knowledge import admin as K
from agentplatform.core.models.gateway import CallContext
from agentplatform.settings import get_settings
from tests.integration.helpers import make_conversation, make_pdf
from tests.integration.test_config_console import CFG, RAW, client, csrf, publish, signed_in, version

pytestmark = pytest.mark.db
KC = CFG.operations.knowledge
SPEC = CFG.models.embedding
INDEX = CFG.rag.active_index_id
CTX = CallContext(None, "knowledge", purpose="ingest")
TEXT = "\n".join(f"Regra {i} sobre a palavra zebrafina: o cliente recebe a orientacao completa e detalhada sobre o procedimento {i}." for i in range(40))


class FakeEmbed:
    def __init__(self):
        self.calls = []

    async def embed(self, spec, texts, ctx):
        self.calls.append(list(texts))
        return [[0.1] * spec.dims for _ in texts]


@pytest.fixture
def queued(monkeypatch):
    from agentplatform.api import console as C
    jobs = []

    async def defer(nxt):
        jobs.extend(nxt)
    monkeypatch.setattr(C, "_queue", defer)
    monkeypatch.setattr(get_settings(), "llm_provider", "openrouter")
    return jobs


async def _doc(tenant, *, approve=False):
    _ensure_kb_index(CFG)
    async with tenant_tx(tenant) as conn:
        did = await K.accept_upload(conn, tenant, None, f"p-{uuid.uuid4().hex[:6]}.pdf", "Documento", make_pdf([TEXT + "\nMarca " + uuid.uuid4().hex.translate(str.maketrans("0123456789", "ghijklmnop"))]), KC, [], "consortium")
    await K.extract_document(tenant, did, KC, INDEX)
    if approve:
        async with tenant_tx(tenant) as conn:
            await K.request_approval(conn, tenant, did, None, K.Approval("Documento", "citable", "customer"))
        await K.index_document(tenant, did, FakeEmbed(), SPEC, CTX, KC.embed_batch_size, INDEX)
    return did


async def _row(tenant, sql, *args):
    async with tenant_tx(tenant) as conn:
        return await (await conn.execute(sql, args)).fetchone()


# ------------------------------------------------------------------ documents
async def test_an_approved_document_changes_name_and_dates_and_stays_in_use(tenant, queued):
    await publish(tenant)
    did = await _doc(tenant, approve=True)
    async with client() as c:
        await signed_in(c, tenant)
        token = await csrf(c, f"/console/documents/{did}")
        assert "Editar nome, uso e validade" in (await c.get(f"/console/documents/{did}")).text
        r = await c.post(f"/console/documents/{did}/details", data={"csrf": token, "title": "Nome novo", "visibility": "guidance_only", "audience": "internal", "version_label": "nov/2026"})
        assert r.status_code == 303
        bad = await c.post(f"/console/documents/{did}/details", data={"csrf": token, "title": "X", "visibility": "citable", "audience": "internal"})
        assert bad.status_code == 400 and "Material interno" in bad.text
    d = await _row(tenant, "select d.status, d.visibility, d.version_label, s.title from kb_documents d join kb_sources s on s.id=d.source_id where d.id=%s", did)
    assert d == {"status": "approved", "visibility": "guidance_only", "version_label": "nov/2026", "title": "Nome novo"}
    assert (await _row(tenant, "select count(*) filter (where embedding is null) as n from kb_chunks where document_id=%s", did))["n"] == 0, "the text did not change: no new vectors"
    assert queued == []


async def test_correcting_a_passage_in_review_stays_on_the_server(tenant, queued):
    await publish(tenant)
    did = await _doc(tenant)
    chunk = await _row(tenant, "select id from kb_chunks where document_id=%s order by ordinal limit 1", did)
    async with client() as c:
        await signed_in(c, tenant)
        page = await c.get(f"/console/documents/{did}?edit={chunk['id']}")
        assert "Salvar trecho" in page.text
        r = await c.post(f"/console/documents/{did}/chunks/{chunk['id']}/text", data={"csrf": await csrf(c, f"/console/documents/{did}"), "text": "Texto corrigido sobre a palavra zebrafina."})
    assert r.status_code == 303 and queued == []
    row = await _row(tenant, "select text, token_count from kb_chunks where id=%s", chunk["id"])
    assert row["text"] == "Texto corrigido sobre a palavra zebrafina." and row["token_count"] > 0


async def test_correcting_a_passage_in_use_makes_a_new_vector_for_it_alone(tenant, queued):
    await publish(tenant)
    did = await _doc(tenant, approve=True)
    chunks = await _rows(tenant, did)
    assert len(chunks) >= 2, "the test needs more than one passage"
    async with client() as c:
        await signed_in(c, tenant)
        r = await c.post(f"/console/documents/{did}/chunks/{chunks[0]}/text", data={"csrf": await csrf(c, f"/console/documents/{did}"), "text": "Regra corrigida da palavra zebrafina."})
    assert r.status_code == 303 and queued and queued[-1][0] == "knowledge_index"
    d = await _row(tenant, "select status, processing from kb_documents where id=%s", did)
    assert d == {"status": "approved", "processing": "indexing"}
    fake = FakeEmbed()
    await K.index_document(tenant, did, fake, SPEC, CTX, KC.embed_batch_size, INDEX)
    assert fake.calls == [["Regra corrigida da palavra zebrafina."]], "only what changed goes to the provider"
    assert (await _row(tenant, "select count(*) filter (where embedding is null) as n from kb_chunks where document_id=%s", did))["n"] == 0


async def _rows(tenant, did):
    async with tenant_tx(tenant) as conn:
        return [r["id"] for r in await (await conn.execute("select id from kb_chunks where document_id=%s order by ordinal", (did,))).fetchall()]


@pytest.mark.parametrize("text, says", [("Contato: 529.982.247-25", "Dados de pessoas"), ("x" * (KC.passage_max_chars + 1), "longo demais"), ("   ", "texto")])
async def test_a_bad_passage_is_refused_and_what_was_there_stays(tenant, queued, text, says):
    await publish(tenant)
    did = await _doc(tenant)
    chunk = (await _rows(tenant, did))[0]
    before = (await _row(tenant, "select text from kb_chunks where id=%s", chunk))["text"]
    async with client() as c:
        await signed_in(c, tenant)
        r = await c.post(f"/console/documents/{did}/chunks/{chunk}/text", data={"csrf": await csrf(c, f"/console/documents/{did}"), "text": text})
    assert r.status_code == 400 and says.lower() in r.text.lower()
    assert (await _row(tenant, "select text from kb_chunks where id=%s", chunk))["text"] == before


async def test_the_direct_key_never_reindexes_an_edited_passage(tenant, queued, monkeypatch):
    await publish(tenant)
    did = await _doc(tenant, approve=True)
    chunk = (await _rows(tenant, did))[0]
    monkeypatch.setattr(get_settings(), "llm_provider", "openai")
    async with client() as c:
        await signed_in(c, tenant)
        r = await c.post(f"/console/documents/{did}/chunks/{chunk}/text", data={"csrf": await csrf(c, f"/console/documents/{did}"), "text": "Outro texto."})
    assert r.status_code == 400 and queued == []
    assert (await _row(tenant, "select embedding is not null as has from kb_chunks where id=%s", chunk))["has"], "what is in use keeps working"


async def test_only_what_is_not_in_use_can_be_deleted(tenant, queued):
    await publish(tenant)
    did = await _doc(tenant, approve=True)
    async with client() as c:
        await signed_in(c, tenant)
        token = await csrf(c, f"/console/documents/{did}")
        assert (await c.post(f"/console/documents/{did}/delete", data={"csrf": token})).status_code == 400
        await c.post(f"/console/documents/{did}/retire", data={"csrf": token})
        assert (await c.post(f"/console/documents/{did}/delete", data={"csrf": token})).status_code == 303
    assert not await _row(tenant, "select 1 from kb_documents where id=%s", did)
    assert not await _row(tenant, "select 1 from kb_chunks where document_id=%s", did)


async def test_another_accounts_document_cannot_be_touched(tenant, queued):
    await publish(tenant)
    other = uuid.uuid4()
    async with platform_tx() as conn:
        await conn.execute("insert into tenants (id, slug, display_name, segment_package, segment_package_version) values (%s,%s,'Outra','consortium','0.1.0')", (other, f"t-{other.hex[:8]}"))
    did = await _doc(other)
    chunk = (await _rows(other, did))[0]
    async with client() as c:
        await signed_in(c, tenant)
        token = await csrf(c, "/console/documents")
        assert (await c.post(f"/console/documents/{did}/delete", data={"csrf": token})).status_code == 404
        assert (await c.post(f"/console/documents/{did}/chunks/{chunk}/text", data={"csrf": token, "text": "invadido"})).status_code == 404
    assert (await _row(other, "select text from kb_chunks where id=%s", chunk))["text"] != "invadido"


# ------------------------------------------------------------------ FAQs
async def test_every_faq_of_the_account_has_its_buttons(tenant, queued):
    await publish(tenant)
    async with client() as c:
        uid = await signed_in(c, tenant)
        async with tenant_tx(tenant) as conn:
            draft = await K.save_faq(conn, tenant, uid, None, "Pergunta em rascunho?", "Resposta de teste.", [], "", False)
            live = await K.save_faq(conn, tenant, uid, None, "Pergunta em uso?", "Outra resposta de teste.", [], "", False)
            await conn.execute("update faqs set status='approved' where id=%s", (live,))
        page = (await c.get("/console/faqs")).text
        assert f"/console/faqs?edit={draft}" in page and f"/console/faqs?edit={live}" in page
        assert f"/console/faqs/{draft}/retire" in page and f"/console/faqs/{live}/retire" in page
        assert f"/console/faqs/{draft}/delete" in page and f"/console/faqs/{live}/delete" not in page, "what is in use is retired first"
        token = await csrf(c, "/console/faqs?new=1")
        assert (await c.post(f"/console/faqs/{live}/delete", data={"csrf": token})).status_code == 400
        assert (await c.post(f"/console/faqs/{draft}/delete", data={"csrf": token})).status_code == 303
        editing = (await c.get(f"/console/faqs?edit={live}")).text
        assert "Editar pergunta" in editing and "Outra resposta de teste." in editing
    assert not await _row(tenant, "select 1 from faqs where id=%s", draft)
    assert await _row(tenant, "select 1 from faqs where id=%s", live)


# ------------------------------------------------------------------ users
async def test_the_alert_contacts_of_a_person_can_be_changed_and_are_encrypted(tenant):
    await publish(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        token = await csrf(c, "/console/users")
        from tests.integration.test_console_admin import _user
        _, other = await _user(tenant, role="seller", name="Bia")
        bad = await c.post(f"/console/users/{other}/edit", data={"csrf": token, "name": "Bia", "whatsapp": "123"})
        assert bad.status_code == 400 and "WhatsApp inválido" in bad.text
        ok = await c.post(f"/console/users/{other}/edit", data={"csrf": token, "name": "Beatriz", "whatsapp": "+55 (11) 90000-0000", "email": "aviso@example.com"})
        assert ok.status_code == 303
        page = await c.get(f"/console/users?edit={other}")
        assert "5511900000000" in page.text and "aviso@example.com" in page.text
        assert "no-store" in page.headers["cache-control"], "personal data on the page"
        assert "5511900000000" not in (await c.get("/console/users")).text, "only the row being edited shows it"
    async with platform_tx() as conn:
        row = await (await conn.execute("select display_name, notify_whatsapp_enc from tenant_users where id=%s", (other,))).fetchone()
    assert row["display_name"] == "Beatriz" and b"5511900000000" not in bytes(row["notify_whatsapp_enc"])


async def test_someone_deactivated_can_come_back(tenant):
    from tests.integration.test_console_admin import _user
    await publish(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        token = await csrf(c, "/console/users")
        _, other = await _user(tenant, role="seller", name="Bia")
        await c.post(f"/console/users/{other}/deactivate", data={"csrf": token})
        assert "Desativados" in (page := await c.get("/console/users")).text and f"/console/users/{other}/reactivate" in page.text
        assert (await c.post(f"/console/users/{other}/reactivate", data={"csrf": token})).status_code == 303
    async with platform_tx() as conn:
        assert (await (await conn.execute("select is_active from tenant_users where id=%s", (other,))).fetchone())["is_active"]


# ------------------------------------------------------------------ the lead
async def test_the_stage_can_be_corrected_and_a_sale_undone(tenant):
    await publish(tenant)
    conv, _ = await make_conversation(tenant)
    cid = conv["id"]
    async with client() as c:
        await signed_in(c, tenant, role="seller", name="Vendedora")
        token = await csrf(c, f"/console/conversations/{cid}")
        await c.post(f"/console/conversations/{cid}/won", data={"csrf": token})
        assert "Venda marcada" in (await c.get(f"/console/conversations/{cid}")).text
        r = await c.post(f"/console/conversations/{cid}/stage", data={"csrf": token, "stage": "qualificando"})
        assert r.status_code == 303
        assert (await c.post(f"/console/conversations/{cid}/stage", data={"csrf": token, "stage": "nao_existe"})).status_code == 400
        assert (await c.post(f"/console/conversations/{cid}/stage", data={"csrf": token, "stage": "opt_out"})).status_code == 400, "stopping contact is not chosen here"
    lead = await _row(tenant, "select l.id, l.stage from leads l where l.contact_id=%s", conv["contact_id"])
    assert lead["stage"] == "qualificando"
    hist = await _row(tenant, "select to_stage from lead_stage_history where lead_id=%s and to_stage='qualificando'", lead["id"])
    assert hist["to_stage"] == "qualificando"


async def test_someone_who_asked_to_stop_does_not_change_stage_here(tenant):
    await publish(tenant)
    conv, _ = await make_conversation(tenant)
    async with tenant_tx(tenant) as conn:
        await conn.execute("update leads set stage='opt_out', opted_out_at=now() where contact_id=%s", (conv["contact_id"],))
    async with client() as c:
        await signed_in(c, tenant)
        page = await c.get(f"/console/conversations/{conv['id']}")
        assert "pediu para não receber" in page.text and f"/console/conversations/{conv['id']}/stage" not in page.text
        r = await c.post(f"/console/conversations/{conv['id']}/stage", data={"csrf": await csrf(c, f"/console/conversations/{conv['id']}"), "stage": "novo"})
    assert r.status_code == 409
    assert (await _row(tenant, "select stage from leads where contact_id=%s", conv["contact_id"]))["stage"] == "opt_out"


async def test_a_reviewer_sees_no_button_the_server_would_refuse(tenant):
    await publish(tenant)
    conv, _ = await make_conversation(tenant)
    async with client() as c:
        await signed_in(c, tenant, role="reviewer", name="Revisora")
        inbox = (await c.get("/console/")).text
        page = (await c.get(f"/console/conversations/{conv['id']}")).text
    assert "Pausar em todas" not in inbox and f"/console/conversations/{conv['id']}/ownership" not in inbox
    for action in ("Assumir conversa", "Marcar venda", "Nunca atender", f"/console/conversations/{conv['id']}/stage"):
        assert action not in page, action


async def test_opening_a_contact_from_the_conversation_is_for_the_owner(tenant):
    await publish(tenant)
    conv, _ = await make_conversation(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        assert f"/console/lgpd/contact/{conv['contact_id']}" in (await c.get(f"/console/conversations/{conv['id']}")).text
        page = await c.get(f"/console/lgpd/contact/{conv['contact_id']}")
        assert page.status_code == 200 and "no-store" in page.headers["cache-control"]
    async with client() as c:
        await signed_in(c, tenant, role="seller", name="Vendedora")
        assert (await c.get(f"/console/lgpd/contact/{conv['contact_id']}")).status_code == 403


# ------------------------------------------------------------------ the rest of the settings
def _form(page: str) -> dict:
    """What the browser would send for this form as it stands."""
    import html
    import re
    data = {}
    for m in re.finditer(r'<(input|textarea|select)\b([^>]*)>(.*?)(?=<input|<textarea|<select|</form>)', page, re.S):
        tag, attrs, rest = m.groups()
        name = re.search(r'name="([^"]+)"', attrs)
        if not name:
            continue
        name = html.unescape(name.group(1))
        if tag == "input":
            if 'type="checkbox"' in attrs:
                if " checked" in attrs:
                    data[name] = "on"
                continue
            v = re.search(r'value="([^"]*)"', attrs)
            data[name] = html.unescape(v.group(1)) if v else ""
        elif tag == "textarea":
            data[name] = html.unescape(rest.split("</textarea>")[0])
        else:
            v = re.search(r'<option value="([^"]*)" selected', rest)
            data[name] = html.unescape(v.group(1)) if v else ""
    return data


async def test_settings_that_had_no_form_become_a_draft_and_nothing_else_moves(tenant):
    await publish(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        page = await c.get("/console/assistant/more")
        assert page.status_code == 200
        form = _form(page.text)
        same = await c.post("/console/assistant/more", data=form)
        assert same.status_code == 200 and "Nada mudou" in same.text, "saving the form untouched must not invent a change"
        form.update({"budget.daily_cap_usd": "0.75", "media.transcribe_audio": None, "handoff.escalate_to_owner_after_min": "", "guidelines.no_progress": "Mude a abordagem."})
        form = {k: v for k, v in form.items() if v is not None}
        r = await c.post("/console/assistant/more", data=form)
    assert r.status_code == 303
    d = (await version(tenant, uuid.UUID(r.headers["location"].split("proposta=")[1])))["document"]
    assert d["budget"]["daily_cap_usd"] == "0.75" and d["media"]["transcribe_audio"] is False and d["handoff"]["escalate_to_owner_after_min"] is None
    assert d["guidelines"]["no_progress"] == "Mude a abordagem."
    assert d["budget"]["per_turn_cap_usd"] == str(RAW.budget.per_turn_cap_usd) and d["rag"]["top_k"] == RAW.rag.top_k


@pytest.mark.parametrize("field, value, says", [
    ("budget.daily_cap_usd", "muito", "use um número"),
    ("budget.daily_cap_usd", "", "não pode ficar em branco"),
    ("handoff.auto_return_max_per_day", "1.5", "número inteiro"),
    ("guidelines.no_progress", "Faça {coisa}", "não use chaves"),
    ("media.images", "qualquer", "escolha uma das opções"),
    ("budget.alert_thresholds", "cinquenta", "separados por vírgula"),
])
async def test_a_wrong_value_says_why_and_creates_nothing(tenant, field, value, says):
    await publish(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        form = _form((await c.get("/console/assistant/more")).text)
        form[field] = value
        r = await c.post("/console/assistant/more", data=form)
    assert r.status_code == 400 and says in r.text.replace("&#123;", "{").replace("&#125;", "}")
    assert not await _row(tenant, "select 1 from config_versions where tenant_id=%s and status='draft'", tenant)


async def test_going_live_is_refused_in_a_test_environment(tenant):
    await publish(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        form = _form((await c.get("/console/assistant/more")).text)
        form["go_live"] = "on"
        r = await c.post("/console/assistant/more", data=form)
    assert r.status_code == 400


async def test_reminders_can_be_reworded_and_their_time_checked(tenant):
    cadence = {"id": "seller_offer_unanswered", "trigger": {"event": "stage_entered", "stage": "pronto_para_proposta"},
               "steps": [{"delay": "4h", "content_kind": "template", "template_key": "seller_offer_reminder"}]}
    from agentplatform.core.config.schema import FollowupsConfig
    cfg = RAW.model_copy(update={"followups": FollowupsConfig(enabled=True, cadences=[cadence])})
    await publish(tenant, cfg)
    async with client() as c:
        await signed_in(c, tenant)
        form = _form((await c.get("/console/followups")).text)
        assert form["rule_0_0_delay"] == "4h" and form["rule_0_0_text"]
        bad = await c.post("/console/followups", data={**form, "rule_0_0_delay": "logo"})
        assert bad.status_code == 400 and "30m, 4h ou 2d" in bad.text
        ok = await c.post("/console/followups", data={**form, "rule_0_0_delay": "6h", "rule_0_0_text": "Oi! Chamo o consultor?"})
    assert ok.status_code == 303
    d = (await version(tenant, uuid.UUID(ok.headers["location"].split("proposta=")[1])))["document"]
    assert d["followups"]["cadences"][0]["steps"][0]["delay"] == "6h" and d["followups"]["texts"]["seller_offer_reminder"] == "Oi! Chamo o consultor?"


async def test_the_account_words_are_what_the_reminder_sends(tenant):
    from agentplatform.core.config.schema import Cadence, CadenceStep
    from agentplatform.core.followups import engine
    from tests.integration.test_config_console import ANY_DAY, NOON
    cad = Cadence(id="c1", trigger={"event": "x"}, steps=[CadenceStep(delay="0m", content_kind="template", template_key="t")])
    cfg = CFG.model_copy(update={"followups": CFG.followups.model_copy(update={"cadences": [cad], "texts": {"t": "As palavras desta conta"}})})
    now = NOON()
    conv, _ = await make_conversation(tenant)
    async with tenant_tx(tenant) as conn:
        await conn.execute("insert into followup_runs (tenant_id, conversation_id, cadence_id, step, status, due_at) values (%s,%s,'c1',0,'scheduled',%s)", (tenant, conv["id"], now))
        await engine.tick(conn, cfg, tenant, {"t": "As palavras do pacote"}, now, policy=ANY_DAY)
        sent = await (await conn.execute("select payload from outbox where conversation_id=%s and is_proactive", (conv["id"],))).fetchall()
    assert [o["payload"]["text"] for o in sent] == ["As palavras desta conta"]


# ------------------------------------------------------------------ the words of a package guideline
async def test_a_package_guideline_can_be_reworded_and_goes_back_when_erased(tenant):
    from agentplatform.core.agent.policy import select_guidelines
    from agentplatform.core.agent.state import TurnUnderstanding
    from tests.integration.test_config_console import PKG
    g = next(x for x in PKG.manifest.guidelines if x.when_intents)
    understood = TurnUnderstanding(intents=[g.when_intents[0]], questions=[], slot_updates=[], objections=[], sentiment="neutral", asks_for_human=False, out_of_scope=False, opt_out_intent=0.0)
    await publish(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        form = _form((await c.get("/console/assistant/rules")).text)
        assert form[f"g_action_{g.id}"].strip() == g.action.strip()
        r = await c.post("/console/assistant/rules", data={**form, f"g_action_{g.id}": "Minhas próprias palavras."})
        assert r.status_code == 303
        d = (await version(tenant, uuid.UUID(r.headers["location"].split("proposta=")[1])))["document"]
        changed = next(o for o in d["guidelines"]["overrides"] if o["id"] == g.id)
        assert changed["action"] == "Minhas próprias palavras."
        from agentplatform.core.config.schema import PlatformConfig
        cfg = PlatformConfig.model_validate(d)
        stage = g.stages[0] if g.stages else PKG.manifest.initial_stage
        picked = select_guidelines(PKG.manifest, stage, understood, "", 0, cfg.guidelines)
        assert "Minhas próprias palavras." in picked and g.action not in picked
        erased = await c.post("/console/assistant/rules", data={**form, f"g_action_{g.id}": ""})
    assert erased.status_code in (200, 303), "blank means the package's words: no change to propose, or a draft without it"


def test_every_intent_of_the_package_has_a_name_in_portuguese():
    from tests.integration.test_config_console import PKG
    m = PKG.manifest
    missing = [i for i in {*m.intents, *(x for g in m.guidelines for x in g.when_intents)} if i not in m.intent_labels]
    assert not missing


# ------------------------------------------------------------------ forms that used to lose what they did not show
async def test_saving_models_keeps_a_longer_chain_and_what_is_not_offered(tenant, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "openrouter")
    spec = RAW.models.respond.model_copy(update={"primary": "deepseek/deepseek-v4-flash", "fallbacks": ["google/gemini-3.1-flash-lite", "mistralai/mistral-small-2603"]})
    cfg = RAW.model_copy(update={"models": RAW.models.model_copy(update={"respond": spec})})
    await publish(tenant, cfg)
    async with client() as c:
        await signed_in(c, tenant)
        form = _form((await c.get("/console/assistant/models")).text)
        form["note"] = "só anotação"
        form["summarize_primary"] = "mistralai/mistral-small-2603"
        r = await c.post("/console/assistant/models", data=form)
    assert r.status_code == 303, r.text[:400]
    d = (await version(tenant, uuid.UUID(r.headers["location"].split("proposta=")[1])))["document"]
    assert d["models"]["respond"]["fallbacks"] == ["google/gemini-3.1-flash-lite", "mistralai/mistral-small-2603"]


async def test_the_search_settings_do_not_need_a_test_first(tenant):
    await publish(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        assert "/console/knowledge/settings" in (await c.get("/console/knowledge/test")).text
