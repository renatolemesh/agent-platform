"""ModelGateway + budget with a mocked provider (no network, no spend)."""
import asyncio
import json
import uuid
from decimal import Decimal

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from agentplatform.core.config.schema import ModelRef, NodeModel, ProviderPolicy
from agentplatform.core.models.budget import BudgetService, ScopeCap
from agentplatform.core.models.gateway import BudgetDenied, CallContext, ModelGateway, strict_schema
from agentplatform.settings import Settings

pytestmark = pytest.mark.db


class Out(BaseModel):
    answer: str
    score: float | None = None


def fake_openai(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json={
            "model": body["model"],
            "choices": [{"message": {"content": json.dumps({"answer": "oi", "score": 0.9})}}],
            "usage": {"prompt_tokens": len(json.dumps(body["messages"])) // 4, "completion_tokens": 50}})
    return httpx.MockTransport(handler)


def make(cap="0.50", calls=None, key_name=None):
    s = Settings(llm_provider="openai", openai_api_key=SecretStr("sk-test"))
    budget = BudgetService(Decimal(cap), Decimal(cap), key_name=key_name or f"test-{uuid.uuid4().hex[:8]}")
    return ModelGateway(budget, s, transport=fake_openai(calls if calls is not None else []))


SPEC = NodeModel(primary="openai/gpt-4.1-nano", prompt_version="t@v1", max_output_tokens=200)


async def test_chat_structured_and_cost(db):
    calls = []
    gw = make(calls=calls)
    r = await gw.chat(SPEC, [{"role": "user", "content": "olá"}], CallContext(None, "test", purpose="ci"), Out)
    assert r.parsed.answer == "oi"
    assert calls[0]["model"] == "gpt-4.1-nano"                      # provider prefix stripped for OpenAI
    assert calls[0]["response_format"]["json_schema"]["strict"] is True
    expected = (Decimal(r.input_tokens) * Decimal("0.10") + Decimal(r.output_tokens) * Decimal("0.40")) / Decimal(1_000_000)
    assert r.cost_usd == expected
    usage = await gw.budget.usage()
    assert usage[f"{gw.budget.key_name}:total"]["spent"] == expected.quantize(Decimal("0.000001"))
    assert usage[f"{gw.budget.key_name}:total"]["reserved"] == 0


async def test_openrouter_request_never_empties_the_provider_list(db):
    """Found on the first real call: `only: []` means no provider at all to OpenRouter, so every chat and embedding failed with HTTP 404."""
    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        if req.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}], "usage": {"prompt_tokens": 1}})
        return httpx.Response(200, json={"model": "openai/gpt-4.1-nano", "choices": [{"message": {"content": json.dumps({"answer": "oi", "score": 0.9})}}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.000001}})

    s = Settings(llm_provider="openrouter", openrouter_api_key=SecretStr("sk-test"))
    gw = ModelGateway(BudgetService(Decimal("0.50"), Decimal("0.50"), key_name=f"test-{uuid.uuid4().hex[:8]}"), s, transport=httpx.MockTransport(handler))
    ctx = CallContext(None, "test", purpose="ci")
    await gw.chat(SPEC, [{"role": "user", "content": "olá"}], ctx, Out)
    await gw.chat(SPEC.model_copy(update={"provider": ProviderPolicy(order=["deepinfra"], only=["deepinfra"])}), [{"role": "user", "content": "olá"}], ctx, Out)
    await gw.embed(ModelRef(model="qwen/qwen3-embedding-8b", dims=1024), ["olá"], ctx)
    for body in bodies:
        assert body["provider"]["zdr"] is True and body["provider"]["data_collection"] == "deny"    # never traded away for routing
    assert "only" not in bodies[0]["provider"] and "order" not in bodies[0]["provider"] and "only" not in bodies[2]["provider"]
    assert bodies[1]["provider"]["only"] == ["deepinfra"] and bodies[1]["provider"]["order"] == ["deepinfra"]    # a real choice still goes through


async def test_total_cap_denies_before_calling(db):
    calls = []
    gw = make(cap="0.0001", calls=calls)
    with pytest.raises(BudgetDenied):
        await gw.chat(SPEC, [{"role": "user", "content": "x" * 3000}], CallContext(None, "test", purpose="ci"), Out)
    assert calls == []                                               # provider never called


async def test_parallel_calls_never_exceed_cap(db):
    calls = []
    gw = make(cap="0.0005", calls=calls)                             # each reservation ~0.0001
    async def one():
        try:
            await gw.chat(SPEC, [{"role": "user", "content": "olá"}], CallContext(None, "test", purpose="ci"), Out)
            return True
        except BudgetDenied:
            return False
    results = await asyncio.gather(*[one() for _ in range(20)])
    usage = await gw.budget.usage()
    total = usage[f"{gw.budget.key_name}:total"]
    assert total["spent"] + total["reserved"] <= Decimal("0.0005")
    assert 0 < sum(results) < 20


async def test_tenant_scope_cap(db):
    gw = make()
    scope = ScopeCap(f"conversation:{uuid.uuid4()}", Decimal("0.00001"), None)
    with pytest.raises(BudgetDenied):
        await gw.chat(SPEC, [{"role": "user", "content": "olá"}], CallContext(None, "t", scopes=[scope]), Out)


async def test_unknown_model_refused(db):
    gw = make()
    with pytest.raises(Exception):
        await gw.chat(NodeModel(primary="openai/unknown-x", prompt_version="v"), [{"role": "user", "content": "a"}],
                      CallContext(None, "t"), Out)


def test_strict_schema_shape():
    s = strict_schema(Out)
    assert s["additionalProperties"] is False and set(s["required"]) == {"answer", "score"}
