"""Segment package contract (plan 13, ADR-013). The core only knows these types; segments live in packages/."""
from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentplatform.core.config.schema import ComplianceConfig


class M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SlotSpec(M):
    name: str
    type: Literal["enum", "money", "int", "text", "bool", "range"]
    question: str | None = None
    values: list[str] = []
    synonyms: dict[str, list[str]] = {}       # enum value -> customer words (normalized match)
    required_for: list[str] = []
    label: str | None = None                  # console display name (e.g. "Valor do bem")
    value_labels: dict[str, str] = {}         # console display of enum values
    hint: str | None = None                   # extraction hint for the analyzer (what counts as evidence)
    not_evidence: list[str] = []              # words that alone never prove this slot (e.g. the product's own name)


class AutoTransition(M):
    """Code-owned transitions (principle 1): applied without asking the LLM."""
    frm: list[str] = Field(alias="from")
    to: str
    when: Literal["customer_replied", "slot_filled"]
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class StageSpec(M):
    name: str
    goal: str
    label: str | None = None                  # console display name
    guidelines: list[str] = []
    priority_questions: list[str] = []
    tools: list[str] = []
    max_turns_without_progress: int | None = None
    is_terminal: bool = False
    marked_by_seller_only: bool = False


class TransitionSpec(M):
    frm: str = Field(alias="from")
    to: str
    trigger: str = ""
    regression: bool = False
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class GuidelineSpec(M):
    id: str
    when_intents: list[str] = []
    when_keywords: list[str] = []
    action: str
    stages: list[str] = []          # empty = all
    when_tools: list[str] = []      # fires when one of these tools returned a result in this turn
    priority: int = 100


class FlowStep(M):
    id: str
    collect: list[str] = []
    collect_any: list[str] = []
    collect_optional: list[str] = []


class HandoffTriggerSpec(M):
    id: str
    intents: list[str] = []
    keywords: list[str] = []
    reason: str
    locked: bool = False            # the owner cannot turn this one off in the console


class FaqSpec(M):
    id: str
    question: str
    variants: list[str] = []
    answer: str


class ReplyExample(M):
    """A good reply in the business's own voice (few-shot for the respond prompt): tone and length, never facts."""
    customer: str
    reply: str


class CommentTemplate(M):
    """Ready-made texts for comment automation, offered in the console. `{seller}` becomes the seller's display name."""
    id: str
    label: str
    keywords: list[str] = []
    public_variants: list[str] = []
    private_variants: dict[str, str] = {}
    repeat_variants: list[str] = []


class CadenceRef(M):
    id: str


class PackageManifest(M):
    name: str
    version: str
    glossary: dict[str, str] = {}
    persona_template: str             # {seller_name}, and {company} for " <company>" when the account has one
    slots: list[SlotSpec]
    stages: list[StageSpec]
    transitions: list[TransitionSpec]
    initial_stage: str
    opt_out_stage: str
    form_stage: str
    form_sent_stage: str | None = None       # stage after the pre-registration link is sent
    form_resend_keywords: list[str] = []     # customer asks for the link again -> new single-use link
    ready_question: str | None = None        # asked in form_stage when no form goes out: the next step is the person
    ready_offer_pattern: str | None = None   # a question matching it is ready_question in the model's own words
    short_yes: list[str] = []                # the only words of a message that accepts ready_question
    offer_again_keywords: list[str] = []     # the person will show the offer to someone: the simulation runs again as a summary
    # Asked once in form_stage, before ready_question, while none of profile_slots is known: what the seller needs to pick the option
    profile_question: str | None = None
    profile_slots: list[str] = []
    seller_stage: str
    won_stage: str
    min_profile_for_proposal: list[list[str]]     # AND of ORs, e.g. [["goal"], ["credit_amount","monthly_budget"], ["term_months"]]
    flow: list[FlowStep]
    flow_stages: list[str] = []            # stages where the flow asks its questions (empty = all non-terminal)
    intents: list[str]
    intent_labels: dict[str, str] = {}   # how the console names each intent
    objections: list[str] = []
    stop_asking_on: list[str] = []         # intents and objections after which the flow asks nothing this turn
    guidelines: list[GuidelineSpec] = []
    handoff_triggers: list[HandoffTriggerSpec] = []
    compliance: ComplianceConfig
    faqs_seed: list[FaqSpec] = []
    lead_form_fields: list[str]
    lead_form_labels: dict[str, str] = {}
    allowed_urls: list[str] = []
    templates: dict[str, str] = {}        # fixed texts approved by the client (follow-ups, notices)
    auto_transitions: list[AutoTransition] = []
    scope: str = ""                        # what is in scope for this business (used by the analyzer)
    intent_hints: dict[str, str] = {}      # short description per intent
    reply_examples: list[ReplyExample] = []   # tone/length references for the reply (numbers in them are never sources)
    comment_templates: list[CommentTemplate] = []
    never_import_name_hashes: list[str] = []     # files refused on upload whatever the scan finds; only the hash of the name (knowledge.admin.forbidden_name_hash)
    quick_questions: list[str] = []        # suggestions for the questions offered in a new direct; published only by a person
    media_context_slots: list[str] = []    # enum slots the seller can pre-answer per post ("this post is about X")

    def persona_text(self, seller: str, company: str = "") -> str:
        return self.persona_template.format(seller_name=seller, company=f" {company}" if company else "")

    @model_validator(mode="after")
    def consistent(self) -> "PackageManifest":
        names = {s.name for s in self.stages}
        for st in (self.initial_stage, self.opt_out_stage, self.form_stage, self.seller_stage, self.won_stage):
            if st not in names:
                raise ValueError(f"stage {st!r} not declared")
        for t in self.transitions:
            if t.frm not in names or t.to not in names:
                raise ValueError(f"transition {t.frm}->{t.to} uses undeclared stage")
        slot_names = {s.name for s in self.slots}
        for group in self.min_profile_for_proposal:
            for s in group:
                if s not in slot_names:
                    raise ValueError(f"min_profile slot {s!r} not declared")
        for s in self.media_context_slots:
            if s not in slot_names:
                raise ValueError(f"media_context_slots {s!r} not declared")
        return self

    def stage(self, name: str) -> StageSpec:
        return next(s for s in self.stages if s.name == name)

    def allowed(self, frm: str, to: str) -> TransitionSpec | None:
        return next((t for t in self.transitions if t.frm == frm and t.to == to), None)

    def profile_complete(self, profile: dict[str, Any]) -> bool:
        return profile_has(profile, self.min_profile_for_proposal)

    def slot_catalog(self) -> str:
        return "; ".join(f"{x.name} ({x.type}{': ' + '|'.join(x.values) if x.values else ''}){' = ' + x.hint if x.hint else ''}"
                         for x in self.slots)

    def intent_catalog(self) -> str:
        return "; ".join(f"{i}{' = ' + self.intent_hints[i] if i in self.intent_hints else ''}" for i in self.intents)


def profile_has(profile: dict[str, Any], groups: list[list[str]]) -> bool:
    """AND of ORs over filled profile slots."""
    def has(s: str) -> bool:
        v = profile.get(s)
        return isinstance(v, dict) and v.get("value") not in (None, "", [])
    return all(any(has(s) for s in group) for group in groups)


class ToolSpec(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    allowed_stages: list[str] | Literal["*"]
    effect_class: Literal["read", "internal_write", "external"]
    feature_flag: str | None = None
    fn: Callable[..., Any]
    proactive_when: list[list[str]] = []   # profile (AND of ORs) that makes the code run it unasked when one of its inputs is filled


class DatasetSpec(BaseModel):
    """A structured table the tenant maintains in the console (register, edit, import, activate, roll back).
    Columns, labels and choices come from row_model's fields (Field(title=..., description=...,
    json_schema_extra={"choices": {value: label}})); the core renders and validates it without knowing the segment."""
    model_config = ConfigDict(arbitrary_types_allowed=True)
    name: str
    label: str
    description: str = ""
    row_model: type[BaseModel]
    key: str | None = None                 # column that must be unique per row
    feature_flag: str | None = None        # config flag that makes the agent use this table
    used_for: str = ""                     # how the agent uses it (console help text)
    example: list[dict] = []               # illustrative rows for the empty state / template download
    preview: Callable[[Any], dict[str, str]] | None = None   # extra read-only columns per row (e.g. a computed value)


class SegmentPackage(Protocol):
    manifest: PackageManifest

    def tools(self) -> list[ToolSpec]: ...


def package_datasets(pkg: Any) -> list[DatasetSpec]:
    """Datasets are optional for a package."""
    fn = getattr(pkg, "datasets", None)
    return list(fn()) if callable(fn) else []


def load_package(name: str) -> SegmentPackage:
    for ep in entry_points(group="agent_platform.segment_packages"):
        if ep.name == name:
            return ep.load()
    # editable/dev fallback
    import importlib
    return importlib.import_module(f"agentplatform_packages.{name}").package
