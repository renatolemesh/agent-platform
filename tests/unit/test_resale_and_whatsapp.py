"""Customers who want a letter to sell it later, and customers who move to WhatsApp. The wording here is invented."""
from decimal import Decimal

import pytest

from agentplatform.core.agent.guards import input_guard, normalize
from agentplatform.core.agent.policy import apply_slots, select_guidelines
from agentplatform.core.agent.state import SlotUpdate, TurnUnderstanding
from agentplatform.core.config.schema import FunnelConfig, GuidelinesConfig, InputGuardConfig
from agentplatform.core.packages.contract import load_package
from agentplatform_packages.consortium import PlanRow, SimulateIn, simulate

M = load_package("consortium").manifest


def U(**kw):
    return TurnUnderstanding(**{"intents": [], "questions": [], "slot_updates": [], "objections": [], "sentiment": "neutral", "asks_for_human": False, "out_of_scope": False, "opt_out_intent": 0.0, **kw})


@pytest.mark.parametrize("text", ["me chama no whatsapp amanhã", "pode ser pelo whats?", "vamos pelo zap", "pode me ligar à tarde?", "esse é meu número [TELEFONE]"])
def test_moving_to_whatsapp_or_sending_a_number_goes_to_the_seller(text):
    r = input_guard(text, InputGuardConfig(), M)
    assert r.action == "handoff" and r.handoff_reason == "explicit_request"


def test_it_cannot_be_turned_off():
    assert input_guard("me chama no whatsapp", InputGuardConfig(), M, ["explicit_request"]).handoff_reason == "explicit_request"


@pytest.mark.parametrize("text", ["quero saber o valor da parcela", "qual o telefone de vocês?", "como funciona?"])
def test_the_rest_of_the_conversation_stays_with_the_assistant(text):
    assert input_guard(text, InputGuardConfig(), M).action == "continue"


@pytest.mark.parametrize("said", ["quero vender a carta depois", "é pra alavancagem"])
def test_the_goal_is_understood(said):
    evidence = "vender a carta" if "vender" in said else "alavancagem"
    u = U(slot_updates=[SlotUpdate(slot="goal", value="resale", confidence=0.95, evidence_quote=evidence, is_correction=False)])
    assert apply_slots(M, {}, u, said, FunnelConfig(), None).accepted["goal"]["value"] == "resale"


def test_the_guideline_comes_with_the_words_and_never_promises():
    picked = select_guidelines(M, "qualificando", U(), normalize("quero vender a carta depois"), 0, GuidelinesConfig())
    assert any("nada disso é garantido" in g for g in picked)


def _row(plan, goal):
    return PlanRow(plan_id=plan, goal=goal, credit=Decimal(100000), term_months=60, installment=Decimal(1750))


def test_a_letter_to_sell_later_is_quoted_from_any_plan():
    out = simulate([_row("A", "vehicle"), _row("B", "real_estate")], SimulateIn(goal="resale", credit_amount=Decimal(100000)), "d", 1)
    assert {o.plan_id for o in out.options} == {"A", "B"}


def test_unless_the_table_says_which():
    out = simulate([_row("A", "vehicle"), _row("C", "resale")], SimulateIn(goal="resale", credit_amount=Decimal(100000)), "d", 1)
    assert [o.plan_id for o in out.options] == ["C"]


def test_every_other_goal_stays_as_it_was():
    out = simulate([_row("A", "vehicle"), _row("C", "resale")], SimulateIn(goal="vehicle", credit_amount=Decimal(100000)), "d", 1)
    assert [o.plan_id for o in out.options] == ["A"]
