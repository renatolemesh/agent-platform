"""Nothing to fill in the conversation: with the minimum profile the next step is the person. The wording here is invented."""
import pytest

from agentplatform.core.agent.guards import _affirmative_match, input_guard, normalize
from agentplatform.core.agent.policy import accepted_the_offer, offered_the_person
from agentplatform.core.config.schema import InputGuardConfig
from agentplatform.core.packages.contract import load_package

M = load_package("consortium").manifest


def forbidden(text):
    n = normalize(text)
    return any(_affirmative_match(normalize(p), n) for p in M.compliance.forbidden_phrases)


def test_the_funnel_has_no_form_step():
    assert M.form_sent_stage is None and M.ready_question
    assert "pre_cadastro_enviado" not in {s.name for s in M.stages}
    assert not any("create_lead_form_link" in s.tools for s in M.stages)
    assert M.allowed(M.form_stage, M.seller_stage)


def test_nothing_the_package_says_offers_a_form():
    fixed = [*M.templates.values(), M.ready_question]
    assert not any(forbidden(t) for t in fixed)
    assert not any("pré-cadastro" in g.action for g in M.guidelines)


def test_the_configured_reminder_exists():
    from agentplatform.core.config.registry import load_yaml
    from agentplatform.core.followups.engine import trigger_stages
    from agentplatform.paths import ROOT
    for name in ("dev-consortium", "prod-consortium.example"):
        for cad in load_yaml(ROOT / f"config/tenants/{name}.yaml").followups.cadences:
            stages = trigger_stages(cad.trigger)
            assert stages and set(stages) <= {s.name for s in M.stages}
            assert all(step.template_key in M.templates for step in cad.steps)


@pytest.mark.parametrize("text", ["É só preencher esse formulário.", "Vou te mandar o pré-cadastro.", "Vou te mandar o pre-cadastro.", "Me devolve a ficha.", "Agora é só preencher seus dados."])
def test_a_form_is_never_offered(text):
    assert forbidden(text)


@pytest.mark.parametrize("text", ["Não precisa de formulário, o consultor segue com você.", "O consultor cuida do cadastro com você no sistema oficial.", "Posso chamar o consultor para seguir com você?",
                                  "O pré-cadastro é o consultor que faz com você.", "Caiu a ficha, né?"])
def test_saying_there_is_none_still_passes(text):
    assert not forbidden(text)


@pytest.mark.parametrize("text", ["tem algum formulário pra preencher?", "me manda o formulário", "quero fazer o pré-cadastro", "pode chamar", "chama o consultor", "chama ele aí"])
def test_asking_for_a_form_or_accepting_the_seller_is_a_hot_lead(text):
    r = input_guard(text, InputGuardConfig(), M)
    assert r.action == "handoff" and r.handoff_reason == "hot_lead"


@pytest.mark.parametrize("text", ["quem é o consultor?", "o que é o cadastro de reserva?", "me chamo Ana"])
def test_the_consultant_alone_is_not_accepting(text):
    assert input_guard(text, InputGuardConfig(), M).action != "handoff"


def test_the_offer_is_not_repeated_right_after_it_was_made():
    assert offered_the_person(M, ["A parcela fica em torno disso. Posso chamar o consultor para seguir com você?"])
    assert offered_the_person(M, [M.ready_question]) and offered_the_person(M, [M.templates["seller_offer_reminder"]])


OFFER = ["Com isso já dá pra montar. Posso chamar o consultor pra seguir com você?"]


@pytest.mark.parametrize("text", ["sim", "Sim!", "pode", "pode sim", "bora", "claro", "quero", "ok", "pode ser", "sim pfv", "tá bom", "com certeza", "claro, por favor", "👍"])
def test_a_short_yes_to_the_offer_is_accepting(text):
    assert accepted_the_offer(M, text, OFFER)


@pytest.mark.parametrize("text", ["não", "agora não", "pode?", "sim?", "quero ver outra opção", "sim, mas quanto fica?", "ok obrigado", "simula aí", "como assim?", "quero sim mas depois", "👀", ""])
def test_anything_else_is_not(text):
    assert not accepted_the_offer(M, text, OFFER)


@pytest.mark.parametrize("before", [[], ["Você teria algum valor guardado para ofertar lance?"], [OFFER[0], "A parcela fica nessa faixa."]])
def test_a_yes_to_something_else_is_not(before):
    assert not accepted_the_offer(M, "sim", before)


# The first four are how the model actually worded it on a live run: a yes to any of them used to fall to the classifier.
@pytest.mark.parametrize("offer", [
    "A parcela vai depender do prazo, mas posso chamar o consultor para montar uma simulação certinha pra você. Posso chamar ele?",
    "A parcela depende do prazo que você escolher. Quer que eu chame o consultor para te mostrar as opções e a parcela certinha?",
    "Show! Posso chamar o consultor pra te mostrar as opções e a parcela certinha pra esse valor?",
    "Quer que eu chame o consultor para montar uma simulação certinha pra você?",
    "Quer falar com o consultor?", "Posso pedir para ele falar com você?", "Prefere que eu chame ele?", "Chamo ele pra você?",
    "Quer que ele entre em contato?", "Posso te colocar em contato com ele?", "Posso chamar o consultor? 😊",
])
def test_the_offer_in_the_model_words_is_still_the_offer(offer):
    assert accepted_the_offer(M, "bora", [offer])


@pytest.mark.parametrize("question", [
    "Você teria algum valor guardado para ofertar lance?", "Quer que eu explique como funciona o lance?",
    "Quer que eu explique para você como funciona?", "Posso te explicar com mais detalhes para seguir?",
    "Quer simular outro valor?", "Você já falou com algum consultor?", "Como você se chama?",
    "O consultor vai te chamar. Tudo bem até aqui",
])
def test_other_questions_are_not_the_offer(question):
    assert not accepted_the_offer(M, "sim", [question])
