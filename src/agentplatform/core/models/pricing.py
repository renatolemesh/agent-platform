"""Price table (USD). Unknown model => the gateway refuses the call (no call without a budget estimate).

Sources (consulted 2026-10-09): OpenRouter /api/v1/models; OpenAI pricing page. See docs/plan/18-fontes-externas.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

D = Decimal


@dataclass(frozen=True)
class ChatPrice:
    input_per_m: Decimal
    output_per_m: Decimal
    cached_input_per_m: Decimal | None = None
    reasoning: bool = False          # reasoning tokens billed as output; temperature not supported


CHAT: dict[str, ChatPrice] = {
    # OpenAI direct / via OpenRouter
    "openai/gpt-4.1-nano": ChatPrice(D("0.10"), D("0.40"), D("0.025")),
    "openai/gpt-4.1-mini": ChatPrice(D("0.40"), D("1.60"), D("0.10")),
    "openai/gpt-5-nano": ChatPrice(D("0.05"), D("0.40"), D("0.005"), reasoning=True),
    "openai/gpt-5-mini": ChatPrice(D("0.25"), D("2.00"), D("0.025"), reasoning=True),
    "openai/gpt-5.4-nano": ChatPrice(D("0.20"), D("1.25"), None, reasoning=True),
    "openai/gpt-5.4-mini": ChatPrice(D("0.75"), D("4.50"), None, reasoning=True),
    # OpenRouter (ZDR endpoints)
    "google/gemini-3.1-flash-lite": ChatPrice(D("0.25"), D("1.50")),
    "deepseek/deepseek-v4-flash": ChatPrice(D("0.09"), D("0.18"), reasoning=True),
    "mistralai/mistral-small-2603": ChatPrice(D("0.15"), D("0.60")),
    "meta-llama/llama-4-maverick": ChatPrice(D("0.19"), D("0.65")),
    "anthropic/claude-haiku-4.5": ChatPrice(D("1.00"), D("5.00")),
    "openai/gpt-oss-120b": ChatPrice(D("0.037"), D("0.17")),
}

EMBEDDING_PER_M: dict[str, Decimal] = {
    "openai/text-embedding-3-small": D("0.02"),
    "openai/text-embedding-3-large": D("0.13"),
    "qwen/qwen3-embedding-8b": D("0.01"),
    "baai/bge-m3": D("0.01"),
}

EMBEDDING_DIMS: dict[str, int] = {
    "openai/text-embedding-3-small": 1536,
    "openai/text-embedding-3-large": 3072,
    "qwen/qwen3-embedding-8b": 1024,
    "baai/bge-m3": 1024,
}

TRANSCRIPTION_PER_MIN: dict[str, Decimal] = {
    "openai/gpt-4o-mini-transcribe": D("0.003"),
    "openai/whisper-1": D("0.006"),
    "openai/whisper-large-v3-turbo": D("0.00067"),
}


# Priced for the direct key, but OpenRouter has no zero-retention route for them, so the console does not offer them there.
DIRECT_KEY_ONLY = frozenset({"openai/gpt-4o-mini-transcribe", "openai/whisper-1"})


class UnknownPrice(LookupError):
    pass


def chat_price(model: str) -> ChatPrice:
    try:
        return CHAT[model]
    except KeyError as e:
        raise UnknownPrice(f"no price for model {model!r}; add it to pricing.CHAT before use") from e


def chat_cost(model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> Decimal:
    p = chat_price(model)
    cached = min(cached_tokens, input_tokens)
    cached_rate = p.cached_input_per_m if p.cached_input_per_m is not None else p.input_per_m
    return ((input_tokens - cached) * p.input_per_m + cached * cached_rate + output_tokens * p.output_per_m) / D(1_000_000)


def embedding_cost(model: str, tokens: int) -> Decimal:
    if model not in EMBEDDING_PER_M:
        raise UnknownPrice(f"no price for embedding model {model!r}")
    return tokens * EMBEDDING_PER_M[model] / D(1_000_000)


def transcription_cost(model: str, seconds: float) -> Decimal:
    if model not in TRANSCRIPTION_PER_MIN:
        raise UnknownPrice(f"no price for transcription model {model!r}")
    return TRANSCRIPTION_PER_MIN[model] * D(str(max(seconds, 1.0) / 60))


def estimate_tokens(text: str) -> int:
    """Upper bound used for budget reservations: tokenizers average ~4 chars/token in pt-BR, so 2 chars/token
    (plus JSON/message overhead already present in `text`) stays above the real count."""
    return max(1, len(text) // 2 + 16)
