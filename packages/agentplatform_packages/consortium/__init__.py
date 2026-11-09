"""Consortium segment package. Domain tools are deterministic: numbers never come from the LLM (principle 6)."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from agentplatform.core.packages.contract import DatasetSpec, PackageManifest, ToolSpec

MANIFEST = PackageManifest.model_validate(yaml.safe_load((Path(__file__).parent / "manifest.yaml").read_text(encoding="utf-8")))

Goal = Literal["real_estate", "vehicle", "heavy_vehicle", "services", "other_movable", "resale"]


GOAL_LABELS = next(s for s in MANIFEST.slots if s.name == "goal").value_labels


class PlanRow(BaseModel):
    """Row of the official plans table (dataset 'plans'), maintained by the tenant in the console."""
    plan_id: str = Field(title="Código", min_length=1, max_length=60,
                         description="Identificador único do plano, ex.: IMOVEL-300-200")
    name: str | None = Field(None, title="Nome do plano", max_length=120, description="Opcional, ex.: Imóvel 200 meses")
    goal: Goal = Field(title="Objetivo", json_schema_extra={"choices": GOAL_LABELS})
    group: str | None = Field(None, title="Grupo", max_length=40, description="Opcional")
    credit: Decimal = Field(title="Crédito (R$)", gt=0, description="Valor da carta de crédito")
    term_months: int | None = Field(None, title="Prazo (meses)", gt=0, le=420,
                                    description="Obrigatório se não houver parcela oficial")
    admin_fee_pct: Decimal | None = Field(None, title="Taxa de administração total (%)", ge=0, le=100,
                                          description="Percentual sobre o crédito no prazo todo, ex.: 15. Obrigatória se não houver parcela oficial")
    reserve_fund_pct: Decimal = Field(Decimal("0"), title="Fundo de reserva total (%)", ge=0, le=100,
                                      description="Percentual sobre o crédito no prazo todo; 0 se não houver")
    insurance_monthly_pct: Decimal = Field(Decimal("0"), title="Seguro mensal (%)", ge=0, le=5,
                                           description="Percentual ao mês sobre o crédito, informado à parte")
    installment: Decimal | None = Field(None, title="Parcela oficial (R$)", gt=0,
                                        description="Se a tabela da administradora já traz a parcela, ela é usada no lugar do cálculo")
    reduced_installment: Decimal | None = Field(None, title="Parcela reduzida até a contemplação (R$)", gt=0,
                                                description="Opcional: parcela especial/meia parcela paga até ser contemplado")
    adjustment_index: str | None = Field(None, title="Índice de reajuste", max_length=30, description="ex.: INCC, IPCA")
    avg_contemplation_months: int | None = Field(None, title="Média de contemplação do grupo (meses)", gt=0, le=420,
                                                 description="Opcional: em quantos meses, em média, as cotas desse grupo têm sido contempladas, "
                                                             "pelo relatório das assembleias. O assistente cita sempre com o aviso de que é histórico")

    @model_validator(mode="after")
    def _priced(self) -> "PlanRow":
        if self.installment is None and (self.term_months is None or self.admin_fee_pct is None):
            raise ValueError("informe a parcela oficial ou o prazo e a taxa de administração")
        if self.reduced_installment is not None and self.installment is not None and self.reduced_installment >= self.installment:
            raise ValueError("a parcela reduzida deve ser menor que a parcela oficial")
        return self


class SimulateIn(BaseModel):
    goal: Goal | None = None                     # unknown goal: every goal (the cheapest entry first)
    credit_amount: Decimal | None = None
    monthly_budget: Decimal | None = None
    term_months: int | None = None


class PlanOption(BaseModel):
    plan_id: str
    name: str | None = None
    goal: str                                    # display label (Imóvel, Carro ou moto...)
    group: str | None = None
    credit: Decimal
    term_months: int | None = None
    installment: Decimal                         # after contemplation (full installment)
    reduced_installment: Decimal | None = None   # until contemplation, when the plan has one
    admin_fee_pct: Decimal | None = None
    reserve_fund_pct: Decimal
    insurance_monthly: Decimal
    adjustment_index: str | None = None
    entry_installment: Decimal                   # what the customer pays from the start (reduced if the plan has one)
    avg_contemplation_months: int | None = None  # the group's history, never a promise: it always goes with history_not_guarantee


class SimulateOut(BaseModel):
    options: list[PlanOption]
    dataset_id: str
    dataset_version: int
    disclaimer_key: str = "simulation_estimate"
    disclaimer_keys: list[str] = []              # more official texts its numbers need (the group's history)
    required_text: str | None = None             # the offer, written here: the model may not reword it
    closing_question: str | None = None


def installment(row: PlanRow) -> tuple[Decimal, Decimal]:
    """Monthly installment = the official value when the table has it, else credit x (1 + admin fee + reserve fund) / term;
    insurance shown separately."""
    base = row.installment or row.credit * (Decimal(1) + row.admin_fee_pct / 100 + row.reserve_fund_pct / 100) / row.term_months
    # (the validator guarantees installment, or term + admin fee)
    ins = row.credit * row.insurance_monthly_pct / 100
    q = Decimal("0.01")
    return base.quantize(q, ROUND_HALF_UP), ins.quantize(q, ROUND_HALF_UP)


def offer_text(o: PlanOption, *, name_goal: bool) -> str:
    """An estimate, as the seller would say it: the credit and the lowest installment its groups start from, exactly as in the table.
    A reduced installment always says it is the one until contemplation; the rest of the plan is the seller's to explain."""
    t = MANIFEST.templates
    history = t["plan_offer_history"].format(months=o.avg_contemplation_months,
                                             note=MANIFEST.compliance.required_disclosures["history_not_guarantee"]) \
        if o.avg_contemplation_months else ""
    installments = t["plan_offer_reduced"].format(reduced=_brl(o.reduced_installment), full=_brl(o.installment)) if o.reduced_installment \
        else _brl(o.installment)
    return t["plan_offer"].format(
        credit=_brl(o.credit), installments=installments,
        goal=t["plan_offer_goal"].format(goal=o.goal.lower()) if name_goal else "",
        term=t["plan_offer_term"].format(months=o.term_months) if o.term_months else "",
        insurance=t["plan_offer_insurance"].format(value=_brl(o.insurance_monthly)) if o.insurance_monthly else "") + history


def simulate(rows: list[PlanRow], req: SimulateIn, dataset_id: str, version: int, limit: int = 3) -> SimulateOut:
    wanted = req.goal
    if wanted == "resale" and not any(r.goal == "resale" for r in rows):
        wanted = None      # a letter to sell later can come from any plan, unless the table says which
    cands = [r for r in rows if (wanted is None or r.goal == wanted) and
             (req.term_months is None or r.term_months is None or abs(r.term_months - req.term_months) <= 24)]
    scored = []
    for r in cands:
        inst, ins = installment(r)
        entry = r.reduced_installment or inst      # what the customer pays from the start
        if req.credit_amount is not None:
            score = abs(r.credit - req.credit_amount)
        elif req.monthly_budget is not None:
            if entry > req.monthly_budget:
                continue
            score = req.monthly_budget - entry
        else:
            score = Decimal(0)
        scored.append(((score, entry), PlanOption(
            plan_id=r.plan_id, name=r.name, goal=GOAL_LABELS.get(r.goal, r.goal), group=r.group, credit=r.credit,
            term_months=r.term_months, installment=inst, reduced_installment=r.reduced_installment,
            admin_fee_pct=r.admin_fee_pct, reserve_fund_pct=r.reserve_fund_pct, insurance_monthly=ins,
            adjustment_index=r.adjustment_index, entry_installment=entry, avg_contemplation_months=r.avg_contemplation_months)))
    scored.sort(key=lambda x: x[0])               # closest credit (or budget), then the cheapest entry
    options = [o for _, o in scored[:limit]]
    # a letter to sell later is not for the plan's own goal
    # "from": the lowest entry among the options with the credit closest to what was asked
    lowest = min((o for o in options if o.credit == options[0].credit), key=lambda o: o.entry_installment) if options else None
    offer = offer_text(lowest, name_goal=req.goal != "resale") if lowest else None
    return SimulateOut(options=options, dataset_id=dataset_id, dataset_version=version, required_text=offer,
                       disclaimer_keys=["history_not_guarantee"] if any(o.avg_contemplation_months for o in options) else [],
                       closing_question=MANIFEST.templates["plan_offer_question"] if options else None)


class GroupHistoryIn(BaseModel):
    group: str


class GroupHistoryOut(BaseModel):
    group: str
    period: str
    observations: int
    bid_contemplations: int
    draw_contemplations: int
    disclaimer_key: str = "history_not_guarantee"


def _brl(v: Decimal) -> str:
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _plan_preview(row: PlanRow) -> dict[str, str]:
    inst, ins = installment(row)
    return {"Parcela estimada": _brl(inst) + (" (oficial)" if row.installment else ""),
            "Até a contemplação": _brl(row.reduced_installment) if row.reduced_installment else "—",
            "Seguro/mês": _brl(ins) if ins else "—"}


PLANS = DatasetSpec(
    name="plans", label="Tabela de planos", row_model=PlanRow, key="plan_id",
    feature_flag="consortium.simulation_enabled",
    description="Planos em venda: crédito, prazo e taxas. É daqui que saem os valores de parcela que o assistente informa.",
    used_for="Assim que o cliente diz o valor do bem (ou a parcela que cabe no bolso), o assistente busca aqui a opção "
             "mais próxima e já mostra o crédito, o prazo e as parcelas, com os valores exatos da tabela, para puxar o "
             "cliente ao próximo passo; se o objetivo ainda não foi dito, usa a opção de entrada mais baixa. Ele sempre "
             "avisa que são valores estimados. Sem tabela ativa, ou com o uso desligado, não informa valores e, se o "
             "cliente pedir a simulação, passa a conversa para o vendedor.",
    example=[
        {"plan_id": "IMOVEL-300-200", "name": "Imóvel 200 meses", "goal": "real_estate", "credit": "300000",
         "term_months": "200", "admin_fee_pct": "20", "reserve_fund_pct": "2", "insurance_monthly_pct": "0.05",
         "adjustment_index": "INCC"},
        {"plan_id": "AUTO-80-60", "name": "Auto 60 meses", "goal": "vehicle", "credit": "80000", "term_months": "60",
         "admin_fee_pct": "14", "reserve_fund_pct": "2", "adjustment_index": "IPCA"},
        {"plan_id": "IMOVEL-200-ESP", "name": "Imóvel parcela especial", "goal": "real_estate", "credit": "200000",
         "installment": "1100", "reduced_installment": "770"},
    ],
    preview=_plan_preview)


class _Pkg:
    manifest = MANIFEST

    def datasets(self) -> list[DatasetSpec]:
        return [PLANS]

    def tools(self) -> list[ToolSpec]:
        async def _simulate(ctx, args: SimulateIn) -> SimulateOut:
            rows, ds_id, ver = await ctx.load_dataset("plans", PlanRow)
            return simulate(rows, args, ds_id, ver)

        async def _group_history(ctx, args: GroupHistoryIn) -> GroupHistoryOut:
            rows, _, _ = await ctx.load_dataset("group_results", dict)
            mine = [r for r in rows if str(r.get("group")) == args.group]
            dates = sorted({r["date"] for r in mine}) or ["-"]
            return GroupHistoryOut(group=args.group, period=f"{dates[0]}..{dates[-1]}", observations=len(mine),
                                   bid_contemplations=sum(1 for r in mine if str(r.get("type", "")).startswith("Lance")),
                                   draw_contemplations=sum(1 for r in mine if r.get("type") == "Sorteio"))

        return [
            ToolSpec(name="simulate_consortium", description="Simula opções a partir da tabela oficial de planos.",
                     input_model=SimulateIn, output_model=SimulateOut, allowed_stages=["qualificando", "pronto_para_proposta", "objecao"],
                     effect_class="read", feature_flag="consortium.simulation_enabled", fn=_simulate,
                     proactive_when=[["credit_amount", "monthly_budget"]]),
            ToolSpec(name="group_history", description="Agregados históricos de assembleias de um grupo.",
                     input_model=GroupHistoryIn, output_model=GroupHistoryOut, allowed_stages=["educando", "qualificando", "pronto_para_proposta"],
                     effect_class="read", feature_flag="consortium.group_history_enabled", fn=_group_history),
        ]


package = _Pkg()
