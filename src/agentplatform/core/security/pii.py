"""PII detection and masking (Brazil). Runs before any LLM call, in every ownership state (privacy_gate)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

PLACEHOLDERS = ("[CPF]", "[CNPJ]", "[CARTAO]", "[EMAIL]", "[TELEFONE]", "[RG]", "[SAUDE]", "[BANCO]", "[ENDERECO]")

_CPF = re.compile(r"(?<!\d)(\d{3})[.\s]?(\d{3})[.\s]?(\d{3})[-.\s]?(\d{2})(?!\d)")
_CNPJ = re.compile(r"(?<!\d)(\d{2})[.\s]?(\d{3})[.\s]?(\d{3})[/\s]?(\d{4})[-.\s]?(\d{2})(?!\d)")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)(?:\+?55[\s-]?)?\(?\d{2}\)?[\s-]?9?\d{4}[\s-]?\d{4}(?!\d)")
_RG = re.compile(r"\b(?:RG|identidade)\b\D{0,15}\d[\d.\-xX]{4,13}\b", re.IGNORECASE)
_BANK = re.compile(
    r"\b(?:ag(?:[eê]ncia)?\.?\s*(?:n[ºo°.]?\s*)?:?\s*\d{3,5}(?:-[\dxX])?"
    r"|conta(?:\s+(?:corrente|poupan[cç]a))?\s*(?:n[ºo°.]?\s*)?:?\s*\d{4,12}(?:-[\dxX])?)(?!\w)",
    re.IGNORECASE,
)
_ADDRESS = re.compile(
    r"\b(?:rua|avenida|av\.|travessa|alameda|rodovia|estrada)\s+[^\d,.;!?\n]{2,40}?,?\s*(?:n[ºo°.]?\s*)?\d{1,5}\b"
    r"|\bcep\b\D{0,5}\d{5}-?\d{3}\b|(?<!\d)\d{5}-\d{3}(?!\d)",
    re.IGNORECASE,
)
_HEALTH = re.compile(
    r"\b(c[aâ]ncer|diabet\w*|hipertens\w*|hiv|aids|cirurgia|quimioterapia|doen[cç]a\s+\w+|cid[-\s]?\d+|"
    r"depress[aã]o|infarto|avc|transplante|hemodi[aá]lise|di[aá]lise|leucemia|linfoma|tumor|met[aá]stase|gr[aá]vid[ao]|gestante|"
    r"esquizofreni\w*|bipolar|epilepsia|alzheimer|parkinson|insulina|hepatite|tuberculose)\b",
    re.IGNORECASE,
)


def _cpf_valid(d: str) -> bool:
    if len(d) != 11 or d == d[0] * 11:
        return False
    for i in (9, 10):
        s = sum(int(d[j]) * ((i + 1) - j) for j in range(i))
        if (s * 10 % 11) % 10 != int(d[i]):
            return False
    return True


def _cnpj_valid(d: str) -> bool:
    if len(d) != 14 or d == d[0] * 14:
        return False
    w1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    w2 = [6] + w1
    for w, pos in ((w1, 12), (w2, 13)):
        s = sum(int(d[i]) * w[i] for i in range(pos))
        r = s % 11
        if (0 if r < 2 else 11 - r) != int(d[pos]):
            return False
    return True


def _luhn(d: str) -> bool:
    total, alt = 0, False
    for ch in reversed(d):
        n = int(ch)
        if alt:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        alt = not alt
    return total % 10 == 0


@dataclass
class MaskResult:
    text: str
    found: dict[str, int] = field(default_factory=dict)

    @property
    def has_pii(self) -> bool:
        return bool(self.found)

    @property
    def has_sensitive(self) -> bool:
        return any(k in self.found for k in ("cpf", "cnpj", "card", "rg", "health", "bank"))


def find_phones(text: str | None) -> list[str]:
    """Phone numbers as written, for the one place that keeps a number the customer gave on purpose (handoff.customer_phone)."""
    return [m.group(0) for m in _PHONE.finditer(text or "")]


def mask(text: str, only: tuple[str, ...] | list[str] | None = None) -> MaskResult:
    """`only`: just these kinds. Default is everything."""
    found: dict[str, int] = {}

    def bump(kind: str) -> None:
        found[kind] = found.get(kind, 0) + 1

    def sub_cnpj(m: re.Match) -> str:
        digits = "".join(m.groups())
        if _cnpj_valid(digits):
            bump("cnpj")
            return "[CNPJ]"
        return m.group(0)

    def sub_cpf(m: re.Match) -> str:
        digits = "".join(m.groups())
        if _cpf_valid(digits):
            bump("cpf")
            return "[CPF]"
        return m.group(0)

    def sub_card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            bump("card")
            return "[CARTAO]"
        return m.group(0)

    def sub_simple(kind: str, placeholder: str):
        def _f(m: re.Match) -> str:
            bump(kind)
            return placeholder
        return _f

    out = text
    for kind, pattern, replace in (("email", _EMAIL, sub_simple("email", "[EMAIL]")), ("cnpj", _CNPJ, sub_cnpj), ("cpf", _CPF, sub_cpf), ("card", _CARD, sub_card),
                                   ("rg", _RG, sub_simple("rg", "[RG]")), ("bank", _BANK, sub_simple("bank", "[BANCO]")),
                                   ("address", _ADDRESS, sub_simple("address", "[ENDERECO]")), ("phone", _PHONE, sub_simple("phone", "[TELEFONE]")),
                                   ("health", _HEALTH, sub_simple("health", "[SAUDE]"))):
        if only is None or kind in only:
            out = pattern.sub(replace, out)
    return MaskResult(out, found)


def contains_placeholder(text: str) -> bool:
    return any(p in text for p in PLACEHOLDERS)
