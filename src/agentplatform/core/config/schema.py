"""PlatformConfig — configuration contract (plan 08, ADR-014).

Canonical copy. docs/plan/anexos/config_schema.py is the plan's original and has not followed the changes since (ADR-022).
"""
from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Prob = Annotated[float, Field(ge=0, le=1)]
PosInt = Annotated[int, Field(gt=0)]
Usd = Annotated[Decimal, Field(ge=0, max_digits=10, decimal_places=4)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    # Options that no longer exist. Versions already stored still carry them, so they keep loading.
    _removed: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def _without_removed_options(cls, data):
        return {k: v for k, v in data.items() if k not in cls._removed} if cls._removed and isinstance(data, dict) else data


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
    retries: Annotated[int, Field(ge=0, le=2)] = 1     # again after a provider hiccup (timeout, 429, 5xx), before giving up
    reasoning_effort: Literal["none", "minimal", "low", "medium"] = "minimal"
    structured_output: Literal["json_schema", "function_calling", "none"] = "json_schema"
    prompt_version: str
    provider: ProviderPolicy = ProviderPolicy()


class ModelRef(Strict):
    """Non-chat model (embedding, STT): provider policy is mandatory too (review #4)."""
    model: str
    provider: ProviderPolicy = ProviderPolicy()
    dims: PosInt | None = None          # embeddings only
    timeout_s: Annotated[float, Field(gt=0, le=60)] = 10
    batch_timeout_s: Annotated[float, Field(gt=0, le=300)] = 60      # several texts at once, when indexing: nobody is waiting for it


class ModelsConfig(Strict):
    _removed: ClassVar[frozenset[str]] = frozenset({"rerank"})
    analyze_turn: NodeModel
    respond: NodeModel
    output_judge: NodeModel
    input_classifier: NodeModel | None = None
    summarize: NodeModel
    comment_intent: NodeModel | None = None
    image_describe: NodeModel | None = None      # off by default: images go to local OCR + masking (media.images)
    embedding: ModelRef
    transcription: ModelRef
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
    _removed: ClassVar[frozenset[str]] = frozenset({"day_boundary_tz", "degradation_order", "ci_daily_cap_usd"})
    pilot_total_cap_usd: Usd | None = Decimal("4.50")
    net_credit_usd: Usd | None = Decimal("4.74")       # credit after the 5.5% purchase fee
    daily_cap_usd: Usd = Decimal("2.00")
    per_conversation_cap_usd: Usd = Decimal("0.10")
    per_turn_cap_usd: Usd = Decimal("0.02")
    alert_thresholds: list[Prob] = [0.5, 0.8, 1.0]
    experiment_cap_usd: Usd = Decimal("1.00")          # one evaluation run stops here
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
    company_name: str = ""                     # the company or brand the seller represents, when they should mention it
    tone: Literal["friendly", "formal", "neutral"] = "friendly"
    disclose_on_first_message: bool = True


class ReplyExampleConfig(Strict):
    """A reply in the business's own voice, added to the package's examples: tone and length, never facts."""
    customer: Annotated[str, Field(min_length=1, max_length=300)]
    reply: Annotated[str, Field(min_length=1, max_length=500)]


class ConversationConfig(Strict):
    _removed: ClassVar[frozenset[str]] = frozenset({"repetition_similarity_block"})
    persona: PersonaConfig
    # What the owner wants the assistant to know or do, in their own words. It never overrides the rules above it in the prompt, and every
    # reply still goes through the guards.
    business_instructions: Annotated[str, Field(max_length=3000)] = ""
    reply_examples: Annotated[list[ReplyExampleConfig], Field(max_length=12)] = []
    allowed_urls: list[str] = []               # this account's own sites, on top of the package's: any other link in a reply is blocked
    tone_instructions: dict[str, str] = {
        "friendly": "Tom próximo e descontraído, como quem conversa no direct.",
        "formal": "Tom mais formal e respeitoso: sem gírias e sem emojis, mas ainda em frases curtas.",
        "neutral": "Tom neutro e objetivo: direto ao ponto, sem gírias e sem emojis."}
    language: Literal["pt-BR"] = "pt-BR"
    max_messages_in_context: PosInt = 8
    max_questions_per_message: Literal[1] = 1
    bubble_delay_ms: Annotated[int, Field(ge=0, le=10000)] = 1200
    waiting_message: str = "Só um instante, já te respondo."
    first_message_disclosure: str = "Aqui é o assistente virtual de {seller}."       # added by code when the first reply forgets to say it
    greeting_pattern: str = r"^(oi|olá|ola|bom dia|boa tarde|boa noite|e aí|e ai)\b[!,.]*\s*"   # the disclosure goes right after a greeting, not before it
    safe_failure_message: str = "Vou pedir para {seller} te responder por aqui."
    opt_out_confirmation: str = "Pronto, não vou mais te mandar mensagens. Se mudar de ideia, é só chamar por aqui que {seller} te responde."
    # Customer edited a message already answered: below this similarity (or with other numbers) it is a correction, and gets a new answer
    edit_reanswer_similarity: Prob = 0.85

    @model_validator(mode="after")
    def _links_are_https(self) -> "ConversationConfig":
        bad = [u for u in self.allowed_urls if not u.startswith("https://")]
        if bad:
            raise ValueError(f"conversation.allowed_urls: use https:// ({bad[0]})")
        return self


class AgentConfig(Strict):
    _removed: ClassVar[frozenset[str]] = frozenset({"analyze_mode"})
    max_llm_calls_per_turn: Annotated[int, Field(ge=2, le=6)] = 4
    max_tool_calls_per_turn: Annotated[int, Field(ge=0, le=4)] = 2
    max_reply_attempts: Annotated[int, Field(ge=1, le=3)] = 2     # drafts per turn before giving the conversation to a person
    turn_timeout_s: Annotated[float, Field(gt=0, le=90)] = 40     # past this the turn ends like a model outage: short message and handoff
    slow_after_s: Annotated[float, Field(gt=0)] = 15              # past this the customer gets conversation.waiting_message, once
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
    action: Annotated[str, Field(min_length=1, max_length=1500)] | None = None      # this account's words for what the guideline asks


class CustomGuideline(Strict):
    """A guideline the owner wrote. Fires on these words or intents, in these stages (empty = all)."""
    id: Annotated[str, Field(min_length=1, max_length=60)]
    action: Annotated[str, Field(min_length=1, max_length=500)]
    when_keywords: list[str] = []
    when_intents: list[str] = []
    stages: list[str] = []
    priority: int = 50

    @model_validator(mode="after")
    def fires_on_something(self) -> "CustomGuideline":
        if not self.when_keywords and not self.when_intents:
            raise ValueError(f"guideline {self.id}: needs at least one word or intent")
        return self


class GuidelinesConfig(Strict):
    max_per_turn: Annotated[int, Field(ge=1, le=12)] = 6
    overrides: list[GuidelineOverride] = []
    custom: Annotated[list[CustomGuideline], Field(max_length=30)] = []
    no_progress: str = "A conversa não avança: mude a abordagem e ofereça falar com o vendedor."
    out_of_scope: str = "Pergunta fora do escopo do negócio: recuse em uma frase, com educação, e volte ao assunto."
    asks_if_bot: str = "Confirme que é um assistente virtual e ofereça falar com o vendedor."
    opt_out_check: str = "O cliente parece querer parar de receber mensagens: pergunte, em uma frase, se ele quer que você pare."
    form_link: str = ("Perfil mínimo completo: ofereça o pré-cadastro neste link {url} (sem pedir CPF, RG ou renda) ou, se preferir, falar "
                      "direto com {seller}. Não faça outras perguntas agora.")


class ToolOverride(Strict):
    enabled: bool = True
    allowed_stages: list[str] | None = None
    timeout_s: Annotated[float, Field(gt=0, le=30)] = 5


# ---------- RAG ----------
class RagConfig(Strict):
    _removed: ClassVar[frozenset[str]] = frozenset({"pre_rerank_k", "rerank"})
    active_index_id: str
    faq_threshold: Prob = 0.86
    min_score: Prob = 0.35
    top_k: Annotated[int, Field(ge=1, le=12)] = 4
    candidates_k: Annotated[int, Field(ge=4, le=200)] = 30      # per method (words, meaning), before they are combined
    rrf_k: PosInt = 60
    max_questions_per_turn: Annotated[int, Field(ge=1, le=3)] = 2
    max_chunks_per_turn: Annotated[int, Field(ge=1, le=12)] = 6
    degraded_faq_min_rank: Annotated[float, Field(ge=0)] = 0.1  # no embeddings available: how close the words must be to use an FAQ
    curation_group_similarity: Prob = 0.6                       # an unanswered question this close to an open one joins its group
    conflict_similarity: Prob = 0.85                            # passages this close, with other numbers, are flagged as a conflict


# ---------- concurrency ----------
class ConcurrencyConfig(Strict):
    _removed: ClassVar[frozenset[str]] = frozenset({"heartbeat_timeout_s"})
    debounce_window_s: Annotated[float, Field(gt=0, le=30)] = 4
    debounce_max_wait_s: Annotated[float, Field(gt=0, le=60)] = 15
    media_max_wait_s: Annotated[float, Field(gt=0, le=90)] = 25
    new_message_policy: Literal["cancel_if_unsent_else_next_turn", "always_next_turn"] = "cancel_if_unsent_else_next_turn"
    max_consecutive_restarts: Annotated[int, Field(ge=0, le=5)] = 2
    max_staleness_reply_s: PosInt = 120
    max_staleness_proactive_s: PosInt = 600
    echo_grace_s: Annotated[float, Field(gt=0, le=30)] = 3
    echo_reconcile_window_s: Annotated[float, Field(gt=0, le=600)] = 120
    ambiguous_send_policy: Literal["check_conversation_api_then_fail"] = "check_conversation_api_then_fail"  # never blind resend
    turn_lease_s: PosInt = 60
    pending_sweep_after_s: PosInt = 60          # a customer message not picked up after this goes back to the queue
    sending_orphan_after_s: PosInt = 180        # a send without an outcome after this (worker died) goes to reconciliation

    @model_validator(mode="after")
    def window_le_max(self) -> "ConcurrencyConfig":
        if self.debounce_window_s > self.debounce_max_wait_s:
            raise ValueError("debounce_window_s must be <= debounce_max_wait_s")
        if self.echo_grace_s > self.echo_reconcile_window_s:
            raise ValueError("echo_grace_s must be <= echo_reconcile_window_s")
        return self


# ---------- media and input guard (were hardcoded; review #18) ----------
class MediaConfig(Strict):
    _removed: ClassVar[frozenset[str]] = frozenset({"raw_media_retention_days"})
    transcribe_audio: bool = True
    transcription_language: str = "pt"         # ISO-639-1 hint sent to the speech-to-text model
    images: Literal["local_ocr_masked", "refuse_and_handoff", "llm_describe"] = "local_ocr_masked"
    documents: Literal["local_extract_masked", "refuse_and_handoff"] = "local_extract_masked"
    max_audio_seconds: PosInt = 180
    max_download_mb: PosInt = 16
    max_extracted_chars: PosInt = 1200        # OCR / PDF / transcript text kept per item (masked)


class InputGuardConfig(Strict):
    max_chars: PosInt = 4000
    opt_out_intent_confirm_threshold: Prob = 0.6    # the assistant asks whether the customer wants it to stop
    opt_out_intent_apply_threshold: Prob = 0.85     # clear enough: recorded right away, like the keywords
    injection_score_threshold: Prob = 0.6
    # Above this no turn runs (no spending): the messages stay pending and are answered together when it calms down. A stop request still counts.
    per_contact_messages_per_min: PosInt = 20


class OutputGuardConfig(Strict):
    guidance_only_max_ngram: PosInt = 12                       # this many words in a row from a "guidance only" passage is a quote: blocked
    max_bytes: PosInt = 1000
    repetition_window: PosInt = 5                              # recent assistant messages compared against the draft
    numbers_must_appear_in_sources: Literal[True] = True       # used_numbers from the LLM is never proof
    pii_placeholder_in_output_blocks: Literal[True] = True     # no re-hydration at dispatch
    repetition_rewrite_similarity: Prob = 0.8                  # similarity to a recent assistant message -> rewrite
    repetition_word_overlap: Prob = 0.7                        # shared content words / words of the shorter message -> rewrite
    # A reply that reaches the guard was not recorded as an opt-out, so it cannot promise to stop (normalized text).
    opt_out_promise_patterns: list[str] = [r"\bvou parar de\b", r"\bnao vou mais (te |lhe )?(mandar|enviar|chamar|incomodar)",
                                           r"\bnao (te |lhe )?(mando|envio|chamo|incomodo) mais\b", r"\bparei de\b"]
    repetition_min_words: PosInt = 8                           # word overlap only applies to messages at least this long
    # The search found nothing, so a reply cannot open by settling the question (normalized text). Explaining in general and promising to confirm are fine.
    no_basis_claim_patterns: list[str] = [r"^(sim|pode sim|pode|claro|com certeza|da sim|nao tem|nao pode|nao da)\b"]


# ---------- handoff ----------
class RoutingRule(Strict):
    when: str = "true"                        # expression over lead/profile, evaluated by code
    seller_ids: list[str]
    strategy: Literal["single", "round_robin", "least_open"] = "single"


class HandoffConfig(Strict):
    _removed: ClassVar[frozenset[str]] = frozenset({"triggers_enabled", "return_to_bot_after_inactivity_min", "notifier", "notifier_fallback"})
    # Reasons the owner turned off. A person asking for a person, and the package's locked triggers, always hand off (policy.handoff_reason).
    disabled_reasons: list[str] = []
    routing: list[RoutingRule]
    sla_minutes: PosInt = 30
    expectation_message_in_hours: str
    expectation_message_off_hours: str
    send_expectation_message: bool = True
    # A handoff caused by our own failure (not by the customer or a seller) goes back to the assistant after this, and the
    # customer's unanswered messages are answered. A conversation a seller took never comes back on its own.
    auto_return_reasons: list[str] = ["technical_failure", "budget_exhausted"]
    auto_return_after_min: PosInt | None = 15
    auto_return_max_per_day: PosInt = 3
    # Customer still unanswered this long after a handoff: the account owner is told, once per handoff.
    escalate_to_owner_after_min: PosInt | None = 240
    # Off hours the assistant keeps serving after a handoff: what it tells a customer who asks again (nothing is re-sent to the seller).
    already_notified_guideline: str = ("O pedido do cliente já está registrado e o consultor responde assim que puder: diga isso em uma "
                                       "frase e ofereça ajuda enquanto isso.")
    notice_dedupe_hours: PosInt = 24            # at most one notice per reason and conversation in this period
    # Off hours: ownership stays BOT with handoff_pending=True; the bot keeps answering without pushing the sale,
    # and moves to AWAITING_HUMAN when human hours start (review #8).
    off_hours_mode: Literal["bot_keeps_serving", "await_human"] = "bot_keeps_serving"
    notify_off_hours: Literal["immediately", "at_open"] = "at_open"
    # Unanswered inbound while HUMAN owns the conversation re-alerts the seller (review #9)
    human_unanswered_realert_min: PosInt = 30
    closed_reopens_on_inbound: bool = True      # False: the message is recorded and the conversation stays closed until a person opens it
    console_links_require_login: Literal[True] = True


# ---------- sending / schedule ----------
class QuietHours(Strict):
    start: time = time(22, 0)
    end: time = time(8, 0)
    timezone: str = "America/Sao_Paulo"


class SendingConfig(Strict):
    _removed: ClassVar[frozenset[str]] = frozenset({"holidays_local_calendar", "channel_rate_safety_margin"})
    quiet_hours: QuietHours = QuietHours()
    holidays_national: bool = True
    holidays_local: list[str] = []             # the city's holidays: "MM-DD" every year, "YYYY-MM-DD" once
    proactive_jitter_s: tuple[Annotated[int, Field(ge=0)], Annotated[int, Field(ge=0)]] = (30, 300)   # no two follow-ups leave at the same second
    frequency_cap_per_contact_day: PosInt = 2
    frequency_cap_per_contact_week: PosInt = 4

    @model_validator(mode="after")
    def _local_holidays_are_dates(self) -> "SendingConfig":
        for h in self.holidays_local:
            try:
                date.fromisoformat(h if len(h) == 10 else f"2024-{h}")      # 2024: leap year, so 02-29 is accepted
            except ValueError as e:
                raise ValueError(f"sending.holidays_local: {h!r} is not MM-DD or YYYY-MM-DD") from e
        return self


# ---------- comments ----------
class CommentsConfig(Strict):
    """Platform limits and filters. Which posts answer, with which words and texts, is set in the console (ADR-018)."""
    _removed: ClassVar[frozenset[str]] = frozenset({"hide_spam", "rules"})
    enabled: bool = True
    private_reply_dedup_window_days: Annotated[int, Field(ge=1, le=7)] = 7
    person_contact_window_days: Annotated[int, Field(ge=0, le=30)] = 7   # someone in contact gets no new opener from another post
    sensitive_topics_to_human: bool = True
    # Only what is clearly not a person interested: "tem promoção?" or "kkkkkk quero saber" are customers.
    spam_patterns: list[str] = [r"https?://", r"\b(sigam|siga meu|ganhe dinheiro|ganhe renda extra|pix gratis|clique no link)\b",
                                r"^(.)\1{6,}$", r"(@\w+\s*){4,}"]
    sensitive_patterns: list[str] = [r"\b(golpe|golpist\w*|fraude|enganad\w*|reclama\w*|procon|processar|advogad\w*|roubo|vergonha|lixo|picaret\w*)\b"]
    attention_patterns: list[str] = [r"\?"]        # comments the seller should see even when nothing is sent (questions)
    any_ignore_patterns: list[str] = [r"^[^a-z0-9]*$", r"^(@\w+\s*)+$"]   # "any comment" skips emoji-only and friend tags
    disclosure_terms: list[str] = ["assistente", "atendimento virtual"]   # the first automatic direct must identify the assistant (persona.disclose_on_first_message)
    min_public_variants: PosInt = 3                # no identical mass replies
    public_max_chars: PosInt = 300
    private_max_chars: PosInt = 640
    intent_threshold: Prob = 0.7
    polling_enabled: bool = True                   # safety net for lost webhooks; also what works before App Review
    poll_lookback_days: PosInt = 7
    poll_max_media: PosInt = 10
    poll_page_size: PosInt = 50
    catalog_max_media: PosInt = 25
    backfill_window_days: Annotated[int, Field(ge=1, le=7)] = 7   # the provider refuses private replies after 7 days
    contacts_page_size: PosInt = 50                # list of who already talks to the account (ADR-019)
    contacts_max_pages_per_run: PosInt = 20
    quick_questions_max: PosInt = 4                # questions offered in a new direct (the provider's limit)
    quick_question_max_chars: PosInt = 80
    origin_max_items: PosInt = 3
    origin_max_chars: PosInt = 400


# ---------- followups ----------
class CadenceStep(Strict):
    delay: str                                 # ISO-like "4h", "30m"
    content_kind: Literal["template", "generated"]
    template_key: str | None = None
    prompt_version: str | None = None
    max_chars: PosInt = 200


class Cadence(Strict):
    _removed: ClassVar[frozenset[str]] = frozenset({"stop_on"})     # the conversation's events stop every run (orchestration)
    id: str
    label: str = ""                            # the name in the console; empty means the id, humanized
    enabled: bool = True
    trigger: dict                              # event: stage_entered | bot_message_unanswered; stages: [...]
    steps: list[CadenceStep]
    max_attempts: PosInt = 1


class FollowupsConfig(Strict):
    enabled: bool = True
    cadences: list[Cadence] = []
    texts: dict[str, str] = {}                 # this account's words for the package's reminder templates
    stalled_after: Annotated[str, Field(pattern=r"^\d+\s*[smhd]$")] = "24h"   # silence that lists a contact as stalled in the console
    min_delay: Annotated[str, Field(pattern=r"^\d+\s*[smhd]$")] = "15m"       # the shortest wait a rule may set in the console
    max_steps: PosInt = 3                      # attempts a rule may have in the console


# ---------- compliance (merge must be monotonic) ----------
class ComplianceConfig(Strict):
    forbidden_phrases: list[str] = []
    required_disclosures: dict[str, str] = {}
    never_request: list[str] = ["cpf", "rg", "income", "assets", "bank_data", "health_data"]
    opt_out_keywords: list[str] = ["parar", "pare", "para", "parem", "pode parar", "sair", "stop", "cancelar mensagens", "não quero mais",
                                   "chega", "me deixa em paz"]
    # Sentences that ask to stop, anywhere in the message (normalized text). Conservative on purpose: priority 1 over conversion.
    opt_out_patterns: list[str] = [
        r"\bnao quero (mais )?(receber|ser chamad|que me (mande|mandem|envie|enviem|chame|chamem))",
        r"\b(para|pare|parem|parar) de (me )?(mandar|enviar|chamar|mensag|incomodar)",
        r"\bnao (me )?(manda|mande|mandem|envia|envie|enviem|chama|chame|chamem|incomoda|incomode|procura|procure|procurem)\b.*\bmais\b",
        r"\bnao (me )?(entre|entra|entrem)( mais)? em contato\b",
        r"\bme (deixa|deixe|deixem) em paz\b",
        r"\bpode(m)? parar\b",
        r"\b(tira|tire|tirem|remove|remova|removam|exclui|exclua) (meu (numero|contato)|da lista|dessa lista|desta lista)",
    ]
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
        if not set(base.opt_out_patterns) <= set(self.opt_out_patterns):
            problems.append("opt_out_patterns removed")
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
    handoff_undelivered_minutes: PosInt = 10   # a seller who should have been told and was not, after this
    archive_failing_minutes: PosInt = 15       # database backups (WAL archiving) failing for this long
    restore_test_max_age_days: PosInt = 35     # production: last good restore test older than this (it runs monthly)
    disk_used_percent: Annotated[int, Field(gt=0, le=100)] = 80


class DatasetsConfig(Strict):
    """Tables the tenant maintains in the console (package DatasetSpec): limits and who may activate a new version."""
    max_rows: PosInt = 5000
    max_upload_bytes: PosInt = 2_000_000
    two_person_activation: bool = False        # True: a version must be activated by someone other than its author


class KnowledgeConfig(Strict):
    """Material managed in the console (ADR-021)."""
    max_upload_bytes: PosInt = 30_000_000
    max_pages: PosInt = 500
    never_import_name_hashes: list[str] = []   # files of this account that must never come in, whatever the scan finds (admin.forbidden_name_hash)
    maskable_on_request: list[str] = ["email", "phone", "cnpj"]    # can be the company's own; CPF and cards always belong to a person
    page_min_chars: PosInt = 30                # less text than this on a page: read it with OCR
    max_ocr_pages: PosInt = 60                 # one page at a time, on the small production machine
    ocr_dpi: PosInt = 200
    embed_batch_size: PosInt = 64
    processing_retry_after_min: PosInt = 20    # longer than the slowest OCR, so a job still running is not asked for twice
    processing_max_requeues: PosInt = 3
    faq_question_max_chars: PosInt = 200
    faq_answer_max_chars: PosInt = 1000
    passage_max_chars: PosInt = 4000


class ReviewConfig(Strict):
    """A person reading conversations (daily during the pilot): how many the console offers per day and what counts as a violation."""
    daily_sample: PosInt = 20
    findings_window_days: PosInt = 30
    note_max_chars: PosInt = 1000
    categories: dict[str, str] = {"compliance": "Regra de conformidade", "wrong_information": "Informação errada",
                                  "tone": "Tom ou conversa ruim", "missed_handoff": "Devia ter chamado o vendedor", "other": "Outro"}
    violation_categories: list[str] = ["compliance"]


class PostsConfig(Strict):
    """Posts written in the console and published through the provider (ADR-024)."""
    publishing_enabled: bool = False           # off until the provider grants the permission and someone turns it on
    caption_max_chars: PosInt = 2200
    caption_max_hashtags: PosInt = 30
    caption_max_mentions: PosInt = 20
    carousel_max_items: PosInt = 10
    max_image_bytes: PosInt = 8_000_000
    max_video_bytes: PosInt = 100_000_000      # the edge proxy has to allow this too (docker/Caddyfile)
    file_chunk_bytes: PosInt = 1_000_000       # what the small API process holds in memory per file
    image_min_width: PosInt = 320
    image_min_ratio: Annotated[float, Field(gt=0)] = 0.8      # width / height the provider accepts in the feed
    image_max_ratio: Annotated[float, Field(gt=0)] = 1.91
    max_per_day: PosInt = 10                   # ours, well under the provider's limit: a queue of mistakes stops here
    late_after_min: PosInt = 60                # an approved post this late waits for a person instead of going out
    container_wait_min: PosInt = 5             # how long the provider may take with a file before the attempt ends


class OperationsConfig(Strict):
    _removed: ClassVar[frozenset[str]] = frozenset({"trace_sampling", "canary_percent"})
    alerts: OpsAlertsConfig = OpsAlertsConfig()
    review: ReviewConfig = ReviewConfig()
    knowledge: KnowledgeConfig = KnowledgeConfig()
    datasets: DatasetsConfig = DatasetsConfig()
    posts: PostsConfig = PostsConfig()
    feature_flags: dict[str, bool] = {}
    dm_recovery_enabled: bool = True           # after an outage, directs the provider gave up delivering are read back
    dm_recovery_lookback_minutes: PosInt = 180
    dm_recovery_max_threads: PosInt = 25
    evaluation_stuck_after_min: PosInt = 60    # an evaluation running longer than this is reported as failed
    invite_valid_hours: PosInt = 72            # the link that lets someone join the console
    retention_days: dict[str, PosInt] = {
        "inbound_raw": 30, "media": 30, "messages_original": 365, "comments": 365, "llm_calls": 365, "checkpoints": 30,
        "ownership_evidence": 90, "dead_letters": 30, "tool_call_args": 90, "handoff_summaries": 365,
        "lead_forms": 730, "audit_log": 1825, "reviews": 365, "invites": 30, "post_drafts": 90}


class PackageRef(Strict):
    name: str
    version: str


class PlatformConfig(Strict):
    _removed: ClassVar[frozenset[str]] = frozenset({"meta"})     # the Graph API version is per process: META_GRAPH_VERSION
    schema_version: Literal[1] = 1
    environment: Literal["dev", "staging", "prod"] = "prod"
    tenant_slug: str
    segment_package: PackageRef
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
        if self.operations.feature_flags.get("go_live", False) and self.environment == "dev":
            raise ValueError("go_live needs a prod or staging config")
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
