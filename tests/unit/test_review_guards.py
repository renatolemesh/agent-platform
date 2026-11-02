"""Rules that were taking buyers away from the assistant, and privacy rules that were letting things through (independent review).
Every phrase here is synthetic."""
from pathlib import Path

import pytest

from agentplatform.core.agent import guards
from agentplatform.core.agent.guards import OutputContext, failed, keyword_hit, normalize, output_guard, privacy_gate
from agentplatform.core.config.registry import load_yaml, resolve_with_package
from agentplatform.core.packages.contract import load_package
from agentplatform.core.security.pii import mask

ROOT = Path(__file__).resolve().parents[2]
PKG = load_package("consortium")
CFG = resolve_with_package(load_yaml(ROOT / "config/tenants/dev-consortium.yaml"), PKG.manifest.compliance)


def guard(text: str, sources=()):
    ctx = OutputContext(compliance=CFG.compliance, sources_text=list(sources), allowed_numbers=set(), recent_bot_texts=[], max_bytes=1000)
    return {v.rule_id for v in failed(output_guard(text, ctx))}


@pytest.mark.parametrize("text", [
    "não me manda mais nada", "para", "pode parar", "me deixa em paz", "não me procure mais", "não entre mais em contato",
    "parar", "não quero mais receber mensagens", "Para!", "chega", "parem de me mandar mensagem", "não me chame mais",
])
def test_stop_requests_are_recognized_without_the_model(text):
    assert privacy_gate([text], CFG.compliance).opt_out


@pytest.mark.parametrize("text", [
    "para quando seria?", "quero uma carta para imóvel", "não vou parar de pagar", "pode ser a parcela menor?", "não quero mais caro",
    "chega de aluguel, quero comprar",
])
def test_ordinary_sentences_are_not_stop_requests(text):
    assert not privacy_gate([text], CFG.compliance).opt_out


@pytest.mark.parametrize("text", [
    "E se eu desistir depois, perco o que paguei?", "como funciona a assembleia?", "dá pra pagar por boleto?",
    "sou advogado e queria uma carta de 200 mil", "isso é golpe? tenho medo de golpe", "não quero falar com vendedor, só tirar uma dúvida",
    "quero uma carta de 300 mil", "preciso com urgência",
])
def test_buying_questions_stay_with_the_assistant(text):
    assert guards.input_guard(text, CFG.input_guard, PKG.manifest).action == "continue"


@pytest.mark.parametrize("text, reason", [
    ("quero falar com um vendedor", "explicit_request"), ("não gostei, quero falar com um atendente", "explicit_request"),
    ("vocês são uns golpistas", "complaint_or_abuse"), ("vou processar vocês", "legal"), ("meu advogado vai ligar", "legal"),
    ("cadê meu boleto?", "post_sale"), ("fui contemplado ontem", "post_sale"), ("quero cancelar minha cota", "cancellation"),
    ("pode ser essa opção de 300 mil?", "hot_lead"),
])
def test_what_really_needs_a_person_still_goes_to_one(text, reason):
    r = guards.input_guard(text, CFG.input_guard, PKG.manifest)
    assert (r.action, r.handoff_reason) == ("handoff", reason)


def test_keywords_match_from_the_start_of_a_word():
    assert keyword_hit("rg", normalize("meu rg é")) and not keyword_hit("rg", normalize("preciso com urgência"))
    assert keyword_hit("logo", normalize("quero logo")) and not keyword_hit("logo", normalize("vi no catálogo"))
    assert keyword_hit("taxa", normalize("quais as taxas?"))


@pytest.mark.parametrize("text", [
    "Diferente do financiamento, aqui não tem juros: tem taxa de administração.", "O financiamento cobra juros, o consórcio não.",
    "Você quer uma renda extra?", "O seguro cobre doença grave conforme a apólice.",
])
def test_ordinary_replies_are_not_blocked(text):
    assert not guard(text) - {"one_question"}


@pytest.mark.parametrize("text, rule", [
    ("Vamos fechar o seu financiamento?", "forbidden_phrase"), ("O consórcio é um tipo de financiamento.", "forbidden_phrase"),
    ("Você tem um prazo em mente para o financiamento do seu carro?", "forbidden_phrase"),
    ("Me passa a sua renda?", "never_request"), ("Qual é o seu salário?", "never_request"),
    ("Seu CPF 529.982.247-25 está certo?", "pii_in_output"), ("A conta 12345-6 está ok.", "pii_in_output"),
])
def test_what_must_never_go_out_is_still_blocked(text, rule):
    assert rule in guard(text)


def test_a_negated_statement_about_the_product_passes():
    assert "forbidden_phrase" not in guard("O consórcio não é um financiamento.")


@pytest.mark.parametrize("text, placeholder", [
    ("tenho leucemia", "[SAUDE]"), ("estou grávida", "[SAUDE]"), ("minha identidade é 123456789", "[RG]"), ("rg 12.345.678-9", "[RG]"),
    ("ag 1234 conta 56789-0", "[BANCO]"), ("moro na rua das flores 100", "[ENDERECO]"), ("cep 01001-000", "[ENDERECO]"),
    ("meu cpf é 529.982.247-25", "[CPF]"),
])
def test_personal_data_is_masked(text, placeholder):
    assert placeholder in mask(text).text


@pytest.mark.parametrize("text", [
    "quero uma carta de 300 mil", "parcela de 1.500", "a conta de luz está alta", "em qual rua fica o escritório?", "prazo de 200 meses",
])
def test_ordinary_sentences_are_left_alone(text):
    assert mask(text).text == text


def test_a_placeholder_can_never_go_out():
    assert "pii_placeholder" in guard("Anotei [BANCO] e [ENDERECO] aqui.")
