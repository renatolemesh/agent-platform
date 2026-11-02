"""What each post does with comments (ADR-018): account default (active or inactive) plus an optional per-post choice.
The seller edits this without a second approval, so the texts pass the same deterministic checks as the assistant's replies
when saved. Nothing here sends anything."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from psycopg import AsyncConnection

from agentplatform.core.agent.guards import OutputContext, failed, output_guard
from agentplatform.core.config.schema import PlatformConfig

MODES = ("off", "register", "public", "private", "both")
SENDS_PUBLIC = ("public", "both")
SENDS_PRIVATE = ("private", "both")
_PLACEHOLDER = re.compile(r"\{(\w*)\}")
_CHECKED = ("url_not_allowed", "numbers_provenance", "forbidden_phrase", "required_context", "never_request", "pii_placeholder",
            "pii_in_output", "human_impersonation", "one_question", "empty")


@dataclass(frozen=True)
class Effective:
    mode: str = "off"
    source: str = "account"            # account | post
    trigger_kind: str = "keyword"
    keywords: list[str] = field(default_factory=list)
    public_variants: list[str] = field(default_factory=list)
    private_variants: dict[str, str] = field(default_factory=dict)
    repeat_variants: list[str] = field(default_factory=list)
    active_since: datetime | None = None
    backfill_since: datetime | None = None
    hide_spam: bool = False
    context_slots: dict = field(default_factory=dict)


def effective(settings: dict | None, auto: dict | None, now: datetime) -> Effective:
    """Per-post choice over the account default; empty post fields inherit the account's."""
    s = settings or {}
    a = auto or {}
    own = a.get("mode") not in (None, "default")
    mode = a["mode"] if own else (s["default_mode"] if s.get("default_active") else "off")
    if (a.get("valid_from") and now < a["valid_from"]) or (a.get("valid_to") and now > a["valid_to"]):
        mode = "off"
    return Effective(
        mode=mode, source="post" if own else "account",
        trigger_kind=a.get("trigger_kind") or s.get("trigger_kind") or "keyword",
        keywords=list(a.get("keywords") or s.get("keywords") or []),
        public_variants=list(a.get("public_variants") or s.get("public_variants") or []),
        private_variants=dict(a.get("private_variants") or s.get("private_variants") or {}),
        repeat_variants=list(s.get("repeat_variants") or []),
        active_since=a.get("active_since") if own else s.get("active_since"),
        backfill_since=a.get("backfill_since"), hide_spam=bool(s.get("hide_spam")),
        context_slots=dict(a.get("context_slots") or {}))


async def load_settings(conn: AsyncConnection, channel_account_id: UUID) -> dict | None:
    return await (await conn.execute("select * from comment_settings where channel_account_id=%s", (channel_account_id,))).fetchone()


async def effective_for(conn: AsyncConnection, channel_account_id: UUID, media: dict, now: datetime) -> Effective:
    """An ad post follows its own choice, else the choice of the organic post it promotes."""
    auto = await (await conn.execute("select * from media_automations where media_id=%s", (media["id"],))).fetchone()
    if (auto is None or auto["mode"] == "default") and media.get("original_media_id"):
        auto = await (await conn.execute(
            """select a.* from media_automations a join channel_media m on m.id = a.media_id
               where m.tenant_id=%s and m.external_media_id=%s""", (media["tenant_id"], media["original_media_id"]))).fetchone() or auto
    return effective(await load_settings(conn, channel_account_id), auto, now)


def render_text(text: str, seller: str) -> str:
    return text.replace("{seller}", seller)


@dataclass(frozen=True)
class TextProblem:
    field: str                # public | private | repeat | keywords | trigger
    rule: str
    text: str = ""


def text_problems(cfg: PlatformConfig, where: str, text: str, max_chars: int, allowed_urls: list[str],
                  checked: tuple[str, ...] = _CHECKED) -> list[TextProblem]:
    out: list[TextProblem] = []
    unknown = [m for m in _PLACEHOLDER.findall(text) if m != "seller"]
    if unknown:
        out.append(TextProblem(where, "unknown_placeholder", text))
    rendered = render_text(text, cfg.conversation.persona.seller_display_name)
    if len(rendered) > max_chars:
        out.append(TextProblem(where, "too_long", text))
    ctx = OutputContext(compliance=cfg.compliance, sources_text=[], allowed_numbers=set(), recent_bot_texts=[], max_bytes=1000,
                        strict=False, repetition_threshold=1.1, repetition_overlap=1.1, repetition_min_words=1000,
                        allowed_url_prefixes=allowed_urls, required_urls=[])
    out.extend(TextProblem(where, v.rule_id, text) for v in failed(output_guard(rendered, ctx)) if v.rule_id in checked)
    return out


def validate_questions(cfg: PlatformConfig, questions: list[str], allowed_urls: list[str]) -> list[TextProblem]:
    """Questions offered in a new direct: published text, so the same checks as the automatic replies."""
    out = [] if questions else [TextProblem("question", "missing_question")]
    if len(questions) > cfg.comments.quick_questions_max:
        out.append(TextProblem("question", "too_many_questions"))
    for q in questions:
        out.extend(text_problems(cfg, "question", q, cfg.comments.quick_question_max_chars, allowed_urls))
    return out


def validate(cfg: PlatformConfig, *, mode: str, trigger_kind: str, keywords: list[str], public: list[str], private: dict[str, str],
             repeat: list[str], allowed_urls: list[str]) -> list[TextProblem]:
    """Effective texts of a choice (post merged with account). Whatever will not be sent is not required, but what is
    filled in always has to pass: it can start being used with one click."""
    c = cfg.comments
    out: list[TextProblem] = []

    def check(where: str, text: str, max_chars: int) -> None:
        out.extend(text_problems(cfg, where, text, max_chars, allowed_urls))

    for t in public:
        check("public", t, c.public_max_chars)
    for t in repeat:
        check("repeat", t, c.public_max_chars)
    for t in private.values():
        check("private", t, c.private_max_chars)
        terms = c.disclosure_terms if cfg.conversation.persona.disclose_on_first_message else []
        if terms and not any(x.lower() in t.lower() for x in terms):
            out.append(TextProblem("private", "missing_disclosure", t))
    if mode in SENDS_PUBLIC and len(public) < c.min_public_variants:
        out.append(TextProblem("public", "not_enough_variants"))
    if mode in SENDS_PRIVATE and not private:
        out.append(TextProblem("private", "missing_text"))
    if mode != "off" and trigger_kind == "keyword" and not keywords:
        out.append(TextProblem("keywords", "missing_keywords"))
    if trigger_kind == "intent" and cfg.models.comment_intent is None:
        out.append(TextProblem("trigger", "intent_unavailable"))
    return out
