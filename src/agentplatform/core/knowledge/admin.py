"""Material managed by a person (plan 06 §5, ADR-021): upload, review, approval and retirement of documents, FAQs and the curation queue.
Search only reads approved items, so nothing reaches a customer before a person says so. Vectors are made only on approval, the first time
any of the text leaves the server. Network calls happen outside transactions; job arguments carry IDs only."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

import yaml
from psycopg import AsyncConnection

from agentplatform.core.agent.guards import extract_numbers
from agentplatform.core.comments.settings import TextProblem, text_problems
from agentplatform.core.config.schema import KnowledgeConfig, ModelRef, PlatformConfig
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.knowledge.ingest import (
    DocMeta,
    TooManyPages,
    UnreadableDocument,
    chunk_pages,
    read_document,
    scan_pii,
)
from agentplatform.core.knowledge.service import vec_literal
from agentplatform.core.models import pricing
from agentplatform.core.models.gateway import BudgetDenied, CallContext, ModelCallError, ModelGateway
from agentplatform.core.security.keys import decrypt_blob, encrypt_blob
from agentplatform.core.security.pii import mask

log = logging.getLogger(__name__)
Next = list[tuple[str, dict, dict]]

_APPROVABLE = ("in_review", "do_not_use", "superseded")
_FAQ_CHECKED = ("url_not_allowed", "forbidden_phrase", "required_context", "never_request", "pii_placeholder", "pii_in_output",
                "human_impersonation", "empty")      # numbers are fine here: an approved answer is itself a source
_CHUNK_CHECKED = ("forbidden_phrase", "required_context", "never_request")


class Refused(Exception):
    """Something a person asked that cannot be done. `reason` becomes a sentence in the console."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


def parse_id(value: str) -> UUID:
    """An ID that came from a form."""
    try:
        return UUID(value)
    except ValueError:
        raise Refused("not_found") from None


def reindex_job(tenant_id: UUID, run_id: UUID) -> tuple[str, dict, dict]:
    return ("knowledge_reindex", {"tenant_id": str(tenant_id), "run_id": str(run_id)}, {"queueing_lock": f"kbr:{run_id}"})


def extract_job(tenant_id: UUID, document_id: UUID) -> tuple[str, dict, dict]:
    return ("knowledge_extract", {"tenant_id": str(tenant_id), "document_id": str(document_id)}, {"queueing_lock": f"kb:{document_id}"})


def index_job(tenant_id: UUID, document_id: UUID) -> tuple[str, dict, dict]:
    return ("knowledge_index", {"tenant_id": str(tenant_id), "document_id": str(document_id)}, {"queueing_lock": f"kb:{document_id}"})


def faq_job(tenant_id: UUID, faq_id: UUID) -> tuple[str, dict, dict]:
    return ("knowledge_index_faq", {"tenant_id": str(tenant_id), "faq_id": str(faq_id)}, {"queueing_lock": f"kbf:{faq_id}"})


# ---------------------------------------------------------------- reading
def forbidden_name_hash(file_name: str) -> str:
    """A file's name without its folder, case or the " (1)" of a second download. The package keeps only this, so the code can be public without
    saying which file of whom must never come in."""
    name = unicodedata.normalize("NFC", Path(file_name.replace("\\", "/")).name).strip().lower()
    return hashlib.sha256(re.sub(r"\s*\(\d+\)(?=\.[^.]+$)", "", name).encode()).hexdigest()


def is_forbidden_file(file_name: str, name_hashes: list[str]) -> bool:
    """Files the package says must never come in, whatever the scan finds in them."""
    return forbidden_name_hash(file_name) in name_hashes


def material_to_load(folder: Path, name_hashes: list[str]) -> tuple[list[tuple[Path, DocMeta]], list[dict]]:
    """What `agentplatform ingest` brings in for review. An optional knowledge_inventory.yaml beside the files proposes their metadata; it stays with
    the material, out of the code, and can name forbidden files by the hash of their name. A file it does not list arrives as internal guidance, the
    most cautious use. Forbidden files never come in."""
    if not folder.is_dir():
        return [], []
    on_disk = {unicodedata.normalize("NFC", f.name): f for f in folder.iterdir() if f.is_file() and f.suffix.lower() == ".pdf"}
    inventory = folder / "knowledge_inventory.yaml"
    data = (yaml.safe_load(inventory.read_text(encoding="utf-8")) or {}) if inventory.exists() else {}
    listed = data.get("documents", [])
    name_hashes = [*name_hashes, *data.get("never_import_name_hashes", [])]
    known = {unicodedata.normalize("NFC", d["file"]) for d in listed}
    proposed = [*listed, *({"file": name, "title": Path(name).stem, "status": "in_review", "visibility": "guidance_only", "audience": "internal"}
                           for name in sorted(on_disk) if name not in known)]
    load, skipped = [], []
    for d in proposed:
        name = unicodedata.normalize("NFC", d["file"])
        if is_forbidden_file(name, name_hashes):
            skipped.append({"file": name, "result": "never_import"})
        elif name not in on_disk:
            skipped.append({"file": name, "result": "missing"})
        else:
            load.append((on_disk[name], DocMeta(**d)))
    return load, skipped


async def accept_upload(conn: AsyncConnection, tenant_id: UUID, user_id: UUID | None, file_name: str, title: str, data: bytes,
                        cfg: KnowledgeConfig, never_import: list[str], package: str, mask_identifiers: bool = False) -> UUID:
    name = Path(file_name.replace("\\", "/")).name[:200] or "documento.pdf"
    if len(data) > cfg.max_upload_bytes:
        raise Refused("too_big")
    if not data.startswith(b"%PDF-"):
        raise Refused("not_pdf")
    if is_forbidden_file(name, never_import):
        raise Refused("never_import")
    digest = hashlib.sha256(data).hexdigest()
    dup = await (await conn.execute("select file_name, status from kb_documents where file_sha256=%s and processing <> 'failed' limit 1", (digest,))).fetchone()
    if dup and not (dup["status"] == "blocked_pii" and mask_identifiers):
        raise Refused("blocked_again" if dup["status"] == "blocked_pii" else "duplicate", dup["file_name"])
    src = await (await conn.execute(
        "insert into kb_sources (tenant_id, scope, package, title, kind) values (%s,'tenant',%s,%s,'pdf') returning id",
        (tenant_id, package, title.strip()[:200] or Path(name).stem))).fetchone()
    doc = await (await conn.execute(
        """insert into kb_documents (tenant_id, source_id, file_name, file_sha256, status, visibility, audience, created_by, processing, processing_at, mask_identifiers)
           values (%s,%s,%s,%s,'draft','guidance_only','internal',%s,'queued',now(),%s) returning id""",
        (tenant_id, src["id"], name, digest, user_id, mask_identifiers))).fetchone()
    await conn.execute("insert into kb_uploads (document_id, tenant_id, content_enc) values (%s,%s,%s)",
                       (doc["id"], tenant_id, await encrypt_blob(conn, tenant_id, data, "kb_uploads.content")))
    return doc["id"]


async def _fail(conn: AsyncConnection, tenant_id: UUID, document_id: UUID, code: str) -> None:
    await conn.execute(
        """update kb_documents set processing='failed', processing_error=%s,
                  approved_by=case when status='approved' then approved_by end, approved_at=case when status='approved' then approved_at end
           where id=%s and tenant_id=%s""", (code, document_id, tenant_id))
    await conn.execute("delete from kb_uploads where document_id=%s", (document_id,))


async def store_reading(conn: AsyncConnection, tenant_id: UUID, document_id: UUID, index_id: UUID, pages: list[str], info: dict,
                        exclude_pages: tuple[int, ...] = (), keep_status: str | None = None, mask_kinds: list[str] | None = None) -> str:
    """A document with hard identifiers keeps no text at all: the way forward is a clean file, not reviewing personal data here. The one
    exception is a person saying the identifiers are the company's own, and then only for kinds that can be: `mask_kinds`."""
    pii = scan_pii(pages)
    if mask_kinds and pii["blocking"] and set(pii["blocking"]) <= set(mask_kinds):
        pages = [mask(p, only=mask_kinds).text for p in pages]
        pii = {**pii, "blocking": {}, "masked": pii["blocking"]}
    status = "blocked_pii" if pii["blocking"] else (keep_status or "in_review")
    chunks = [] if status in ("blocked_pii", "do_not_use") else chunk_pages(pages, exclude=exclude_pages)
    for c in chunks:
        await conn.execute(
            """insert into kb_chunks (tenant_id, index_id, document_id, ordinal, heading_path, text, token_count, page)
               values (%s,%s,%s,%s,%s,%s,%s,%s)""", (tenant_id, index_id, document_id, c.ordinal, c.heading_path, c.text, c.tokens, c.page))
    await conn.execute(
        """update kb_documents set status=%s, pii_scan=%s, ocr=%s, pages=%s, processing='done', processing_error=null where id=%s""",
        (status, json.dumps(pii), json.dumps(info), len(pages), document_id))
    await conn.execute("delete from kb_uploads where document_id=%s", (document_id,))
    return status


async def extract_document(tenant_id: UUID, document_id: UUID, cfg: KnowledgeConfig, index_name: str) -> None:
    async with tenant_tx(tenant_id) as conn:
        row = await (await conn.execute(
            """select d.processing, d.mask_identifiers, u.content_enc, (select id from kb_indexes where name=%s) as index_id
               from kb_documents d left join kb_uploads u on u.document_id = d.id where d.id=%s and d.tenant_id=%s""",
            (index_name, document_id, tenant_id))).fetchone()
        if not row or row["processing"] != "queued":
            return
        if row["content_enc"] is None or row["index_id"] is None:
            await _fail(conn, tenant_id, document_id, "lost_file" if row["content_enc"] is None else "no_index")
            return
        data = await decrypt_blob(conn, tenant_id, row["content_enc"], "kb_uploads.content")
    failure = None
    try:
        pages, info = await read_document(data, cfg)
    except TooManyPages:
        failure = "too_many_pages"
    except UnreadableDocument as e:
        log.info("document %s unreadable: %s", document_id, e)
        failure = "unreadable"
    async with tenant_tx(tenant_id) as conn:
        now = await (await conn.execute("select processing from kb_documents where id=%s for update", (document_id,))).fetchone()
        if not now or now["processing"] != "queued":      # another run got here first
            return
        if failure:
            await _fail(conn, tenant_id, document_id, failure)
        else:
            await store_reading(conn, tenant_id, document_id, row["index_id"], pages, info,
                                mask_kinds=cfg.maskable_on_request if row["mask_identifiers"] else None)


# ---------------------------------------------------------------- review
def chunk_flags(text: str, cfg: PlatformConfig) -> list[str]:
    """What a reviewer should look at in a passage. Replies still go through the guards: this is so nobody has to find it by reading it all."""
    rules = [p.rule for p in text_problems(cfg, "chunk", text, 10 ** 9, [], checked=_CHUNK_CHECKED)]
    return sorted(set(rules)) + sorted(k for k in mask(text).found if k != "health")


async def set_chunk_excluded(conn: AsyncConnection, tenant_id: UUID, document_id: UUID, chunk_id: UUID, user_id: UUID, excluded: bool) -> bool:
    """True when the passage needs a vector it does not have, because its document is already in use."""
    row = await (await conn.execute(
        """update kb_chunks set excluded_at=case when %(x)s then now() end, excluded_by=case when %(x)s then %(u)s::uuid end
           where id=%(c)s and document_id=%(d)s and tenant_id=%(t)s returning embedding is null as no_vector, ordinal""",
        {"x": excluded, "u": user_id, "c": chunk_id, "d": document_id, "t": tenant_id})).fetchone()
    if not row:
        raise Refused("not_found")
    await conn.execute(      # the same passage in another search index, while the model is being replaced
        """update kb_chunks set excluded_at=case when %(x)s then now() end, excluded_by=case when %(x)s then %(u)s::uuid end
           where document_id=%(d)s and ordinal=%(o)s and id<>%(c)s""",
        {"x": excluded, "u": user_id, "d": document_id, "o": row["ordinal"], "c": chunk_id})
    if excluded or not row["no_vector"]:
        return False
    doc = await (await conn.execute(
        "update kb_documents set processing='indexing', processing_at=now(), processing_attempts=0 where id=%s and status='approved' returning id",
        (document_id,))).fetchone()
    return bool(doc)


@dataclass
class Approval:
    title: str
    visibility: str
    audience: str
    version_label: str = ""
    effective_from: str = ""
    effective_to: str = ""
    supersedes: str = ""


def _day(value: str) -> date | None:
    if not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise Refused("bad_date") from None


async def _copy_chunks(conn: AsyncConnection, tenant_id: UUID, to_index: UUID, document_id: UUID | None = None) -> int:
    """Passages of this tenant that exist in another search index and not in this one, without vectors. A document approved later gets its vector then."""
    cur = await conn.execute(
        """insert into kb_chunks (tenant_id, index_id, document_id, ordinal, heading_path, text, context_prefix, token_count, page, excluded_at, excluded_by)
           select distinct on (c.document_id, c.ordinal) c.tenant_id, %(i)s, c.document_id, c.ordinal, c.heading_path, c.text, c.context_prefix,
                  c.token_count, c.page, c.excluded_at, c.excluded_by
           from kb_chunks c
           where c.tenant_id=%(t)s and c.index_id<>%(i)s and (%(d)s::uuid is null or c.document_id=%(d)s)
             and not exists (select 1 from kb_chunks n where n.index_id=%(i)s and n.document_id=c.document_id and n.ordinal=c.ordinal)""",
        {"t": tenant_id, "i": to_index, "d": document_id})
    return cur.rowcount


async def request_approval(conn: AsyncConnection, tenant_id: UUID, document_id: UUID, user_id: UUID, a: Approval) -> None:
    doc = await (await conn.execute("select * from kb_documents where id=%s and tenant_id=%s for update", (document_id, tenant_id))).fetchone()
    if not doc:
        raise Refused("not_found")
    if doc["status"] not in _APPROVABLE or doc["processing"] in ("queued", "indexing"):
        raise Refused("not_ready")
    if a.visibility not in ("citable", "guidance_only") or a.audience not in ("customer", "internal"):
        raise Refused("missing_choice")
    if a.audience == "internal" and a.visibility == "citable":
        raise Refused("internal_citable")
    start, end = _day(a.effective_from), _day(a.effective_to)
    if end and end < date.today():
        raise Refused("expired")
    usable = await (await conn.execute("select count(*) as n from kb_chunks where document_id=%s and excluded_at is null", (document_id,))).fetchone()
    if not usable["n"]:
        raise Refused("no_text")
    replaced = None
    if a.supersedes:
        old = await (await conn.execute("select id from kb_documents where id=%s and tenant_id=%s and status='approved' and id<>%s",
                                        (parse_id(a.supersedes), tenant_id, document_id))).fetchone()
        if not old:
            raise Refused("not_found")
        replaced = old["id"]
    if a.title.strip():
        await conn.execute("update kb_sources set title=%s where id=%s", (a.title.strip()[:200], doc["source_id"]))
    await conn.execute(
        """update kb_documents set visibility=%s, audience=%s, version_label=%s, effective_from=%s, effective_to=%s, supersedes=%s,
                  approved_by=%s, approved_at=now(), processing='indexing', processing_error=null, processing_at=now(), processing_attempts=0
           where id=%s""",
        (a.visibility, a.audience, a.version_label.strip()[:60] or None, start, end, replaced, user_id, document_id))


async def index_document(tenant_id: UUID, document_id: UUID, gateway: ModelGateway | None, embedding: ModelRef, ctx: CallContext, batch_size: int,
                         index_name: str, conflict_similarity: float = 1.0) -> None:
    async with tenant_tx(tenant_id) as conn:
        doc = await (await conn.execute("select processing from kb_documents where id=%s and tenant_id=%s", (document_id, tenant_id))).fetchone()
        if not doc or doc["processing"] != "indexing":
            return
        idx = await (await conn.execute("select id from kb_indexes where name=%s", (index_name,))).fetchone()
        if not idx:
            await _fail(conn, tenant_id, document_id, "no_index")
            return
        await _copy_chunks(conn, tenant_id, idx["id"], document_id)
        rows = await (await conn.execute(
            "select id, text from kb_chunks where document_id=%s and index_id=%s and embedding is null and excluded_at is null order by ordinal",
            (document_id, idx["id"]))).fetchall()
    failure = None
    try:
        if rows and gateway is None:
            raise ModelCallError("no gateway")
        for i in range(0, len(rows), batch_size):      # each batch stays done: a retry only pays for what is missing
            part = rows[i:i + batch_size]
            vectors = await gateway.embed(embedding, [r["text"] for r in part], ctx)
            async with tenant_tx(tenant_id) as conn:
                for r, v in zip(part, vectors):
                    await conn.execute("update kb_chunks set embedding=%s::vector where id=%s", (vec_literal(v), r["id"]))
    except BudgetDenied:
        failure = "budget"
    except ModelCallError as e:
        log.warning("indexing failed for document %s: %s", document_id, e)
        failure = "provider"
    async with tenant_tx(tenant_id) as conn:
        if failure:
            await _fail(conn, tenant_id, document_id, failure)
            return
        done = await (await conn.execute(      # retired meanwhile: stays retired
            "update kb_documents set status='approved', processing='done', processing_error=null where id=%s and processing='indexing' returning supersedes",
            (document_id,))).fetchone()
        if done and done["supersedes"]:
            await conn.execute("update kb_documents set status='superseded' where id=%s and tenant_id=%s and status='approved'",
                               (done["supersedes"], tenant_id))
        if done:
            await find_conflicts(conn, tenant_id, document_id, idx["id"], embedding.dims, conflict_similarity)


async def find_conflicts(conn: AsyncConnection, tenant_id: UUID, document_id: UUID, index_id: UUID, dims: int, similarity: float) -> int:
    """Passages about the same thing with different numbers, in documents both in use. A person decides which one is right; both stay in use
    until then. Uses the vectors already stored: nothing leaves the server."""
    pairs = await (await conn.execute(
        f"""select c.id as chunk_id, c.text, o.id as other_chunk_id, o.text as other_text, o.document_id as other_document_id,
                   1 - ((c.embedding::vector({dims})) <=> (o.embedding::vector({dims}))) as sim
            from kb_chunks c
            join kb_chunks o on o.index_id = c.index_id and o.document_id <> c.document_id and o.excluded_at is null and o.embedding is not null
            join kb_documents d on d.id = o.document_id and d.status = 'approved'
            where c.document_id=%(doc)s and c.index_id=%(idx)s and c.excluded_at is null and c.embedding is not null
              and 1 - ((c.embedding::vector({dims})) <=> (o.embedding::vector({dims}))) >= %(sim)s""",
        {"doc": document_id, "idx": index_id, "sim": similarity})).fetchall()
    found = 0
    for p in pairs:
        mine, theirs = extract_numbers(p["text"], lenient=True), extract_numbers(p["other_text"], lenient=True)
        if mine - theirs and theirs - mine:      # each says something the other does not; one that only adds a number is not a contradiction
            found += (await conn.execute(
                """insert into kb_conflicts (tenant_id, document_id, chunk_id, other_document_id, other_chunk_id, similarity)
                   values (%s,%s,%s,%s,%s,%s) on conflict (chunk_id, other_chunk_id) do nothing""",
                (tenant_id, document_id, p["chunk_id"], p["other_document_id"], p["other_chunk_id"], p["sim"]))).rowcount
    return found


async def dismiss_conflict(conn: AsyncConnection, tenant_id: UUID, conflict_id: UUID, user_id: UUID) -> None:
    row = await (await conn.execute(
        "update kb_conflicts set status='dismissed', dismissed_by=%s where id=%s and tenant_id=%s and status='open' returning id",
        (user_id, conflict_id, tenant_id))).fetchone()
    if not row:
        raise Refused("not_found")


# ---------------------------------------------------------------- replacing the embedding model
async def start_reindex(conn: AsyncConnection, tenant_id: UUID, slug: str, user_id: UUID, model: str, current_index: str) -> UUID:
    """A new index next to the current one. Customers keep being served from the current one, and a person activates the new one through a normal
    draft once it is complete, so a canary and a rollback keep working."""
    if model not in pricing.EMBEDDING_PER_M or model not in pricing.EMBEDDING_DIMS:
        raise Refused("unknown_model", model)
    src = await (await conn.execute("select id, embedding_model from kb_indexes where name=%s", (current_index,))).fetchone()
    if not src:
        raise Refused("no_index")
    if src["embedding_model"] == model:
        raise Refused("same_model", model)
    if await (await conn.execute("select 1 from kb_reindex_runs where tenant_id=%s and status='running'", (tenant_id,))).fetchone():
        raise Refused("reindex_running")
    dims = pricing.EMBEDDING_DIMS[model]
    name = f"kb-{slug}-{hashlib.sha256(f'{model}:{dims}:{uuid4().hex}'.encode()).hexdigest()[:10]}"
    idx = await (await conn.execute(
        "insert into kb_indexes (name, strategy, embedding_model, dims, status) values (%s,%s,%s,%s,'building') returning id",
        (name, json.dumps({"chunking": "headings_1400c_200o"}), model, dims))).fetchone()
    run = await (await conn.execute(
        """insert into kb_reindex_runs (tenant_id, from_index, to_index, model, dims, created_by) values (%s,%s,%s,%s,%s,%s)
           on conflict (tenant_id) where status = 'running' do nothing returning id""",
        (tenant_id, src["id"], idx["id"], model, dims, user_id))).fetchone()
    if not run:      # two clicks at once: raising undoes the index created above
        raise Refused("reindex_running")
    return run["id"]


async def run_reindex(tenant_id: UUID, run_id: UUID, gateway: ModelGateway | None, embedding: ModelRef, ctx: CallContext, batch_size: int) -> str:
    """Only what is in use goes to the provider, in batches that stay done: a retry pays only for what is missing. Returns the run's status."""
    async with tenant_tx(tenant_id) as conn:
        run = await (await conn.execute("select * from kb_reindex_runs where id=%s and tenant_id=%s", (run_id, tenant_id))).fetchone()
        if not run or run["status"] != "running":
            return run["status"] if run else "missing"
        await _copy_chunks(conn, tenant_id, run["to_index"])
        chunks = await (await conn.execute(
            """select c.id, c.text from kb_chunks c join kb_documents d on d.id = c.document_id
               where c.index_id=%s and c.tenant_id=%s and d.status='approved' and c.embedding is null and c.excluded_at is null order by c.document_id, c.ordinal""",
            (run["to_index"], tenant_id))).fetchall()
        variants = await (await conn.execute(
            """select v.id, v.question as text from faq_variants v join faqs f on f.id = v.faq_id
               where f.tenant_id=%s and f.status='approved'
                 and not exists (select 1 from faq_variant_vectors x where x.variant_id=v.id and x.index_id=%s)""",
            (tenant_id, run["to_index"]))).fetchall()
        await conn.execute("update kb_reindex_runs set total=done+%s, heartbeat_at=now() where id=%s", (len(chunks) + len(variants), run_id))
    failure = None
    try:
        if (chunks or variants) and gateway is None:
            raise ModelCallError("no gateway")
        for rows, is_chunk in ((chunks, True), (variants, False)):
            for i in range(0, len(rows), batch_size):
                part = rows[i:i + batch_size]
                vectors = await gateway.embed(embedding, [r["text"] for r in part], ctx)
                async with tenant_tx(tenant_id) as conn:
                    for r, v in zip(part, vectors):
                        if is_chunk:
                            await conn.execute("update kb_chunks set embedding=%s::vector where id=%s", (vec_literal(v), r["id"]))
                        else:
                            await conn.execute(
                                """insert into faq_variant_vectors (variant_id, index_id, tenant_id, embedding) values (%s,%s,%s,%s::vector)
                                   on conflict (variant_id, index_id) do update set embedding=excluded.embedding""",
                                (r["id"], run["to_index"], tenant_id, vec_literal(v)))
                    await conn.execute("update kb_reindex_runs set done=done+%s, heartbeat_at=now() where id=%s", (len(part), run_id))
    except BudgetDenied:
        failure = "budget"
    except ModelCallError as e:
        log.warning("reindex %s failed: %s", run_id, e)
        failure = "provider"
    async with tenant_tx(tenant_id) as conn:
        if failure:
            await conn.execute("update kb_reindex_runs set status='failed', error=%s, finished_at=now() where id=%s and status='running'", (failure, run_id))
            return "failed"
        await conn.execute("update kb_indexes set status='ready' where id=%s and status='building'", (run["to_index"],))
        await conn.execute("update kb_reindex_runs set status='ready', finished_at=now(), error=null where id=%s and status='running'", (run_id,))
    return "ready"


async def retry_reindex(conn: AsyncConnection, tenant_id: UUID, run_id: UUID) -> None:
    row = await (await conn.execute(
        "update kb_reindex_runs set status='running', error=null, finished_at=null, attempts=attempts+1, heartbeat_at=now() where id=%s and tenant_id=%s and status='failed' returning id",
        (run_id, tenant_id))).fetchone()
    if not row:
        raise Refused("not_found")


async def cancel_reindex(conn: AsyncConnection, tenant_id: UUID, run_id: UUID) -> None:
    row = await (await conn.execute(
        "update kb_reindex_runs set status='cancelled', finished_at=now() where id=%s and tenant_id=%s and status in ('running','failed','ready') and draft_version_id is null returning to_index",
        (run_id, tenant_id))).fetchone()
    if not row:
        raise Refused("not_found")
    await conn.execute("update kb_indexes set status='retired' where id=%s", (row["to_index"],))


async def retire_document(conn: AsyncConnection, tenant_id: UUID, document_id: UUID) -> None:
    """Out of the search at once. The text and vectors stay, so approving it again does not cost anything."""
    row = await (await conn.execute(
        "update kb_documents set status='do_not_use', processing='done', processing_error=null where id=%s and tenant_id=%s returning id",
        (document_id, tenant_id))).fetchone()
    if not row:
        raise Refused("not_found")
    await conn.execute("delete from kb_uploads where document_id=%s", (document_id,))


# ---------------------------------------------------------------- FAQs
def validate_faq(cfg: PlatformConfig, question: str, answer: str, variants: list[str], allowed_urls: list[str]) -> list[TextProblem]:
    k = cfg.operations.knowledge
    out: list[TextProblem] = []
    if not question.strip():
        out.append(TextProblem("faq_question", "missing_faq_question"))
    if not answer.strip():
        out.append(TextProblem("faq_answer", "missing_faq_answer"))
    for q in [question, *variants]:
        if len(q) > k.faq_question_max_chars:
            out.append(TextProblem("faq_question", "too_long", q))
    if answer.strip():
        out.extend(text_problems(cfg, "faq_answer", answer, k.faq_answer_max_chars, allowed_urls, checked=_FAQ_CHECKED))
    return out


def _questions(question: str, variants: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    for q in [question, *variants]:
        text = mask(q.strip()).text
        if text:
            seen.setdefault(text.lower(), text)
    return list(seen.values())


async def save_faq(conn: AsyncConnection, tenant_id: UUID, user_id: UUID, faq_id: UUID | None, question: str, answer: str, variants: list[str],
                   valid_to: str, approve: bool) -> UUID:
    """Changing the text of an approved FAQ takes it out of use until it is approved again: nobody reads an answer no one approved."""
    questions = _questions(question, variants)
    end = _day(valid_to)
    if end and end < date.today():
        raise Refused("expired")
    if faq_id is None:
        row = await (await conn.execute(
            "insert into faqs (tenant_id, canonical_question, answer, status, valid_to, owner) values (%s,%s,%s,'draft',%s,%s) returning id",
            (tenant_id, questions[0], answer.strip(), end, user_id))).fetchone()
        faq_id = row["id"]
        changed_questions = True
    else:
        old = await (await conn.execute("select answer, status from faqs where id=%s and tenant_id=%s for update", (faq_id, tenant_id))).fetchone()
        if not old:
            raise Refused("not_found")
        current = {r["question"] for r in await (await conn.execute("select question from faq_variants where faq_id=%s", (faq_id,))).fetchall()}
        changed_questions = current != set(questions)
        if changed_questions or old["answer"] != answer.strip() or old["status"] == "retired":
            await conn.execute("update faqs set status='draft', approved_by=null, approved_at=null where id=%s", (faq_id,))
        await conn.execute("update faqs set canonical_question=%s, answer=%s, valid_to=%s where id=%s", (questions[0], answer.strip(), end, faq_id))
    if changed_questions:
        await conn.execute("delete from faq_variants where faq_id=%s", (faq_id,))
        for q in questions:
            await conn.execute("insert into faq_variants (tenant_id, faq_id, question) values (%s,%s,%s)", (tenant_id, faq_id, q))
    if approve:
        await conn.execute(
            "update faqs set approved_by=%s, approved_at=now(), processing='indexing', processing_error=null, processing_at=now(), processing_attempts=0 where id=%s",
            (user_id, faq_id))
    return faq_id


async def add_variant(conn: AsyncConnection, tenant_id: UUID, faq_id: UUID, question: str) -> bool:
    """Another way of asking something already answered. True when it needs a vector to start working."""
    faq = await (await conn.execute("select status from faqs where id=%s and tenant_id=%s and status <> 'retired' for update", (faq_id, tenant_id))).fetchone()
    if not faq:
        raise Refused("not_found")
    text = mask(question.strip()).text
    if await (await conn.execute("select 1 from faq_variants where faq_id=%s and lower(question)=lower(%s)", (faq_id, text))).fetchone():
        return False
    await conn.execute("insert into faq_variants (tenant_id, faq_id, question) values (%s,%s,%s)", (tenant_id, faq_id, text))
    if faq["status"] != "approved":
        return False
    await conn.execute("update faqs set processing='indexing', processing_at=now(), processing_attempts=0 where id=%s", (faq_id,))
    return True


async def retire_faq(conn: AsyncConnection, tenant_id: UUID, faq_id: UUID) -> None:
    row = await (await conn.execute("update faqs set status='retired', processing='done' where id=%s and tenant_id=%s returning id", (faq_id, tenant_id))).fetchone()
    if not row:
        raise Refused("not_found")


async def index_faq(tenant_id: UUID, faq_id: UUID, gateway: ModelGateway | None, embedding: ModelRef, ctx: CallContext, index_name: str) -> None:
    async with tenant_tx(tenant_id) as conn:
        faq = await (await conn.execute("select processing from faqs where id=%s and tenant_id=%s", (faq_id, tenant_id))).fetchone()
        if not faq or faq["processing"] != "indexing":
            return
        idx = await (await conn.execute("select id from kb_indexes where name=%s", (index_name,))).fetchone()
        if not idx:
            await conn.execute("update faqs set processing='failed', processing_error='no_index' where id=%s", (faq_id,))
            return
        rows = await (await conn.execute(
            """select v.id, v.question from faq_variants v where v.faq_id=%s
               and not exists (select 1 from faq_variant_vectors x where x.variant_id=v.id and x.index_id=%s)""", (faq_id, idx["id"]))).fetchall()
    failure = None
    vectors: list[list[float]] = []
    try:
        if rows and gateway is None:
            raise ModelCallError("no gateway")
        if rows:
            vectors = await gateway.embed(embedding, [r["question"] for r in rows], ctx)
    except BudgetDenied:
        failure = "budget"
    except ModelCallError as e:
        log.warning("indexing failed for faq %s: %s", faq_id, e)
        failure = "provider"
    async with tenant_tx(tenant_id) as conn:
        if failure:
            await conn.execute(
                """update faqs set processing='failed', processing_error=%s, approved_by=case when status='approved' then approved_by end,
                          approved_at=case when status='approved' then approved_at end where id=%s""", (failure, faq_id))
            return
        for r, v in zip(rows, vectors):
            await conn.execute(
                """insert into faq_variant_vectors (variant_id, index_id, tenant_id, embedding) values (%s,%s,%s,%s::vector)
                   on conflict (variant_id, index_id) do update set embedding=excluded.embedding""", (r["id"], idx["id"], tenant_id, vec_literal(v)))
        await conn.execute("update faqs set status='approved', processing='done', processing_error=null where id=%s and processing='indexing'", (faq_id,))


# ---------------------------------------------------------------- curation
async def close_question(conn: AsyncConnection, tenant_id: UUID, item_id: UUID, status: str, resolution: dict) -> None:
    row = await (await conn.execute(
        "update curation_queue set status=%s, resolution=%s where id=%s and tenant_id=%s and status='open' returning id",
        (status, json.dumps(resolution), item_id, tenant_id))).fetchone()
    if not row:
        raise Refused("not_found")
    await conn.execute(      # the questions grouped under it were answered too
        "update curation_queue set status=%s, resolution=%s where cluster_id=%s and tenant_id=%s and status='open'",
        (status, json.dumps(resolution), item_id, tenant_id))


async def similar_questions(conn: AsyncConnection, tenant_id: UUID, item_id: UUID) -> list[str]:
    """Grouped under this one: they become other ways of asking the same thing, so the next person who asks like that gets the answer too."""
    rows = await (await conn.execute(
        "select question_masked from curation_queue where cluster_id=%s and tenant_id=%s and status='open' order by created_at", (item_id, tenant_id))).fetchall()
    return [r["question_masked"] for r in rows]


async def ungroup_question(conn: AsyncConnection, tenant_id: UUID, leader_id: UUID, question: str) -> None:
    """The grouping is only by words, so a person can undo it."""
    row = await (await conn.execute(
        "update curation_queue set cluster_id=null where cluster_id=%s and tenant_id=%s and status='open' and question_masked=%s returning id",
        (leader_id, tenant_id, question))).fetchone()
    if not row:
        raise Refused("not_found")


async def open_question(conn: AsyncConnection, tenant_id: UUID, item_id: UUID) -> dict:
    row = await (await conn.execute("select id, question_masked from curation_queue where id=%s and tenant_id=%s and status='open'", (item_id, tenant_id))).fetchone()
    if not row:
        raise Refused("not_found")
    return row


# ---------------------------------------------------------------- sweep and bulk load
async def _catch_up_index(conn: AsyncConnection, tenant_id: UUID, index_name: str) -> Next:
    """After the search moved to a new index: what was read or approved in between. Without this, a document or FAQ approved while the new index was
    waiting for approval would silently stop being found."""
    idx = await (await conn.execute("select id from kb_indexes where name=%s", (index_name,))).fetchone()
    if not idx:
        return []
    await _copy_chunks(conn, tenant_id, idx["id"])
    docs = await (await conn.execute(
        """update kb_documents d set processing='indexing', processing_at=now(), processing_attempts=0
           where d.tenant_id=%(t)s and d.status='approved' and d.processing='done' and exists (
             select 1 from kb_chunks c where c.document_id=d.id and c.index_id=%(i)s and c.embedding is null and c.excluded_at is null) returning d.id""",
        {"t": tenant_id, "i": idx["id"]})).fetchall()
    faqs = await (await conn.execute(
        """update faqs f set processing='indexing', processing_at=now(), processing_attempts=0
           where f.tenant_id=%(t)s and f.status='approved' and f.processing='done' and exists (
             select 1 from faq_variants v where v.faq_id=f.id
               and not exists (select 1 from faq_variant_vectors x where x.variant_id=v.id and x.index_id=%(i)s)) returning f.id""",
        {"t": tenant_id, "i": idx["id"]})).fetchall()
    return [*(index_job(tenant_id, d["id"]) for d in docs), *(faq_job(tenant_id, f["id"]) for f in faqs)]


async def sweep(conn: AsyncConnection, tenant_id: UUID, cfg: KnowledgeConfig, index_name: str | None = None) -> Next:
    """A queued job can be lost (it has a queueing lock, so the queue does not retry it). The state is here: ask again, and give up with a reason."""
    nxt: Next = await _catch_up_index(conn, tenant_id, index_name) if index_name else []
    runs = await (await conn.execute(
        "select id from kb_reindex_runs where tenant_id=%s and status='running' and heartbeat_at < now() - make_interval(mins => %s)",
        (tenant_id, cfg.processing_retry_after_min))).fetchall()
    for r in runs:
        await conn.execute("update kb_reindex_runs set heartbeat_at=now(), attempts=attempts+1 where id=%s", (r["id"],))
        nxt.append(reindex_job(tenant_id, r["id"]))
    stale = "processing_at < now() - make_interval(mins => %s)"
    docs = await (await conn.execute(
        f"select id, processing, processing_attempts from kb_documents where tenant_id=%s and processing in ('queued','indexing') and {stale}",
        (tenant_id, cfg.processing_retry_after_min))).fetchall()
    for d in docs:
        if d["processing_attempts"] >= cfg.processing_max_requeues:
            await _fail(conn, tenant_id, d["id"], "timeout")
            continue
        await conn.execute("update kb_documents set processing_at=now(), processing_attempts=processing_attempts+1 where id=%s", (d["id"],))
        nxt.append(extract_job(tenant_id, d["id"]) if d["processing"] == "queued" else index_job(tenant_id, d["id"]))
    faqs = await (await conn.execute(
        f"select id, processing_attempts from faqs where tenant_id=%s and processing='indexing' and {stale}", (tenant_id, cfg.processing_retry_after_min))).fetchall()
    for f in faqs:
        if f["processing_attempts"] >= cfg.processing_max_requeues:
            await conn.execute("update faqs set processing='failed', processing_error='timeout' where id=%s", (f["id"],))
            continue
        await conn.execute("update faqs set processing_at=now(), processing_attempts=processing_attempts+1 where id=%s", (f["id"],))
        nxt.append(faq_job(tenant_id, f["id"]))
    return nxt


async def load_file(conn: AsyncConnection, tenant_id: UUID, data: bytes, meta: DocMeta, index_id: UUID, package: str, cfg: KnowledgeConfig) -> dict:
    """Bulk load from the operator's folder (`agentplatform ingest`): it arrives for review exactly like an upload, with the proposed metadata
    filled in. Whatever the inventory proposes, a person still approves it in the console."""
    digest = hashlib.sha256(data).hexdigest()
    dup = await (await conn.execute("select file_name from kb_documents where file_sha256=%s and processing <> 'failed' limit 1", (digest,))).fetchone()
    if dup:
        return {"file": meta.file, "result": "duplicate", "of": dup["file_name"]}
    try:
        pages, info = await read_document(data, cfg)
    except (TooManyPages, UnreadableDocument) as e:
        return {"file": meta.file, "result": "unreadable", "error": type(e).__name__}
    src = await (await conn.execute(
        "insert into kb_sources (tenant_id, scope, package, title, segment, kind) values (%s,'tenant',%s,%s,%s,'pdf') returning id",
        (tenant_id, package, meta.title, meta.segment))).fetchone()
    doc = await (await conn.execute(
        """insert into kb_documents (tenant_id, source_id, file_name, file_sha256, version_label, effective_from, effective_to, status, visibility, audience)
           values (%s,%s,%s,%s,%s,%s,%s,'draft',%s,%s) returning id""",
        (tenant_id, src["id"], meta.file, digest, meta.version_label, meta.effective_from, meta.effective_to, meta.visibility, meta.audience))).fetchone()
    status = await store_reading(conn, tenant_id, doc["id"], index_id, pages, info, exclude_pages=tuple(meta.exclude_pages),
                                 keep_status="do_not_use" if meta.status == "do_not_use" else None)
    return {"file": meta.file, "result": status, "pages": len(pages), "ocr_pages": len(info["ocr_pages"])}


async def seed_faqs(conn: AsyncConnection, tenant_id: UUID, package: str, faqs: list) -> int:
    """The package's suggestions arrive as drafts of this tenant: its owner decides whether they are true for this business."""
    n = 0
    for f in faqs:
        if await (await conn.execute("select 1 from faqs where tenant_id=%s and package=%s and canonical_question=%s", (tenant_id, package, f.question))).fetchone():
            continue
        row = await (await conn.execute(
            "insert into faqs (tenant_id, package, canonical_question, answer, status) values (%s,%s,%s,%s,'draft') returning id",
            (tenant_id, package, f.question, f.answer))).fetchone()
        for q in _questions(f.question, list(f.variants)):
            await conn.execute("insert into faq_variants (tenant_id, faq_id, question) values (%s,%s,%s)", (tenant_id, row["id"], q))
        n += 1
    return n
