from agentplatform.core.agent.policy import apply_slots, decide_stage, handoff_reason, next_question, select_guidelines
from agentplatform.core.agent.state import SlotUpdate, StageProposal, TurnUnderstanding
from agentplatform.core.config.schema import FunnelConfig, GuidelinesConfig
from agentplatform.core.packages.contract import load_package

M = load_package("consortium").manifest
F = FunnelConfig()


def U(**kw):
    base = dict(intents=[], questions=[], slot_updates=[], objections=[], sentiment="neutral", asks_for_human=False,
                out_of_scope=False, opt_out_intent=0.0)
    base.update(kw)
    return TurnUnderstanding(**base)


def test_slots_need_literal_evidence_and_valid_values():
    u = U(slot_updates=[
        SlotUpdate(slot="credit_amount", value="80 mil", evidence_quote="carta de 80 mil", confidence=0.9, is_correction=False),
        SlotUpdate(slot="goal", value="vehicle", evidence_quote="quero um carro", confidence=0.9, is_correction=False),
        SlotUpdate(slot="term_months", value="60", evidence_quote="em 5 anos inventado", confidence=0.9, is_correction=False),
        SlotUpdate(slot="cpf", value="x", evidence_quote="x", confidence=1, is_correction=False),
        SlotUpdate(slot="goal", value="spaceship", evidence_quote="quero um carro", confidence=0.9, is_correction=False),
    ])
    d = apply_slots(M, {}, u, "Oi, quero um carro, uma carta de 80 mil", F, "m1")
    assert d.accepted["credit_amount"]["value"] == "80000"
    assert d.accepted["goal"]["value"] == "vehicle"
    reasons = dict(d.rejected)
    assert reasons["term_months"] == "evidence_not_in_turn" and reasons["cpf"] == "unknown_slot"


def test_stage_only_allowed_transitions():
    assert not decide_stage(M, "novo", StageProposal(stage="cliente", confidence=1, evidence=["x"]), {}, F).applied
    assert not decide_stage(M, "novo", StageProposal(stage="com_vendedor", confidence=1, evidence=["x"]), {}, F).applied
    d = decide_stage(M, "novo", StageProposal(stage="engajado", confidence=0.9, evidence=["oi"]), {}, F)
    assert d.applied and d.to == "engajado"
    assert not decide_stage(M, "novo", StageProposal(stage="engajado", confidence=0.4, evidence=["oi"]), {}, F).applied


def test_min_profile_moves_to_ready():
    prof = {"goal": {"value": "vehicle"}, "credit_amount": {"value": "80000"}}      # no term: the table has it
    d = decide_stage(M, "qualificando", StageProposal(stage="qualificando", confidence=0.9, evidence=["x"]), prof, F)
    assert d.applied and d.to == "pronto_para_proposta"


def test_next_question_follows_flow():
    assert "imóvel" in next_question(M, {})
    assert "valor" in next_question(M, {"goal": {"value": "vehicle"}})
    assert "lance" in next_question(M, {"goal": {"value": "vehicle"}, "monthly_budget": {"value": "900"}})   # never the term


def test_guidelines_and_handoff():
    g = select_guidelines(M, "educando", U(intents=["bid_question"]), "como funciona o lance?", 0, GuidelinesConfig())
    assert any("lance" in x for x in g)
    assert not any("entry_approx" in x for x in select_guidelines(M, "qualificando", U(), "100k", 0, GuidelinesConfig()))
    g = select_guidelines(M, "qualificando", U(), "100k", 0, GuidelinesConfig(), tools=["simulate_consortium"])
    assert "entry_approx" in g[0]                               # the table result pulls the customer, first
    assert handoff_reason(M, U(intents=["simulation_request"]), False, "qualificando", 0) == "simulation_request"
    assert handoff_reason(M, U(intents=["simulation_request"]), True, "qualificando", 0) is None
    assert handoff_reason(M, U(intents=["wants_human"]), False, "novo", 0) == "explicit_request"
    assert handoff_reason(M, U(asks_for_human=True), False, "novo", 0) is None    # LLM flag alone is not enough


def test_product_words_alone_are_not_goal_evidence():
    """"Uma carta" names the product, not what the customer will buy with it: no goal (it was read as other_movable)."""
    def goal(quote):
        u = U(slot_updates=[SlotUpdate(slot="goal", value="other_movable", evidence_quote=quote, confidence=0.9,
                                       is_correction=False)])
        return apply_slots(M, {}, u, quote, F, "m1")
    assert dict(goal("Uma carta").rejected)["goal"] == "not_evidence"
    assert dict(goal("quero uma carta de crédito de 100k").rejected)["goal"] == "not_evidence"
    assert goal("uma carta pra comprar um barco").accepted["goal"]["value"] == "other_movable"
    assert "goal" in goal("um apê").accepted                                             # short words still count


def test_flow_moves_on_from_a_question_just_asked():
    from agentplatform.core.agent.policy import pending_questions, recently_asked
    qs = pending_questions(M, {})
    assert "imóvel" in qs[0] and "valor" in qs[1]                     # goal, then amount, then the optional ones
    asked = "Oi! Você tá pensando em comprar o quê: imóvel, carro, moto, caminhão ou outra coisa?"
    assert next(q for q in qs if not recently_asked(q, [asked])) == qs[1]
    amount = {"credit_amount": {"value": "100000"}}
    assert pending_questions(M, amount, optional=False) == [qs[0]]      # goal still missing: no side question (lance)
