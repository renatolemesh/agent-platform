"""PlatformConfig — configuration contract (plan 08, ADR-014).

Canonical copy. docs/plan/anexos/config_schema.py mirrors it for the plan.
"""
from __future__ import annotations

from datetime import time
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Prob = Annotated[float, Field(ge=0, le=1)]
PosInt = Annotated[int, Field(gt=0)]
Usd = Annotated[Decimal, Field(ge=0, max_digits=10, decimal_places=4)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------- models ----------
class ProviderPolicy(Strict):
    data_collection: Literal["deny"] = "deny"          # LGPD: never allow
    zdr: Literal[True] = True
    require_parameters: bool = True                   # only providers supporting json_schema/tools
    allow_fallbacks: bool = False
    order: list[str] = []
    only: list[str] = []


class NodeModel(Strict):
    primary: str
    fallbacks: list[str] = []
    temperature: Annotated[float, Field(ge=0, le=2)] = 0.2
    max_output_tokens: PosInt = 600
    timeout_s: Annotated[float, Field(gt=0, le=60)] = 10
    reasoning_effort: Literal["none", "minimal", "low", "medium"] = "minimal"
    structured_output: Literal["json_schema", "function_calling", "none"] = "json_schema"
    prompt_version: str
    provider: ProviderPolicy = ProviderPolicy()


class ModelRef(Strict):
    """Non-chat model (embedding, STT, rerank): provider policy is mandatory too (review #4)."""
    model: str
    provider: ProviderPolicy = ProviderPolicy()
    dims: PosInt | None = None          # embeddings only
    timeout_s: Annotated[float, Field(gt=0, le=60)] = 10


class ModelsConfig(Strict):
    analyze_turn: NodeModel
    respond: NodeModel
    output_judge: NodeModel
    input_classifier: NodeModel | None = None
    summarize: NodeModel
    comment_intent: NodeModel | None = None
    image_describe: NodeModel | None = None      # off by default: images go to local OCR + masking (media.images)
    embedding: ModelRef
    transcription: ModelRef
    rerank: ModelRef | None = None
    # Only for dev with a single-provider key (e.g. OpenAI direct). PlatformConfig rejects it outside dev.
    allow_same_family_judge: bool = False

    @model_validator(mode="after")
    def judge_other_family(self) -> "ModelsConfig":
        fam = lambda m: m.split("/")[0]
        respond_fams = {fam(m) for m in [self.respond.primary, *self.respond.fallbacks]}
        judge_fams = {fam(m) for m in [self.output_judge.primary, *self.output_judge.fallbacks]}
        if respond_fams & judge_fams and not self.allow_same_family_judge:
            raise ValueError("output_judge (primary and fallbacks) must be from a different model family than respond")
        if self.embedding.dims is None:
            raise ValueError("embedding.dims is required")
        return self


class Subprocessor(Strict):
    """LGPD inventory: every external operator that may receive personal data (review #4)."""
    name: str                                    # e.g. 'OpenRouter', 'DeepInfra', 'Google', 'Meta', 'Voyage'
    purpose: str
    data_categories: list[str]
    country: str
    transfer_mechanism: Literal["anpd_scc", "adequacy", "not_applicable", "pending"]
    zdr: bool


# ---------- budget ----------
class BudgetConfig(Strict):
    day_boundary_tz: Literal["UTC"] = "UTC"            # same boundary as OpenRouter limit_reset (review #13)
    pilot_total_cap_usd: Usd | None = Decimal("4.50")
    net_credit_usd: Usd | None = Decimal("4.74")       # credit after the 5.5% purchase fee
    daily_cap_usd: Usd = Decimal("2.00")
    per_conversation_cap_usd: Usd = Decimal("0.10")
    per_turn_cap_usd: Usd = Decimal("0.02")
    alert_thresholds: list[Prob] = [0.5, 0.8, 1.0]
    degradation_order: list[Literal["cheaper_model", "faq_only", "handoff_short_message"]] = [
        "cheaper_model", "faq_only", "handoff_short_message"]
    experiment_cap_usd: Usd = Decimal("1.00")
    ci_daily_cap_usd: Usd = Decimal("0.50")
    reservation_ttl_s: PosInt = 120                    # reservations of dead workers expire
    openrouter_key_limit_usd: Usd = Decimal("2.50")     # second lock lives in OpenRouter; must be > daily cap

    @model_validator(mode="after")
    def harness_below_provider_lock(self) -> "BudgetConfig":
        if self.daily_cap_usd >= self.openrouter_key_limit_usd:
            raise ValueError("daily_cap_usd must be below openrouter_key_limit_usd so degradation happens before cutoff")
        if self.pilot_total_cap_usd is not None and self.net_credit_usd is not None \
                and self.pilot_total_cap_usd >= self.net_credit_usd:
            raise ValueError("pilot_total_cap_usd must be below net credit so degradation happens before HTTP 402")
        return self


# ---------- conversation ----------
class PersonaConfig(Strict):
    assistant_name: str
    seller_display_name: str
    tone: Literal["friendly", "formal", "neutral"] = "friendly"
    disclose_on_first_message: bool = True


class ConversationConfig(Strict):
    persona: PersonaConfig
    language: Literal["pt-BR"] = "pt-BR"
    max_messages_in_context: PosInt = 8
    max_questions_per_message: Literal[1] = 1
    bubble_delay_ms: Annotated[int, Field(ge=0, le=10000)] = 1200
    waiting_message: str = "Só um instante, já te respondo."
    safe_failure_message: str = "Vou pedir para {seller} te responder por aqui."
    repetition_similarity_block: Prob = 0.9


class AgentConfig(Strict):
    analyze_mode: Literal["merged", "parallel"] = "merged"
    max_llm_calls_per_turn: Annotated[int, Field(ge=2, le=6)] = 4
    max_tool_calls_per_turn: Annotated[int, Field(ge=0, le=4)] = 2
    turn_timeout_s: Annotated[float, Field(gt=0, le=90)] = 40
    slow_after_s: Annotated[float, Field(gt=0)] = 15
    summary_every_n_turns: PosInt = 5


# ---------- funnel / guidelines / tools ----------
class FunnelConfig(Strict):
    stage_confidence_threshold: Prob = 0.7
    slot_confidence_threshold: Prob = 0.6
    max_turns_without_progress_override: dict[str, PosInt] = {}
    # A flow question the customer just ignored is not asked again for this many assistant messages
    question_cooldown_turns: Annotated[int, Field(ge=0, le=10)] = 2


class GuidelineOverride(Strict):
    id: str
    enabled: bool = True
    priority: int | None = None


class GuidelinesConfig(Strict):
    max_per_turn: Annotated[int, Field(ge=1, le=12)] = 6
    overrides: list[GuidelineOverride] = []


class ToolOverride(Strict):
    enabled: bool = True
    allowed_stages: list[str] | None = None
    timeout_s: Annotated[float, Field(gt=0, le=30)] = 5


# ---------- RAG ----------
class RagConfig(Strict):
    active_index_id: str
    faq_threshold: Prob = 0.86
    min_score: Prob = 0.35
    top_k: Annotated[int, Field(ge=1, le=12)] = 4
    pre_rerank_k: Annotated[int, Field(ge=4, le=60)] = 20
    rrf_k: PosInt = 60
    rerank: bool = False
    max_questions_per_turn: Annotated[int, Field(ge=1, le=3)] = 2


# ---------- concurrency ----------
class ConcurrencyConfig(Strict):
    debounce_window_s: Annotated[float, Field(gt=0, le=30)] = 4
    debounce_max_wait_s: Annotated[float, Field(gt=0, le=60)] = 15
    media_max_wait_s: Annotated[float, Field(gt=0, le=90)] = 25
    new_message_policy: Literal["cancel_if_unsent_else_next_turn", "always_next_turn"] = "cancel_if_unsent_else_next_turn"
    max_consecutive_restarts: Annotated[int, Field(ge=0, le=5)] = 2
    heartbeat_timeout_s: PosInt = 45
    max_staleness_reply_s: PosInt = 120
    max_staleness_proactive_s: PosInt = 600
    echo_grace_s: Annotated[float, Field(gt=0, le=30)] = 3
    echo_reconcile_window_s: Annotated[float, Field(gt=0, le=600)] = 120
    ambiguous_send_policy: Literal["check_conversation_api_then_fail"] = "check_conversation_api_then_fail"  # never blind resend
    turn_lease_s: PosInt = 60

    @model_validator(mode="after")
    def window_le_max(self) -> "ConcurrencyConfig":
        if self.debounce_window_s > self.debounce_max_wait_s:
            raise ValueError("debounce_window_s must be <= debounce_max_wait_s")
        if self.echo_grace_s > self.echo_reconcile_window_s:
            raise ValueError("echo_grace_s must be <= echo_reconcile_window_s")
        return self


# ---------- media and input guard (were hardcoded; review #18) ----------
class MediaConfig(Strict):
    transcribe_audio: bool = True
    transcription_language: str = "pt"         # ISO-639-1 hint sent to the speech-to-text model
    images: Literal["local_ocr_masked", "refuse_and_handoff", "llm_describe"] = "local_ocr_masked"
    documents: Literal["local_extract_masked", "refuse_and_handoff"] = "local_extract_masked"
    max_audio_seconds: PosInt = 180
    max_download_mb: PosInt = 16
    max_extracted_chars: PosInt = 1200        # OCR / PDF / transcript text kept per item (masked)
    raw_media_retention_days: PosInt = 30


class InputGuardConfig(Strict):
    max_chars: PosInt = 4000
    opt_out_intent_confirm_threshold: Prob = 0.85
    injection_score_threshold: Prob = 0.6
    per_contact_messages_per_min: PosInt = 20


class OutputGuardConfig(Strict):
    guidance_only_max_ngram: PosInt = 12
    numbers_must_appear_in_sources: Literal[True] = True       # used_numbers from the LLM is never proof
    pii_placeholder_in_output_blocks: Literal[True] = True     # no re-hydration at dispatch
    repetition_rewrite_similarity: Prob = 0.8                  # similarity to a recent assistant message -> rewrite
    repetition_word_overlap: Prob = 0.7                        # shared content words / words of the shorter message -> rewrite
    repetition_min_words: PosInt = 8                           # word overlap only applies to messages at least this long


# ---------- handoff ----------
class RoutingRule(Strict):
    when: str = "true"                        # expression over lead/profile, evaluated by code
    seller_ids: list[str]
    strategy: Literal["single", "round_robin", "least_open"] = "single"


class HandoffConfig(Strict):
    triggers_enabled: list[str]
    routing: list[RoutingRule]
    sla_minutes: PosInt = 30
    expectation_message_in_hours: str
    expectation_message_off_hours: str
    send_expectation_message: bool = True
    return_to_bot_after_inactivity_min: PosInt | None = None    # P5: disabled by default
    # Off hours: ownership stays BOT with handoff_pending=True; the bot keeps answering without pushing the sale,
    # and moves to AWAITING_HUMAN when human hours start (review #8).
    off_hours_mode: Literal["bot_keeps_serving", "await_human"] = "bot_keeps_serving"
    notify_off_hours: Literal["immediately", "at_open"] = "at_open"
    # Unanswered inbound while HUMAN owns the conversation re-alerts the seller (review #9)
    human_unanswered_realert_min: PosInt = 30
    closed_reopens_on_inbound: bool = True
    notifier: Literal["whatsapp_cloud", "email", "telegram"] = "whatsapp_cloud"
    notifier_fallback: Literal["email", "none"] = "email"
    console_links_require_login: Literal[True] = True


# ---------- sending / schedule ----------
class QuietHours(Strict):
    start: time = time(22, 0)
    end: time = time(8, 0)
    timezone: str = "America/Sao_Paulo"


class SendingConfig(Strict):
    quiet_hours: QuietHours = QuietHours()
    holidays_national: bool = True
    holidays_local_calendar: str | None = None
    proactive_jitter_s: tuple[PosInt, PosInt] = (30, 300)
    frequency_cap_per_contact_day: PosInt = 2
    frequency_cap_per_contact_week: PosInt = 4
    channel_rate_safety_margin: Prob = 0.2


# ---------- comments ----------
class CommentTrigger(Strict):
    kind: Literal["keyword", "regex", "intent"]
    values: list[str]


class CommentRule(Strict):
    id: str
    scope: Literal["account", "media", "ad"]
    scope_ref: str | None = None
    trigger: CommentTrigger
    actions: list[Literal["public_reply", "private_reply", "register_lead"]]
    public_variants: list[str] = []
    private_variants: dict[str, str] = {}     # variant_id -> text
    private_weights: dict[str, Prob] = {}
    priority: int = 100
    valid_from: str | None = None
    valid_to: str | None = None

    @model_validator(mode="after")
    def variants_present(self) -> "CommentRule":
        if "public_reply" in self.actions and len(self.public_variants) < 3:
            raise ValueError(f"rule {self.id}: public_reply needs at least 3 variants (no identical mass replies)")
        if "private_reply" in self.actions and not self.private_variants:
            raise ValueError(f"rule {self.id}: private_reply needs variants")
        return self


class CommentsConfig(Strict):
    enabled: bool = True
    private_reply_dedup_window_days: Annotated[int, Field(ge=1, le=7)] = 7
    sensitive_topics_to_human: bool = True
    hide_spam: bool = False
    rules: list[CommentRule] = []


# ---------- followups ----------
class CadenceStep(Strict):
    delay: str                                 # ISO-like "4h", "30m"
    content_kind: Literal["template", "generated"]
    template_key: str | None = None
    prompt_version: str | None = None
    max_chars: PosInt = 200


class Cadence(Strict):
    id: str
    trigger: dict
    steps: list[CadenceStep]
    stop_on: list[str]
    max_attempts: PosInt = 1


class FollowupsConfig(Strict):
    enabled: bool = True
    cadences: list[Cadence] = []


# ---------- compliance (merge must be monotonic) ----------
class ComplianceConfig(Strict):
    forbidden_phrases: list[str] = []
    required_disclosures: dict[str, str] = {}
    never_request: list[str] = ["cpf", "rg", "income", "assets", "bank_data", "health_data"]
    opt_out_keywords: list[str] = ["parar", "pare", "sair", "stop", "cancelar mensagens", "não quero mais"]
    judge_risk_threshold: Prob = 0.5
    product_numbers_require_provenance: Literal[True] = True
    internal_documents_never_cited: Literal[True] = True
    extra_rules: list[str] = []          # ids of package-specific guard rules, e.g. 'contracting_only_in_official_system'
    require_with: dict[str, str] = {}    # phrase regex -> regex that must appear in the same message (e.g. "sem juros" -> fees)

    def is_at_least_as_strict_as(self, base: "ComplianceConfig") -> list[str]:
        problems = []
        if not set(base.forbidden_phrases) <= set(self.forbidden_phrases):
            problems.append("forbidden_phrases removed")
        for key, text in base.required_disclosures.items():
            mine = self.required_disclosures.get(key, "")
            if text.strip() not in mine:        # tenant may extend the text, never remove or blank it (review #18)
                problems.append(f"required_disclosures[{key}] removed or weakened")
        if not set(base.extra_rules) <= set(self.extra_rules):
            problems.append("extra_rules removed")
        if any(self.require_with.get(k) != v for k, v in base.require_with.items()):
            problems.append("require_with removed or changed")
        if not set(base.never_request) <= set(self.never_request):
            problems.append("never_request removed")
        if not set(base.opt_out_keywords) <= set(self.opt_out_keywords):
            problems.append("opt_out_keywords removed")
        if self.judge_risk_threshold > base.judge_risk_threshold:
            problems.append("judge_risk_threshold raised (less strict)")
        return problems


# ---------- operations ----------
class OpsAlertsConfig(Strict):
    """Operator alerts evaluated every minute (core/ops/observability.py); repeated alerts respect the cooldown."""
    no_inbound_hours: PosInt = 24              # tenant with a live channel and no inbound event for this long
    inbound_backlog_minutes: PosInt = 5        # inbound events still unprocessed after this
    outbox_stuck_minutes: PosInt = 10          # pending/sending/unknown outbox items older than this
    turn_failures_per_hour: PosInt = 5
    dead_letters_per_hour: PosInt = 1
    spool_bytes: PosInt = 1_000_000            # webhook spool not yet replayed
    cooldown_minutes: PosInt = 60


class DatasetsConfig(Strict):
    """Tables the tenant maintains in the console (package DatasetSpec): limits and who may activate a new version."""
    max_rows: PosInt = 5000
    max_upload_bytes: PosInt = 2_000_000
    two_person_activation: bool = False        # True: a version must be activated by someone other than its author


class OperationsConfig(Strict):
    alerts: OpsAlertsConfig = OpsAlertsConfig()
    datasets: DatasetsConfig = DatasetsConfig()
    feature_flags: dict[str, bool] = {}
    canary_percent: Annotated[int, Field(ge=0, le=100)] = 0
    trace_sampling: Prob = 1.0
    retention_days: dict[str, PosInt] = {
        "inbound_raw": 30, "media": 30, "messages_original": 365, "llm_calls": 365, "checkpoints": 30,
        "ownership_evidence": 90, "dead_letters": 30, "tool_call_args": 90, "handoff_summaries": 365,
        "lead_forms": 730, "audit_log": 1825}


class PackageRef(Strict):
    name: str
    version: str


class MetaConfig(Strict):
    graph_api_version: str = "v26.0"
    login_kind: Literal["facebook_login", "instagram_login"] = "facebook_login"


class PlatformConfig(Strict):
    schema_version: Literal[1] = 1
    environment: Literal["dev", "staging", "prod"] = "prod"
    tenant_slug: str
    segment_package: PackageRef
    meta: MetaConfig = MetaConfig()
    models: ModelsConfig
    budget: BudgetConfig = BudgetConfig()
    conversation: ConversationConfig
    agent: AgentConfig = AgentConfig()
    funnel: FunnelConfig = FunnelConfig()
    guidelines: GuidelinesConfig = GuidelinesConfig()
    tools: dict[str, ToolOverride] = {}
    rag: RagConfig
    concurrency: ConcurrencyConfig = ConcurrencyConfig()
    handoff: HandoffConfig
    sending: SendingConfig = SendingConfig()
    comments: CommentsConfig = CommentsConfig()
    followups: FollowupsConfig = FollowupsConfig()
    compliance: ComplianceConfig = ComplianceConfig()
    media: MediaConfig = MediaConfig()
    input_guard: InputGuardConfig = InputGuardConfig()
    output_guard: OutputGuardConfig = OutputGuardConfig()
    subprocessors: list[Subprocessor]
    operations: OperationsConfig = OperationsConfig()

    @model_validator(mode="after")
    def llm_images_need_explicit_subprocessor(self) -> "PlatformConfig":
        if self.models.allow_same_family_judge and self.environment != "dev":
            raise ValueError("allow_same_family_judge is only allowed in environment=dev")
        if self.media.images == "llm_describe" and self.models.image_describe is None:
            raise ValueError("media.images=llm_describe requires models.image_describe")
        if any(s.transfer_mechanism == "pending" for s in self.subprocessors) and self.operations.feature_flags.get("go_live", False):
            raise ValueError("go_live blocked: subprocessor transfer mechanism pending (LGPD)")
        return self


def validate_against_package(cfg: PlatformConfig, package_compliance: ComplianceConfig) -> None:
    """Tenant may override anything except compliance, which can only become stricter."""
    problems = cfg.compliance.is_at_least_as_strict_as(package_compliance)
    if problems:
        raise ValueError("tenant compliance less strict than package: " + "; ".join(problems))
