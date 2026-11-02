"""Code-owned decisions of the turn (plan 04 §3): the LLM proposes, code applies. No segment terms here."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from agentplatform.core.agent.guards import extract_numbers, keyword_hit, normalize
from agentplatform.core.agent.state import StageProposal, TurnUnderstanding
from agentplatform.core.config.schema import FunnelConfig, GuidelinesConfig
from agentplatform.core.packages.contract import PackageManifest

KNOWLEDGE_INTENTS = {"product_question", "bid_question", "insurance_question", "price_question", "compare_financing",
                     "wants_fast"}
RISK_INTENTS = {"bid_question", "wants_fast", "compare_financing", "insurance_question", "price_question", "objection"}


@dataclass
class StageDecision:
    applied: bool
    frm: str
    to: str
    reason: str
    regression: bool = False


@dataclass
class SlotDecision:
    accepted: dict[str, dict] = field(default_factory=dict)
    rejected: list[tuple[str, str]] = field(default_factory=list)


def _norm_value(slot_type: str, value: str, values: list[str], synonyms: dict[str, list[str]] | None = None) -> object | None:
    v = value.strip()
    if slot_type == "money":
        nums = extract_numbers(v)
        return f"{max(nums):f}" if nums else None
    if slot_type in ("int", "range"):
        nums = extract_numbers(v)
        if not nums:
            return v or None
        n = max(nums)
        if re.search(r"\bano", normalize(v)):            # "5 anos" -> 60 months
            n = n * 12
        return f"{n:f}"
    if slot_type == "bool":
        n = normalize(v)
        if n in ("sim", "true", "tenho", "yes", "s"):
            return True
        if n in ("nao", "false", "no", "n", "nao tenho"):
            return False
        return None
    if slot_type == "enum":
        if v in values:
            return v
        n = normalize(v)
        for val, words in (synonyms or {}).items():
            if n == normalize(val) or any(normalize(w) == n or normalize(w) in n.split() or normalize(w) in n for w in words):
                return val
        return None
    return v or None


_FILLER_WORDS = {"um", "uma", "uns", "umas", "o", "a", "os", "as", "de", "da", "do", "pra", "pro", "para", "com", "e", "eu",
                 "me", "minha", "meu", "so", "tipo", "assim", "ai", "la"}


def _meaningful_words(evidence: str, not_evidence: list[str]) -> list[str]:
    """Evidence words left after fillers, numbers and the slot's non-evidence words (e.g. "uma carta" -> [])."""
    skip = _FILLER_WORDS | {normalize(w) for w in not_evidence}
    return [w for w in re.findall(r"\w+", normalize(evidence)) if w not in skip and not any(c.isdigit() for c in w)]


def apply_slots(manifest: PackageManifest, profile: dict, u: TurnUnderstanding, turn_text: str,
                funnel: FunnelConfig, message_id: str | None, recent_customer_text: str = "") -> SlotDecision:
    """Evidence must be literal: from this turn, or from a recent customer message when the slot is still empty."""
    specs = {s.name: s for s in manifest.slots}
    out = SlotDecision()
    norm_turn = normalize(turn_text)
    norm_recent = normalize(recent_customer_text)
    for up in u.slot_updates:
        spec = specs.get(up.slot)
        if spec is None:
            out.rejected.append((up.slot, "unknown_slot"))
            continue
        if up.confidence < funnel.slot_confidence_threshold:
            out.rejected.append((up.slot, "low_confidence"))
            continue
        q = normalize(up.evidence_quote or "")[:40]
        in_turn = bool(q) and q in norm_turn
        in_recent = bool(q) and q in norm_recent and not (profile.get(up.slot) or {}).get("value")
        if not (in_turn or in_recent):
            out.rejected.append((up.slot, "evidence_not_in_turn"))
            continue
        if spec.not_evidence and not _meaningful_words(up.evidence_quote or "", spec.not_evidence):
            out.rejected.append((up.slot, "not_evidence"))
            continue
        val = _norm_value(spec.type, up.value, spec.values, spec.synonyms)
        if val is None:
            out.rejected.append((up.slot, "invalid_value"))
            continue
        prev = profile.get(up.slot)
        if prev and prev.get("confidence", 0) > up.confidence and not up.is_correction:
            out.rejected.append((up.slot, "kept_higher_confidence"))
            continue
        out.accepted[up.slot] = {"value": val, "source_message_id": message_id, "confidence": up.confidence,
                                 "at": datetime.now(timezone.utc).isoformat()}
    return out


def decide_stage(manifest: PackageManifest, current: str, proposal: StageProposal, profile_after: dict,
                 funnel: FunnelConfig, *, filled_this_turn: bool = False) -> StageDecision:
    target = proposal.stage
    # code-owned auto transitions declared by the package; "slot_filled" wins over "customer_replied"
    if target == current or target not in {s.name for s in manifest.stages}:
        for when in ("slot_filled", "customer_replied"):
            if when == "slot_filled" and not filled_this_turn:
                continue
            for at in manifest.auto_transitions:
                if at.when == when and current in at.frm and manifest.allowed(current, at.to):
                    return StageDecision(True, current, at.to, f"auto:{when}")
    # code rule: minimum profile complete during qualification -> ready for proposal
    form_ready_from = {t.frm for t in manifest.transitions if t.to == manifest.form_stage}
    if current in form_ready_from and manifest.profile_complete(profile_after) and target == current:
        target = manifest.form_stage
        if manifest.allowed(current, target):
            return StageDecision(True, current, target, "min_profile_complete")
    if target == current:
        return StageDecision(False, current, current, "same")
    if manifest.stage(target).marked_by_seller_only:
        return StageDecision(False, current, target, "seller_only")
    t = manifest.allowed(current, target)
    if t is None:
        return StageDecision(False, current, target, "transition_not_allowed")
    if proposal.confidence < funnel.stage_confidence_threshold:
        return StageDecision(False, current, target, "low_confidence")
    if not proposal.evidence:
        return StageDecision(False, current, target, "no_evidence")
    return StageDecision(True, current, target, "applied", regression=t.regression)


def pending_questions(manifest: PackageManifest, profile: dict, optional: bool = True) -> list[str]:
    """Flow questions still unanswered, in order: required steps first, then (optional=True) the optional ones."""
    specs = {s.name: s for s in manifest.slots}

    def has(s: str) -> bool:
        v = profile.get(s)
        return isinstance(v, dict) and v.get("value") not in (None, "", [])
    out = []
    for step in manifest.flow:
        out += [specs[s].question for s in step.collect if not has(s)]
        if step.collect_any and not any(has(s) for s in step.collect_any):
            out.append(specs[step.collect_any[0]].question)
    for step in manifest.flow if optional else ():
        out += [specs[s].question for s in step.collect_optional if not has(s)]
    return [q for q in out if q]


def next_question(manifest: PackageManifest, profile: dict) -> str | None:
    return next(iter(pending_questions(manifest, profile)), None)


_QUESTION_STOPWORDS = {"voce", "voces", "qual", "quais", "como", "para", "esta", "essa", "esse", "seria",
                       "teria", "algum", "alguma", "hoje", "mais", "menos", "quando", "onde", "pensando"}


def recently_asked(question: str, recent_bot_texts: list[str]) -> bool:
    """True when one of the recent assistant messages already asked this flow question (same wording or a close
    paraphrase), so it is not repeated while the customer is talking about something else."""
    def words(t: str) -> set[str]:
        return {w for w in re.findall(r"\w+", normalize(t)) if len(w) > 3 and w not in _QUESTION_STOPWORDS}
    q = words(question)
    if not q:
        return False
    need = max(min(2, len(q)), -(-len(q) // 3))         # >= 2 shared words (or all of a 1-word question), >= 1/3
    for text in recent_bot_texts:
        for sent in re.split(r"(?<=[.!?])\s+", text):
            if "?" in sent and len(q & words(sent)) >= need:
                return True
    return False


def stage_limit(manifest: PackageManifest, stage: str, funnel: FunnelConfig | None = None) -> int | None:
    """Turns without progress this stage allows: the owner's number, or the package's."""
    override = funnel.max_turns_without_progress_override.get(stage) if funnel else None
    return override or manifest.stage(stage).max_turns_without_progress


def select_guidelines(manifest: PackageManifest, stage: str, u: TurnUnderstanding, turn_text: str, turns_in_stage: int,
                      cfg: GuidelinesConfig, tools: Iterable[str] = (), funnel: FunnelConfig | None = None) -> list[str]:
    """tools: tools that returned a usable result in this turn (guidelines with when_tools)."""
    n = normalize(turn_text)
    changes = {o.id: o for o in cfg.overrides}
    candidates = []
    for g in [*manifest.guidelines, *cfg.custom]:
        o = changes.get(g.id)
        if o and not o.enabled:
            continue
        candidates.append((o.priority if o and o.priority is not None else g.priority, g))
    picked = []
    for _, g in sorted(candidates, key=lambda c: c[0]):
        if g.stages and stage not in g.stages:
            continue
        if set(g.when_intents) & set(u.intents) or any(keyword_hit(k, n) for k in g.when_keywords) \
                or set(getattr(g, "when_tools", [])) & set(tools):
            picked.append(g.action)
    limit = stage_limit(manifest, stage, funnel)
    if limit and turns_in_stage >= limit:
        picked.insert(0, cfg.no_progress)
    if u.out_of_scope:
        picked.insert(0, cfg.out_of_scope)
    if "asks_if_bot" in u.intents:
        picked.insert(0, cfg.asks_if_bot)
    return picked[:cfg.max_per_turn]


def can_turn_off(manifest: PackageManifest) -> list[str]:
    """Handoff reasons the owner may disable. A person asking for a person and the package's locked triggers are not among them."""
    reasons = ["frustration", "no_progress", *(t.reason for t in manifest.handoff_triggers if not t.locked)]
    locked = {t.reason for t in manifest.handoff_triggers if t.locked}
    return [r for r in dict.fromkeys(reasons) if r not in locked]


def handoff_reason(manifest: PackageManifest, u: TurnUnderstanding, simulation_enabled: bool, stage: str,
                   turns_in_stage: int, funnel: FunnelConfig | None = None, disabled: Iterable[str] = ()) -> str | None:
    off = set(disabled) & set(can_turn_off(manifest))
    if "wants_human" in u.intents:          # the LLM flag alone is too noisy; the regex input guard is primary
        return "explicit_request"
    if u.sentiment == "angry" and "frustration" not in off:
        return "frustration"
    for trig in manifest.handoff_triggers:
        if set(trig.intents) & set(u.intents):
            if (trig.reason == "simulation_request" and simulation_enabled) or trig.reason in off:
                continue
            return trig.reason
    limit = stage_limit(manifest, stage, funnel)
    if limit and turns_in_stage >= 2 * limit and "no_progress" not in off:
        return "no_progress"
    return None


def needs_knowledge(u: TurnUnderstanding) -> bool:
    return bool(u.questions) or bool(set(u.intents) & KNOWLEDGE_INTENTS)


def is_risky(u: TurnUnderstanding) -> bool:
    return bool(set(u.intents) & RISK_INTENTS) or bool(u.objections)


def config_numbers() -> set[Decimal]:
    """Numbers the bot may state from configuration (business hours, window)."""
    return {Decimal(8), Decimal(22), Decimal(24), Decimal(7)}
