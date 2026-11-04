"""Material managed by a person (ADR-021): nothing is used before approval, and no text leaves the server before it. All material here is
synthetic and the provider is a fake: indexing through the direct key is refused on purpose."""
import json
import uuid
from datetime import date, timedelta

import pytest

from agentplatform.cli import _ensure_kb_index
from agentplatform.core.config.registry import load_yaml, registry, resolve_with_package
from agentplatform.core.config.schema import KnowledgeConfig
from agentplatform.core.db.pool import platform_tx, tenant_tx
from agentplatform.core.knowledge import admin as K
from agentplatform.core.knowledge import ingest
from agentplatform.core.knowledge.ingest import DocMeta
from agentplatform.core.knowledge.service import KnowledgeService
from agentplatform.core.models.gateway import BudgetDenied, CallContext, ModelCallError
from agentplatform.core.packages.contract import load_package
from agentplatform.settings import get_settings
from tests.integration.helpers import make_conversation, make_pdf
from tests.integration.test_console_admin import PW, ROOT, _client, _user
from tests.integration.test_dm_scope import _csrf, _publish

pytestmark = pytest.mark.db
PKG = load_package("consortium")
CFG = resolve_with_package(load_yaml(ROOT / "config/tenants/dev-consortium.yaml"), PKG.manifest.compliance)
KC = CFG.operations.knowledge
SPEC = CFG.models.embedding
INDEX = CFG.rag.active_index_id
CTX = CallContext(None, "knowledge", purpose="ingest")
INDEX = CFG.rag.active_index_id
TEXT = "\n".join(f"Regra {i} sobre a palavra zebrafina: o cliente recebe a orientacao completa e detalhada sobre o procedimento {i}." for i in range(1, 14))
PDF = make_pdf([TEXT])


class FakeGateway:
    def __init__(self, fail_after=None, error=None):
        self.calls, self.fail_after, self.error = [], fail_after, error

    async def embed(self, spec, texts, ctx):
        if self.error is not None and (self.fail_after is None or len(self.calls) >= self.fail_after):
            raise self.error
        self.calls.append((ctx.purpose, list(texts)))
        return [[0.1] * spec.dims for _ in texts]


@pytest.fixture
def env(monkeypatch):
    """Production provider, queue captured. The gateway the screens would call is never reached here: jobs run with a fake."""
    from agentplatform.jobs import app as J
    _ensure_kb_index(CFG)
    monkeypatch.setattr(get_settings(), "llm_provider", "openrouter")
    queued = []

    async def defer(nxt):
        queued.extend(nxt)
    monkeypatch.setattr(J, "defer_jobs", defer)
    return queued


async def owner(tenant, role="tenant_owner"):
    await _publish(tenant)
    email, uid = await _user(tenant, role=role, name="Dona")
    return email, uid


def upload(c, csrf, data=PDF, name="politica.pdf", title="Política de teste"):
    return c.post("/console/documents/upload", data={"csrf": csrf, "title": title}, files={"file": (name, data, "application/pdf")})


async def doc(tenant, did):
    async with tenant_tx(tenant) as conn:
        return await (await conn.execute("select * from kb_documents where id=%s", (did,))).fetchone()


async def chunks(tenant, did):
    async with tenant_tx(tenant) as conn:
        return await (await conn.execute("select id, text, embedding, excluded_at from kb_chunks where document_id=%s order by ordinal", (did,))).fetchall()


async def found(tenant):
    """What the search would hand to a reply."""
    service = KnowledgeService(None, SPEC, CFG.rag, "consortium")
    async with tenant_tx(tenant) as conn:
        return await service._hybrid(conn, "zebrafina", [0.1] * SPEC.dims)


async def direct_upload(tenant, data=PDF, name="politica.pdf", cfg=KC):
    async with tenant_tx(tenant) as conn:
        return await K.accept_upload(conn, tenant, None, name, "Documento", data, cfg, PKG.manifest.never_import_name_hashes, "consortium")


async def read(tenant, did, cfg=KC):
    await K.extract_document(tenant, did, cfg, INDEX)


async def ready(tenant, **approval):
    """Uploaded, read and approved; waiting for its vectors."""
    did = await direct_upload(tenant, data=make_pdf([TEXT + "\nMarca " + "".join(chr(97 + int(c, 16)) for c in uuid.uuid4().hex)]))      # letters: digits can read as a phone
    await read(tenant, did)
    async with tenant_tx(tenant) as conn:
        await K.request_approval(conn, tenant, did, None, K.Approval("Documento", approval.get("visibility", "guidance_only"), approval.get("audience", "internal"), supersedes=approval.get("supersedes", "")))
    return did


# ------------------------------------------------------------------ the whole path
async def test_a_document_reaches_the_search_only_after_a_person_approves(tenant, env):
    email, uid = await owner(tenant)
    async with _client() as c:
        await c.post("/console/login", data={"email": email, "password": PW})
        csrf = await _csrf(c, "/console/documents")
        r = await upload(c, csrf)
        assert r.status_code == 303
        did = uuid.UUID(r.headers["location"].rsplit("/", 1)[1])
        assert env[-1][0] == "knowledge_extract" and env[-1][1] == {"tenant_id": str(tenant), "document_id": str(did)}, "job arguments carry IDs only"
        async with tenant_tx(tenant) as conn:
            stored = await (await conn.execute("select content_enc from kb_uploads where document_id=%s", (did,))).fetchone()
        assert b"zebrafina" not in bytes(stored["content_enc"]) and b"%PDF" not in bytes(stored["content_enc"]), "the file is encrypted while it waits"
        assert "Lendo o arquivo" in (await c.get(f"/console/documents/{did}")).text

        await read(tenant, did)
        d, cs = await doc(tenant, did), await chunks(tenant, did)
        assert d["status"] == "in_review" and d["processing"] == "done" and d["pages"] == 1 and cs
        assert all(x["embedding"] is None for x in cs), "no text leaves the server before approval"
        async with tenant_tx(tenant) as conn:
            assert not await (await conn.execute("select 1 from kb_uploads where document_id=%s", (did,))).fetchone(), "the file is not kept after reading"
        assert await found(tenant) == []
        page = await c.get(f"/console/documents/{did}")
        assert "zebrafina" in page.text and "Aprovar para uso" in page.text

        form = {"csrf": csrf, "title": "Política revisada", "version_label": "nov/2026"}
        assert (await c.post(f"/console/documents/{did}/approve", data=form)).status_code == 400, "the use has to be chosen"
        wrong = await c.post(f"/console/documents/{did}/approve", data={**form, "visibility": "citable", "audience": "internal"})
        assert wrong.status_code == 400 and "Material interno" in wrong.text
        ok = await c.post(f"/console/documents/{did}/approve", data={**form, "visibility": "citable", "audience": "customer"})
        assert ok.status_code == 303 and env[-1][0] == "knowledge_index"
        assert await found(tenant) == [], "still not in use: its vectors do not exist yet"

        fake = FakeGateway()
        await K.index_document(tenant, did, fake, SPEC, CTX, KC.embed_batch_size, INDEX)
        d = await doc(tenant, did)
        assert d["status"] == "approved" and d["approved_by"] and d["approved_at"] and d["visibility"] == "citable" and d["version_label"] == "nov/2026"
        assert all(x["embedding"] is not None for x in await chunks(tenant, did))
        assert fake.calls[0][0] == "ingest"
        result = await found(tenant)
        assert result and result[0].visibility == "citable"
        assert "Política revisada" in (await c.get("/console/documents")).text
    async with tenant_tx(tenant) as conn:
        actions = {r["action"] for r in await (await conn.execute("select action from audit_log where action like 'kb.%%'")).fetchall()}
    assert {"kb.document_upload", "kb.document_approve"} <= actions


async def test_a_passage_can_be_taken_out_and_put_back(tenant, env):
    email, _ = await owner(tenant)
    did = await ready(tenant)
    await K.index_document(tenant, did, FakeGateway(), SPEC, CTX, KC.embed_batch_size, INDEX)
    all_chunks = await chunks(tenant, did)
    before = len(await found(tenant))
    async with _client() as c:
        await c.post("/console/login", data={"email": email, "password": PW})
        csrf = await _csrf(c, f"/console/documents/{did}")
        url = f"/console/documents/{did}/chunks/{all_chunks[0]['id']}"
        assert (await c.post(url, data={"csrf": csrf, "excluded": "1"})).status_code == 303
        assert all_chunks[0]["id"] not in [uuid.UUID(x.id) for x in await found(tenant)] and len(await found(tenant)) <= before
        assert "fora de uso" in (await c.get(f"/console/documents/{did}")).text
        assert (await c.post(url, data={"csrf": csrf, "excluded": "0"})).status_code == 303
    assert all_chunks[0]["id"] in [uuid.UUID(x.id) for x in await found(tenant)]
    assert env == [], "it already had its vector: nothing to pay for"


async def test_passage_taken_out_before_approval_gets_its_vector_when_it_comes_back(tenant, env):
    did = await ready(tenant)
    cs = await chunks(tenant, did)
    async with tenant_tx(tenant) as conn:
        await K.set_chunk_excluded(conn, tenant, did, cs[0]["id"], uuid.uuid4(), True)
    fake = FakeGateway()
    await K.index_document(tenant, did, fake, SPEC, CTX, KC.embed_batch_size, INDEX)
    assert cs[0]["text"] not in fake.calls[0][1], "an excluded passage never goes to the provider"
    async with tenant_tx(tenant) as conn:
        assert await K.set_chunk_excluded(conn, tenant, did, cs[0]["id"], uuid.uuid4(), False) is True
    await K.index_document(tenant, did, fake, SPEC, CTX, KC.embed_batch_size, INDEX)
    assert (await doc(tenant, did))["status"] == "approved" and all(x["embedding"] is not None for x in await chunks(tenant, did))


async def test_retired_document_leaves_the_search_at_once_and_coming_back_is_free(tenant, env):
    did = await ready(tenant)
    await K.index_document(tenant, did, FakeGateway(), SPEC, CTX, KC.embed_batch_size, INDEX)
    assert await found(tenant)
    async with tenant_tx(tenant) as conn:
        await K.retire_document(conn, tenant, did)
    assert await found(tenant) == []
    async with tenant_tx(tenant) as conn:
        await K.request_approval(conn, tenant, did, None, K.Approval("Documento", "guidance_only", "internal"))
    fake = FakeGateway()
    await K.index_document(tenant, did, fake, SPEC, CTX, KC.embed_batch_size, INDEX)
    assert fake.calls == [] and await found(tenant)


async def test_retiring_during_indexing_wins(tenant, env):
    did = await ready(tenant)

    class RetiresMeanwhile(FakeGateway):
        async def embed(self, spec, texts, ctx):
            async with tenant_tx(tenant) as conn:
                await K.retire_document(conn, tenant, did)
            return await super().embed(spec, texts, ctx)
    await K.index_document(tenant, did, RetiresMeanwhile(), SPEC, CTX, KC.embed_batch_size, INDEX)
    assert (await doc(tenant, did))["status"] == "do_not_use"


async def test_a_new_version_replaces_the_old_one_only_when_ready(tenant, env):
    old = await ready(tenant)
    await K.index_document(tenant, old, FakeGateway(), SPEC, CTX, KC.embed_batch_size, INDEX)
    new = await ready(tenant, supersedes=str(old))
    assert (await doc(tenant, old))["status"] == "approved", "the old one keeps answering meanwhile"
    await K.index_document(tenant, new, FakeGateway(), SPEC, CTX, KC.embed_batch_size, INDEX)
    assert (await doc(tenant, old))["status"] == "superseded" and (await doc(tenant, new))["status"] == "approved"


async def test_expired_document_is_refused_and_future_end_is_kept(tenant, env):
    did = await direct_upload(tenant)
    await read(tenant, did)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    with pytest.raises(K.Refused) as e:
        async with tenant_tx(tenant) as conn:
            await K.request_approval(conn, tenant, did, None, K.Approval("x", "guidance_only", "internal", effective_to=yesterday))
    assert e.value.reason == "expired"
    with pytest.raises(K.Refused) as e:
        async with tenant_tx(tenant) as conn:
            await K.request_approval(conn, tenant, did, None, K.Approval("x", "guidance_only", "internal", effective_to="31/12/2030"))
    assert e.value.reason == "bad_date"


# ------------------------------------------------------------------ what never comes in
@pytest.mark.parametrize("data,name,cfg,reason", [
    (b"nao sou um pdf", "politica.pdf", KC, "not_pdf"),
    (PDF, "grande.pdf", KnowledgeConfig(max_upload_bytes=10), "too_big"),
    (PDF, "Arquivo Proibido de Teste.pdf", KC, "never_import"),
    (PDF, "arquivo proibido de teste (1).pdf", KC, "never_import"),
    (PDF, "C:\\Users\\x\\Arquivo Proibido de Teste.pdf", KC, "never_import"),
])
async def test_refused_files(tenant, data, name, cfg, reason):
    with pytest.raises(K.Refused) as e:
        await direct_upload(tenant, data=data, name=name, cfg=cfg)
    assert e.value.reason == reason


async def test_a_file_with_a_similar_name_is_not_caught_by_the_ban(tenant):
    assert await direct_upload(tenant, name="Guia - Arquivo Proibido  (1).pdf")


def test_material_comes_in_with_its_inventory_or_as_internal_guidance_and_never_if_forbidden(tmp_path):
    for name in ("listado.pdf", "solto.pdf", "Arquivo Proibido de Teste.pdf"):
        (tmp_path / name).write_bytes(b"%PDF-1.4 x")
    (tmp_path / "notas.txt").write_text("nao e pdf")
    (tmp_path / "knowledge_inventory.yaml").write_text(
        "documents:\n  - {file: listado.pdf, title: Listado, status: in_review, visibility: citable, audience: customer}\n"
        "  - {file: sumiu.pdf, title: Sumiu, status: in_review, visibility: citable, audience: customer}\n", encoding="utf-8")
    load, skipped = K.material_to_load(tmp_path, PKG.manifest.never_import_name_hashes)
    metas = {p.name: m for p, m in load}
    assert set(metas) == {"listado.pdf", "solto.pdf"}
    assert metas["listado.pdf"].visibility == "citable" and metas["solto.pdf"].visibility == "guidance_only" and metas["solto.pdf"].audience == "internal"
    assert {s["result"] for s in skipped} == {"never_import", "missing"}


def test_the_inventory_can_forbid_a_file_by_the_hash_of_its_name(tmp_path):
    (tmp_path / "Lista Interna (1).pdf").write_bytes(b"%PDF-1.4 x")
    (tmp_path / "knowledge_inventory.yaml").write_text(f"never_import_name_hashes: [{K.forbidden_name_hash('lista interna.pdf')}]\n", encoding="utf-8")
    load, skipped = K.material_to_load(tmp_path, [])
    assert not load and skipped == [{"file": "Lista Interna (1).pdf", "result": "never_import"}]


def test_a_missing_material_folder_is_not_a_crash(tmp_path):
    assert K.material_to_load(tmp_path / "nao-existe", []) == ([], [])


def test_the_material_folder_works_without_an_inventory(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4 x")
    load, skipped = K.material_to_load(tmp_path, [])
    assert [p.name for p, _ in load] == ["a.pdf"] and not skipped


async def test_same_file_twice_is_refused_unless_the_first_failed(tenant):
    did = await direct_upload(tenant)
    with pytest.raises(K.Refused) as e:
        await direct_upload(tenant, name="outro-nome.pdf")
    assert e.value.reason == "duplicate" and e.value.detail == "politica.pdf"
    async with tenant_tx(tenant) as conn:
        await K._fail(conn, tenant, did, "unreadable")
    assert await direct_upload(tenant, name="outro-nome.pdf")


async def test_direct_key_never_approves(tenant, env, monkeypatch):
    email, _ = await owner(tenant)
    did = await direct_upload(tenant)
    await read(tenant, did)
    monkeypatch.setattr(get_settings(), "llm_provider", "openai")
    async with _client() as c:
        await c.post("/console/login", data={"email": email, "password": PW})
        csrf = await _csrf(c, "/console/documents")
        assert "chave de testes" in (await c.get("/console/documents")).text
        r = await c.post(f"/console/documents/{did}/approve", data={"csrf": csrf, "visibility": "guidance_only", "audience": "internal"})
    assert r.status_code == 400 and "chave de testes" in r.text and env == []
    assert (await doc(tenant, did))["processing"] == "done" and not (await doc(tenant, did))["approved_by"]


async def test_personal_data_blocks_and_nothing_of_the_text_is_kept(tenant, env):
    email, _ = await owner(tenant)
    did = await direct_upload(tenant, data=make_pdf([TEXT + "\nContato do cliente: maria.silva@example.com\nCPF 529.982.247-25"]))
    await read(tenant, did)
    d = await doc(tenant, did)
    assert d["status"] == "blocked_pii" and d["pii_scan"]["blocking"] == {"email": 1, "cpf": 1}
    assert await chunks(tenant, did) == [] and "maria" not in json.dumps(d["pii_scan"])
    async with tenant_tx(tenant) as conn:
        assert not await (await conn.execute("select 1 from kb_uploads where document_id=%s", (did,))).fetchone()
    async with _client() as c:
        await c.post("/console/login", data={"email": email, "password": PW})
        page = await c.get(f"/console/documents/{did}")
        assert "Bloqueado" in page.text and "Aprovar para uso" not in page.text and "maria" not in page.text
        r = await c.post(f"/console/documents/{did}/approve", data={"csrf": await _csrf(c, "/console/documents"), "visibility": "guidance_only", "audience": "internal"})
    assert r.status_code == 400


async def test_company_contacts_can_be_replaced_when_a_person_says_so(tenant, env):
    email, _ = await owner(tenant)
    data = make_pdf([TEXT + "\nCentral de atendimento: contato@empresa.example ou (11) 3333-4444"])
    plain = await direct_upload(tenant, data=data, name="com-contato.pdf")
    await read(tenant, plain)
    assert (await doc(tenant, plain))["status"] == "blocked_pii"
    with pytest.raises(K.Refused) as e:
        await direct_upload(tenant, data=data, name="de-novo.pdf")
    assert e.value.reason == "blocked_again"
    async with _client() as c:
        await c.post("/console/login", data={"email": email, "password": PW})
        csrf = await _csrf(c, "/console/documents")
        r = await c.post("/console/documents/upload", data={"csrf": csrf, "mask_identifiers": "1"}, files={"file": ("com-contato.pdf", data, "application/pdf")})
        assert r.status_code == 303
        masked = uuid.UUID(r.headers["location"].rsplit("/", 1)[1])
        await read(tenant, masked)
        d = await doc(tenant, masked)
        assert d["status"] == "in_review" and d["pii_scan"]["blocking"] == {} and set(d["pii_scan"]["masked"]) == {"email", "phone"}
        text = " ".join(x["text"] for x in await chunks(tenant, masked))
        assert "contato@empresa" not in text and "3333-4444" not in text and "[EMAIL]" in text and "zebrafina" in text
        assert "Substituí por marcadores" in (await c.get(f"/console/documents/{masked}")).text
    async with tenant_tx(tenant) as conn:
        audit = await (await conn.execute("select after from audit_log where action='kb.document_upload' and target=%s", (str(masked),))).fetchone()
    assert audit["after"]["mask_identifiers"] is True, "who said so stays on record"


async def test_a_cpf_stays_blocked_even_if_a_person_asks_to_replace(tenant):
    did = await direct_upload(tenant, data=make_pdf([TEXT + "\nCliente com CPF 529.982.247-25 e contato@empresa.example"]))
    async with tenant_tx(tenant) as conn:
        await conn.execute("update kb_documents set mask_identifiers = true where id=%s", (did,))
    await read(tenant, did)
    assert (await doc(tenant, did))["status"] == "blocked_pii" and await chunks(tenant, did) == []


# ------------------------------------------------------------------ reading
async def test_broken_and_oversized_pdfs_fail_with_a_reason(tenant):
    broken = await direct_upload(tenant, data=b"%PDF-1.4 isto nao e um pdf de verdade")
    await read(tenant, broken)
    d = await doc(tenant, broken)
    assert d["processing"] == "failed" and d["processing_error"] == "unreadable"
    big = await direct_upload(tenant, data=make_pdf(["a", "b", "c"]), name="grande.pdf")
    await read(tenant, big, KnowledgeConfig(max_pages=2))
    assert (await doc(tenant, big))["processing_error"] == "too_many_pages"


async def test_scanned_pages_are_read_by_ocr_and_marked_for_review(tenant, monkeypatch):
    seen = []

    async def ocr(png):
        seen.append(png[:4])
        return TEXT.replace("zebrafina", "lidaporocr")
    monkeypatch.setattr(ingest, "ocr_image", ocr)
    did = await direct_upload(tenant, data=make_pdf([TEXT, "", ""]))
    await read(tenant, did, KnowledgeConfig(max_ocr_pages=1))
    d = await doc(tenant, did)
    assert seen == [b"\x89PNG"], "one page rendered, within the limit"
    assert d["ocr"]["ocr_pages"] == [2] and d["ocr"]["skipped_pages"] == [3] and d["status"] == "in_review"
    assert any("lidaporocr" in c["text"] for c in await chunks(tenant, did))


async def test_without_tesseract_the_document_still_comes_in_and_says_so(tenant, monkeypatch):
    async def none(png):
        return None
    monkeypatch.setattr(ingest, "ocr_image", none)
    did = await direct_upload(tenant, data=make_pdf([TEXT, ""]))
    await read(tenant, did)
    assert (await doc(tenant, did))["ocr"]["ocr_unavailable"] is True


async def test_passages_a_person_must_see_are_flagged(tenant):
    assert K.chunk_flags("A comissão aqui é um valor fictício usado só neste teste de contrato.", CFG) == ["forbidden_phrase"]
    assert K.chunk_flags("Texto comum sobre como funciona o atendimento, sem nada de especial nele.", CFG) == []
    assert "address" in K.chunk_flags("Atendimento na Rua das Flores, 123 em horario comercial.", CFG)


# ------------------------------------------------------------------ failures while indexing
@pytest.mark.parametrize("error,code", [(BudgetDenied("day"), "budget"), (ModelCallError("down"), "provider")])
async def test_failure_leaves_it_out_of_use_with_a_reason_and_can_be_approved_again(tenant, error, code):
    did = await ready(tenant)
    await K.index_document(tenant, did, FakeGateway(error=error), SPEC, CTX, KC.embed_batch_size, INDEX)
    d = await doc(tenant, did)
    assert d["status"] == "in_review" and d["processing"] == "failed" and d["processing_error"] == code and d["approved_by"] is None
    assert await found(tenant) == []
    async with tenant_tx(tenant) as conn:
        await K.request_approval(conn, tenant, did, None, K.Approval("x", "guidance_only", "internal"))
    await K.index_document(tenant, did, FakeGateway(), SPEC, CTX, KC.embed_batch_size, INDEX)
    assert (await doc(tenant, did))["status"] == "approved"


async def test_a_retry_only_pays_for_what_is_missing(tenant):
    did = await ready(tenant)
    first = FakeGateway(fail_after=1, error=ModelCallError("down"))
    await K.index_document(tenant, did, first, SPEC, CTX, 1, INDEX)
    assert len(first.calls) == 1
    total = len(await chunks(tenant, did))
    async with tenant_tx(tenant) as conn:
        await K.request_approval(conn, tenant, did, None, K.Approval("x", "guidance_only", "internal"))
    second = FakeGateway()
    await K.index_document(tenant, did, second, SPEC, CTX, 1, INDEX)
    assert len(second.calls) == total - 1


async def test_lost_jobs_are_asked_for_again_and_then_given_up(tenant):
    reading = await direct_upload(tenant)
    indexing = await ready(tenant)
    async with tenant_tx(tenant) as conn:
        faq = await K.save_faq(conn, tenant, uuid.uuid4(), None, "Pergunta?", "Resposta.", [], "", True)
        assert await K.sweep(conn, tenant, KC) == [], "not stale yet"
        await conn.execute("update kb_documents set processing_at = now() - interval '1 hour' where tenant_id=%s", (tenant,))
        await conn.execute("update faqs set processing_at = now() - interval '1 hour' where tenant_id=%s", (tenant,))
        nxt = await K.sweep(conn, tenant, KC)
    assert {(n[0], n[1].get("document_id") or n[1].get("faq_id")) for n in nxt} == {("knowledge_extract", str(reading)), ("knowledge_index", str(indexing)), ("knowledge_index_faq", str(faq))}
    async with tenant_tx(tenant) as conn:
        await conn.execute("update kb_documents set processing_at = now() - interval '1 hour', processing_attempts = 99 where tenant_id=%s", (tenant,))
        await conn.execute("update faqs set processing_at = now() - interval '1 hour', processing_attempts = 99 where tenant_id=%s", (tenant,))
        assert await K.sweep(conn, tenant, KC) == []
        assert not await (await conn.execute("select 1 from kb_uploads where document_id=%s", (reading,))).fetchone()
    assert (await doc(tenant, reading))["processing_error"] == "timeout" and (await doc(tenant, indexing))["processing"] == "failed"


# ------------------------------------------------------------------ who can do what
async def test_seller_has_no_access(tenant, env):
    email, _ = await owner(tenant, role="seller")
    async with _client() as c:
        await c.post("/console/login", data={"email": email, "password": PW})
        for path in ("/console/documents", "/console/faqs", "/console/curation"):
            assert (await c.get(path)).status_code == 403
        csrf = await _csrf(c, "/console/posts")
        assert (await upload(c, csrf)).status_code == 403


async def test_document_of_another_tenant_cannot_be_seen_or_changed(tenant, env):
    other = uuid.uuid4()
    async with platform_tx() as conn:
        await conn.execute("insert into tenants (id, slug, display_name, segment_package, segment_package_version) values (%s,%s,'o','consortium','0.1.0')", (other, f"o-{other.hex[:8]}"))
    foreign = await direct_upload(other)
    email, _ = await owner(tenant)
    async with _client() as c:
        await c.post("/console/login", data={"email": email, "password": PW})
        csrf = await _csrf(c, "/console/documents")
        assert (await c.get(f"/console/documents/{foreign}")).status_code == 404
        assert (await c.post(f"/console/documents/{foreign}/retire", data={"csrf": csrf})).status_code == 404
    assert (await doc(other, foreign))["status"] == "draft"
    assert await direct_upload(tenant), "the same file in another tenant is not a duplicate"


async def test_package_document_is_read_only(tenant, env):
    async with platform_tx() as conn:
        src = await (await conn.execute("insert into kb_sources (scope, package, title, kind) values ('package','consortium','Do pacote','pdf') returning id")).fetchone()
        d = await (await conn.execute(
            "insert into kb_documents (source_id, file_name, file_sha256, status, visibility, audience) values (%s,'pacote.pdf',%s,'in_review','guidance_only','internal') returning id",
            (src["id"], uuid.uuid4().hex))).fetchone()
    email, _ = await owner(tenant)
    async with _client() as c:
        await c.post("/console/login", data={"email": email, "password": PW})
        page = await c.get(f"/console/documents/{d['id']}")
        assert "só leitura" in page.text and "Aprovar para uso" not in page.text and "Não usar este documento" not in page.text
        csrf = await _csrf(c, "/console/documents")
        assert (await c.post(f"/console/documents/{d['id']}/retire", data={"csrf": csrf})).status_code == 404


# ------------------------------------------------------------------ FAQs
async def faq_found(tenant, question="como funciona?"):
    service = KnowledgeService(None, SPEC, CFG.rag, "consortium")
    async with tenant_tx(tenant) as conn:
        row = await service._match_faq(conn, question, [0.1] * SPEC.dims)
    return row["answer"] if row else None


async def test_faq_is_used_only_after_approval_and_changing_it_takes_it_out(tenant, env):
    email, _ = await owner(tenant)
    async with _client() as c:
        await c.post("/console/login", data={"email": email, "password": PW})
        csrf = await _csrf(c, "/console/faqs?new=1")
        r = await c.post("/console/faqs", data={"csrf": csrf, "question": "Como funciona?", "answer": "Assim funciona.", "variants": "como que é\n\nme explica", "action": "save"})
        assert r.status_code == 303 and env == []
        assert await faq_found(tenant) is None, "a draft is not used"
        async with tenant_tx(tenant) as conn:
            row = await (await conn.execute("select id from faqs where tenant_id=%s", (tenant,))).fetchone()
            questions = {v["question"] for v in await (await conn.execute("select question from faq_variants where faq_id=%s", (row["id"],))).fetchall()}
        fid = row["id"]
        assert questions == {"Como funciona?", "como que é", "me explica"}
        form = {"csrf": csrf, "faq_id": str(fid), "question": "Como funciona?", "answer": "Assim funciona.", "variants": "como que é\nme explica", "action": "approve"}
        assert (await c.post("/console/faqs", data=form)).status_code == 303 and env[-1][0] == "knowledge_index_faq"
        assert await faq_found(tenant) is None, "not before its vectors exist"
        fake = FakeGateway()
        await K.index_faq(tenant, fid, fake, SPEC, CTX, INDEX)
        assert await faq_found(tenant) == "Assim funciona." and len(fake.calls[0][1]) == 3

        await c.post("/console/faqs", data={**form, "answer": "Agora é diferente.", "action": "save"})
        assert await faq_found(tenant) is None, "nobody reads an answer no one approved"
        await c.post("/console/faqs", data={**form, "answer": "Agora é diferente.", "action": "approve"})
        again = FakeGateway()
        await K.index_faq(tenant, fid, again, SPEC, CTX, INDEX)
        assert await faq_found(tenant) == "Agora é diferente." and again.calls == [], "same questions: nothing to pay for"
        assert "Agora é diferente." in (await c.get(f"/console/faqs?edit={fid}")).text

        assert (await c.post(f"/console/faqs/{fid}/retire", data={"csrf": csrf})).status_code == 303
        assert await faq_found(tenant) is None


async def test_faq_past_its_date_stops_being_used(tenant):
    async with tenant_tx(tenant) as conn:
        fid = await K.save_faq(conn, tenant, uuid.uuid4(), None, "Como funciona?", "Assim.", [], (date.today() + timedelta(days=1)).isoformat(), True)
    await K.index_faq(tenant, fid, FakeGateway(), SPEC, CTX, INDEX)
    assert await faq_found(tenant) == "Assim."
    async with tenant_tx(tenant) as conn:
        await conn.execute("update faqs set valid_to = current_date - 1 where id=%s", (fid,))
    assert await faq_found(tenant) is None


async def test_faq_text_goes_through_the_checks_but_may_have_numbers(tenant, env):
    email, _ = await owner(tenant)
    async with _client() as c:
        await c.post("/console/login", data={"email": email, "password": PW})
        csrf = await _csrf(c, "/console/faqs?new=1")
        bad = await c.post("/console/faqs", data={"csrf": csrf, "question": "Quanto ganha?", "answer": "A comissão do consultor é boa.", "action": "save"})
        assert bad.status_code == 400 and "proibido" in bad.text and "A comissão do consultor é boa." in bad.text, "what was typed stays"
        assert (await c.post("/console/faqs", data={"csrf": csrf, "question": "", "answer": "", "action": "save"})).status_code == 400
        ok = await c.post("/console/faqs", data={"csrf": csrf, "question": "Qual o prazo?", "answer": "O prazo é de 180 meses.", "variants": "meu cpf 529.982.247-25", "action": "save"})
        assert ok.status_code == 303
    async with tenant_tx(tenant) as conn:
        questions = [v["question"] for v in await (await conn.execute("select question from faq_variants where tenant_id=%s", (tenant,))).fetchall()]
    assert not any("529.982" in q for q in questions)


async def test_faq_failure_says_why(tenant):
    async with tenant_tx(tenant) as conn:
        fid = await K.save_faq(conn, tenant, uuid.uuid4(), None, "Como?", "Assim.", [], "", True)
    await K.index_faq(tenant, fid, FakeGateway(error=BudgetDenied("day")), SPEC, CTX, INDEX)
    async with tenant_tx(tenant) as conn:
        f = await (await conn.execute("select status, processing, processing_error, approved_by from faqs where id=%s", (fid,))).fetchone()
    assert f["status"] == "draft" and f["processing"] == "failed" and f["processing_error"] == "budget" and f["approved_by"] is None


# ------------------------------------------------------------------ curation
async def question(tenant, text="Dá para pagar com cartão?", turn=None):
    service = KnowledgeService(None, SPEC, CFG.rag, "consortium")
    async with tenant_tx(tenant) as conn:
        await service.report_gap(conn, tenant, text, turn)
        return (await (await conn.execute("select id from curation_queue where tenant_id=%s and status='open' order by created_at desc limit 1", (tenant,))).fetchone())["id"]


async def test_answering_a_question_makes_the_next_person_get_it(tenant, env):
    email, _ = await owner(tenant)
    conv, _ = await make_conversation(tenant)
    async with tenant_tx(tenant) as conn:
        turn = await (await conn.execute(
            "insert into turns (tenant_id, conversation_id, run_epoch, input_message_ids, status, lease_id, lease_expires_at) values (%s,%s,1,'{}','committed',gen_random_uuid(),now()) returning id",
            (tenant, conv["id"]))).fetchone()
    item = await question(tenant, turn=turn["id"])
    await question(tenant)
    async with _client() as c:
        await c.post("/console/login", data={"email": email, "password": PW})
        page = await c.get("/console/curation")
        assert "Dá para pagar com cartão?" in page.text and "Perguntaram 2 vezes" in page.text and f"/console/conversations/{conv['id']}" in page.text
        csrf = await _csrf(c, "/console/curation")
        bad = await c.post(f"/console/curation/{item}/faq", data={"csrf": csrf, "question": "Dá para pagar com cartão?", "answer": "A comissão é boa."})
        assert bad.status_code == 400 and "A comissão é boa." in bad.text
        r = await c.post(f"/console/curation/{item}/faq", data={"csrf": csrf, "question": "Dá para pagar com cartão?", "answer": "Sim, com cartão."})
        assert r.status_code == 303 and env[-1][0] == "knowledge_index_faq"
        assert "Dá para pagar com cartão?" not in (await c.get("/console/curation")).text
        again = await c.post(f"/console/curation/{item}/faq", data={"csrf": csrf, "question": "Outra?", "answer": "Outra."})
        assert again.status_code == 400, "already answered"
    async with tenant_tx(tenant) as conn:
        q = await (await conn.execute("select status, resolution from curation_queue where id=%s", (item,))).fetchone()
        assert q["status"] == "resolved" and q["resolution"]["action"] == "faq"
        count = await (await conn.execute("select count(*) as n from faqs where tenant_id=%s", (tenant,))).fetchone()
    assert count["n"] == 1, "the refused retry left nothing behind"
    await K.index_faq(tenant, uuid.UUID(q["resolution"]["faq_id"]), FakeGateway(), SPEC, CTX, INDEX)
    assert await faq_found(tenant) == "Sim, com cartão."


async def test_answering_on_the_test_key_keeps_a_draft(tenant, env, monkeypatch):
    email, _ = await owner(tenant)
    item = await question(tenant)
    monkeypatch.setattr(get_settings(), "llm_provider", "openai")
    async with _client() as c:
        await c.post("/console/login", data={"email": email, "password": PW})
        r = await c.post(f"/console/curation/{item}/faq", data={"csrf": await _csrf(c, "/console/curation"), "question": "Dá?", "answer": "Dá."})
        assert r.status_code == 303 and "rascunho" in (await c.get(r.headers["location"])).text
    assert env == []
    async with tenant_tx(tenant) as conn:
        assert (await (await conn.execute("select status from faqs where tenant_id=%s", (tenant,))).fetchone())["status"] == "draft"


async def test_other_answers_to_a_question(tenant, env):
    email, _ = await owner(tenant)
    async with tenant_tx(tenant) as conn:
        fid = await K.save_faq(conn, tenant, uuid.uuid4(), None, "Como funciona?", "Assim.", [], "", True)
    await K.index_faq(tenant, fid, FakeGateway(), SPEC, CTX, INDEX)
    did = await ready(tenant)
    await K.index_document(tenant, did, FakeGateway(), SPEC, CTX, KC.embed_batch_size, INDEX)
    a, b, d = await question(tenant, "Me explica como é?"), await question(tenant, "Qual a previsão do tempo?"), await question(tenant, "Tem regra disso?")
    async with _client() as c:
        await c.post("/console/login", data={"email": email, "password": PW})
        csrf = await _csrf(c, "/console/curation")
        assert (await c.post(f"/console/curation/{a}/variant", data={"csrf": csrf, "faq_id": str(fid)})).status_code == 303
        assert env[-1][0] == "knowledge_index_faq"
        assert (await c.post(f"/console/curation/{b}/out-of-scope", data={"csrf": csrf})).status_code == 303
        assert (await c.post(f"/console/curation/{d}/document", data={"csrf": csrf, "document_id": str(did)})).status_code == 303
        assert (await c.post(f"/console/curation/{d}/out-of-scope", data={"csrf": csrf})).status_code == 400
    fake = FakeGateway()
    await K.index_faq(tenant, fid, fake, SPEC, CTX, INDEX)
    assert fake.calls[0][1] == ["Me explica como é?"], "only the new way of asking is paid for"
    assert await faq_found(tenant) == "Assim.", "the answer stayed in use meanwhile"
    async with tenant_tx(tenant) as conn:
        rows = {r["id"]: r for r in await (await conn.execute("select id, status, resolution from curation_queue where tenant_id=%s", (tenant,))).fetchall()}
    assert rows[a]["resolution"]["action"] == "variant" and rows[b]["status"] == "out_of_scope" and rows[d]["resolution"]["document_id"] == str(did)


# ------------------------------------------------------------------ bulk load and first activation
async def test_bulk_load_approves_nothing_and_calls_nobody(tenant):
    idx = _ensure_kb_index(CFG)
    meta = dict(title="Doc", visibility="citable", audience="customer", version_label="v1")
    async with tenant_tx(tenant) as conn:
        approved = await K.load_file(conn, tenant, make_pdf([TEXT, "Pagina de comissao que fica de fora " * 8]), DocMeta(file="a.pdf", status="approved", exclude_pages=(2,), **meta), idx, "consortium", KC)
        old = await K.load_file(conn, tenant, make_pdf([TEXT + "\nantigo"]), DocMeta(file="b.pdf", status="do_not_use", **meta), idx, "consortium", KC)
        again = await K.load_file(conn, tenant, make_pdf([TEXT + "\nantigo"]), DocMeta(file="c.pdf", status="approved", **meta), idx, "consortium", KC)
        seeded = await K.seed_faqs(conn, tenant, "consortium", PKG.manifest.faqs_seed)
        assert await K.seed_faqs(conn, tenant, "consortium", PKG.manifest.faqs_seed) == 0
        docs = {r["file_name"]: r for r in await (await conn.execute(
            "select d.file_name, d.status, d.approved_by, d.visibility, (select count(*) from kb_chunks c where c.document_id=d.id) as n, (select count(*) from kb_chunks c where c.document_id=d.id and c.text ilike '%%comissao%%') as commission, (select count(*) from kb_chunks c where c.document_id=d.id and c.embedding is not null) as vectors from kb_documents d where d.tenant_id=%s", (tenant,))).fetchall()}
        faqs = await (await conn.execute("select status from faqs where tenant_id=%s", (tenant,))).fetchall()
    assert approved["result"] == "in_review" and old["result"] == "do_not_use" and again["result"] == "duplicate"
    a = docs["a.pdf"]
    assert a["status"] == "in_review" and a["approved_by"] is None and a["visibility"] == "citable" and a["n"] and a["commission"] == 0 and a["vectors"] == 0
    assert docs["b.pdf"]["n"] == 0
    assert seeded == len(PKG.manifest.faqs_seed) and {f["status"] for f in faqs} == {"draft"}


async def test_account_can_be_prepared_before_its_first_activation(tenant, monkeypatch):
    from agentplatform.core.config.registry import ConfigError
    from agentplatform.runtime import Runtime
    rt = Runtime(adapters={}, gateway=None, inbox=object())
    monkeypatch.setattr(rt.settings, "app_env", "dev")
    slug_free = tenant
    with pytest.raises(ConfigError):       # dev falls back to a file only for a tenant with one; a test tenant has none
        await rt.tenant_for_setup(slug_free)
    async with tenant_tx(tenant) as conn:
        await registry.publish(conn, tenant, load_yaml(ROOT / "config/tenants/dev-consortium.yaml"), package_compliance=PKG.manifest.compliance)
    with pytest.raises(ConfigError):
        await rt.tenant(tenant)
    prepared = await rt.tenant_for_setup(tenant)
    assert prepared.knowledge is None and prepared.config.operations.knowledge.max_pages
