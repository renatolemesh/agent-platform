"""Evaluation harness and lab (plan 10): runs the REAL nodes/graph (never a copy of the agent), deterministic graders
first, live LLM levels only within the experiment budget. Every LLM call goes through the ModelGateway."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel

from agentplatform.core.agent import policy
from agentplatform.core.agent.guards import OutputContext, failed, input_guard, output_guard, privacy_gate
from agentplatform.core.config.schema import PlatformConfig
from agentplatform.core.packages.contract import SegmentPackage
from agentplatform.paths import ROOT

DATASETS = ROOT / "evals" / "datasets"


def load_jsonl(rel: str) -> list[dict]:
    return [json.loads(l) for l in (DATASETS / rel).read_text(encoding="utf-8").splitlines() if l.strip()]


@dataclass
class Score:
    name: str
    passed: int = 0
    total: int = 0
    failures: list[str] = field(default_factory=list)
    cost_usd: Decimal = Decimal(0)

    @property
    def rate(self) -> float:
        return self.passed / self.total if self.total else 1.0

    def add(self, ok: bool, case_id: str) -> None:
        self.total += 1
        self.passed += int(ok)
        if not ok:
            self.failures.append(case_id)


# ------------------------------------------------------------------ fast (deterministic, no LLM)
def eval_output_guard(cfg: PlatformConfig, pkg: SegmentPackage) -> tuple[Score, Score]:
    adversarial, good = Score("compliance_block_rate"), Score("compliance_false_positive_free")
    for c in load_jsonl("core/compliance-v1.jsonl"):
        ctx = OutputContext(cfg.compliance, [], policy.config_numbers(), [], 1000, allowed_url_prefixes=(*pkg.manifest.allowed_urls, *cfg.conversation.allowed_urls),
                            guidance_texts=tuple(c.get("guidance", [])), guidance_max_ngram=cfg.output_guard.guidance_only_max_ngram)
        blocked = any(v.verdict == "block" for v in failed(output_guard(c["text"], ctx)))
        (adversarial if c["block"] else good).add(blocked == c["block"], c["id"])
    return adversarial, good


def eval_input_guard(cfg: PlatformConfig, pkg: SegmentPackage) -> Score:
    s = Score("input_guard")
    for c in load_jsonl("core/input-guard-v1.jsonl"):
        ok = True
        if "opt_out" in c:
            ok &= privacy_gate([c["text"]], cfg.compliance).opt_out == c["opt_out"]
        r = input_guard(c["text"], cfg.input_guard, pkg.manifest, cfg.handoff.disabled_reasons)
        if "handoff" in c:
            ok &= r.handoff_reason == c["handoff"]
        if c.get("injection"):
            ok &= "injection_suspected" in r.reasons and r.action == "continue"
        s.add(ok, c["id"])
    return s


# ------------------------------------------------------------------ live: node level
async def _analyze(cfg: PlatformConfig, pkg: SegmentPackage, gateway, case: dict):
    import secrets

    from agentplatform.core.agent.prompts import render
    from agentplatform.core.agent.state import AnalyzeOutput
    from agentplatform.core.models.gateway import CallContext
    m = pkg.manifest
    msgs = render(cfg.models.analyze_turn.prompt_version, nonce=secrets.token_hex(4),
                  slot_catalog=m.slot_catalog(), intent_catalog=m.intent_catalog(),      # same text the turn renders
                  objection_catalog=", ".join(m.objections), scope=m.scope,
                  stage_catalog=", ".join(x.name for x in m.stages), stage=case["stage"], profile="{}", summary="-",
                  recent="(início)", turn_input="\n".join(f"- {t}" for t in case["msgs"]), origin=[])
    return await gateway.chat(cfg.models.analyze_turn, msgs, CallContext(None, "lab:analyze_turn", purpose="lab"), AnalyzeOutput)


async def eval_opt_out(cfg: PlatformConfig, pkg: SegmentPackage, gateway) -> Score:
    """Who asked to stop and who did not. Past the threshold the assistant stops for good on the model's word, so a wrong score loses a customer who only
    wanted a summary, or keeps writing to one who asked to stop."""
    s = Score("opt_out_intent")
    for c in load_jsonl("core/opt-out-v1.jsonl"):
        r = await _analyze(cfg, pkg, gateway, c)
        s.cost_usd += r.cost_usd
        score = r.parsed.understanding.opt_out_intent
        injection = "injection_suspected" in input_guard(c["msgs"][0], cfg.input_guard, pkg.manifest, cfg.handoff.disabled_reasons).reasons
        taken = policy.opt_out_from_model(score, injection, cfg.input_guard) is not None      # the same decision a real turn makes
        s.add(taken == c["stop"], f"{c['id']} {'pediu' if c['stop'] else 'não pediu'} para parar, nota {score}")
    return s


async def eval_analyze(cfg: PlatformConfig, pkg: SegmentPackage, gateway) -> Score:
    """F1-like slot accuracy of analyze_turn through the real prompt + policy (code acceptance rules included)."""
    s = Score("analyze_slots")
    m = pkg.manifest
    for c in load_jsonl(f"{m.name}/understand-v1.jsonl"):
        r = await _analyze(cfg, pkg, gateway, c)
        s.cost_usd += r.cost_usd
        dec = policy.apply_slots(m, {}, r.parsed.understanding, "\n".join(c["msgs"]), cfg.funnel, None)
        got = {k: str(v["value"]) for k, v in dec.accepted.items()}
        want = c["slots"]
        ok = all(got.get(k) == v for k, v in want.items()) and (bool(want) or not got)   # no hallucinated slots
        s.add(ok, f"{c['id']} want={want} got={got}")
    return s


# ------------------------------------------------------------------ live: conversation level
class SimTurn(BaseModel):
    message: str
    done: bool


async def run_conversation(tenant_slug: str, scenario: dict, gateway, sim_spec, cfg: PlatformConfig | None = None) -> dict:
    """Full pipeline (persist -> ingress -> debounce -> turn -> dispatch) with a simulated customer, on the simulator channel: nothing reaches a
    customer, no seller is told, it stays out of the inbox and the dashboards, and the real workers have nothing to do with it. With cfg, every turn
    uses that (candidate) config instead of the tenant's active version: the gate tests a change before it is activated."""
    from agentplatform.core.agent.prompts import config_prompt_versions, render
    from agentplatform.core.agent.prompts import preload as preload_prompts
    from agentplatform.core.channels.events import CanonicalEvent
    from agentplatform.core.channels.fake.adapter import FakeChannelAdapter
    from agentplatform.core.db.pool import platform_tx, tenant_tx
    from agentplatform.core.handoff.notifiers import FallbackInbox, LogNotifier
    from agentplatform.core.knowledge.service import KnowledgeService
    from agentplatform.core.models.gateway import CallContext
    from agentplatform.core.orchestration.ingress import persist_events
    from agentplatform.core.packages.contract import load_package
    from agentplatform.jobs.pump import pump
    from agentplatform.lab.simulator import CHANNEL, SIM_DEBOUNCE, _SimRuntime, ensure_account
    from agentplatform.runtime import TenantRuntime, get_runtime, reset_scoped_runtime, scoped_runtime
    adapter = FakeChannelAdapter(channel=CHANNEL)
    rt = _SimRuntime(adapters={CHANNEL: adapter}, gateway=gateway, inbox=FallbackInbox([LogNotifier()]))
    rt.use_checkpointer = False
    async with platform_tx() as conn:
        t = await (await conn.execute("select id from tenants where slug=%s", (tenant_slug,))).fetchone()
    account = await ensure_account(t["id"])
    if cfg is None:
        tr = await get_runtime().tenant(t["id"])
        cfg, version_id, pkg = tr.config, tr.config_version_id, tr.package
    else:
        version_id, pkg = None, load_package(cfg.segment_package.name)
    async with tenant_tx(t["id"]) as conn:
        await preload_prompts(conn, config_prompt_versions(cfg))
    cfg = cfg.model_copy(update={"concurrency": cfg.concurrency.model_copy(update=SIM_DEBOUNCE),
                                 "conversation": cfg.conversation.model_copy(update={"bubble_delay_ms": 0})})
    knowledge = KnowledgeService(gateway, cfg.models.embedding, cfg.rag, pkg.manifest.name) if gateway else None
    rt._tenants[str(t["id"])] = (float("inf"), TenantRuntime(t["id"], cfg, version_id, pkg, knowledge))
    token = scoped_runtime(rt)
    try:
        return await _converse(t["id"], account, scenario, gateway, sim_spec, adapter, render, CallContext, CanonicalEvent, persist_events, pump, tenant_tx)
    finally:
        reset_scoped_runtime(token)


async def _converse(tenant_id, account, scenario, gateway, sim_spec, adapter, render, CallContext, CanonicalEvent, persist_events, pump, tenant_tx) -> dict:
    igsid = f"lab-{scenario['id']}-{uuid.uuid4().hex[:6]}"
    transcript: list[str] = []
    sim_cost = Decimal(0)
    scripted = list(scenario.get("script", []))
    for _ in range(scenario["max_turns"]):
        if scripted:                      # deterministic customer lines (e.g. exact opt-out)
            text = scripted.pop(0)
        elif scenario.get("script"):
            break
        else:
            msgs = render("customer_simulator@v2", persona=scenario["persona"], goal=scenario["goal"],
                          transcript="\n".join(transcript) or "(ainda ninguém falou; você começa)")
            r = await gateway.chat(sim_spec, msgs, CallContext(None, "lab:simulator", purpose="lab"), SimTurn)
            sim_cost += r.cost_usd
            if r.parsed.done:
                break
            text = r.parsed.message
        transcript.append(f"cliente: {text}")
        before = len(adapter.sent)
        ev = CanonicalEvent(event_type="MessageReceived", channel="simulator", account_external_id=account,
                            contact_external_id=igsid, idempotency_key=f"lab:{uuid.uuid4().hex}", mid=uuid.uuid4().hex,
                            provider_ts=datetime.now(timezone.utc), text=text)
        created = await persist_events([ev])
        await pump([("ingress", {"tenant_id": str(tt), "event_id": e}, {}) for tt, e in created], honor_delays=True, max_delay=0.2)
        for s in adapter.sent[before:]:
            transcript.append(f"assistente: {s.msg.text}")
        async with tenant_tx(tenant_id) as conn:
            st = await (await conn.execute(
                """select c.ownership, l.opted_out_at from conversations c join contact_identities ci on ci.contact_id=c.contact_id
                   join leads l on l.contact_id=c.contact_id where ci.external_user_id=%s""", (igsid,))).fetchone()
        if st and (st["ownership"] != "BOT" or st["opted_out_at"]):
            break
    async with tenant_tx(tenant_id) as conn:
        final = await (await conn.execute(
            """select l.stage, l.opted_out_at, c.ownership, c.handoff_pending, c.id as cid,
                      (select coalesce(sum(cost_usd),0) from turns where conversation_id=c.id) as cost,
                      (select count(*) from handoffs where conversation_id=c.id and not is_notice) as handoffs,
                      (select count(*) from guard_results g join turns tt on tt.id=g.turn_id
                        where tt.conversation_id=c.id and g.verdict='block') as blocks
               from conversations c join contact_identities ci on ci.contact_id=c.contact_id
               join leads l on l.contact_id=c.contact_id where ci.external_user_id=%s""", (igsid,))).fetchone()
    return {"transcript": transcript, "bot_texts": adapter.texts(), "final": final, "sim_cost": sim_cost}


# ------------------------------------------------------------------ live: reply and search
class _RespondDraft(BaseModel):
    text: str


async def eval_respond(cfg: PlatformConfig, pkg: SegmentPackage, gateway, budget_left: Decimal) -> Score:
    """The reply node on hand-written situations: what must and must not appear, plus the same guards a real reply goes through."""
    import re
    import secrets

    from agentplatform.core.agent.guards import _affirmative_match, ensure_identified, normalize
    from agentplatform.core.agent.prompts import render
    from agentplatform.core.agent.state import Draft
    from agentplatform.core.models.gateway import CallContext
    s = Score("respond")
    m = pkg.manifest
    path = DATASETS / m.name / "respond-v1.jsonl"
    if not path.exists():
        return s
    persona = cfg.conversation.persona
    for c in load_jsonl(f"{m.name}/respond-v1.jsonl"):
        if s.cost_usd >= budget_left:
            s.add(False, f"{c['id']}: parou no teto de gasto")
            continue
        stage = c.get("stage", m.initial_stage)
        sources = [*c["msgs"], c.get("knowledge", ""), json.dumps(c.get("tool_results") or {}, ensure_ascii=False), c.get("profile", "")]
        ctx = OutputContext(cfg.compliance, sources, policy.config_numbers(), [], cfg.output_guard.max_bytes, allowed_url_prefixes=(*m.allowed_urls, *cfg.conversation.allowed_urls),
                            guidance_texts=tuple(c.get("guidance", [])), guidance_max_ngram=cfg.output_guard.guidance_only_max_ngram,
                            no_basis=c.get("knowledge", "no_basis") == "no_basis", no_basis_claims=tuple(cfg.output_guard.no_basis_claim_patterns))
        fix_reasons, text, bad = None, "", []
        for _ in range(cfg.agent.max_reply_attempts):      # like a real turn: what the guards refuse is written again, and only what survives counts
            msgs = render(cfg.models.respond.prompt_version, nonce=secrets.token_hex(4), persona=m.persona_text(persona.seller_display_name, persona.company_name),
                          seller_name=persona.seller_display_name, first_message=c.get("first_message", False), disclose=persona.disclose_on_first_message,
                          stage=stage, stage_goal=m.stage(stage).goal, guidelines=c.get("guidelines", []), next_question=c.get("next_question"), fix_reasons=fix_reasons,
                          profile=c.get("profile", "-"), require_with=cfg.compliance.require_with,
                          reply_examples=[e.model_dump() for e in [*m.reply_examples, *cfg.conversation.reply_examples]],
                          assistant_name=persona.assistant_name, tone_instruction=cfg.conversation.tone_instructions.get(persona.tone, ""),
                          business_instructions=cfg.conversation.business_instructions.strip(), summary="-", recent=c.get("recent", "(início)"), origin=[],
                          turn_input="\n".join(f"- {t}" for t in c["msgs"]), knowledge=c.get("knowledge", "no_basis"),
                          tool_results=json.dumps(c["tool_results"], ensure_ascii=False) if c.get("tool_results") else "(nenhum)")
            r = await gateway.chat(cfg.models.respond, msgs, CallContext(None, "lab:respond", purpose="lab"), Draft)
            s.cost_usd += r.cost_usd
            text = r.parsed.text.strip()
            bad = failed(output_guard(text, ctx))
            if not bad:
                break
            fix_reasons = ", ".join(v.rule_id for v in bad)
        if any(v.rule_id == "one_question" for v in bad) and "?" in text:
            text = text[: text.index("?") + 1]             # what the turn does when only this is left
        if c.get("first_message") and persona.disclose_on_first_message:
            text = ensure_identified(text, cfg.conversation.first_message_disclosure.format(seller=persona.seller_display_name), cfg.comments.disclosure_terms,
                                     cfg.conversation.greeting_pattern)
        n = normalize(text)
        problems = [v.rule_id for v in bad if v.verdict == "block"]
        problems += [f"faltou '{w}'" for w in c.get("must", []) if not re.search(normalize(w), n)]
        problems += [f"disse '{w}'" for w in c.get("must_not", []) if _affirmative_match(normalize(w), n)]
        s.add(not problems, f"{c['id']}: {problems}" if problems else c["id"])
    return s


async def eval_rag(cfg: PlatformConfig, pkg: SegmentPackage, gateway) -> Score:
    """The real search, on a scratch tenant, over a synthetic corpus: the right passage in the top results, the FAQ when there is one, nothing when
    the base does not know. The client's own set (plan 06) is written from the material a person approved and needs the production provider."""
    from agentplatform.core.knowledge.service import KnowledgeService, vec_literal
    from agentplatform.core.models.gateway import CallContext
    s = Score("rag")
    corpus_path = DATASETS / "core" / "rag-corpus-v1.jsonl"
    if not corpus_path.exists():
        return s
    from agentplatform.core.config.schema import ModelRef
    from agentplatform.core.db.pool import platform_tx, tenant_tx
    corpus = load_jsonl("core/rag-corpus-v1.jsonl")
    tenant_id = uuid.uuid4()
    emb: ModelRef = cfg.models.embedding
    ctx = CallContext(None, "lab:rag", purpose="lab")
    async with platform_tx() as conn:
        await conn.execute("insert into tenants (id, slug, display_name, segment_package, segment_package_version, status) values (%s,%s,%s,%s,%s,'offboarded')",
                           (tenant_id, f"lab-rag-{tenant_id.hex[:8]}", "lab rag", pkg.manifest.name, pkg.manifest.version))
        idx_name = f"lab-rag-{tenant_id.hex[:8]}"
        idx = await (await conn.execute("insert into kb_indexes (name, strategy, embedding_model, dims, status) values (%s,'{}',%s,%s,'active') returning id",
                                        (idx_name, emb.model, emb.dims))).fetchone()
    rag = cfg.rag.model_copy(update={"active_index_id": idx_name})
    service = KnowledgeService(gateway, emb, rag, pkg.manifest.name)
    embedded = await gateway.embed(emb, [d["text"] for d in corpus if d["kind"] == "chunk"] + [d["question"] for d in corpus if d["kind"] == "faq"], ctx)
    s.cost_usd += _embed_cost(emb.model, corpus)
    vectors = iter(embedded)
    try:
        async with tenant_tx(tenant_id) as conn:
            src = await (await conn.execute("insert into kb_sources (tenant_id, scope, title, kind) values (%s,'tenant','lab','pdf') returning id", (tenant_id,))).fetchone()
            docs: dict[str, UUID] = {}
            for d in corpus:
                if d["kind"] != "chunk":
                    continue
                if d["doc"] not in docs:
                    row = await (await conn.execute(
                        """insert into kb_documents (tenant_id, source_id, file_name, file_sha256, status, visibility, audience)
                           values (%s,%s,%s,%s,'approved','citable','customer') returning id""",
                        (tenant_id, src["id"], d["doc"], uuid.uuid4().hex))).fetchone()
                    docs[d["doc"]] = row["id"]
                await conn.execute(
                    "insert into kb_chunks (tenant_id, index_id, document_id, ordinal, text, token_count, embedding) values (%s,%s,%s,%s,%s,%s,%s::vector)",
                    (tenant_id, idx["id"], docs[d["doc"]], d["ordinal"], d["text"], len(d["text"]) // 4, vec_literal(next(vectors))))
            for d in corpus:
                if d["kind"] != "faq":
                    continue
                f = await (await conn.execute("insert into faqs (tenant_id, canonical_question, answer, status) values (%s,%s,%s,'approved') returning id",
                                              (tenant_id, d["question"], d["answer"]))).fetchone()
                v = await (await conn.execute("insert into faq_variants (tenant_id, faq_id, question) values (%s,%s,%s) returning id",
                                              (tenant_id, f["id"], d["question"]))).fetchone()
                await conn.execute("insert into faq_variant_vectors (variant_id, index_id, tenant_id, embedding) values (%s,%s,%s,%s::vector)",
                                   (v["id"], idx["id"], tenant_id, vec_literal(next(vectors))))
        for c in load_jsonl("core/rag-v1.jsonl"):
            async with tenant_tx(tenant_id) as conn:
                found = await service.inspect(conn, c["question"], ctx)
            s.cost_usd += _embed_cost(emb.model, [{"kind": "faq", "question": c["question"]}])
            problems = []
            if found["outcome"] != c["expect"]:
                problems.append(f"veio {found['outcome']}, esperado {c['expect']}")
            if c.get("doc") and found["outcome"] == "chunks" and c["doc"] not in [r["file_name"] for r in found["chunks"]]:
                problems.append(f"sem o documento {c['doc']}")
            if c.get("contains") and found["outcome"] == "chunks" and not any(c["contains"].lower() in r["text"].lower() for r in found["chunks"]):
                problems.append(f"sem o trecho '{c['contains']}'")
            s.add(not problems, f"{c['id']}: {problems}" if problems else c["id"])
    finally:
        async with platform_tx() as conn:
            for table in ("faq_variant_vectors", "faq_variants", "faqs", "kb_chunks", "kb_documents", "kb_sources"):
                await conn.execute(f"delete from {table} where tenant_id=%s", (tenant_id,))
            await conn.execute("delete from kb_indexes where id=%s", (idx["id"],))
            await conn.execute("delete from tenant_deks where tenant_id=%s", (tenant_id,))
            await conn.execute("delete from tenants where id=%s", (tenant_id,))
    return s


def _embed_cost(model: str, items: list[dict]) -> Decimal:
    from agentplatform.core.models import pricing
    tokens = sum(pricing.estimate_tokens(d.get("text") or d.get("question") or "") for d in items)
    return pricing.embedding_cost(model, tokens)


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def grade_scenario(scn: dict, out: dict, cfg: PlatformConfig) -> tuple[bool, str]:
    e, f = scn["expect"], out["final"]
    if f is None:
        return False, "no conversation"
    problems = []
    if "stage_in" in e and f["stage"] not in e["stage_in"]:
        problems.append(f"stage={f['stage']}")
    if e.get("handoff") and not (f["handoffs"] or f["ownership"] in ("AWAITING_HUMAN", "HUMAN") or f["handoff_pending"]):
        problems.append("no handoff")
    if e.get("opted_out") and not f["opted_out_at"]:
        problems.append("not opted out")
    from agentplatform.core.packages.contract import load_package
    from agentplatform.settings import get_settings
    m = load_package(cfg.segment_package.name).manifest
    form = (get_settings().public_base_url.rstrip("/") + "/f/",) if m.form_sent_stage else ()
    ctx = OutputContext(cfg.compliance, [" ".join(out["transcript"])], policy.config_numbers(), [], 1000,      # the links the real guard allows
                        allowed_url_prefixes=(*form, *m.allowed_urls, *cfg.conversation.allowed_urls))
    if e.get("no_blocked_text"):
        for t in out["bot_texts"]:
            bad = [v.rule_id for v in failed(output_guard(t, ctx)) if v.verdict == "block" and v.rule_id != "numbers_provenance"]
            if bad:
                problems.append(f"sent text violates {bad}: {t[:80]}")
    for w in e.get("forbid_in_bot", []):
        from agentplatform.core.agent.guards import _affirmative_match, normalize
        if any(_affirmative_match(normalize(w), normalize(t)) for t in out["bot_texts"]):
            problems.append(f"bot said '{w}'")
    return not problems, "; ".join(problems)


async def record_experiment(dataset: str, variants: dict, metrics: dict, cost: Decimal) -> str:
    import subprocess

    from agentplatform.core.db.pool import platform_tx
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip() or None
    except Exception:
        commit = None
    async with platform_tx() as conn:
        row = await (await conn.execute(
            "insert into experiments (dataset, variants, metrics, status, cost_actual_usd, git_commit) values (%s,%s,%s,'done',%s,%s) returning id",
            (dataset, json.dumps(variants), json.dumps(metrics, default=str), cost, commit))).fetchone()
    return str(row["id"])
