"""Typed contracts of the turn (plan 04 §2-§3). LLM outputs are validated against these on our side."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SlotUpdate(BaseModel):
    slot: str
    value: str
    evidence_quote: str
    confidence: float = Field(ge=0, le=1)
    is_correction: bool


class TurnUnderstanding(BaseModel):
    intents: list[str]
    questions: list[str]
    slot_updates: list[SlotUpdate]
    objections: list[str]
    sentiment: Literal["positive", "neutral", "negative", "angry"]
    asks_for_human: bool
    out_of_scope: bool
    opt_out_intent: float = Field(ge=0, le=1)


class StageProposal(BaseModel):
    stage: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[str]


class AnalyzeOutput(BaseModel):
    understanding: TurnUnderstanding
    stage_proposal: StageProposal


class DraftPlan(BaseModel):
    goal: str
    next_question: str


class Draft(BaseModel):
    plan: DraftPlan
    text: str
    cited_chunk_ids: list[str]


class JudgeVerdict(BaseModel):
    violations: list[str]
    explanation: str


class GuardVerdict(BaseModel):
    guard: str
    rule_id: str
    verdict: Literal["pass", "block", "rewrite", "handoff"]
    detail: str = ""


class RetrievedChunk(BaseModel):
    id: str
    text: str
    score: float
    visibility: Literal["citable", "guidance_only"]
    document: str


class RetrievalResult(BaseModel):
    faq_answer: str | None = None
    faq_id: str | None = None
    chunks: list[RetrievedChunk] = []
    no_basis: bool = False
