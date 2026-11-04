"""The real provider with invented material (T22 and T23 in the go-live list): approving a document and a FAQ, answering from them, and replacing the
search model. Costs less than a cent. Run explicitly, before going live and whenever the models change:
LLM_PROVIDER=openrouter pytest -m live tests/integration/test_live_openrouter.py -s"""
import uuid

import pytest

from agentplatform.cli import _ensure_kb_index
from agentplatform.core.config.registry import load_yaml, registry, resolve_with_package
from agentplatform.core.config.schema import NodeModel
from agentplatform.core.db.pool import platform_tx, tenant_tx
from agentplatform.core.knowledge import admin as K
from agentplatform.core.knowledge.service import KnowledgeService
from agentplatform.core.models.gateway import CallContext, get_gateway
from agentplatform.core.packages.contract import load_package
from agentplatform.lab import runner as L
from agentplatform.paths import ROOT
from agentplatform.settings import get_settings
from tests.integration.helpers import make_pdf
from tests.integration.test_console_admin import _user
from tests.integration.test_phase3 import _plans_active

pytestmark = [pytest.mark.live, pytest.mark.db]
PKG = load_package("consortium")
RAW = load_yaml(ROOT / "config/tenants/prod-consortium.example.yaml").model_copy(update={"environment": "dev"})
CFG = resolve_with_package(RAW, PKG.manifest.compliance)
SPEC = CFG.models.embedding
INDEX = CFG.rag.active_index_id
KC = CFG.operations.knowledge
CTX = CallContext(None, "knowledge", purpose="ingest")
# Invented on purpose: a word and a number no real material has, so an answer can only come from here
PASSAGE = "Regra da taxa zebrafina: a devolução da taxa zebrafina acontece em 17 dias úteis, contados do pedido feito por escrito ao atendimento. "
TEXT = "\n".join(f"{PASSAGE}Esta orientação vale para o procedimento geral descrito neste documento de teste, parte {'abcdefgh'[i]}." for i in range(8))


@pytest.fixture(autouse=True)
def fresh_gateway():
    """The process-wide gateway's HTTP client is bound to one event loop; each test has its own loop."""
    from agentplatform.core.models import gateway
    if get_settings().llm_provider != "openrouter":
        pytest.skip("needs LLM_PROVIDER=openrouter: the direct key refuses material on purpose")
    gateway._gateway = None
    yield
    gateway._gateway = None


async def _approved_material(tenant, gw):
    _ensure_kb_index(CFG)
    async with tenant_tx(tenant) as conn:
        await registry.publish(conn, tenant, RAW, package_compliance=PKG.manifest.compliance, activate=True)
        did = await K.accept_upload(conn, tenant, None, "politica-zebrafina.pdf", "Política de teste", make_pdf([TEXT]), KC, [], "consortium")
    await K.extract_document(tenant, did, KC, INDEX)
    uid = (await _user(tenant))[1]
    async with tenant_tx(tenant) as conn:
        await K.request_approval(conn, tenant, did, uid, K.Approval("Política de teste", "citable", "customer"))
        faq = await K.save_faq(conn, tenant, uid, None, "Vocês atendem aos sábados?", "Sim, o atendimento de teste funciona aos sábados até o meio-dia.", [], "", True)
    await K.index_document(tenant, did, gw, SPEC, CTX, KC.embed_batch_size, INDEX)
    await K.index_faq(tenant, faq, gw, SPEC, CTX, INDEX)
    return did, faq, uid


async def test_t22_approved_material_gets_vectors_and_answers_a_real_turn(tenant):
    gw = get_gateway()
    did, faq, _ = await _approved_material(tenant, gw)
    async with tenant_tx(tenant) as conn:
        d = await (await conn.execute("select status, processing from kb_documents where id=%s", (did,))).fetchone()
        cs = await (await conn.execute("select count(*) as n, count(embedding) as v from kb_chunks where document_id=%s", (did,))).fetchone()
        f = await (await conn.execute("select status, processing from faqs where id=%s", (faq,))).fetchone()
    assert d["status"] == "approved" and cs["n"] > 0 and cs["v"] == cs["n"], (d, cs)
    assert f["status"] == "approved", f
    service = KnowledgeService(gw, SPEC, CFG.rag, "consortium")
    async with tenant_tx(tenant) as conn:
        chunks = await service.retrieve(conn, tenant, "em quanto tempo a taxa zebrafina é devolvida?", CTX)
        by_faq = await service.retrieve(conn, tenant, "vocês abrem no sábado?", CTX)
    assert chunks.chunks and "zebrafina" in chunks.chunks[0].text, chunks
    print("\nFAQ com outras palavras:", by_faq.faq_id == str(faq), "| trechos:", len(chunks.chunks))
    assert by_faq.faq_id == str(faq), "the reworded question reaches the approved FAQ"

    async with platform_tx() as conn:
        await conn.execute("update tenants set status='active' where id=%s", (tenant,))
        slug = (await (await conn.execute("select slug from tenants where id=%s", (tenant,))).fetchone())["slug"]
    sim = NodeModel(primary=CFG.models.analyze_turn.primary, prompt_version="customer_simulator@v2", max_output_tokens=120)
    scenario = {"id": f"t22-{uuid.uuid4().hex[:4]}", "max_turns": 2, "persona": "-", "goal": "-",
                "script": ["Oi! Em quantos dias vocês devolvem a taxa zebrafina?", "E vocês atendem aos sábados?"]}
    out = await L.run_conversation(slug, scenario, gw, sim, CFG)
    print("\n".join(out["transcript"]), "\ncusto:", out["final"]["cost"], "| bloqueios:", out["final"]["blocks"], "| dono:", out["final"]["ownership"])
    said = " ".join(out["bot_texts"]).lower()
    assert "17" in said, out["transcript"]
    assert "sábado" in said or "sabado" in said, out["transcript"]


async def test_t23_replacing_the_search_model_builds_beside_and_answers_on_the_new_one(tenant):
    gw = get_gateway()
    did, faq, uid = await _approved_material(tenant, gw)
    new_model = "baai/bge-m3"
    async with tenant_tx(tenant) as conn:
        run = await K.start_reindex(conn, tenant, "t", uid, new_model, INDEX)
        row = await (await conn.execute("select to_index from kb_reindex_runs where id=%s", (run,))).fetchone()
        idx = await (await conn.execute("select name, dims from kb_indexes where id=%s", (row["to_index"],))).fetchone()
    new_spec = SPEC.model_copy(update={"model": new_model, "dims": idx["dims"]})
    assert await K.run_reindex(tenant, run, gw, new_spec, CTX, KC.embed_batch_size) == "ready"
    new_rag = CFG.rag.model_copy(update={"active_index_id": idx["name"]})
    async with tenant_tx(tenant) as conn:
        old = await KnowledgeService(gw, SPEC, CFG.rag, "consortium").retrieve(conn, tenant, "em quanto tempo a taxa zebrafina é devolvida?", CTX)
        new = await KnowledgeService(gw, new_spec, new_rag, "consortium").retrieve(conn, tenant, "em quanto tempo a taxa zebrafina é devolvida?", CTX)
    assert old.chunks and new.chunks, "the current index keeps working while the new one is ready"
    assert "zebrafina" in new.chunks[0].text


async def test_the_main_path_shows_the_installment_from_the_table(tenant):
    """The customer's amount, the code's simulation, the model's words. The table is invented, and the number can only come from it."""
    gw = get_gateway()
    async with tenant_tx(tenant) as conn:
        await registry.publish(conn, tenant, RAW, package_compliance=PKG.manifest.compliance, activate=True)
    async with platform_tx() as conn:
        await conn.execute("update tenants set status='active' where id=%s", (tenant,))
        slug = (await (await conn.execute("select slug from tenants where id=%s", (tenant,))).fetchone())["slug"]
    sim = NodeModel(primary=CFG.models.analyze_turn.primary, prompt_version="customer_simulator@v2", max_output_tokens=120)
    scenario = {"id": f"quote-{uuid.uuid4().hex[:4]}", "max_turns": 3, "persona": "-", "goal": "-",
                "script": ["Oi, quero trocar de carro", "uns 90 mil", "e quanto fica a parcela?"]}
    await _plans_active(b"plan_id;goal;credit;installment;reduced_installment\nA090;vehicle;90000;1750;\n")
    try:
        out = await L.run_conversation(slug, scenario, gw, sim, CFG)
    finally:
        await _plans_active(None)
    print("\n".join(out["transcript"]), "\ncusto:", out["final"]["cost"], "| etapa:", out["final"]["stage"], "| bloqueios:", out["final"]["blocks"])
    said = " ".join(out["bot_texts"])
    assert "1.750" in said or "1750" in said, out["transcript"]


async def test_someone_who_wants_to_sell_the_letter_gets_the_installment_and_no_promise(tenant):
    """Asking what they would earn is the seller's to answer: the assistant shows the table and stops there."""
    gw = get_gateway()
    async with tenant_tx(tenant) as conn:
        await registry.publish(conn, tenant, RAW, package_compliance=PKG.manifest.compliance, activate=True)
    async with platform_tx() as conn:
        await conn.execute("update tenants set status='active' where id=%s", (tenant,))
        slug = (await (await conn.execute("select slug from tenants where id=%s", (tenant,))).fetchone())["slug"]
    sim = NodeModel(primary=CFG.models.analyze_turn.primary, prompt_version="customer_simulator@v2", max_output_tokens=120)
    scenario = {"id": f"resale-{uuid.uuid4().hex[:4]}", "max_turns": 3, "persona": "-", "goal": "-",
                "script": ["Oi, quero comprar uma carta pra vender depois", "uns 100 mil", "e quanto eu ganho vendendo ela?"]}
    await _plans_active(b"plan_id;goal;credit;installment;reduced_installment\nA100;vehicle;100000;1750;\n")
    try:
        out = await L.run_conversation(slug, scenario, gw, sim, CFG)
    finally:
        await _plans_active(None)
    print(*out["transcript"], "custo: " + str(out["final"]["cost"]), "dono: " + out["final"]["ownership"], "bloqueios: " + str(out["final"]["blocks"]), sep=chr(10))
    said = " ".join(out["bot_texts"]).lower()
    assert "1.750" in said or "1750" in said, out["transcript"]
    assert not any(w in said for w in ("lucr", "rentab", "garantid")), out["transcript"]
    assert out["final"]["ownership"] == "AWAITING_HUMAN" or out["final"]["handoffs"], "the result of a sale is the seller's to explain"

