"""Turn runner: fixed LangGraph workflow with the LLM at 3-4 decision points (plan 04, ADR-005/012/017).

Code decides transitions, tools, sending and permissions; the LLM understands, extracts and drafts.
Fencing: the commit is a CAS on turns(lease_id) + conversations(run_epoch) under the conversation advisory lock.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, TypedDict
from uuid import UUID

import httpx
from langgraph.graph import END, START, StateGraph

from agentplatform.core.agent import policy
from agentplatform.core.agent.guards import (
    OutputContext,
    disclosures_needed,
    ensure_identified,
    failed,
    input_guard,
    keyword_hit,
    normalize,
    output_guard,
    privacy_gate,
    with_disclosures,
)
from agentplatform.core.agent.prompts import render
from agentplatform.core.agent.state import AnalyzeOutput, Draft, GuardVerdict, JudgeVerdict, RetrievalResult
from agentplatform.core.comments.origin import load_origin, origin_comments
from agentplatform.core.config.schema import PlatformConfig
from agentplatform.core.db.pool import advisory_xact_lock, tenant_tx
from agentplatform.core.handoff.service import build_summary, create_handoff, create_notice
from agentplatform.core.knowledge.service import KnowledgeService
from agentplatform.core.media.processor import render_for_turn
from agentplatform.core.models.gateway import BudgetDenied, CallContext, ModelCallError, ModelGateway, tenant_context
from agentplatform.core.orchestration.conversations import conv_lock_key, stop_followups, utcnow
from agentplatform.core.outbox.store import enqueue, enqueue_reply
from agentplatform.core.packages.contract import SegmentPackage, profile_has

log = logging.getLogger(__name__)

SAFE_FAILURE = "safe_failure"
_SPEAKER = {"in": "cliente", "out_human": "vendedor"}
_NO_TURN_KINDS = ("comment", "shared", "deleted")
_NEEDS_A_PERSON_NOTES = {"image_refused", "document_refused", "audio_too_long", "audio_disabled", "transcription_failed", "download_failed"}
_OPENED_WITHOUT_TEXT = "[o cliente abriu a conversa por um anúncio ou link, ainda sem escrever nada]"
_URGENT_HANDOFFS = ("complaint_or_abuse", "legal", "requires_sensitive_data")   # always reach a person, whatever is open
# The customer gets the short "someone will answer" message: nothing the assistant could say would be approved or affordable.
_SAFE_FAILURE_REASONS = ("guard_blocked", "technical_failure", "budget_exhausted", "conversation_budget", "llm_call_cap")


class Superseded(Exception):
    """run_epoch/lease changed: a newer run or an ownership change owns the conversation now."""


class TurnState(TypedDict, total=False):
    tenant_id: str
    conversation_id: str
    turn_id: str
    lease_id: str
    run_epoch: int
    ownership: str
    stage: str
    turns_in_stage: int
    profile: dict
    lead_id: str
    contact_first_name: str | None
    recipient: str
    window_open: bool
    first_message: bool
    summary: str | None
    recent: list[dict]
    origin: list[dict]              # where the conversation came from: comment + post, story, ad (masked data)
    input_ids: list[str]
    input_texts: list[str]
    masked_input: list[str]
    media_notes: list[str]          # e.g. image_refused (config media.images = refuse_and_handoff)
    last_inbound_at: str | None
    # outputs
    route: str
    opt_out: bool
    injection: bool
    handoff_reason: str | None
    analysis: dict | None
    slot_updates: dict
    stage_decision: dict | None
    guidelines: list[str]
    next_question: str | None
    tool_results: list[dict]
    retrieval: dict | None
    draft_text: str | None
    fix_reasons: str | None
    attempts: int
    guard: list[dict]
    final_text: str | None
    degraded: bool
    llm_calls: int
    cost_usd: str
    trace: dict
    open_handoff: bool
    opted_out: bool                 # asked not to be contacted, earlier
    opted_out_returned: bool        # ... and wrote again: the seller hears about it, the assistant stays quiet
    failed_with: str | None         # exception class that ended the turn (technical_failure)
    _result: dict


@dataclass
class TurnDeps:
    config: PlatformConfig
    package: SegmentPackage
    gateway: ModelGateway | None
    knowledge: KnowledgeService | None
    config_version_id: UUID | None = None
    now: Any = utcnow
    dispatch_now: Any = None            # sends what is due for a conversation, for the one message that cannot wait for the turn to end


@dataclass
class TurnResult:
    status: str                         # committed | cancelled | no_input | handoff
    turn_id: UUID | None = None
    outbox_ids: list[UUID] = field(default_factory=list)
    handoff: dict | None = None
    trace: dict | None = None


class TurnRunner:
    def __init__(self, deps: TurnDeps, checkpointer=None):
        self.d = deps
        self.m = deps.package.manifest
        self.graph = self._build().compile(checkpointer=checkpointer)
        self._has_checkpointer = checkpointer is not None

    # =============================================================== public
    async def run(self, tenant_id: UUID, conversation_id: UUID) -> TurnResult:
        start = await self._start(tenant_id, conversation_id)
        if start is None:
            return TurnResult("no_input")
        state: TurnState = start
        thread = {"configurable": {"thread_id": f"{tenant_id}:{conversation_id}:{state['turn_id']}"}}
        waiting = asyncio.create_task(self._say_wait(state))
        try:
            kw = {"durability": "sync"} if self._has_checkpointer else {}   # LangGraph 1.2 fails on sync without saver
            async with asyncio.timeout(self.d.config.agent.turn_timeout_s):
                final = await self.graph.ainvoke(state, thread, **kw)
        except Superseded:
            await self._mark_cancelled(tenant_id, UUID(state["turn_id"]), "superseded")
            return TurnResult("cancelled", UUID(state["turn_id"]))
        except Exception as e:
            log.exception("turn %s failed", state["turn_id"])
            return await self._fail_safe(state, e)
        finally:
            waiting.cancel()
        r = final["_result"]
        return TurnResult(r["status"], UUID(r["turn_id"]), [UUID(x) for x in r["outbox_ids"]], r.get("handoff"), r.get("trace"))

    async def _say_wait(self, state: TurnState) -> None:
        """A slow turn tells the customer once (plan 07 §6): its own transaction, the current send_epoch, and every check the dispatcher makes
        before any message. Failing here never touches the turn."""
        cfg = self.d.config
        if not cfg.conversation.waiting_message:
            return
        await asyncio.sleep(cfg.agent.slow_after_s)
        tid, cid, turn_id = UUID(state["tenant_id"]), UUID(state["conversation_id"]), UUID(state["turn_id"])
        try:
            async with tenant_tx(tid) as conn:
                await advisory_xact_lock(conn, conv_lock_key(cid))
                row = await (await conn.execute(
                    """select c.send_epoch, c.ownership, t.status, l.opted_out_at from turns t join conversations c on c.id = t.conversation_id
                       join leads l on l.contact_id = c.contact_id where t.id=%s""", (turn_id,))).fetchone()
                if not row or row["status"] != "running" or row["ownership"] != "BOT" or row["opted_out_at"]:
                    return
                await enqueue(conn, tenant_id=tid, conversation_id=cid, effect_key=f"{turn_id}:wait", kind="dm_text",
                              text=cfg.conversation.waiting_message, recipient=state["recipient"], send_epoch=row["send_epoch"], priority=0,
                              max_staleness_s=cfg.concurrency.max_staleness_reply_s, turn_id=turn_id,
                              source_inbound_at=datetime.fromisoformat(state["last_inbound_at"]) if state.get("last_inbound_at") else None)
            if self.d.dispatch_now:
                await self.d.dispatch_now(tid, cid)
        except Exception:
            log.exception("waiting message of turn %s not sent", turn_id)

    async def _fail_safe(self, state: TurnState, error: Exception) -> TurnResult:
        """Anything unexpected ends the turn the same way a model outage does: the customer gets the short message and the seller is
        told. A stop request is still honored, and a conversation that is with a person stays silent. If even this cannot be
        written (database down), the exception reaches the queue, which retries the job."""
        turn_id = UUID(state["turn_id"])
        pg = privacy_gate(state["input_texts"], self.d.config.compliance)
        s: TurnState = {**state, "masked_input": pg.masked_texts, "degraded": True, "failed_with": type(error).__name__,
                        "trace": {**state.get("trace", {}), "failed_with": type(error).__name__}}
        if pg.opt_out:
            s |= {"opt_out": True, "route": "opt_out"}
        elif state["ownership"] != "BOT":
            s |= {"route": "silent"}
        else:
            s |= {"route": "handoff", "handoff_reason": "technical_failure"}
        try:
            r = (await self.n_commit(s))["_result"]
        except Superseded:
            await self._mark_cancelled(UUID(state["tenant_id"]), turn_id, "superseded")
            return TurnResult("cancelled", turn_id)
        return TurnResult(r["status"], UUID(r["turn_id"]), [UUID(x) for x in r["outbox_ids"]], r.get("handoff"), r.get("trace"))

    # =============================================================== start / context
    async def _start(self, tenant_id: UUID, conversation_id: UUID) -> TurnState | None:
        cfg = self.d.config
        async with tenant_tx(tenant_id) as conn:
            await advisory_xact_lock(conn, conv_lock_key(conversation_id))
            conv = await (await conn.execute(
                """select c.*, ci.external_user_id as recipient, l.id as lead_id, l.stage, l.profile, l.turns_in_stage, l.opted_out_at, ct.first_name
                   from conversations c join contacts ct on ct.id = c.contact_id
                   join contact_identities ci on ci.contact_id = c.contact_id
                   join leads l on l.contact_id = c.contact_id where c.id = %s""", (conversation_id,))).fetchone()
            pending = await (await conn.execute(
                """select id, text_masked, kind, media from messages
                   where conversation_id = %s and direction = 'in' and turn_id is null and kind <> all(%s)
                   order by provider_ts, created_at""",
                (conversation_id, list(_NO_TURN_KINDS)))).fetchall()
            if not pending:
                return None
            await conn.execute("update turns set status='cancelled', cancel_reason='replaced' where conversation_id=%s and status='running'",
                               (conversation_id,))
            turn_id, lease = uuid.uuid4(), uuid.uuid4()
            await conn.execute(
                """insert into turns (id, tenant_id, conversation_id, run_epoch, config_version_id, input_message_ids, status,
                                      lease_id, lease_expires_at, stage_before, heartbeat_at)
                   values (%s,%s,%s,%s,%s,%s,'running',%s, now() + make_interval(secs => %s), %s, now())""",
                (turn_id, tenant_id, conversation_id, conv["run_epoch"], self.d.config_version_id,
                 [p["id"] for p in pending], lease, cfg.concurrency.turn_lease_s, conv["stage"]))
            recent = await (await conn.execute(
                """select direction, text_masked from messages where conversation_id = %s and text_masked is not null
                   and (direction <> 'in' or turn_id is not null) and kind not in ('comment_reply', 'deleted')
                   order by provider_ts desc limit %s""",
                (conversation_id, cfg.conversation.max_messages_in_context))).fetchall()
            had_bot = await (await conn.execute(      # a public reply on a post is not an introduction in the direct
                "select 1 from messages where conversation_id=%s and direction='out_bot' and kind <> 'comment_reply' limit 1",
                (conversation_id,))).fetchone()
            origin = await load_origin(conn, conversation_id, conv["lead_id"], cfg.comments)
            open_handoff = await (await conn.execute(
                "select 1 from handoffs where conversation_id=%s and taken_at is null and not is_notice and created_at > now() - interval '24 hours' limit 1",
                (conversation_id,))).fetchone()
        now = self.d.now()
        texts = []
        for p in pending:
            t = p["text_masked"] or ""
            media = p["media"] or {}
            if media.get("attachments") and p["kind"] in ("audio", "image", "file", "video"):
                rendered = render_for_turn(p["kind"], media)
                t = t if rendered in t else (t + " " + rendered).strip()      # an audio's transcript is already its text
            if p["kind"] == "referral" and not t:
                t = _OPENED_WITHOUT_TEXT
            texts.append(t)
        return TurnState(
            tenant_id=str(tenant_id), conversation_id=str(conversation_id), turn_id=str(turn_id), lease_id=str(lease),
            run_epoch=conv["run_epoch"], ownership=conv["ownership"], stage=conv["stage"],
            turns_in_stage=conv["turns_in_stage"], profile=conv["profile"] or {}, lead_id=str(conv["lead_id"]),
            contact_first_name=conv["first_name"], recipient=conv["recipient"],
            window_open=bool(conv["window_expires_at"] and conv["window_expires_at"] > now),
            first_message=not had_bot, summary=conv["summary"], open_handoff=bool(open_handoff), opted_out=bool(conv["opted_out_at"]),
            recent=[{"from": _SPEAKER.get(r["direction"], "assistente"), "text": r["text_masked"]} for r in reversed(recent)],
            origin=origin,
            input_ids=[str(p["id"]) for p in pending], input_texts=texts,
            media_notes=[(p["media"] or {}).get("note") for p in pending if (p["media"] or {}).get("note")],
            last_inbound_at=conv["last_inbound_at"].isoformat() if conv["last_inbound_at"] else None,
            attempts=0, llm_calls=0, cost_usd="0", tool_results=[], guard=[], guidelines=[], slot_updates={},
            degraded=False, trace={},
        )

    # =============================================================== graph
    def _build(self) -> StateGraph:
        g = StateGraph(TurnState)
        g.add_node("privacy_gate", self.n_privacy)
        g.add_node("silent_update", self.n_silent)
        g.add_node("input_guard", self.n_input_guard)
        g.add_node("analyze_turn", self.n_analyze)
        g.add_node("plan", self.n_plan)
        g.add_node("retrieve", self.n_retrieve)
        g.add_node("respond", self.n_respond)
        g.add_node("output_guard", self.n_output_guard)
        g.add_node("degraded_reply", self.n_degraded)
        g.add_node("commit", self.n_commit)
        g.add_edge(START, "privacy_gate")
        g.add_conditional_edges("privacy_gate", lambda s: s["route"],
                                {"opt_out": "commit", "silent": "silent_update", "continue": "input_guard"})
        g.add_edge("silent_update", "commit")
        g.add_conditional_edges("input_guard", lambda s: s["route"], {"handoff": "commit", "continue": "analyze_turn"})
        g.add_conditional_edges("analyze_turn", lambda s: s["route"], {"plan": "plan", "degraded": "degraded_reply"})
        g.add_conditional_edges("plan", lambda s: s["route"],
                                {"opt_out": "commit", "handoff": "commit", "retrieve": "retrieve", "respond": "respond"})
        g.add_conditional_edges("retrieve", lambda s: s["route"], {"respond": "respond", "degraded": "degraded_reply"})
        g.add_conditional_edges("respond", lambda s: s["route"],
                                {"output_guard": "output_guard", "degraded": "degraded_reply", "handoff": "commit"})
        g.add_conditional_edges("output_guard", lambda s: s["route"],
                                {"respond": "respond", "commit": "commit", "handoff": "commit"})
        g.add_edge("degraded_reply", "commit")
        g.add_edge("commit", END)
        return g

    # ---------------------------------------------------------------- helpers
    def _ctx(self, s: TurnState, node: str) -> CallContext:
        return tenant_context(self.d.config.budget, UUID(s["tenant_id"]), node, conversation_id=s["conversation_id"],
                              turn_id=s["turn_id"])

    async def _ensure_current(self, s: TurnState) -> None:
        async with tenant_tx(UUID(s["tenant_id"])) as conn:
            row = await (await conn.execute(
                """select c.run_epoch, c.ownership, t.lease_id, t.status from conversations c join turns t on t.id = %s
                   where c.id = %s""", (s["turn_id"], s["conversation_id"]))).fetchone()
            await conn.execute("update turns set heartbeat_at = now() where id = %s", (s["turn_id"],))
        if row is None or row["run_epoch"] != s["run_epoch"] or str(row["lease_id"]) != s["lease_id"] \
                or row["status"] != "running":
            raise Superseded()

    def _llm_budget_left(self, s: TurnState) -> bool:
        return s["llm_calls"] < self.d.config.agent.max_llm_calls_per_turn

    async def _call(self, s: TurnState, node: str, spec, messages, out_model):
        await self._ensure_current(s)
        if self.d.gateway is None:
            raise ModelCallError("no gateway")
        r = await self.d.gateway.chat(spec, messages, self._ctx(s, node), out_model)
        return r, {"llm_calls": s["llm_calls"] + 1, "cost_usd": str(Decimal(s["cost_usd"]) + r.cost_usd)}

    def _created_links(self, s: TurnState) -> tuple[str, ...]:
        return tuple(t["result"]["url"] for t in s.get("tool_results", []) if isinstance(t.get("result"), dict) and t["result"].get("url"))

    def _allowed_urls(self, s: TurnState) -> tuple[str, ...]:
        return (*self.m.allowed_urls, *self.d.config.conversation.allowed_urls, *self._created_links(s))     # own form links only when created in THIS turn

    def _final(self, s: TurnState, text: str) -> str:
        """Code-owned completion of an approved reply: a link created this turn is never lost, and the official
        disclosure of the tool whose numbers the reply uses is always present."""
        for url in self._created_links(s):
            if url not in text:
                text = f"{text.rstrip()} {url}"
        customer = [*s["masked_input"], *(m["text"] for m in s["recent"] if m["from"] == "cliente")]
        return self._identified(s, with_disclosures(text, disclosures_needed(text, s.get("tool_results", []), customer,
                                                                             self.d.config.compliance.required_disclosures)))

    def _identified(self, s: TurnState, text: str) -> str:
        cfg = self.d.config
        persona = cfg.conversation.persona
        if not (s.get("first_message") and persona.disclose_on_first_message):
            return text
        return ensure_identified(text, cfg.conversation.first_message_disclosure.format(seller=persona.seller_display_name), cfg.comments.disclosure_terms,
                                 cfg.conversation.greeting_pattern)

    def _fmt_recent(self, s: TurnState) -> str:
        return "\n".join(f"- {m['from']}: {m['text']}" for m in s["recent"]) or "(início da conversa)"

    def _fmt_profile(self, profile: dict) -> str:
        return json.dumps({k: v.get("value") for k, v in profile.items() if isinstance(v, dict)}, ensure_ascii=False)

    # ---------------------------------------------------------------- nodes
    async def n_privacy(self, s: TurnState) -> dict:
        pg = privacy_gate(s["input_texts"], self.d.config.compliance)
        if pg.opt_out:
            return {"route": "opt_out", "opt_out": True, "masked_input": pg.masked_texts}
        if s.get("opted_out"):       # asked us to stop and wrote again: whether the assistant may answer is not its own call
            return {"route": "silent", "opted_out_returned": True, "masked_input": pg.masked_texts}
        if s["ownership"] != "BOT":
            return {"route": "silent", "masked_input": pg.masked_texts}
        return {"route": "continue", "masked_input": pg.masked_texts}

    async def n_silent(self, s: TurnState) -> dict:
        # no LLM spend while a human owns the conversation; messages are consumed by the commit
        return {"route": "silent"}

    async def n_input_guard(self, s: TurnState) -> dict:
        text = "\n".join(s["masked_input"])
        r = input_guard(text, self.d.config.input_guard, self.m, self.d.config.handoff.disabled_reasons)
        upd = {"injection": "injection_suspected" in r.reasons, "trace": {**s["trace"], "input_guard": r.reasons}}
        if r.action == "handoff":
            if s.get("open_handoff") and r.handoff_reason not in _URGENT_HANDOFFS:
                # already asked: do not re-notify nor repeat the expectation message; the bot keeps helping
                return {**upd, "route": "continue", "guidelines": [self.d.config.handoff.already_notified_guideline]}
            return {**upd, "route": "handoff", "handoff_reason": r.handoff_reason}
        return {**upd, "route": "continue"}

    async def n_analyze(self, s: TurnState) -> dict:
        nonce = secrets.token_hex(4)
        msgs = render(self.d.config.models.analyze_turn.prompt_version, nonce=nonce,
                      slot_catalog=self.m.slot_catalog(), intent_catalog=self.m.intent_catalog(),
                      objection_catalog=", ".join(self.m.objections), scope=self.m.scope or "o negócio do cliente",
                      stage_catalog=", ".join(x.name for x in self.m.stages if not x.marked_by_seller_only),
                      stage=s["stage"], profile=self._fmt_profile(s["profile"]), summary=s.get("summary") or "-",
                      recent=self._fmt_recent(s), turn_input="\n".join(f"- {t}" for t in s["masked_input"]),
                      origin=s.get("origin") or [])
        try:
            r, cost = await self._call(s, "analyze_turn", self.d.config.models.analyze_turn, msgs, AnalyzeOutput)
        except BudgetDenied as e:
            return {"route": "degraded", "degraded": True, "handoff_reason": _budget_reason(e)}
        except ModelCallError:
            return {"route": "degraded", "degraded": True, "handoff_reason": "technical_failure"}
        return {**cost, "route": "plan", "analysis": r.parsed.model_dump()}

    async def n_plan(self, s: TurnState) -> dict:
        cfg = self.d.config
        a = AnalyzeOutput.model_validate(s["analysis"])
        u = a.understanding
        turn_text = "\n".join(s["masked_input"])
        wants_out = policy.opt_out_from_model(u.opt_out_intent, bool(s.get("injection")), cfg.input_guard)
        if wants_out == "apply":
            return {"route": "opt_out", "opt_out": True, "trace": {**s["trace"], "intents": u.intents, "opt_out_intent": u.opt_out_intent}}
        if wants_out == "confirm":
            u.intents.append("opt_out")
        if policy.yes_to_another_question(self.m, turn_text, [m["text"] for m in s["recent"] if m["from"] == "assistente"]):
            u.intents = [i for i in u.intents if i not in policy.hot_intents(self.m)]     # agreeing is not deciding: the person is asked next
        recent_customer = "\n".join([*(m["text"] for m in s["recent"] if m["from"] == "cliente"), *origin_comments(s.get("origin"))])
        slots = policy.apply_slots(self.m, s["profile"], u, turn_text, cfg.funnel,
                                   s["input_ids"][-1] if s["input_ids"] else None, recent_customer)
        profile_after = {**s["profile"], **slots.accepted}
        stage_dec = policy.decide_stage(self.m, s["stage"], a.stage_proposal, profile_after, cfg.funnel,
                                        filled_this_turn=bool(slots.accepted))
        if stage_dec.applied and stage_dec.to == self.m.form_stage and policy.stops_asking(self.m, u):
            stage_dec = policy.StageDecision(False, s["stage"], stage_dec.to, "customer_said_no")     # a next step, and its reminder, wait for another turn
        stage_now = stage_dec.to if stage_dec.applied else s["stage"]
        flags = cfg.operations.feature_flags
        sim_enabled = bool(flags.get(f"{self.m.name}.simulation_enabled"))
        reason = policy.handoff_reason(self.m, u, sim_enabled, stage_now, s["turns_in_stage"], cfg.funnel, cfg.handoff.disabled_reasons)
        if not reason and "hot_lead" not in cfg.handoff.disabled_reasons and policy.accepted_the_offer(
                self.m, turn_text, [m["text"] for m in s["recent"] if m["from"] == "assistente"]):
            reason = "hot_lead"
        if not reason and set(s.get("media_notes") or []) & _NEEDS_A_PERSON_NOTES:
            reason = "media_review"      # images/documents the tenant chose not to process, or an audio the assistant could not hear
        tool_results = await self._run_tools(s, stage_now, profile_after, u, set(slots.accepted), turn_text)
        if not reason and "simulation_request" in u.intents and any(
                t["tool"].startswith("simulate") and not (t.get("result") or {}).get("options") for t in tool_results):
            reason = "simulation_request"     # asked for a simulation the table could not answer: the seller does it
        guidelines = s.get("guidelines", []) + policy.select_guidelines(
            self.m, stage_now, u, turn_text, s["turns_in_stage"], cfg.guidelines,
            tools=[t["tool"] for t in tool_results if (t.get("result") or {}).get("options")], funnel=cfg.funnel)
        if reason and reason not in _URGENT_HANDOFFS and s.get("open_handoff"):
            reason = None
            guidelines.insert(0, cfg.handoff.already_notified_guideline)
        if "opt_out" in u.intents:
            guidelines.insert(0, cfg.guidelines.opt_out_check)
        resend = stage_now == self.m.form_sent_stage and any(keyword_hit(k, normalize(turn_text)) for k in self.m.form_resend_keywords)
        if (stage_now == self.m.form_stage or resend) and self.m.form_sent_stage and not s.get("handoff_reason"):
            # code-driven next step: single-use pre-registration link (core tool), then the "form sent" stage
            from agentplatform.core.tools.lead_form import create_link
            from agentplatform.settings import get_settings
            async with tenant_tx(UUID(s["tenant_id"])) as conn:
                url = await create_link(conn, UUID(s["tenant_id"]), UUID(s["lead_id"]), get_settings().public_base_url)
            tool_results.append({"tool": "create_lead_form_link", "result": {"url": url}})
            guidelines.insert(0, cfg.guidelines.form_link.format(url=url, seller=cfg.conversation.persona.seller_display_name))
            if not resend and self.m.allowed(stage_now, self.m.form_sent_stage):
                stage_dec = policy.StageDecision(True, s["stage"], self.m.form_sent_stage, "form_link_sent")
                stage_now = self.m.form_sent_stage
        next_q = None if policy.stops_asking(self.m, u) else self._closing_question(s, tool_results, self._next_question(s, profile_after, stage_now))
        # a tool's closing question the flow did not pick stays out of the prompt, or the model ends with it instead
        tool_results = [{**t, "result": {k: v for k, v in t["result"].items() if k != "closing_question"}}
                        if isinstance(t.get("result"), dict) and t["result"].get("closing_question") not in (None, next_q) else t
                        for t in tool_results]
        upd = {"slot_updates": slots.accepted, "profile": profile_after, "guidelines": guidelines,
               "stage_decision": {"applied": stage_dec.applied, "from": stage_dec.frm, "to": stage_dec.to,
                                  "reason": stage_dec.reason, "regression": stage_dec.regression,
                                  "confidence": a.stage_proposal.confidence, "evidence": a.stage_proposal.evidence},
               "stage": stage_now,
               "next_question": next_q,
               "tool_results": tool_results,
               "trace": {**s["trace"], "intents": u.intents, "slots_rejected": slots.rejected}}
        if reason:
            return {**upd, "route": "handoff", "handoff_reason": reason}
        if policy.needs_knowledge(u) and self.d.knowledge is not None:
            return {**upd, "route": "retrieve"}
        return {**upd, "route": "respond"}

    async def _run_tools(self, s: TurnState, stage: str, profile: dict, u, filled: set[str], turn_text: str = "") -> list[dict]:
        """Code-driven tool use (principle 1): tools run when the flow allows, never chosen freely by the LLM. A simulation
        runs when asked with the minimum profile, or unasked (proactive_when) as soon as one of its inputs is filled, or again
        when the person will show it to someone (offer_again_keywords), so its text can go out as a summary."""
        flags = self.d.config.operations.feature_flags
        results = []
        for spec in self.d.package.tools():
            if spec.feature_flag and not flags.get(spec.feature_flag):
                continue
            own = self.d.config.tools.get(spec.name)
            if own and not own.enabled:
                continue
            stages = own.allowed_stages if own and own.allowed_stages is not None else spec.allowed_stages
            if stages != "*" and stage not in stages:
                continue
            if len(results) >= self.d.config.agent.max_tool_calls_per_turn:
                break
            proactive_ready = bool(spec.proactive_when) and profile_has(profile, spec.proactive_when)
            asked = ("simulation_request" in u.intents or "price_question" in u.intents) and \
                (self.m.profile_complete(profile) or proactive_ready)
            offered = proactive_ready and bool(filled & set(spec.input_model.model_fields))
            again = proactive_ready and any(keyword_hit(k, normalize(turn_text)) for k in self.m.offer_again_keywords)
            if spec.name.startswith("simulate") and (asked or offered or again):
                args = {k: v.get("value") for k, v in profile.items() if isinstance(v, dict)}
                try:
                    inp = spec.input_model.model_validate(args)
                    out = await asyncio.wait_for(spec.fn(_ToolCtx(self, s), inp), own.timeout_s if own else None)
                    results.append({"tool": spec.name, "result": json.loads(out.model_dump_json())})
                except Exception as e:      # tool error -> summarized, never numbers
                    results.append({"tool": spec.name, "error": "ferramenta indisponível; não informe números",
                                    "detail": type(e).__name__})
        return results

    async def n_retrieve(self, s: TurnState) -> dict:
        a = AnalyzeOutput.model_validate(s["analysis"])
        questions = a.understanding.questions[: self.d.config.rag.max_questions_per_turn] or ["\n".join(s["masked_input"])]
        results: list[RetrievalResult] = []
        try:
            async with tenant_tx(UUID(s["tenant_id"])) as conn:
                for q in questions:
                    results.append(await self.d.knowledge.retrieve(conn, UUID(s["tenant_id"]), q, self._ctx(s, "retrieve")))
        except BudgetDenied as e:
            return {"route": "degraded", "degraded": True, "handoff_reason": _budget_reason(e)}
        except (ModelCallError, httpx.HTTPError):       # search unavailable: approved FAQ only, never an answer from memory
            return {"route": "degraded", "degraded": True, "handoff_reason": "technical_failure"}
        merged = RetrievalResult(
            faq_answer=next((r.faq_answer for r in results if r.faq_answer), None),
            faq_id=next((r.faq_id for r in results if r.faq_id), None),      # the turn's record says which answer was used
            chunks=[c for r in results for c in r.chunks][:self.d.config.rag.max_chunks_per_turn],
            no_basis=all(r.no_basis for r in results))
        return {"route": "respond", "retrieval": merged.model_dump()}

    def _knowledge_block(self, s: TurnState) -> str:
        r = RetrievalResult.model_validate(s["retrieval"]) if s.get("retrieval") else None
        if r is None:
            return "(não consultado)"
        if r.faq_answer:
            return f"[faq aprovada, citable] {r.faq_answer}"
        if r.no_basis:
            return "no_basis"
        return "\n".join(f"[{c.id}][{c.visibility}] {c.text}" for c in r.chunks)

    def _closing_question(self, s: TurnState, tool_results: list[dict], next_q: str | None) -> str | None:
        """A tool may close with its own question, ahead of offering the person (who is then offered on the next turn). A question
        the flow still needs comes first, and once the person was offered, a yes to anything else would not count as accepting."""
        closing = next((t["result"]["closing_question"] for t in tool_results
                        if isinstance(t.get("result"), dict) and t["result"].get("closing_question")), None)
        offered = policy.offered_the_person(self.m, [m["text"] for m in s["recent"] if m["from"] == "assistente"])
        return closing if closing and next_q in (None, self.m.ready_question) and not offered else next_q

    def _required_reply(self, s: TurnState, text: str) -> str:
        """A tool's required_text goes out word for word: the model has already reworded numbers away, so if it did not copy it, the
        reply becomes that text and the turn's question. Said in the last message, it is not repeated."""
        required = next((t["result"]["required_text"] for t in s.get("tool_results", [])
                         if isinstance(t.get("result"), dict) and t["result"].get("required_text")), None)
        last = [m["text"] for m in s["recent"] if m["from"] == "assistente"][-1:]
        if not required or normalize(required) in normalize(text) or any(normalize(required) in normalize(b) for b in last):
            return text
        return " ".join(x for x in (required, s.get("next_question")) if x)

    def _next_question(self, s: TurnState, profile_after: dict, stage_now: str) -> str | None:
        n = self.d.config.funnel.question_cooldown_turns
        recent_bot = [m["text"] for m in s["recent"] if m["from"] == "assistente"][-n:] if n else []
        if stage_now == self.m.form_stage and self.m.ready_question:     # once: it does not come back after the cooldown
            bot = [m["text"] for m in s["recent"] if m["from"] == "assistente"]
            if policy.offered_the_person(self.m, bot):
                return None
            if self.m.profile_question and not policy.knows_any(profile_after, self.m.profile_slots) \
                    and not policy.recently_asked(self.m.profile_question, bot):
                return self.m.profile_question       # once, ahead of the person: what they need to pick the right option
            return self.m.ready_question
        if self.m.flow_stages and stage_now not in self.m.flow_stages:
            return None
        # a question just asked and not answered is not repeated: the flow moves on to the next required one; optional
        # questions only once the required ones are answered (never a side question while the essentials are missing)
        pending = policy.pending_questions(self.m, profile_after, optional=False) or policy.pending_questions(self.m, profile_after)
        return next((q for q in pending if not policy.recently_asked(q, recent_bot)), None)

    async def n_respond(self, s: TurnState) -> dict:
        cfg = self.d.config
        if not self._llm_budget_left(s):
            return {"route": "handoff", "handoff_reason": "llm_call_cap"}
        st = self.m.stage(s["stage"])
        persona = self.m.persona_text(cfg.conversation.persona.seller_display_name, cfg.conversation.persona.company_name)
        msgs = render(cfg.models.respond.prompt_version, nonce=secrets.token_hex(4), persona=persona,
                      seller_name=cfg.conversation.persona.seller_display_name,
                      first_message=s["first_message"], disclose=cfg.conversation.persona.disclose_on_first_message,
                      stage=s["stage"], stage_goal=st.goal, guidelines=s["guidelines"], next_question=s.get("next_question"),
                      fix_reasons=s.get("fix_reasons"), profile=self._fmt_profile(s["profile"]),
                      require_with=cfg.compliance.require_with,
                      reply_examples=[e.model_dump() for e in [*self.m.reply_examples, *cfg.conversation.reply_examples]],
                      assistant_name=cfg.conversation.persona.assistant_name,
                      tone_instruction=cfg.conversation.tone_instructions.get(cfg.conversation.persona.tone, ""),
                      business_instructions=cfg.conversation.business_instructions.strip(),
                      summary=s.get("summary") or "-", recent=self._fmt_recent(s), origin=s.get("origin") or [],
                      turn_input="\n".join(f"- {t}" for t in s["masked_input"]), knowledge=self._knowledge_block(s),
                      tool_results=json.dumps(s["tool_results"], ensure_ascii=False) if s["tool_results"] else "(nenhum)")
        try:
            r, cost = await self._call(s, "respond", cfg.models.respond, msgs, Draft)
        except BudgetDenied as e:
            return {"route": "degraded", "degraded": True, "handoff_reason": _budget_reason(e)}
        except ModelCallError:
            return {"route": "handoff", "handoff_reason": "technical_failure"}
        return {**cost, "route": "output_guard", "draft_text": r.parsed.text.strip(), "attempts": s["attempts"] + 1}

    def _fixed_question(self, s: TurnState, text: str) -> str:
        """The question that offers the person, and the one before it, go out as written: reworded, a short yes to them is not
        recognised, and the person is never called. Any other question the model wrote is dropped for it."""
        q = s.get("next_question")
        if not q or q not in (self.m.ready_question, self.m.profile_question):
            return text
        kept = policy.offered_the_person(self.m, [text]) if q == self.m.ready_question else policy.recently_asked(q, [text])
        if kept:
            return text
        sents = [x for x in re.split(r"(?<=[.!?])\s+", text.strip()) if x and not x.endswith("?")]
        return " ".join([*sents, q])

    async def n_output_guard(self, s: TurnState) -> dict:
        cfg = self.d.config
        text = self._fixed_question(s, self._required_reply(s, s["draft_text"] or ""))
        r = RetrievalResult.model_validate(s["retrieval"]) if s.get("retrieval") else RetrievalResult()
        sources = [*s["masked_input"], *(m["text"] for m in s["recent"] if m["from"] == "cliente"), *origin_comments(s.get("origin")),
                   *(c.text for c in r.chunks), *([r.faq_answer] if r.faq_answer else []),
                   *(json.dumps(t, ensure_ascii=False) for t in s["tool_results"]),
                   self._fmt_profile(s["profile"])]     # slots only exist with evidence in a customer message; older ones left `recent`
        ctx = OutputContext(compliance=cfg.compliance, sources_text=sources, allowed_numbers=policy.config_numbers(),
                            recent_bot_texts=[m["text"] for m in s["recent"] if m["from"] == "assistente"][-cfg.output_guard.repetition_window:],
                            max_bytes=cfg.output_guard.max_bytes, strict=s.get("injection", False),
                            guidance_texts=tuple(c.text for c in r.chunks if c.visibility == "guidance_only"),
                            guidance_max_ngram=cfg.output_guard.guidance_only_max_ngram,
                            no_basis=r.no_basis and not r.faq_answer and not r.chunks,
                            no_basis_claims=tuple(cfg.output_guard.no_basis_claim_patterns),
                            repetition_threshold=cfg.output_guard.repetition_rewrite_similarity,
                            repetition_overlap=cfg.output_guard.repetition_word_overlap,
                            repetition_min_words=cfg.output_guard.repetition_min_words,
                            allowed_url_prefixes=self._allowed_urls(s), required_urls=self._created_links(s))
        verdicts = output_guard(text, ctx)
        bad = failed(verdicts)
        if any(re.search(p, normalize(text)) for p in cfg.output_guard.opt_out_promise_patterns):
            bad.append(GuardVerdict(guard="output_guard", rule_id="opt_out_promise", verdict="block"))
        a = AnalyzeOutput.model_validate(s["analysis"]) if s.get("analysis") else None
        if not any(v.verdict == "block" for v in bad) and a and policy.is_risky(a.understanding) and self._llm_budget_left(s):
            try:
                jmsgs = render(cfg.models.output_judge.prompt_version, draft=text, extra_rules=cfg.compliance.extra_rules)
                jr, cost = await self._call(s, "output_judge", cfg.models.output_judge, jmsgs, JudgeVerdict)
                s = {**s, **cost}
                for vio in jr.parsed.violations:
                    bad.append(GuardVerdict(guard="output_judge", rule_id=vio, verdict="block", detail=jr.parsed.explanation[:200]))
            except (BudgetDenied, ModelCallError):
                pass   # rules already passed; the judge is an extra layer
        guard_log = s["guard"] + [v.model_dump() for v in (bad or verdicts[:0])]
        upd = {"guard": guard_log, "llm_calls": s["llm_calls"], "cost_usd": s["cost_usd"]}
        if not bad:
            return {**upd, "route": "commit", "final_text": self._final(s, text)}
        blocks = [v for v in bad if v.verdict == "block"]
        if s["attempts"] < cfg.agent.max_reply_attempts and self._llm_budget_left(s):
            reasons = ", ".join(f"{v.rule_id}{(': ' + v.detail) if v.detail and v.rule_id != 'forbidden_phrase' else ''}" for v in bad)
            return {**upd, "route": "respond", "fix_reasons": reasons}
        if blocks:
            return {**upd, "route": "handoff", "handoff_reason": "guard_blocked", "final_text": None}
        if any(v.rule_id == "one_question" for v in bad) and "?" in text:
            text = text[: text.index("?") + 1]                       # deterministic: keep only the first question
        return {**upd, "route": "commit", "final_text": self._final(s, text)}      # rewrite-only issues after retry: send

    async def n_degraded(self, s: TurnState) -> dict:
        """Budget exhausted / model down: FAQ via FTS (no LLM, no embedding), else handoff with a short message."""
        if self.d.knowledge is not None:
            async with tenant_tx(UUID(s["tenant_id"])) as conn:
                r = await self.d.knowledge.retrieve(conn, UUID(s["tenant_id"]), "\n".join(s["masked_input"]),
                                                    self._ctx(s, "degraded"), degraded=True)
            if r.faq_answer:
                return {"route": "commit", "final_text": self._identified(s, r.faq_answer), "degraded": True}
        return {"route": "handoff", "handoff_reason": s.get("handoff_reason") or "budget_exhausted", "degraded": True}

    # ---------------------------------------------------------------- commit
    async def n_commit(self, s: TurnState) -> dict:
        cfg = self.d.config
        tid, cid, turn_id = UUID(s["tenant_id"]), UUID(s["conversation_id"]), UUID(s["turn_id"])
        now = self.d.now()
        outbox_ids: list[UUID] = []
        handoff = None
        handoff_reason = s.get("handoff_reason") if s.get("route") == "handoff" else None
        status = "committed"
        async with tenant_tx(tid) as conn:
            await advisory_xact_lock(conn, conv_lock_key(cid))
            t = await (await conn.execute(
                "update turns set heartbeat_at = now() where id=%s and status='running' and lease_id=%s returning id",
                (turn_id, s["lease_id"]))).fetchone()
            conv = await (await conn.execute("select * from conversations where id=%s for update", (cid,))).fetchone()
            if t is None or conv["run_epoch"] != s["run_epoch"]:
                raise Superseded()
            await conn.execute("update messages set turn_id = %s where id = any(%s::uuid[])", (turn_id, s["input_ids"]))
            src_at = datetime.fromisoformat(s["last_inbound_at"]) if s.get("last_inbound_at") else None

            if s.get("opt_out"):
                outbox_ids += await self._commit_opt_out(conn, s, conv, now)
                status = "committed"
            elif s.get("route") == "silent":
                status = "committed"
                if s.get("opted_out_returned"):
                    await self._notify_seller(conn, s, "opted_out_returned", now)
            else:
                # lead profile and stage
                sd = s.get("stage_decision") or {}
                applied = bool(sd.get("applied"))
                await conn.execute(
                    """update leads set profile = %s, stage = %s,
                              stage_changed_at = case when %s then now() else stage_changed_at end,
                              turns_in_stage = case when %s then 0 else turns_in_stage + 1 end,
                              is_qualified = is_qualified or %s, qualified_at = coalesce(qualified_at, case when %s then now() end)
                       where id = %s""",
                    (json.dumps(s["profile"]), s["stage"], applied, applied, self.m.profile_complete(s["profile"]),
                     self.m.profile_complete(s["profile"]), s["lead_id"]))
                if sd:
                    await conn.execute(
                        """insert into lead_stage_history (tenant_id, lead_id, from_stage, to_stage, proposed_by, confidence,
                                                           evidence, turn_id, applied, rejected_reason)
                           values (%s,%s,%s,%s,'classifier',%s,%s,%s,%s,%s)""",
                        (tid, s["lead_id"], sd["from"], sd["to"], sd.get("confidence"), json.dumps(sd.get("evidence")),
                         turn_id, applied, None if applied else sd.get("reason")))
                if s.get("final_text") and not handoff_reason:
                    outbox_ids += await enqueue_reply(
                        conn, tenant_id=tid, conversation_id=cid, turn_id=turn_id, text=s["final_text"],
                        recipient=s["recipient"], send_epoch=conv["send_epoch"], max_bytes=1000,
                        max_staleness_s=cfg.concurrency.max_staleness_reply_s, source_inbound_at=src_at)
                if handoff_reason:
                    handoff = await self._commit_handoff(conn, s, conv, handoff_reason, now, src_at)
                    outbox_ids += handoff.pop("outbox_ids", [])
                    status = "handoff"
                elif (s.get("retrieval") or {}).get("no_basis") and s.get("final_text"):
                    # the reply says the seller will confirm: without this, nobody would ever hear about the question
                    await self._notify_seller(conn, s, "knowledge_gap", now)
            for v in s.get("guard") or []:
                await conn.execute(
                    "insert into guard_results (tenant_id, turn_id, guard, rule_id, verdict, detail) values (%s,%s,%s,%s,%s,%s)",
                    (tid, turn_id, v["guard"], v["rule_id"], v["verdict"], json.dumps({"detail": v.get("detail")})))
            trace = {**s.get("trace", {}), "stage_decision": s.get("stage_decision"), "guidelines": s.get("guidelines"),
                     "retrieval_ids": [c["id"] for c in (s.get("retrieval") or {}).get("chunks", [])],
                     "faq": (s.get("retrieval") or {}).get("faq_id"), "no_basis": bool((s.get("retrieval") or {}).get("no_basis")),
                     "prompt": cfg.models.respond.prompt_version, "tools": [t.get("tool") for t in s.get("tool_results", [])],
                     "degraded": s.get("degraded"), "handoff": handoff_reason, "attempts": s.get("attempts")}
            await conn.execute(
                """update turns set status=%s, stage_after=%s, llm_calls=%s, cost_usd=%s, finished_at=now(), trace=%s,
                                    cancel_reason=%s where id=%s""",
                ("failed" if s.get("failed_with") else status, s.get("stage"), s.get("llm_calls", 0), s.get("cost_usd", "0"),
                 json.dumps(trace, default=str), s.get("failed_with"), turn_id))
            await conn.execute("update conversations set consecutive_restarts = 0 where id=%s", (cid,))
        h = {k: str(v) for k, v in handoff.items()} if handoff else None
        return {"_result": {"status": status, "turn_id": str(turn_id), "outbox_ids": [str(x) for x in outbox_ids],
                            "handoff": h, "trace": json.loads(json.dumps(trace, default=str))}}

    async def _notify_seller(self, conn, s: TurnState, reason: str, now: datetime) -> None:
        await create_notice(conn, self.d.config, tenant_id=UUID(s["tenant_id"]), conversation_id=UUID(s["conversation_id"]),
                            lead_id=UUID(s["lead_id"]), reason=reason, now=now,
                            summary=build_summary(s.get("contact_first_name"), s["stage"], reason, s["profile"],
                                                  (s.get("masked_input") or [""])[-1]))

    async def _commit_opt_out(self, conn, s: TurnState, conv, now: datetime) -> list[UUID]:
        tid, cid = UUID(s["tenant_id"]), UUID(s["conversation_id"])
        from agentplatform.core.security.keys import crypto
        await conn.execute(
            """insert into consents (tenant_id, contact_id, purpose, legal_basis, status, evidence, channel)
               values (%s,%s,'marketing','consent','revoked',%s,'instagram')""",
            (tid, conv["contact_id"], json.dumps({"turn_id": s["turn_id"], "message_ids": s["input_ids"]})))
        await conn.execute(
            "insert into suppression_list (tenant_id, identifier_hash, reason) values (%s,%s,'opt_out') on conflict do nothing",
            (tid, crypto().lookup_hash(s["recipient"], kind="igsid")))
        await conn.execute("update leads set opted_out_at = now(), stage = %s where id = %s",
                           (self.m.opt_out_stage, s["lead_id"]))
        await stop_followups(conn, cid, "opt_out")
        await conn.execute("update outbox set status='cancelled', cancel_reason='opt_out' where conversation_id=%s and status='pending' "
                           "and kind <> 'hide_comment'", (cid,))
        row = await (await conn.execute(
            "update conversations set send_epoch = send_epoch + 1 where id=%s returning send_epoch", (cid,))).fetchone()
        if s["ownership"] == "BOT":
            confirmation = self.d.config.conversation.opt_out_confirmation.format(
                seller=self.d.config.conversation.persona.seller_display_name)
            oid = await enqueue(conn, tenant_id=tid, conversation_id=cid, effect_key=f"{s['turn_id']}:optout", kind="dm_text",
                                text=confirmation, recipient=s["recipient"], send_epoch=row["send_epoch"], priority=0, max_staleness_s=3600,
                                turn_id=UUID(s["turn_id"]), extra={"is_optout_confirmation": True})
            return [oid] if oid else []
        await self._notify_seller(conn, s, "opt_out", now)      # a person is on this conversation and must not keep writing to someone who asked to stop
        return []

    async def _commit_handoff(self, conn, s: TurnState, conv, reason: str, now: datetime, src_at) -> dict:
        cfg = self.d.config
        tid, cid = UUID(s["tenant_id"]), UUID(s["conversation_id"])
        open_same = await (await conn.execute(
            "select id from handoffs where conversation_id=%s and reason=%s and taken_at is null and not is_notice limit 1",
            (cid, reason))).fetchone()
        if open_same:                # off hours the assistant keeps serving: same problem, same seller alert, no repeated message
            return {"handoff_id": open_same["id"], "mode": "already_open", "outbox_ids": []}
        summary = build_summary(s.get("contact_first_name"), s["stage"], reason, s["profile"],
                                (s.get("masked_input") or [""])[-1])
        h = await create_handoff(conn, cfg, tenant_id=tid, conversation_id=cid,
                                 lead_id=UUID(s["lead_id"]), reason=reason, summary=summary, now=now)
        ids = []
        if cfg.handoff.send_expectation_message and s.get("window_open", True):
            seller = cfg.conversation.persona.seller_display_name
            text = (cfg.handoff.expectation_message_in_hours if h["mode"] == "awaiting"
                    else cfg.handoff.expectation_message_off_hours).format(seller=seller)
            if reason in _SAFE_FAILURE_REASONS:
                text = cfg.conversation.safe_failure_message.format(seller=seller)
            epoch = (await (await conn.execute("select send_epoch from conversations where id=%s", (cid,))).fetchone())["send_epoch"]
            oid = await enqueue(conn, tenant_id=tid, conversation_id=cid, effect_key=f"{s['turn_id']}:handoff",
                                kind="handoff_expectation", text=text, recipient=s["recipient"], send_epoch=epoch,
                                priority=0, max_staleness_s=cfg.concurrency.max_staleness_reply_s,
                                turn_id=UUID(s["turn_id"]), source_inbound_at=src_at)
            if oid:
                ids.append(oid)
        return {**h, "outbox_ids": ids}

    async def _mark_cancelled(self, tenant_id: UUID, turn_id: UUID, reason: str) -> None:
        async with tenant_tx(tenant_id) as conn:
            await conn.execute("update turns set status='cancelled', cancel_reason=%s, finished_at=now() where id=%s and status='running'",
                               (reason, turn_id))


def _budget_reason(e: BudgetDenied) -> str:
    """This conversation's cap only comes back when a person gives it back; the others (day, total) come back on their own."""
    return "conversation_budget" if (e.scope or "").startswith("conversation:") else "budget_exhausted"


class _ToolCtx:
    def __init__(self, runner: TurnRunner, s: TurnState):
        self.runner, self.s = runner, s

    async def load_dataset(self, name: str, model):
        from agentplatform.core.knowledge import datasets
        async with tenant_tx(UUID(self.s["tenant_id"])) as conn:
            ds = await datasets.in_use(conn, self.runner.m.name, name)
            if ds is None:
                raise LookupError(f"no active dataset {name}")
            data = await datasets.rows_of(conn, ds["id"])
        return ([model.model_validate(d) for d in data] if model is not dict else data), str(ds["id"]), ds["version"]
