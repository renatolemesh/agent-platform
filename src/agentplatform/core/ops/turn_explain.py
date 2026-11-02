"""Why the assistant said that: the record of a turn, for a person to read. Only what the turn already recorded; nothing is recomputed."""
from __future__ import annotations

from uuid import UUID


async def explain_turns(conn, turns: list[dict]) -> dict[UUID, dict]:
    """By turn id. `turns` are rows of `turns` with id, status, stage_before, stage_after, llm_calls, cost_usd, trace."""
    ids = [t["id"] for t in turns]
    if not ids:
        return {}
    guards: dict[UUID, list[dict]] = {}
    for g in await (await conn.execute(
            "select turn_id, guard, rule_id, verdict, detail from guard_results where turn_id = any(%s) and verdict <> 'pass' order by id", (ids,))).fetchall():
        guards.setdefault(g["turn_id"], []).append(g)
    chunk_ids = {UUID(c) for t in turns for c in ((t["trace"] or {}).get("retrieval_ids") or [])}
    chunks = {r["id"]: r for r in await (await conn.execute(
        """select k.id, d.file_name, k.page, left(k.text, 220) as excerpt from kb_chunks k join kb_documents d on d.id = k.document_id
           where k.id = any(%s)""", (list(chunk_ids),))).fetchall()} if chunk_ids else {}
    faq_ids = {UUID(f) for t in turns if (f := (t["trace"] or {}).get("faq"))}
    faqs = {r["id"]: r for r in await (await conn.execute(
        "select id, canonical_question from faqs where id = any(%s)", (list(faq_ids),))).fetchall()} if faq_ids else {}
    out = {}
    for t in turns:
        tr = t["trace"] or {}
        used = [chunks[UUID(c)] for c in tr.get("retrieval_ids") or [] if UUID(c) in chunks]
        faq = faqs.get(UUID(tr["faq"])) if tr.get("faq") else None
        out[t["id"]] = {
            "status": t["status"], "stage_before": t["stage_before"], "stage_after": t["stage_after"], "cost_usd": t["cost_usd"], "llm_calls": t["llm_calls"],
            "config_version": t.get("config_version"), "intents": tr.get("intents") or [], "guidelines": tr.get("guidelines") or [], "tools": tr.get("tools") or [],
            "handoff": tr.get("handoff"), "attempts": tr.get("attempts"), "failed_with": tr.get("failed_with"), "prompt": tr.get("prompt"),
            "faq": faq, "chunks": used, "guards": guards.get(t["id"], []),
            "source": "faq" if faq else "chunks" if used else "no_basis" if tr.get("no_basis") else "none"}
    return out
