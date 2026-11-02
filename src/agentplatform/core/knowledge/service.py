"""Knowledge Service (plan 06): canonical FAQ first, then hybrid search (Portuguese FTS + pgvector, RRF), with a
threshold. Below it the turn is marked no_basis and the question goes to curation — the model never answers from memory.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection

from agentplatform.core.agent.guards import normalize
from agentplatform.core.agent.state import RetrievalResult, RetrievedChunk
from agentplatform.core.config.schema import ModelRef, RagConfig
from agentplatform.core.models.gateway import CallContext, ModelGateway
from agentplatform.core.security.pii import mask

log = logging.getLogger(__name__)
_STOPWORDS = frozenset(("que", "qual", "quais", "como", "para", "por", "com", "uma", "uns", "umas", "tem", "sao", "ser", "esta", "esse", "essa",
                        "isso", "voces", "voce", "dos", "das", "nos", "nas", "mais", "pode", "posso", "quero", "saber", "gostaria"))


def vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in v) + "]"


def question_similarity(a: str, b: str) -> float:
    """How alike two questions are, by their content words. Local: unanswered questions never leave the server to be grouped."""
    def words(t: str) -> set[str]:
        return {w for w in re.findall(r"\w+", normalize(t)) if len(w) > 2 and w not in _STOPWORDS}
    x, y = words(a), words(b)
    return len(x & y) / min(len(x), len(y)) if x and y else 0.0


@dataclass
class KnowledgeService:
    gateway: ModelGateway | None
    embedding: ModelRef
    rag: RagConfig
    package: str

    async def _embed(self, text: str, ctx: CallContext) -> list[float] | None:
        if self.gateway is None:
            return None
        return (await self.gateway.embed(self.embedding, [text], ctx))[0]

    async def retrieve(self, conn: AsyncConnection, tenant_id: UUID, question: str, ctx: CallContext,
                       *, degraded: bool = False) -> RetrievalResult:
        if not degraded and not await self._index_matches(conn):
            degraded = True      # comparing vectors from two models gives confident nonsense: approved FAQs by their words, or "I'll confirm"
        q_vec = None if degraded else await self._embed(question, ctx)
        faq = await self._match_faq(conn, question, q_vec)
        if faq:
            return RetrievalResult(faq_answer=faq["answer"], faq_id=str(faq["id"]))
        if degraded:
            return RetrievalResult(no_basis=True)
        chunks = await self._hybrid(conn, question, q_vec)
        if not chunks:
            await self.report_gap(conn, tenant_id, question, ctx.turn_id)
            return RetrievalResult(no_basis=True)
        return RetrievalResult(chunks=chunks)

    async def inspect(self, conn, question: str, ctx: CallContext) -> dict:
        """What the search finds and why, for a person testing the base. Same queries as `retrieve`; nothing is recorded."""
        matches = await self._index_matches(conn)
        q_vec = await self._embed(question, ctx) if matches else None
        best = await self._closest_faq(conn, q_vec) if q_vec is not None else None
        rows = await self._search(conn, question, q_vec) if q_vec is not None else []
        best_sim = max((r["sim"] for r in rows), default=0.0)
        passes = bool(rows) and (best_sim >= self.rag.min_score or any(r["fts_hit"] for r in rows))
        faq_used = bool(best and best["sim"] >= self.rag.faq_threshold)
        return {"index_ok": matches, "faq": best, "faq_used": faq_used, "chunks": rows, "best_sim": best_sim, "chunks_used": passes and not faq_used,
                "outcome": "faq" if faq_used else "chunks" if passes else "no_basis", "rag": self.rag}

    async def _index_matches(self, conn) -> bool:
        idx = await (await conn.execute(
            "select embedding_model, dims from kb_indexes where name=%s", (self.rag.active_index_id,))).fetchone()
        if idx and (idx["embedding_model"] != self.embedding.model or idx["dims"] != self.embedding.dims):
            log.error("index %s was built with %s/%s; the config asks for %s/%s", self.rag.active_index_id, idx["embedding_model"], idx["dims"],
                      self.embedding.model, self.embedding.dims)
            return False
        return True

    async def _closest_faq(self, conn, q_vec: list[float]) -> dict | None:
        d = self.embedding.dims
        return await (await conn.execute(
            f"""select f.id, f.canonical_question, f.answer, v.question, 1 - ((x.embedding::vector({d})) <=> %(v)s::vector) as sim
                from faq_variant_vectors x join faq_variants v on v.id = x.variant_id join faqs f on f.id = v.faq_id
                where x.index_id = (select id from kb_indexes where name = %(idx)s)
                  and f.status = 'approved' and (f.package = %(p)s or f.tenant_id is not null)
                  and (f.valid_to is null or f.valid_to >= current_date)
                order by (x.embedding::vector({d})) <=> %(v)s::vector limit 1""",
            {"v": vec_literal(q_vec), "idx": self.rag.active_index_id, "p": self.package})).fetchone()

    async def _match_faq(self, conn, question: str, q_vec: list[float] | None) -> dict | None:
        if q_vec is not None:
            row = await self._closest_faq(conn, q_vec)
            if row and row["sim"] >= self.rag.faq_threshold:
                return row
            return None
        # degraded (no embeddings): high-precision FTS on variants only
        row = await (await conn.execute(
            """select f.id, f.answer, ts_rank_cd(v.tsv, q) as rank from faq_variants v join faqs f on f.id = v.faq_id,
                    plainto_tsquery('pt_unaccent', %s) q
               where f.status = 'approved' and (f.package = %s or f.tenant_id is not null) and v.tsv @@ q
                 and (f.valid_to is null or f.valid_to >= current_date)
               order by rank desc limit 1""", (question, self.package))).fetchone()
        return row if row and row["rank"] >= self.rag.degraded_faq_min_rank else None

    async def _hybrid(self, conn, question: str, q_vec: list[float]) -> list[RetrievedChunk]:
        rows = await self._search(conn, question, q_vec)
        best_sim = max((r["sim"] for r in rows), default=0.0)
        if not rows or (best_sim < self.rag.min_score and not any(r["fts_hit"] for r in rows)):
            return []
        return [RetrievedChunk(id=str(r["id"]), text=r["text"], score=float(r["rrf"]), visibility=r["visibility"],
                               document=r["file_name"]) for r in rows]

    async def _search(self, conn, question: str, q_vec: list[float]) -> list[dict]:
        d = self.embedding.dims
        return await (await conn.execute(
            f"""with idx as (select id from kb_indexes where name = %(idx)s and status in ('active','ready')),
            base as (
              select c.id, c.text, c.embedding, c.tsv, c.page, d.visibility, d.file_name, d.id as document_id
              from kb_chunks c join kb_documents d on d.id = c.document_id
              where c.index_id in (select id from idx) and d.status = 'approved' and c.excluded_at is null and c.embedding is not null
                and (d.effective_to is null or d.effective_to >= current_date)),
            fts as (
              select id, row_number() over (order by ts_rank_cd(tsv, websearch_to_tsquery('pt_unaccent', %(q)s)) desc) as r
              from base where tsv @@ websearch_to_tsquery('pt_unaccent', %(q)s) limit %(cand)s),
            vec as (
              select id, 1 - ((embedding::vector({d})) <=> %(v)s::vector) as sim,
                     row_number() over (order by (embedding::vector({d})) <=> %(v)s::vector) as r
              from base order by (embedding::vector({d})) <=> %(v)s::vector limit %(cand)s),
            fused as (
              select id, sum(1.0 / (%(k)s + r)) as rrf from (select id, r from fts union all select id, r from vec) u group by id)
            select b.id, b.text, b.visibility, b.file_name, b.page, b.document_id, f.rrf, coalesce(v.sim, 0) as sim,
                   exists (select 1 from fts where fts.id = b.id) as fts_hit
            from fused f join base b on b.id = f.id left join vec v on v.id = b.id
            order by f.rrf desc limit %(top)s""",
            {"idx": self.rag.active_index_id, "q": question, "v": vec_literal(q_vec), "k": self.rag.rrf_k,
             "top": self.rag.top_k, "cand": self.rag.candidates_k})).fetchall()

    async def report_gap(self, conn, tenant_id: UUID, question: str, turn_id: UUID | None) -> None:
        q = mask(question).text
        existing = await (await conn.execute(
            "select id from curation_queue where tenant_id=%s and status='open' and lower(question_masked)=lower(%s)",
            (tenant_id, q))).fetchone()
        if existing:
            await conn.execute("update curation_queue set frequency = frequency + 1 where id=%s", (existing["id"],))
            return
        leaders = await (await conn.execute(
            """select id, question_masked from curation_queue where tenant_id=%s and status='open' and cluster_id is null
               order by frequency desc, created_at limit 200""", (tenant_id,))).fetchall()
        similar = next((r["id"] for r in leaders if question_similarity(q, r["question_masked"]) >= self.rag.curation_group_similarity), None)
        await conn.execute("insert into curation_queue (tenant_id, question_masked, turn_id, cluster_id) values (%s,%s,%s,%s)",
                           (tenant_id, q, turn_id, similar))
