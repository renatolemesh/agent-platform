"""What a large reading of real conversations showed the assistant must be ready for. The wording here is invented."""
import re
from datetime import datetime, timezone

import pytest

from agentplatform.core.agent.guards import _affirmative_match, input_guard, normalize
from agentplatform.core.agent.policy import select_guidelines, stops_asking
from agentplatform.core.agent.state import TurnUnderstanding
from agentplatform.core.channels.events import Attachment, CanonicalEvent
from agentplatform.core.config.schema import GuidelinesConfig, InputGuardConfig
from agentplatform.core.orchestration.dm_scope import is_shared_only
from agentplatform.core.packages.contract import load_package

M = load_package("consortium").manifest
GUIDE = {g.id: g.action for g in M.guidelines}


def U(**kw):
    return TurnUnderstanding(**{"intents": [], "questions": [], "slot_updates": [], "objections": [], "sentiment": "neutral", "asks_for_human": False, "out_of_scope": False, "opt_out_intent": 0.0, **kw})


def guide(text, **kw):
    return select_guidelines(M, "educando", U(**kw), normalize(text), 0, GuidelinesConfig())


def forbidden(text):
    n = normalize(text)
    return any(_affirmative_match(normalize(p), n) for p in M.compliance.forbidden_phrases)


# ---------- leaving the chat
@pytest.mark.parametrize("text", ["dá pra fazer uma videochamada?", "pode ser vídeo chamada amanhã", "manda o link do meet", "queria marcar uma reunião", "prefiro falar pessoalmente",
                                  "posso passar na loja de vocês?", "posso te ligar?"])
def test_a_call_or_a_meeting_goes_to_the_seller_like_whatsapp(text):
    r = input_guard(text, InputGuardConfig(), M)
    assert r.action == "handoff" and r.handoff_reason == "explicit_request"


@pytest.mark.parametrize("text", ["agora estou em reunião, falo depois", "eu pessoalmente acho caro", "vi um vídeo seu", "como funciona?"])
def test_mentioning_a_meeting_or_a_video_does_not(text):
    assert input_guard(text, InputGuardConfig(), M).action != "handoff"


# ---------- wanting to start
@pytest.mark.parametrize("text", ["como começo?", "e por onde começo", "como faço pra começar?", "onde eu contrato isso?", "vou querer fazer também", "quero montar a minha",
                                  "quais documentos precisa?"])
def test_asking_how_to_start_is_a_hot_lead(text):
    r = input_guard(text, InputGuardConfig(), M)
    assert r.action == "handoff" and r.handoff_reason == "hot_lead"


@pytest.mark.parametrize("text", ["como funciona?", "quanto fica a parcela?", "quero entender melhor", "o que é contemplação?"])
def test_a_question_is_not(text):
    assert input_guard(text, InputGuardConfig(), M).action != "handoff"


# ---------- what no approved text answers
@pytest.mark.parametrize("text", ["tem o histórico de lances desse grupo?", "me passa as últimas assembleias", "qual a média de lance?", "tenho o nome sujo, dá pra fazer?", "estou negativado",
                                  "vocês trabalham com carta contemplada?", "queria comprar uma carta contemplada"])
def test_facts_of_a_group_or_a_person_go_to_the_seller(text):
    r = input_guard(text, InputGuardConfig(), M)
    assert r.action == "handoff" and r.handoff_reason == "specialist_question"


@pytest.mark.parametrize("text", ["e depois de contemplada a carta, como fica?", "como é a venda da carta contemplada?"])
def test_talking_about_the_contemplated_letter_itself_does_not(text):
    assert input_guard(text, InputGuardConfig(), M).action != "handoff"


def test_who_did_not_come_to_buy_is_told_to_the_seller():
    t = next(t for t in M.handoff_triggers if t.id == "business_contact")
    assert t.intents == ["business_contact"] and "business_contact" in M.intents and "business_contact" in M.intent_labels and not t.locked


# ---------- how it answers
@pytest.mark.parametrize("text", ["sem dinheiro agora", "não tá sobrando nada", "vou pensar e te falo", "só mês que vem", "só consigo pagar dia 5"])
def test_no_money_or_thinking_gets_no_pressure(text):
    assert GUIDE["not_now"] in guide(text) and "Não prometa voltar a escrever" in GUIDE["not_now"]


@pytest.mark.parametrize("text", ["preciso falar com minha esposa", "vou ver com a patroa e te falo", "me manda um resumo pra eu mostrar em casa"])
def test_who_talks_at_home_gets_a_summary_and_no_seller(text):
    picked = guide(text)
    assert GUIDE["talk_at_home"] in picked      # "vou ver" may also bring not_now: neither pushes
    assert "Não ofereça o consultor" in GUIDE["talk_at_home"]
    assert any(normalize(k) in normalize(text) for k in M.offer_again_keywords)      # the offer runs again for the summary


@pytest.mark.parametrize("text", ["estou desempregado", "é a pior fase da minha vida", "perdi o emprego semana passada"])
def test_a_hard_moment_comes_first_and_sells_nothing(text):
    picked = guide(text)
    assert picked[0] == GUIDE["financial_hardship"] and "Não ofereça" in picked[0]


@pytest.mark.parametrize("text", ["sua comissão cai quando?", "quanto você ganha nisso?"])
def test_commission_is_not_discussed(text):
    assert GUIDE["commission_question"] in guide(text)


@pytest.mark.parametrize("text", ["isso é pirâmide?", "qual é a empresa?", "tem contrato?", "é regulamentado?"])
def test_doubts_about_who_is_behind_it(text):
    assert GUIDE["distrust"] in guide(text)


def test_calling_it_an_investment_is_not_confirmed():
    assert GUIDE["resale_interest"] in guide("quero investir nisso") and "não confirme" in GUIDE["resale_interest"]


@pytest.mark.parametrize("text", ["não tenho interesse", "obrigado, mas não é pra mim", "por enquanto não, valeu"])
def test_declining_is_thanked_and_not_told_it_was_removed(text):
    assert GUIDE["declined"] in guide(text) and "Não diga que vai parar" in GUIDE["declined"]


@pytest.mark.parametrize("kw", [{"intents": ["not_interested"]}, {"objections": ["not_now"]}, {"objections": ["will_think"]}, {"objections": ["already_has"]}, {"intents": ["not_now"]}])
def test_someone_saying_no_is_not_asked_the_next_question(kw):
    assert stops_asking(M, U(**kw))


@pytest.mark.parametrize("kw", [{}, {"intents": ["product_question"]}, {"objections": ["expensive"]}, {"objections": ["distrust"]}])
def test_someone_with_a_doubt_still_is(kw):
    assert not stops_asking(M, U(**kw))


# ---------- what must never go out
@pytest.mark.parametrize("text", ["Essa condição vale até meia noite.", "O grupo fecha hoje, aproveita.", "Tenho 5 cotas reservadas.", "A empresa compra sua carta depois.", "Tem comprador garantido.",
                                  "Dá pra fazer uma grana boa nisso.", "É dinheiro no bolso.", "Isso vai mudar sua vida.", "É um investimento seguro.", "Baita negócio!", "Rende mais que CDB.",
                                  "Sem consulta de crédito e sem burocracia."])
def test_the_pressure_and_the_promises_a_person_may_not_repeat_here(text):
    assert forbidden(text)


@pytest.mark.parametrize("text", ["Quem compra a carta é outra pessoa, e o consultor explica como funciona.", "Consórcio é uma compra planejada.", "Não existe prazo garantido.",
                                  "Tranquilo, sem pressa nenhuma!", "A parcela e o crédito são reajustados pelo índice do contrato."])
def test_plain_answers_still_pass(text):
    assert not forbidden(text)


def test_no_down_payment_is_never_said_alone():
    assert re.search(normalize(M.compliance.require_with["sem entrada"]), normalize("sem entrada, você começa na primeira parcela"))


# ---------- the words that open most conversations
def test_a_comment_with_the_word_on_the_video_has_a_template():
    t = next(t for t in M.comment_templates if t.id == "carta")
    assert {"carta", "credito", "eu quero"} <= {normalize(k) for k in t.keywords}
    for text in t.private_variants.values():     # one yes or no question, so a lone "sim" means something
        assert text.count("?") == 1 and " ou " not in text and "assistente" in text


def test_questions_people_ask_arrive_as_drafts_with_no_numbers_of_their_own():
    ids = {f.id for f in M.faqs_seed}
    assert {"regulated", "down_payment", "never_contemplated", "giving_up", "readjustment", "cheaper_good"} <= ids
    assert {"need_the_amount", "pay_ahead", "reduced_installment", "embedded_bid", "how_many_contemplated", "odd_credit_value", "keep_paying", "vs_saving"} <= ids
    assert not any(re.search(r"\d", f.answer) for f in M.faqs_seed if f.id not in {"regulated"})     # numbers come from the account, not here
    for f in M.faqs_seed:
        assert "garant" not in normalize(f.answer).replace("nao existe prazo garantido", "")


# ---------- consortium or saving the money
@pytest.mark.parametrize("text", ["por que eu deveria comprar um consorcio, não vale mais a pena investir no CDB?", "não é melhor deixar o dinheiro rendendo?",
                                  "prefiro juntar o dinheiro e comprar à vista", "consórcio ou poupança?"])
def test_comparing_with_saving_is_answered_ahead_of_resale(text):
    picked = guide(text, intents=["compare_financing"])
    assert picked[0] == GUIDE["vs_saving"]
    assert "acima da orientação de revenda" in GUIDE["vs_saving"] and "Não diga quanto a aplicação rende" in GUIDE["vs_saving"]


def test_the_answer_it_asks_for_passes_the_package_rules():
    faq = next(f for f in M.faqs_seed if f.id == "vs_saving")
    assert not forbidden(faq.answer) and "taxa de administra" in faq.answer
    assert not forbidden("O CDB é outra coisa, é investimento. Guardando sozinho, você só compra depois de juntar tudo.")
    for text in ["No CDB o dinheiro rende mais", "a poupança perde para a inflação", "o rendimento do CDB é menor", "o consórcio é melhor que a poupança"]:
        assert forbidden(text), text                              # comparing what it yields stays forbidden


def test_the_offer_with_the_group_history_passes_the_package_rules():
    from decimal import Decimal

    from agentplatform_packages.consortium import PlanRow, SimulateIn, simulate
    row = PlanRow(plan_id="G", goal="vehicle", credit=Decimal(95000), installment=Decimal(980), avg_contemplation_months=18)
    assert not forbidden(simulate([row], SimulateIn(goal="vehicle"), "d", 1).required_text)


# ---------- a reaction
def _story(text, attachments=()):
    return CanonicalEvent(event_type="StoryReply", channel="instagram", account_external_id="a", contact_external_id="c", idempotency_key="k", provider_ts=datetime.now(timezone.utc),
                          text=text, attachments=list(attachments), story_id="s1")


@pytest.mark.parametrize("text", ["❤️", "🔥🔥", "  ", "", None, "!!!"])
def test_a_heart_on_a_story_opens_nothing(text):
    assert is_shared_only(_story(text))


@pytest.mark.parametrize("text", ["top!!", "como funciona?", "kkk", "1"])
def test_words_on_a_story_still_count(text):
    assert not is_shared_only(_story(text))


def test_a_voice_note_on_a_story_still_counts():
    assert not is_shared_only(_story(None, [Attachment(type="audio", url="https://x")]))
