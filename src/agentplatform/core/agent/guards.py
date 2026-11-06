"""Guards (plan 04 §6). Rules first; the LLM judge only on risk categories.

privacy_gate: PII masking + whole-message opt-out — runs in EVERY ownership state (review #1).
input_guard:  size, abuse rate, injection heuristics, explicit human request, package handoff triggers.
output_guard: numbers must literally appear in sources (review #2), forbidden phrases, never-request, PII
              placeholders (no re-hydration, review #3), human impersonation, one question, repetition, bytes,
              links created this turn present.
disclosures_needed / with_disclosures: the official disclosure of a tool whose numbers the reply uses is added by code.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Iterable

from agentplatform.core.agent.state import GuardVerdict
from agentplatform.core.config.schema import ComplianceConfig, InputGuardConfig
from agentplatform.core.packages.contract import PackageManifest
from agentplatform.core.security.pii import contains_placeholder, mask


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", t).strip()


def _bare(text: str) -> str:
    return re.sub(r"[^\w\s]", "", normalize(text)).strip()


def keyword_hit(keyword: str, normalized_text: str) -> bool:
    """From the start of a word: "rg" is not in "urgencia", nor "logo" in "catalogo"; prefixes still work ("taxa" hits "taxas")."""
    return re.search(rf"(?<!\w){re.escape(normalize(keyword))}", normalized_text) is not None


# ---------------------------------------------------------------- privacy gate
@dataclass
class PrivacyResult:
    masked_texts: list[str]
    opt_out: bool
    sensitive: bool


def privacy_gate(texts: list[str], compliance: ComplianceConfig) -> PrivacyResult:
    masked, sensitive = [], False
    keywords = {_bare(k) for k in compliance.opt_out_keywords}
    opt_out = False
    for t in texts:
        r = mask(t)
        masked.append(r.text)
        sensitive = sensitive or r.has_sensitive
        if _bare(t) in keywords or any(re.search(p, normalize(t)) for p in compliance.opt_out_patterns):
            opt_out = True
    return PrivacyResult(masked, opt_out, sensitive)


# ---------------------------------------------------------------- input guard
_INJECTION = [
    r"ignor\w* (as |todas as )?(instru\w+|regras|ordens) anteriores", r"ignore (all |previous |the above )",
    r"(revele|mostre|me (de|passe)|print|repeat) (o |seu |the )?(prompt|system prompt|instru\w+ do sistema)",
    r"voce agora e", r"you are now", r"modo (desenvolvedor|developer|dan)", r"<\s*/?\s*(system|data)\b",
    r"aja como|finja (que|ser)",
]
_HUMAN_REQ = [r"\b(falar|conversar) com (um |uma |o |a )?(humano|atendente|pessoa|vendedor|consultor|alguem)\b",
              r"\b(quero|prefiro) (um |uma )?(humano|atendente|pessoa)\b", r"\bme passa (pro|para o) (vendedor|consultor)\b"]
# Asking "is this a scam?" is what a careful buyer does: only calling someone a crook goes to a person right away.
_ABUSE = [r"\b(vai se f\w+|fdp|filh\w+ da p\w+|idiota\w*|imbecil\w*|lixo|golpist\w*|picaret\w*)\b"]
_HUMAN_REQ_NEGATED = re.compile(r"\b(nao|nem|sem)( \w+)?\W*$")      # "nao quero falar com vendedor", not "nao gostei, quero falar com..."


@dataclass
class InputGuardResult:
    action: str                      # continue | handoff | block
    reasons: list[str] = field(default_factory=list)
    injection_score: float = 0.0
    handoff_reason: str | None = None
    truncated: bool = False


def input_guard(text: str, cfg: InputGuardConfig, manifest: PackageManifest, disabled: Iterable[str] = ()) -> InputGuardResult:
    res = InputGuardResult("continue")
    if len(text) > cfg.max_chars:
        res.truncated = True
        res.reasons.append("truncated")
    n = normalize(text)
    hits = sum(1 for p in _INJECTION if re.search(p, n))
    res.injection_score = min(1.0, hits / 2)
    if res.injection_score >= cfg.injection_score_threshold:
        res.reasons.append("injection_suspected")       # never changes rules; stricter output guard downstream
    if any(not _HUMAN_REQ_NEGATED.search(n[:m.start()]) for p in _HUMAN_REQ for m in re.finditer(p, n)):
        res.action, res.handoff_reason = "handoff", "explicit_request"
        return res
    if any(re.search(p, n) for p in _ABUSE):
        res.action, res.handoff_reason = "handoff", "complaint_or_abuse"
        return res
    off = set(disabled)
    for trig in manifest.handoff_triggers:
        if trig.reason == "hot_lead":       # "nao quero entrar" is the opposite of a hot lead
            hit = any(not _HUMAN_REQ_NEGATED.search(n[:m.start()]) for k in trig.keywords
                      for m in re.finditer(rf"(?<!\w){re.escape(normalize(k))}", n))
        else:
            hit = any(keyword_hit(k, n) for k in trig.keywords)
        if (trig.locked or trig.reason not in off) and hit:
            res.action, res.handoff_reason = "handoff", trig.reason
            return res
    return res


# ---------------------------------------------------------------- numbers
_NUM = re.compile(r"(?:r\$\s*)?(\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?|\d+(?:\.\d+)?)\s*(mil|k|milh(?:ao|oes|ões|ão))?\s*(%)?",
                  re.IGNORECASE)


def _to_decimal(raw: str, suffix: str | None) -> Decimal | None:
    s = raw
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):
        s = s.replace(".", "")
    try:
        v = Decimal(s)
    except InvalidOperation:
        return None
    suf = (suffix or "").lower()
    if suf in ("mil", "k"):
        v *= 1000
    elif suf.startswith("milh"):
        v *= 1_000_000
    return v.normalize()


_EN_DECIMAL = re.compile(r"(?<![\d.,])(\d+\.\d+)(?![\d.,])")
_PT_THOUSANDS = re.compile(r"\d{1,3}(?:\.\d{3})+")


def extract_numbers(text: str, lenient: bool = False) -> set[Decimal]:
    """pt-BR parsing for drafts. lenient=True (sources such as tool JSON) also accepts en-US decimals ("1546.67"), as that one number: reading
    its parts too would make an invented "R$ 67" look like it had a source. "1.546" can be either, so it stays both."""
    out = set()
    norm = normalize(text).replace("r$ ", "r$")
    if lenient:
        def en_decimal(m: re.Match) -> str:
            out.add(Decimal(m.group(1)).normalize())
            return m.group(0) if _PT_THOUSANDS.fullmatch(m.group(1)) else " "
        norm = _EN_DECIMAL.sub(en_decimal, norm)
    for m in _NUM.finditer(norm):
        v = _to_decimal(m.group(1), m.group(2))
        if v is not None:
            out.add(v)
    return out


# ---------------------------------------------------------------- output guard
_NEVER_REQUEST = {
    "cpf": r"\bcpf\b", "rg": r"\brg\b", "income": r"\b(renda(?! extra)|salario|quanto (voce )?ganha)\b",
    "assets": r"\bpatrimonio\b", "bank_data": r"\b(conta bancaria|agencia|chave pix|dados bancarios)\b",
    "health_data": r"\b(saude|doenca|cirurgia|diagnostico)\b", "driver_license_number": r"\b(cnh|carteira de motorista)\b",
}
_REQUEST_VERB = r"(me (passa|envia|manda|informa|diz)|qual (e |eh )?(o |a )?seu|pode (me )?(enviar|passar|informar)|preciso (do|da|de) seu)"
_HUMAN_CLAIM = r"\b(sou|eu sou) (um |uma )?(humano|humana|pessoa real|atendente humano|pessoa de verdade)\b"


@dataclass
class OutputContext:
    compliance: ComplianceConfig
    sources_text: list[str]                   # tool results (json), retrieved chunk texts, customer messages
    allowed_numbers: set[Decimal]             # config numbers (hours, window days...)
    recent_bot_texts: list[str]
    max_bytes: int
    strict: bool = False                      # injection suspected -> stricter
    allowed_url_prefixes: tuple[str, ...] = ()
    repetition_threshold: float = 0.9
    repetition_overlap: float = 1.01          # > 1 disables the word-overlap measure
    repetition_min_words: int = 8
    guidance_texts: tuple[str, ...] = ()      # passages that may guide the reply but may not be quoted
    guidance_max_ngram: int = 12
    no_basis: bool = False                    # the search was asked and found nothing
    no_basis_claims: tuple[str, ...] = ()
    required_urls: tuple[str, ...] = ()       # links created by code this turn (e.g. pre-registration): must be sent


_URL = re.compile(r"https?://[^\s)>\]]+", re.IGNORECASE)
_OUTPUT_LEAK_KINDS = {"cpf", "cnpj", "card", "rg", "email", "phone", "bank"}


def output_guard(text: str, ctx: OutputContext) -> list[GuardVerdict]:
    v: list[GuardVerdict] = []
    n = normalize(text)

    def add(rule: str, verdict: str, detail: str = "") -> None:
        v.append(GuardVerdict(guard="output_guard", rule_id=rule, verdict=verdict, detail=detail))

    # URLs: only allowlisted prefixes; then removed before the number check (a link is not a product number)
    urls = _URL.findall(text)
    bad_urls = [u for u in urls if not any(u.startswith(p) for p in ctx.allowed_url_prefixes)]
    add("url_not_allowed", "block" if bad_urls else "pass", ",".join(bad_urls)[:200])
    text_wo_urls = _URL.sub(" ", text)
    # numbers must appear literally in a source (review #2)
    source_numbers: set[Decimal] = set(ctx.allowed_numbers)
    for s in ctx.sources_text:
        source_numbers |= extract_numbers(_URL.sub(" ", s), lenient=True)
    unsourced = sorted(f"{x:f}" for x in extract_numbers(text_wo_urls) - source_numbers)
    add("numbers_provenance", "block" if unsourced else "pass", ",".join(unsourced))

    hit = next((pat for pat in ctx.compliance.forbidden_phrases if _affirmative_match(normalize(pat), n)), None)
    add("forbidden_phrase", "block" if hit else "pass", hit or "")
    missing = next((pat for pat, need in ctx.compliance.require_with.items()
                    if re.search(normalize(pat), n) and not re.search(normalize(need), n)), None)
    add("required_context", "block" if missing else "pass", missing or "")

    blocked_req = None
    for cat in ctx.compliance.never_request:
        pat = _NEVER_REQUEST.get(cat)
        if not pat:
            continue
        for sent in re.split(r"(?<=[.!?])\s+", n):
            if re.search(pat, sent) and (re.search(_REQUEST_VERB, sent) or "?" in sent) and not re.search(r"\b(nao|nunca|sem)\b", sent):
                blocked_req = cat
    add("never_request", "block" if blocked_req else "pass", blocked_req or "")

    quoted = _quoted_passage(n, ctx.guidance_texts, ctx.guidance_max_ngram)
    add("guidance_quoted", "block" if quoted else "pass", quoted)
    add("answered_without_basis", "block" if ctx.no_basis and any(re.search(p, n) for p in ctx.no_basis_claims) else "pass")
    add("pii_placeholder", "block" if contains_placeholder(text) else "pass")
    # Personal identifiers. Not health words: explaining an insurance that covers "doenca grave" is about no one in particular, and
    # the model never sees the customer's own (privacy_gate masks them before any call).
    add("pii_in_output", "block" if set(mask(text).found) & _OUTPUT_LEAK_KINDS else "pass")
    add("human_impersonation", "block" if re.search(_HUMAN_CLAIM, n) else "pass")
    add("one_question", "rewrite" if text.count("?") > 1 else "pass")
    sim = max((SequenceMatcher(None, n, normalize(p)).ratio() for p in ctx.recent_bot_texts), default=0.0)
    overlap = max((_word_overlap(n, normalize(p), ctx.repetition_min_words) for p in ctx.recent_bot_texts), default=0.0)
    repeated = sim >= ctx.repetition_threshold or overlap >= ctx.repetition_overlap      # paraphrases of a past answer too
    add("repetition", "rewrite" if repeated else "pass", f"{sim:.2f}/{overlap:.2f}")
    add("length", "rewrite" if len(text.encode("utf-8")) > ctx.max_bytes * 3 else "pass")
    missing_urls = [u for u in ctx.required_urls if u not in text]
    add("missing_link", "rewrite" if missing_urls else "pass", ("inclua este link: " + missing_urls[0]) if missing_urls else "")
    if not text.strip():
        add("empty", "block")
    return v


def _quoted_passage(normalized_reply: str, passages: tuple[str, ...], ngram: int) -> str:
    """The words a reply copied from a passage it may only learn from, or "". Internal material must not reach a customer as a quote."""
    words = re.findall(r"\w+", normalized_reply)
    if len(words) < ngram or not passages:
        return ""
    grams = {tuple(words[i:i + ngram]) for i in range(len(words) - ngram + 1)}
    for p in passages:
        source = re.findall(r"\w+", normalize(p))
        for i in range(len(source) - ngram + 1):
            if tuple(source[i:i + ngram]) in grams:
                return " ".join(source[i:i + ngram])
    return ""


def ensure_identified(text: str, disclosure: str, terms: list[str], greeting: str = "") -> str:
    """The first message says it is an assistant. The prompt asks, and a model can forget: a rule like this cannot depend on it."""
    n = normalize(text)
    if any(normalize(t) in n for t in terms):
        return text
    m = re.match(greeting, text, re.IGNORECASE) if greeting else None
    head, body = (text[:m.end()].strip(), text[m.end():]) if m else ("", text)
    return " ".join(part for part in (head, disclosure, body) if part)


def disclosures_needed(text: str, tool_results: list[dict], customer_texts: list[str], disclosures: dict[str, str]) -> list[str]:
    """Official texts the reply must carry: a tool result with a disclaimer_key whose numbers (other than the customer's
    own) appear in the reply, unless the reply already has that text."""
    said: set[Decimal] = set()
    for t in customer_texts:
        said |= extract_numbers(t)
    used = extract_numbers(_URL.sub(" ", text))
    out: list[str] = []
    for t in tool_results:
        r = t.get("result")
        keys = [r.get("disclaimer_key"), *(r.get("disclaimer_keys") or [])] if isinstance(r, dict) else []
        for key in keys:
            note = disclosures.get(key or "")
            if note and note not in out and normalize(note) not in normalize(text) and \
                    used & (extract_numbers(json.dumps(r), lenient=True) - said):
                out.append(note)
    return out


def with_disclosures(text: str, notes: list[str]) -> str:
    """Before the closing question when there is one (the message still ends asking), else at the end."""
    if not notes:
        return text
    sents = re.split(r"(?<=[.!?])\s+", text.strip())
    block = " ".join(notes)
    if len(sents) > 1 and sents[-1].endswith("?"):
        return " ".join([*sents[:-1], block, sents[-1]])
    return f"{text.strip()} {block}"


def _content_words(n: str) -> set[str]:
    return {w for w in re.findall(r"\w+", n) if len(w) > 3}


def _word_overlap(a: str, b: str, min_words: int) -> float:
    """Share of the shorter message's content words that also appear in the other one (catches paraphrased repeats)."""
    wa, wb = _content_words(a), _content_words(b)
    if min(len(wa), len(wb)) < min_words:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


_NEGATION = re.compile(r"(nao|nunca|sem|nenhum\w*|jamais|nem)(\W+\w+){0,3}\W*$")


def _affirmative_match(pattern: str, text: str) -> bool:
    """A forbidden phrase counts only when stated affirmatively (a negated statement such as "sem X garantido" is allowed)."""
    for m in re.finditer(pattern, text, re.IGNORECASE):
        if not _NEGATION.search(text[max(0, m.start() - 40):m.start()]):
            return True
    return False


_NEGATION = re.compile(r"\b(nao|nunca|sem|nenhum\w*|jamais|nem)\b(\W+\w+){0,3}\W*$")


def _affirmative_match(pattern: str, text: str) -> bool:
    """A forbidden phrase counts only when stated affirmatively (a negated statement such as "sem X garantido" is allowed)."""
    for m in re.finditer(pattern, text, re.IGNORECASE):
        if not _NEGATION.search(text[max(0, m.start() - 40):m.start()]):
            return True
    return False


def failed(verdicts: list[GuardVerdict]) -> list[GuardVerdict]:
    return [x for x in verdicts if x.verdict != "pass"]
