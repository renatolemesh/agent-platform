"""ModelGateway: the only way any node, eval or lab tool calls an LLM, embedding or STT model (plan 04/08, ADR-011).

- Provider: OpenRouter (default; ZDR + data_collection=deny) or OpenAI direct (dev, synthetic data only).
- Every call reserves budget first (key caps + tenant scopes); settles at the real cost (provider `usage.cost`,
  or price table x usage tokens); on missing usage it settles at the estimate (never 0).
- Structured output is always validated with Pydantic on our side.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, TypeVar
from uuid import UUID

import httpx
from pydantic import BaseModel

from agentplatform.core.config.schema import ModelRef, NodeModel, ProviderPolicy
from agentplatform.core.models import pricing
from agentplatform.core.models.budget import BudgetService, ScopeCap, utc_day
from agentplatform.core.ops.tracing import span
from agentplatform.settings import Settings, get_settings

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

BASE_URL = {"openai": "https://api.openai.com/v1", "openrouter": "https://openrouter.ai/api/v1"}
RETRY_WAIT_S = 0.5


def _audio_format(audio: bytes) -> str:
    """Channels do not say what a voice note is: the file's own first bytes do."""
    if audio[:4] == b"OggS":
        return "ogg"
    if audio[4:8] == b"ftyp":
        return "m4a"
    if audio[:4] == b"RIFF":
        return "wav"
    if audio[:3] == b"ID3" or audio[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3"
    return "ogg"


class BudgetDenied(RuntimeError):
    def __init__(self, scope: str | None):
        super().__init__(f"budget denied at {scope}")
        self.scope = scope


class ModelCallError(RuntimeError):
    """`transient`: worth one more try (timeout, 429, 5xx). `out_of_credit`: the provider's own stop (HTTP 402)."""

    def __init__(self, message: str = "", *, transient: bool = False, out_of_credit: bool = False):
        super().__init__(message)
        self.transient, self.out_of_credit = transient, out_of_credit


def _provider_policy(policy: ProviderPolicy) -> dict:
    """What OpenRouter receives. To it an empty `only` means no provider at all (HTTP 404 on every call), so empty lists stay out."""
    return {k: v for k, v in policy.model_dump().items() if v != []}


def _http_error(what: str, r: httpx.Response) -> ModelCallError:
    return ModelCallError(f"HTTP {r.status_code} {what}: {r.text[:200]}", transient=r.status_code == 429 or r.status_code >= 500,
                          out_of_credit=r.status_code == 402)


@dataclass
class CallContext:
    tenant_id: UUID | None
    node: str
    purpose: str = "turn"                       # turn | lab | ingest | judge | ci
    turn_id: UUID | None = None
    scopes: list[ScopeCap] = field(default_factory=list)
    reservation_ttl_s: int = 120


def tenant_context(budget, tenant_id: UUID, node: str, *, purpose: str = "turn", conversation_id: UUID | str | None = None,
                   turn_id: UUID | str | None = None) -> CallContext:
    """CallContext with every tenant budget scope that applies: pilot total (raised by recharges), UTC day, and the
    conversation/turn caps when known. All tenant-attributed calls (turn, transcription, summary) must use this."""
    scopes = [ScopeCap(f"day:{utc_day()}", budget.daily_cap_usd, tenant_id)]
    if budget.pilot_total_cap_usd is not None:
        scopes.append(ScopeCap("total", budget.pilot_total_cap_usd, tenant_id))
    if conversation_id is not None:
        scopes.append(ScopeCap(f"conversation:{conversation_id}", budget.per_conversation_cap_usd, tenant_id))
    if turn_id is not None:
        scopes.append(ScopeCap(f"turn:{turn_id}", budget.per_turn_cap_usd, tenant_id))
    return CallContext(tenant_id, node, purpose=purpose, turn_id=UUID(str(turn_id)) if turn_id else None,
                       scopes=scopes, reservation_ttl_s=budget.reservation_ttl_s)


@dataclass
class ChatResult:
    content: str
    parsed: Any | None
    model_used: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    latency_ms: int


def strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema -> OpenAI strict json_schema (all props required, no extra props)."""
    schema = model.model_json_schema()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                props = node.get("properties", {})
                node["required"] = list(props.keys())
                node["additionalProperties"] = False
            for k in ("default", "title"):
                node.pop(k, None)
            for v in list(node.values()):
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return schema


class ModelGateway:
    def __init__(self, budget: BudgetService, settings: Settings | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings or get_settings()
        self.budget = budget
        key = self.settings.llm_api_key
        if key is None:
            raise ModelCallError(f"no API key configured for provider {self.settings.llm_provider}")
        self.provider = self.settings.llm_provider
        self._client = httpx.AsyncClient(
            base_url=BASE_URL[self.provider], transport=transport, timeout=60,
            headers={"Authorization": f"Bearer {key.get_secret_value()}"})      # httpx sets JSON/multipart per request

    async def aclose(self) -> None:
        await self._client.aclose()

    def _wire_model(self, model: str) -> str:
        if self.provider == "openai":
            if not model.startswith("openai/"):
                raise ModelCallError(f"provider=openai cannot route model {model!r}")
            return model.split("/", 1)[1]
        return model

    # ------------------------------------------------------------------ chat
    async def _post(self, path: str, timeout_s: float, **kwargs: Any) -> httpx.Response:
        """The real limit of a call. httpx only counts the time between reads, and OpenRouter sends blank bytes every few seconds while it waits,
        so without this a stuck provider holds the turn until the turn's own limit."""
        try:
            async with asyncio.timeout(timeout_s):
                return await self._client.post(path, timeout=timeout_s, **kwargs)
        except TimeoutError as e:
            raise httpx.ReadTimeout(f"no answer from {path} in {timeout_s}s") from e

    async def chat(self, spec: NodeModel, messages: list[dict], ctx: CallContext,
                   output_model: type[T] | None = None) -> ChatResult:
        with span("llm.chat", node=ctx.node, purpose=ctx.purpose, tenant_id=ctx.tenant_id, model=spec.primary,
                  prompt_version=spec.prompt_version) as sp:
            result = await self._chat(spec, messages, ctx, output_model)
            sp.set_attribute("model_used", result.model_used or "")
            sp.set_attribute("cost_usd", float(result.cost_usd))
            sp.set_attribute("input_tokens", result.input_tokens or 0)
            sp.set_attribute("output_tokens", result.output_tokens or 0)
            return result

    async def _chat(self, spec: NodeModel, messages: list[dict], ctx: CallContext,
                    output_model: type[T] | None = None) -> ChatResult:
        chain = [spec.primary, *spec.fallbacks]
        prompt_text = json.dumps(messages, ensure_ascii=False)
        in_tok = pricing.estimate_tokens(prompt_text)
        estimate = max(pricing.chat_cost(m, in_tok, spec.max_output_tokens * (3 if pricing.chat_price(m).reasoning else 1))
                       for m in chain)                                     # most expensive model of the chain
        res = await self.budget.reserve(estimate, ctx.scopes, ttl_s=ctx.reservation_ttl_s)
        if res.denied:
            await self._record(ctx, spec.primary, None, None, None, None, "budget_denied", spec.prompt_version, 0, None)
            raise BudgetDenied(res.denied_scope)

        result: ChatResult | None = None
        last_err: Exception | None = None
        try:
            # OpenRouter also falls back by itself, but never past our own timeout, nor to a model that refuses the parameters of the first one
            for position, model in enumerate(chain):
                for attempt in range(spec.retries + 1):
                    try:
                        result = await self._chat_once(model, chain[position:], spec, messages, output_model)
                        break
                    # ValueError covers invalid JSON and schema errors; Key/Index/Type a reply without the expected shape
                    except (httpx.HTTPError, ModelCallError, ValueError, KeyError, IndexError, TypeError) as e:
                        last_err = e
                        log.warning("model %s failed for node %s: %s", model, ctx.node, type(e).__name__)
                        slow_with_another_left = isinstance(e, httpx.TimeoutException) and position < len(chain) - 1
                        if slow_with_another_left or not (isinstance(e, httpx.HTTPError) or getattr(e, "transient", False)) or attempt == spec.retries:
                            break
                        await asyncio.sleep(RETRY_WAIT_S * (attempt + 1))
                if result is not None or getattr(last_err, "out_of_credit", False):
                    break
            if result is None:
                if getattr(last_err, "out_of_credit", False):
                    await self._record(ctx, spec.primary, None, None, None, None, "provider_out_of_credit", spec.prompt_version, 0, None)
                    raise BudgetDenied("provider_credit") from last_err
                raise ModelCallError(f"all models failed for node {ctx.node}: {type(last_err).__name__}") from last_err
            return result
        finally:
            if result is not None:
                await self.budget.settle(res, result.cost_usd)
                await self._record(ctx, spec.primary, result.model_used, result.input_tokens, result.output_tokens,
                                   result.cost_usd, "ok", spec.prompt_version, result.latency_ms, "computed")
            else:
                await self.budget.settle(res, estimate, needs_reconcile=True)
                await self._record(ctx, spec.primary, None, None, None, estimate, "error", spec.prompt_version, 0,
                                   "estimate")

    async def _chat_once(self, model: str, chain: list[str], spec: NodeModel, messages: list[dict],
                         output_model: type[BaseModel] | None) -> ChatResult:
        price = pricing.chat_price(model)
        body: dict[str, Any] = {"model": self._wire_model(model), "messages": messages}
        if price.reasoning:
            body["max_completion_tokens"] = spec.max_output_tokens * 3
            body["reasoning_effort"] = "minimal" if spec.reasoning_effort in ("none", "minimal") else spec.reasoning_effort
        else:
            body["max_tokens" if self.provider == "openrouter" else "max_completion_tokens"] = spec.max_output_tokens
            body["temperature"] = spec.temperature
        if output_model is not None and spec.structured_output == "json_schema":
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": output_model.__name__, "strict": True, "schema": strict_schema(output_model)}}
        if self.provider == "openrouter":
            body["models"] = chain
            body["provider"] = _provider_policy(spec.provider)
            body["usage"] = {"include": True}
        t0 = time.monotonic()
        r = await self._post("/chat/completions", spec.timeout_s, json=body)
        latency = int((time.monotonic() - t0) * 1000)
        if r.status_code >= 400:
            raise _http_error(f"from {self.provider}", r)
        data = r.json()
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
        usage = data.get("usage") or {}
        in_t, out_t = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)
        used = data.get("model") or model
        used_key = used if "/" in used else f"openai/{used}"
        if self.provider == "openrouter" and usage.get("cost") is not None:
            cost = Decimal(str(usage["cost"]))
        else:
            base = next((k for k in pricing.CHAT if used_key.startswith(k)), model)
            cost = pricing.chat_cost(base, in_t, out_t, cached)
        parsed = output_model.model_validate_json(content) if output_model is not None else None
        return ChatResult(content, parsed, used_key, in_t, out_t, cost, latency)

    # ------------------------------------------------------------- embeddings
    async def embed(self, spec: ModelRef, texts: list[str], ctx: CallContext) -> list[list[float]]:
        if ctx.purpose == "ingest" and self.provider == "openai":      # no zero data retention on the direct key: synthetic data only
            raise ModelCallError("indexing documents needs LLM_PROVIDER=openrouter")
        tokens = sum(pricing.estimate_tokens(t) for t in texts)
        estimate = pricing.embedding_cost(spec.model, tokens)
        res = await self.budget.reserve(estimate, ctx.scopes, ttl_s=ctx.reservation_ttl_s)
        if res.denied:
            raise BudgetDenied(res.denied_scope)
        cost = estimate
        status = "error"
        t0 = time.monotonic()
        try:
            body: dict[str, Any] = {"model": self._wire_model(spec.model), "input": texts}
            if spec.dims:
                body["dimensions"] = spec.dims
            if self.provider == "openrouter":
                body["provider"] = _provider_policy(spec.provider)
            r = await self._post("/embeddings", spec.timeout_s if len(texts) == 1 else spec.batch_timeout_s, json=body)
            if r.status_code >= 400:
                raise _http_error("embeddings", r)
            data = r.json()
            used = int((data.get("usage") or {}).get("prompt_tokens", tokens))
            cost = pricing.embedding_cost(spec.model, used)
            vectors = [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]
            status = "ok"
            return vectors
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            raise ModelCallError(f"embeddings failed: {type(e).__name__}", transient=isinstance(e, httpx.HTTPError)) from e
        finally:
            await self.budget.settle(res, cost)
            await self._record(ctx, spec.model, spec.model, tokens, 0, cost, status, None, int((time.monotonic() - t0) * 1000), "computed")

    # ---------------------------------------------------------- transcription
    async def transcribe(self, spec: ModelRef, audio: bytes, filename: str, seconds: float, ctx: CallContext,
                         language: str = "pt") -> str:
        estimate = pricing.transcription_cost(spec.model, seconds)
        res = await self.budget.reserve(estimate, ctx.scopes, ttl_s=ctx.reservation_ttl_s)
        if res.denied:
            raise BudgetDenied(res.denied_scope)
        status = "error"
        t0 = time.monotonic()
        try:
            if self.provider == "openrouter":   # JSON is the only form that carries the provider policy: voice is personal data (ZDR)
                r = await self._post("/audio/transcriptions", spec.timeout_s, json={
                    "model": spec.model, "language": language,
                    "input_audio": {"data": base64.b64encode(audio).decode("ascii"), "format": _audio_format(audio)},
                    "provider": {"zdr": spec.provider.zdr, "data_collection": spec.provider.data_collection}})
            else:
                r = await self._post("/audio/transcriptions", spec.timeout_s, files={"file": (filename, audio)},
                                     data={"model": self._wire_model(spec.model), "language": language})
            if r.status_code >= 400:
                raise _http_error("transcription", r)
            text = r.json().get("text", "")
            status = "ok"
            return text
        except (httpx.HTTPError, ValueError) as e:
            raise ModelCallError(f"transcription failed: {type(e).__name__}") from e
        finally:
            await self.budget.settle(res, estimate)
            await self._record(ctx, spec.model, spec.model, None, None, estimate, status, None, int((time.monotonic() - t0) * 1000), "estimate")

    async def _record(self, ctx: CallContext, requested: str, used: str | None, in_t: int | None, out_t: int | None,
                      cost: Decimal | None, status: str, prompt_version: str | None, latency_ms: int,
                      cost_source: str | None) -> None:
        from agentplatform.core.db.pool import platform_tx
        try:
            async with platform_tx() as conn:
                await conn.execute(
                    """insert into llm_calls (tenant_id, turn_id, node, purpose, provider, model_requested, model_used,
                       prompt_version, input_tokens, output_tokens, cost_usd, cost_source, latency_ms, status)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (ctx.tenant_id, ctx.turn_id, ctx.node, ctx.purpose, self.provider, requested, used, prompt_version,
                     in_t, out_t, cost, cost_source, latency_ms, status))
        except Exception:
            log.exception("failed to record llm_call (non-fatal)")


_gateway: ModelGateway | None = None


def get_gateway() -> ModelGateway:
    global _gateway
    if _gateway is None:
        s = get_settings()
        _gateway = ModelGateway(BudgetService(s.llm_total_cap_usd, s.llm_daily_cap_usd), s)
    return _gateway
