"""Scripted gateway for tests, sandbox replay and cassettes (no network, no spend)."""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Callable

from agentplatform.core.models.gateway import BudgetDenied, CallContext, ChatResult, ModelCallError


class ScriptedGateway:
    """`script[node]` is a list of dicts/exceptions consumed in order, or a callable(messages) -> dict."""

    def __init__(self, script: dict[str, list | Callable]):
        self.script = script
        self.calls: list[tuple[str, list[dict]]] = []

    async def chat(self, spec, messages, ctx: CallContext, output_model=None) -> ChatResult:
        self.calls.append((ctx.node, messages))
        item = self.script.get(ctx.node)
        if callable(item):
            data = item(messages)
        elif item:
            data = item.pop(0)
        else:
            raise ModelCallError(f"no scripted response for {ctx.node}")
        if callable(data):
            data = data(messages)
        if isinstance(data, Exception):
            raise data
        content = json.dumps(data, ensure_ascii=False)
        parsed = output_model.model_validate_json(content) if output_model else None
        return ChatResult(content, parsed, spec.primary, 100, 20, Decimal("0.00001"), 5)

    async def embed(self, spec, texts, ctx):
        raise BudgetDenied("scripted:no-embeddings")

    async def transcribe(self, spec, audio, filename, seconds, ctx: CallContext, language: str = "pt") -> str:
        self.calls.append((ctx.node, [{"bytes": len(audio), "seconds": seconds}]))
        item = self.script.get(ctx.node)
        data = item(audio) if callable(item) else (item.pop(0) if item else None)
        if data is None:
            raise ModelCallError(f"no scripted response for {ctx.node}")
        if isinstance(data, Exception):
            raise data
        return data


def analysis(intents=(), slots=(), stage="novo", conf=0.9, evidence=("x",), human=False, out_of_scope=False,
             sentiment="neutral", questions=(), opt_out=0.0):
    return {"understanding": {"intents": list(intents), "questions": list(questions),
                              "slot_updates": [dict(slot=s, value=v, evidence_quote=q, confidence=0.9, is_correction=False)
                                               for s, v, q in slots],
                              "objections": [], "sentiment": sentiment, "asks_for_human": human, "out_of_scope": out_of_scope,
                              "opt_out_intent": opt_out},
            "stage_proposal": {"stage": stage, "confidence": conf, "evidence": list(evidence)}}


def draft(text, goal="qualificar", nq=""):
    return {"plan": {"goal": goal, "next_question": nq}, "text": text, "cited_chunk_ids": []}
