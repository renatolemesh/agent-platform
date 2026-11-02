"""Fictitious auto-insurance package: proves the core is segment-agnostic (plan 13 §3). Not for production."""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from agentplatform.core.config.schema import ComplianceConfig
from agentplatform.core.packages.contract import (
    FlowStep,
    GuidelineSpec,
    HandoffTriggerSpec,
    PackageManifest,
    SlotSpec,
    StageSpec,
    ToolSpec,
    TransitionSpec,
)

MANIFEST = PackageManifest(
    name="auto_insurance", version="0.0.1",
    persona_template="Você é o assistente virtual de {seller_name}, corretor(a) de seguros.",
    slots=[SlotSpec(name="vehicle_model", type="text", question="Qual é o modelo do carro?"),
           SlotSpec(name="vehicle_year", type="int", question="De que ano é?"),
           SlotSpec(name="zip_code", type="text", question="Qual o CEP onde ele dorme?"),
           SlotSpec(name="main_driver_age_band", type="enum", values=["18_25", "26_40", "41_60", "60_plus"]),
           SlotSpec(name="current_insurer", type="text")],
    stages=[StageSpec(name=n, goal=n) for n in ("new", "engaged", "quoting", "quote_ready", "with_broker")]
    + [StageSpec(name="won", goal="won", is_terminal=True, marked_by_seller_only=True),
       StageSpec(name="lost", goal="lost", is_terminal=True), StageSpec(name="opt_out", goal="opt_out", is_terminal=True)],
    transitions=[TransitionSpec(**{"from": a, "to": b}) for a, b in
                 [("new", "engaged"), ("new", "quoting"), ("engaged", "quoting"), ("quoting", "quote_ready"),
                  ("quote_ready", "with_broker"), ("with_broker", "won"), ("with_broker", "lost"), ("lost", "engaged")]],
    initial_stage="new", opt_out_stage="opt_out", form_stage="quote_ready", seller_stage="with_broker", won_stage="won",
    min_profile_for_proposal=[["vehicle_model"], ["vehicle_year"], ["zip_code"]],
    flow=[FlowStep(id="vehicle", collect=["vehicle_model", "vehicle_year"]), FlowStep(id="where", collect=["zip_code"])],
    intents=["greeting", "product_question", "price_question", "wants_human", "opt_out", "out_of_scope", "provide_info",
             "asks_if_bot", "other"],
    guidelines=[GuidelineSpec(id="no_full_coverage_promise", when_intents=["product_question"],
                              action="Nunca prometa cobertura total; cite que as condições estão na apólice.")],
    handoff_triggers=[HandoffTriggerSpec(id="claim", keywords=["sinistro", "batida"], reason="claim")],
    compliance=ComplianceConfig(forbidden_phrases=["cobertura total garantida"], never_request=["cpf", "driver_license_number"]),
    lead_form_fields=["first_name", "contact", "vehicle_model", "vehicle_year", "zip_code"],
)


class EstimateIn(BaseModel):
    vehicle_year: int
    main_driver_age_band: str = "26_40"


class EstimateOut(BaseModel):
    annual_premium: Decimal
    table_version: int = 1


def estimate_premium(req: EstimateIn) -> EstimateOut:
    base = Decimal("2400") + Decimal(max(0, 2026 - req.vehicle_year)) * Decimal("-50")
    factor = {"18_25": Decimal("1.6"), "26_40": Decimal("1.0"), "41_60": Decimal("0.9"), "60_plus": Decimal("1.2")}
    return EstimateOut(annual_premium=(max(base, Decimal("900")) * factor.get(req.main_driver_age_band, Decimal(1))).quantize(Decimal("1")))


class _Pkg:
    manifest = MANIFEST

    def tools(self) -> list[ToolSpec]:
        async def _fn(ctx, args: EstimateIn) -> EstimateOut:
            return estimate_premium(args)
        return [ToolSpec(name="estimate_premium", description="Estimativa determinística de prêmio (tabela fictícia).",
                         input_model=EstimateIn, output_model=EstimateOut, allowed_stages=["quoting", "quote_ready"],
                         effect_class="read", fn=_fn)]


package = _Pkg()
