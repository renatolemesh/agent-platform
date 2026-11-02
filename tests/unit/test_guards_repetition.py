"""Repetition guard: a paraphrase of a previous explanation is rewritten; a different answer on the same topic is not."""
from agentplatform.core.agent.guards import OutputContext, output_guard
from agentplatform.core.config.schema import ComplianceConfig

PREV = ("Para se cadastrar no consórcio, você escolhe o valor da carta de crédito, que no seu caso é para um veículo de "
        "até 90 mil. Depois, entra em um grupo e começa a pagar parcelas mensais. Todo mês tem assembleia para sorteio ou "
        "lance, e quem for contemplado pode usar a carta para comprar o carro à vista.")
PARAPHRASE = ("Para entrar no consórcio, você escolhe o valor da carta de crédito, no seu caso até 90 mil, e começa a pagar "
              "parcelas mensais. Todo mês tem assembleia com sorteio ou lance para contemplação. Quem for contemplado usa a "
              "carta para comprar o carro à vista.")
DIFFERENT = ("O lance é uma oferta de antecipar parcelas para aumentar a chance de ser contemplado. Existe lance livre, fixo "
             "e embutido, conforme o regulamento do grupo.")


def _rep(text, overlap=0.7):
    ctx = OutputContext(ComplianceConfig(), [PREV, text, "90"], set(), [PREV], 1000, repetition_threshold=0.8,
                        repetition_overlap=overlap, repetition_min_words=8)
    return next(v for v in output_guard(text, ctx) if v.rule_id == "repetition").verdict


def test_paraphrase_is_rewritten_and_new_angle_passes():
    assert _rep(PARAPHRASE) == "rewrite"
    assert _rep(DIFFERENT) == "pass"
    assert _rep("Tudo bem, sem pressa!") == "pass"            # short messages are not compared by words
    assert _rep(PARAPHRASE, overlap=1.01) == "pass"           # measure off -> only the character ratio applies
