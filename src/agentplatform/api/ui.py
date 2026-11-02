"""Presentation helpers for the console templates: human labels for internal states, badge tones, formatting and a
small inline icon set (no external assets; the CSP only allows our own origin plus the htmx CDN)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from markupsafe import Markup

OWNER = {"BOT": ("Assistente", "accent"), "AWAITING_HUMAN": ("Aguardando vendedor", "warn"),
         "HUMAN": ("Com vendedor", "ok"), "PAUSED": ("Pausada", "muted"), "CLOSED": ("Encerrada", "muted")}
DIRECTION = {"in": "Cliente", "out_bot": "Assistente", "out_human": "Vendedor", "out_system": "Sistema"}
MESSAGE_KIND = {"comment": "Comentário", "comment_reply": "Resposta no comentário", "private_reply": "Direct automático"}
STATUS = {
    # config versions
    "active": ("Ativa", "ok"), "draft": ("Rascunho", "info"), "canary": ("Canário", "warn"),
    "superseded": ("Substituída", "muted"), "rolled_back": ("Revertida", "muted"),
    # turns
    "committed": ("Concluído", "ok"), "running": ("Em andamento", "info"), "cancelled": ("Cancelado", "muted"),
    "failed": ("Falhou", "bad"), "handoff": ("Handoff", "warn"),
    # tokens
    "valid": ("Válido", "ok"), "expiring": ("Expirando", "warn"), "invalid": ("Inválido", "bad"), "revoked": ("Revogado", "bad"),
    # campaigns
    "scheduled": ("Agendada", "info"), "paused_auto": ("Pausada automaticamente", "bad"), "paused": ("Pausada", "muted"),
    "completed": ("Concluída", "ok"),
    # documents / imports
    "indexed": ("Indexado", "ok"), "in_review": ("Em revisão", "warn"), "blocked_pii": ("Bloqueado (dados pessoais)", "bad"),
    "do_not_use": ("Não usar", "muted"), "approved": ("Aprovado", "ok"), "retired": ("Retirada", "muted"), "pending": ("Pendente", "info"), "rolled_back_batch": ("Desfeito", "muted"),
    # tenants
    "offboarded": ("Desligado", "muted"),
}
ENTRYPOINT = {"dm": "Mensagem direta", "story_reply": "Resposta a story", "story_mention": "Menção em story",
              "ad_referral": "Anúncio", "icebreaker": "Pergunta rápida", "comment": "Comentário", "campaign": "Campanha",
              "import": "Importação", "webchat": "Chat do site"}
REASON = {"knowledge_gap": "Pergunta sem resposta aprovada", "opt_out": "Pediu para parar", "opted_out_returned": "Pediu para parar e voltou a escrever",
          "technical_failure": "Falha técnica", "conversation_budget": "Teto desta conversa",
          "human_unanswered": "Cliente sem resposta",
          "explicit_request": "Pediu um vendedor", "hot_lead": "Lead quente", "frustration": "Cliente irritado",
          "no_progress": "Conversa sem avanço", "guard_blocked": "Resposta bloqueada pela revisão", "media_review": "Mídia para revisar",
          "complaint_or_abuse": "Reclamação", "legal": "Assunto jurídico", "requires_sensitive_data": "Exige dado sensível",
          "simulation_request": "Pediu simulação", "post_sale": "Pós-venda", "cancellation": "Cancelamento",
          "out_of_scope": "Fora do escopo", "budget_exhausted": "Orçamento de IA esgotado"}
ROLE = {"platform_admin": "Administrador da plataforma", "tenant_owner": "Responsável", "seller": "Vendedor",
        "reviewer": "Revisor"}
_RETRY_SAME_BROWSER = ("Este retorno do Facebook não corresponde a uma conexão aberta neste navegador (link antigo, já usado "
                       "ou aberto em outro navegador). Clique em Reconectar com o Facebook e conclua neste mesmo navegador.")
_FACEBOOK_FAILED = "O Facebook não confirmou a autorização. Tente reconectar em alguns minutos."
OAUTH_ERROR = {
    "select_one_page": "Mais de uma Página com Instagram foi autorizada. Clique em Reconectar com o Facebook e, na tela "
                       "do Facebook, marque só a Página do Instagram que o assistente vai atender.",
    "page_not_granted": "A Página deste Instagram não foi autorizada. Clique em Reconectar com o Facebook e, na tela do "
                        "Facebook, marque a Página ligada a este Instagram.",
    "account_in_use": "Este Instagram já está conectado em outra conta da plataforma. Desconecte de lá antes de conectar aqui.",
    "denied_by_user": "A autorização foi cancelada na tela do Facebook. Clique em Reconectar com o Facebook e confirme as "
                      "permissões.",
    "page_token_invalid": "O Facebook devolveu um acesso inválido para a Página. Tente de novo; se repetir, confira se a "
                          "sua conta é administradora da Página.",
    "state_expired": "A tela do Facebook ficou aberta tempo demais e o pedido expirou. Clique em Reconectar com o Facebook "
                     "de novo.",
    "code_exchange_failed": _FACEBOOK_FAILED, "long_lived_exchange_failed": _FACEBOOK_FAILED,
    "state_missing": _RETRY_SAME_BROWSER, "state_bad_signature": _RETRY_SAME_BROWSER,
    "state_used_or_unknown": _RETRY_SAME_BROWSER, "browser_mismatch": _RETRY_SAME_BROWSER,
}
MODE = {"off": "Inativo", "register": "Só registrar", "public": "Só resposta no comentário", "private": "Só direct",
        "both": "Comentário + direct"}
TRIGGER = {"keyword": "Comentários com as palavras abaixo", "any": "Qualquer comentário", "intent": "Interesse identificado por IA (custa por comentário)"}
MEDIA_KIND = {"post": "Post", "reel": "Reel", "story": "Story", "ad": "Anúncio", "live": "Live"}
COMMENT_DECISION = {
    "acted": ("Respondido", "ok"), "registered": ("Interessado registrado", "info"), "human_queue": ("Encaminhado ao vendedor", "warn"),
    "with_human": ("Está com o vendedor", "warn"), "existing_customer": ("Já é cliente", "warn"), "spam": ("Spam", "muted"),
    "inactive": ("Post inativo", "muted"), "paused": ("Automação pausada", "warn"), "before_activation": ("Anterior à ativação", "muted"),
    "no_rule": ("Sem palavra-chave", "muted"), "dedup_window": ("Já recebeu direct deste post", "muted"),
    "already_in_contact": ("Já está em conversa", "muted"), "opted_out": ("Pediu para não receber", "muted"),
    "ignored_self": ("Da própria conta", "muted"),
    "known_contact": ("Já conversa com a conta", "warn"), "blocked_contact": ("Marcado para não atender", "muted"),
    "contacts_pending": ("Aguardando a lista de contatos", "warn"),
}
DM_SCOPE = {"origin_only": "Só quem veio de comentário, anúncio ou resposta a story",
            "origin_and_new": "Também contatos novos, sem conversa anterior com a conta",
            "all": "Todos que escreverem, inclusive quem já conversa com a conta"}
TEXT_PROBLEM = {
    "not_enough_variants": "Cadastre mais respostas no comentário, para não repetir a mesma frase.",
    "missing_text": "Escreva o texto do direct.",
    "missing_question": "Escreva pelo menos uma pergunta.",
    "missing_faq_question": "Escreva a pergunta.",
    "missing_faq_answer": "Escreva a resposta.",
    "too_many_questions": "São perguntas demais para o Instagram.",
    "missing_keywords": "Informe as palavras que acionam a resposta.",
    "intent_unavailable": "A identificação de interesse por IA não está disponível nesta configuração.",
    "missing_disclosure": "O direct é a primeira mensagem ao cliente: diga que é o assistente virtual.",
    "too_long": "Texto longo demais.",
    "unknown_placeholder": "Só {seller} pode ser usado entre chaves.",
    "numbers_provenance": "Não use valores ou números: eles saem da tabela quando o cliente responder.",
    "forbidden_phrase": "Tem uma promessa ou termo proibido.",
    "required_context": "Tem uma expressão que exige um complemento na mesma frase.",
    "never_request": "Não peça dados pessoais sensíveis.",
    "pii_in_output": "Não inclua telefone, e-mail ou outro dado pessoal.",
    "pii_placeholder": "Não inclua dados pessoais.",
    "human_impersonation": "O assistente não pode dizer que é uma pessoa.",
    "one_question": "Faça só uma pergunta por mensagem.",
    "url_not_allowed": "Esse link não está liberado.",
    "empty": "Texto vazio.",
}
KB_REFUSAL = {
    "unknown_model": "Este modelo não tem preço cadastrado, então o sistema não pode usá-lo.",
    "same_model": "Esse já é o modelo em uso.",
    "reindex_running": "Já tem uma troca de modelo em andamento.",
    "provider_blocked": "Este ambiente usa a chave de testes, que não recebe material real. A troca precisa do OpenRouter.",
    "no_gateway": "O provedor de IA não está configurado neste ambiente.",
    "no_index": "O índice de busca não foi encontrado.",
    "too_big": "Arquivo grande demais.",
    "not_pdf": "Envie um arquivo PDF.",
    "never_import": "Este arquivo não pode entrar na base desta conta.",
    "duplicate": "Este arquivo já foi enviado como {detail}.",
    "blocked_again": "Este arquivo já foi bloqueado por ter dados de pessoas. Envie uma cópia sem eles. Se forem só dados da própria empresa, envie marcando a opção de substituir.",
    "not_found": "Não encontrei este item. Ele pode já ter sido resolvido.",
    "not_ready": "Este documento não pode ser aprovado agora.",
    "missing_choice": "Escolha como o assistente usa o documento e para quem ele foi feito.",
    "internal_citable": "Material interno não pode ser citado ao cliente. Escolha \"Só orienta o assistente\".",
    "bad_date": "Data inválida.",
    "expired": "A validade já passou.",
    "no_text": "Não sobrou nenhum trecho para usar neste documento.",
    "direct_key": "Este ambiente usa a chave de testes, que não recebe material real. Aprovar só funciona com o provedor de produção.",
}
KB_ERROR = {
    "unreadable": "Não consegui ler este PDF: ele pode estar corrompido ou com senha. Envie outra cópia.",
    "too_many_pages": "O PDF tem páginas demais. Envie só a parte que interessa.",
    "lost_file": "O arquivo se perdeu antes de ser lido. Envie de novo.",
    "no_index": "A base ainda não foi preparada neste servidor. Avise o responsável técnico (comando bootstrap).",
    "budget": "O orçamento de IA acabou antes de terminar. Recarregue em Orçamento e aprove de novo.",
    "provider": "O provedor de IA não respondeu. Aprove de novo em alguns minutos.",
    "timeout": "O processamento não terminou. Tente de novo.",
}
KB_FLAG = {"forbidden_phrase": "Promessa ou termo proibido", "required_context": "Expressão sem o complemento obrigatório", "never_request": "Pede dado sensível",
           "rg": "RG", "bank": "Dados bancários", "address": "Endereço"}
KB_PII = {"cpf": "CPF", "cnpj": "CNPJ", "card": "cartão", "email": "e-mail", "phone": "telefone", "rg": "RG", "bank": "dados bancários", "address": "endereço", "health": "saúde"}
KB_USE = {"citable": "Pode citar ao cliente", "guidance_only": "Só orienta o assistente"}
KB_AUDIENCE = {"customer": "Feito para o cliente", "internal": "Interno"}
_TEXT_FIELD = {"message": "Mensagem", "faq_question": "Pergunta", "faq_answer": "Resposta", "question": "Pergunta pronta", "public": "Resposta no comentário", "private": "Direct", "repeat": "Resposta a quem já está em conversa", "keywords": "Palavras", "trigger": "Acionamento"}
CHANNEL_PAUSE = {"token_invalid": "o acesso à Meta expirou ou foi revogado. Reconecte com o Facebook para liberar."}


def entry_label(v: str | None) -> str:
    return ENTRYPOINT.get(v or "", humanize(v))


def reason_label(v: str | None) -> str:
    """Handoff reasons; send failures carry details after ':' (e.g. send_failure:token_invalid)."""
    key, _, detail = (v or "").partition(":")
    if key == "send_failure":
        return "Falha no envio" + (f" ({detail.split(':')[0].replace('_', ' ')})" if detail else "")
    return REASON.get(key, humanize(v))


def oauth_error(v: str | None) -> str:
    """Facebook Login callback error codes, as an instruction for the person connecting the channel."""
    return OAUTH_ERROR.get(v or "", "Não foi possível conectar. Clique em Reconectar com o Facebook e tente de novo.")


def pause_reason(v: str | None) -> str:
    """Automatic channel pauses have codes; manual ones carry the text typed by whoever paused."""
    return CHANNEL_PAUSE.get(v or "", (v or "sem motivo informado").rstrip(".") + ".")


def mode_label(v: str | None) -> str:
    return MODE.get(v or "off", humanize(v))


def trigger_label(v: str | None) -> str:
    return TRIGGER.get(v or "", humanize(v))


def media_kind_label(v: str | None) -> str:
    return MEDIA_KIND.get(v or "", humanize(v))


def decision_label(v: str | None) -> str:
    return COMMENT_DECISION.get(v or "", (humanize(v), "muted"))[0]


def decision_tone(v: str | None) -> str:
    return COMMENT_DECISION.get(v or "", ("", "muted"))[1]


TONE = {"friendly": "Próximo e descontraído", "formal": "Mais formal", "neutral": "Neutro e objetivo"}
OFF_HOURS_MODE = {"bot_keeps_serving": "Continua atendendo, sem empurrar a venda", "await_human": "Avisa e espera o vendedor"}
NOTIFY_OFF_HOURS = {"at_open": "Quando o horário começa", "immediately": "Na hora"}
MODEL_NODE = {"respond": "Escrever a resposta", "analyze_turn": "Entender a mensagem", "output_judge": "Revisar a resposta", "summarize": "Resumir a conversa",
              "comment_intent": "Entender comentários"}
RAG_PRESET = {"strict": "Mais rigoroso: responde menos, confirma mais", "balanced": "Equilibrado", "broad": "Mais abrangente: acha mais, com risco de trecho só parecido"}
EVAL_STATUS = {"queued": "Na fila", "running": "Rodando", "passed": "Passou", "failed": "Não passou", "error": "Não terminou: tente de novo"}
EVAL_TONE = {"queued": "info", "running": "info", "passed": "ok", "failed": "bad", "error": "warn"}
EVAL_LEVEL = {"fast": "Checagem rápida", "full": "Avaliação completa"}
EVAL_SCORE = {"compliance_block_rate": "Barra o que é proibido", "compliance_false_positive_free": "Não barra resposta boa", "input_guard": "Reconhece pedidos de parar e de falar com alguém",
              "analyze_slots": "Entende o que o cliente diz", "respond": "Qualidade da resposta", "rag": "Acha o trecho certo", "scenarios": "Conversas simuladas de ponta a ponta"}
GUARD_RULE = {"url_not_allowed": "Link não permitido", "numbers_provenance": "Número sem fonte", "forbidden_phrase": "Frase proibida", "required_context": "Faltou o complemento obrigatório",
              "never_request": "Pediu dado sensível", "guidance_quoted": "Copiou material interno", "answered_without_basis": "Afirmou sem ter base", "pii_placeholder": "Marcador de dado pessoal", "pii_in_output": "Dado pessoal na resposta",
              "human_impersonation": "Disse ser uma pessoa", "one_question": "Mais de uma pergunta", "repetition": "Repetiu mensagem", "length": "Longa demais", "missing_link": "Faltou o link",
              "empty": "Resposta vazia", "opt_out_promise": "Prometeu parar sem registrar"}
CONFIG_ERROR = {
    "stale_draft": "Este rascunho foi feito a partir de outra versão: aprovar desfaria o que foi ativado depois. Use “Atualizar rascunho”.",
    "two_person": "Quem propôs não pode aprovar: precisa de outra pessoa. Convide em Usuários.",
    "not_a_draft": "Esta versão não é mais um rascunho.",
    "needs_evaluation": "Em produção, só aprova depois que a avaliação completa deste rascunho passar.",
    "embedding_mismatch": "O modelo de busca não é o que gerou os vetores desta base. Troque pelo botão em Base de conhecimento, que refaz os vetores antes.",
    "index_not_ready": "O índice de busca desta versão ainda não está pronto.",
    "merge_conflict": "Os dois lados mudaram o mesmo campo ({detail}). Descarte o rascunho e refaça a mudança.",
    "merge_invalid": "A combinação das mudanças não é válida. Descarte o rascunho e refaça a mudança.",
    "no_base": "Não sei de qual versão este rascunho veio. Descarte e refaça a mudança.",
    "compliance_weakened": "A mudança enfraquece uma regra de conformidade do pacote: {detail}",
    "nothing_to_roll_back": "Não há versão anterior para voltar.",
    "no_canary": "Não há canário em andamento.",
    "evaluation_running": "Já tem uma avaliação rodando. Espere terminar.",
}
CONFIG_FIELD = {
    "conversation.persona.seller_display_name": "Nome de quem atende", "conversation.persona.assistant_name": "Nome do assistente",
    "conversation.persona.company_name": "Empresa que representa", "conversation.allowed_urls": "Links permitidos",
    "conversation.persona.tone": "Tom", "conversation.persona.disclose_on_first_message": "Dizer na primeira mensagem que é assistente",
    "conversation.business_instructions": "Instruções do negócio", "conversation.reply_examples": "Exemplos de resposta",
    "conversation.waiting_message": "Mensagem de espera", "conversation.first_message_disclosure": "Identificação na primeira mensagem", "conversation.safe_failure_message": "Mensagem quando algo dá errado",
    "conversation.opt_out_confirmation": "Mensagem quando pedem para parar", "handoff.expectation_message_in_hours": "Aviso em horário de atendimento",
    "handoff.expectation_message_off_hours": "Aviso fora do horário", "handoff.send_expectation_message": "Avisar o cliente da passagem",
    "handoff.sla_minutes": "Prazo para responder", "handoff.off_hours_mode": "Fora do horário, o assistente", "handoff.notify_off_hours": "Quando avisar o vendedor",
    "handoff.human_unanswered_realert_min": "Avisar de novo depois de", "handoff.disabled_reasons": "Motivos que não chamam o vendedor",
    "sending.quiet_hours.start": "Início do silêncio", "sending.quiet_hours.end": "Fim do silêncio", "sending.quiet_hours.timezone": "Fuso horário",
    "sending.holidays_national": "Feriados nacionais", "sending.holidays_local": "Feriados da cidade", "sending.frequency_cap_per_contact_day": "Limite por dia",
    "sending.frequency_cap_per_contact_week": "Limite por semana", "followups.enabled": "Lembretes automáticos", "guidelines.overrides": "Diretrizes do pacote alteradas",
    "guidelines.custom": "Suas diretrizes", "funnel.max_turns_without_progress_override": "Paciência por estágio", "tools": "Ferramentas",
    "compliance.forbidden_phrases": "Frases proibidas", "rag.faq_threshold": "FAQ: semelhança necessária", "rag.min_score": "Trecho: nota mínima", "rag.top_k": "Quantos trechos entram",
    "rag.active_index_id": "Índice de busca", "models.embedding.model": "Modelo de busca", "models.embedding.dims": "Dimensões do modelo de busca",
    "budget.pilot_total_cap_usd": "Teto de gasto", "models.transcription.model": "Transcrição de áudio",
}


def config_field(path: str) -> str:
    if path in CONFIG_FIELD:
        return CONFIG_FIELD[path]
    m = path.split(".")
    if len(m) >= 3 and m[0] == "models" and m[1] in MODEL_NODE:
        what = {"primary": "modelo", "fallbacks": "reserva", "prompt_version": "versão do prompt"}.get(m[2], m[2])
        return f"{MODEL_NODE[m[1]]}: {what}"
    return path


def config_message(err: dict) -> str:
    return {"time_parsing": "use o formato 22:00", "greater_than": "precisa ser maior que zero", "int_parsing": "precisa ser um número", "string_too_long": "texto longo demais",
            "too_long": "itens demais", "string_too_short": "não pode ficar em branco", "literal_error": "opção inválida", "less_than_equal": "valor alto demais",
            "greater_than_equal": "valor baixo demais", "float_parsing": "precisa ser um número"}.get(err.get("type"), err.get("msg", "valor inválido"))


def config_error(code: str, detail: str = "") -> str:
    return CONFIG_ERROR.get(code, detail or "Não foi possível fazer isso.").replace("{detail}", detail)


def _flat(doc, prefix: str = "") -> dict:
    out = {}
    for k, v in (doc or {}).items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict) and path not in CONFIG_FIELD:
            out.update(_flat(v, path))
        else:
            out[path] = v
    return out


def _shown(value) -> str:
    if value is None or value == "" or value == [] or value == {}:
        return "—"
    if isinstance(value, bool):
        return "Sim" if value else "Não"
    if isinstance(value, list):
        return "\n".join(_shown(v) if not isinstance(v, dict) else " · ".join(str(x) for x in v.values() if x not in (None, "", [])) for v in value)
    if isinstance(value, dict):
        return "\n".join(f"{k}: {_shown(v)}" for k, v in value.items())
    return str(value)


def config_changes(before: dict | None, after: dict | None) -> list[dict]:
    """What a draft changes, field by field, in words: the person approving should not have to read a YAML diff."""
    a, b = _flat(before), _flat(after)
    changed = [p for p in sorted({*a, *b}) if a.get(p) != b.get(p)]
    words = {"conversation.persona.tone": TONE, "handoff.off_hours_mode": OFF_HOURS_MODE, "handoff.notify_off_hours": NOTIFY_OFF_HOURS}
    say = lambda p, v: words.get(p, {}).get(v, _shown(v))
    return [{"path": p, "label": config_field(p), "before": say(p, a.get(p)), "after": say(p, b.get(p))} for p in changed]


SETUP_ITEM = {
    "config": ("Configuração", "Ainda não há nenhuma versão: crie a primeira em Configuração."),
    "identity": ("Quem é o assistente", "Nome de quem atende, tom e instruções."),
    "provider": ("Provedor de IA de produção", "O OpenRouter com retenção zero. Com a chave de testes, material real não pode ser aprovado."),
    "channel": ("Canal conectado", "A conta do Instagram ligada e com acesso válido."),
    "tables": ("Tabelas em uso", "Os valores que o assistente pode informar."),
    "material": ("Material aprovado", "Sem isto, ele diz “vou confirmar” a quase tudo."),
    "faqs": ("Perguntas frequentes", "Respostas prontas, aprovadas por você."),
    "conflicts": ("Sem documentos em conflito", "Trechos que falam da mesma coisa com números diferentes."),
    "curation": ("Curadoria em dia", "Perguntas que o assistente não soube responder."),
    "second_person": ("Segundo aprovador", "Mudanças precisam de duas pessoas: quem propõe não aprova."),
    "seller_notice": ("Aviso ao vendedor", "Para onde vão os avisos de quem pediu atendimento."),
    "legal": ("Revisão jurídica", "Subprocessadores com a transferência internacional definida."),
    "evaluation": ("Avaliação aprovada", "A versão a ativar passou na avaliação completa."),
    "active": ("Configuração ativa", "Uma versão aprovada por duas pessoas."),
    "go_live": ("Liberação final", "Sem ela, o assistente não atende ninguém em produção."),
}


def setup_docs(approved: int, in_review: int, blocked: int) -> str:
    return f"{approved} em uso, {in_review} em revisão" + (f", {blocked} bloqueado(s)" if blocked else "")


def setup_faqs(approved: int, drafts: int) -> str:
    return f"{approved} aprovada(s)" + (f", {drafts} rascunho(s)" if drafts else "")


def embedding_choices(current: str) -> list[dict]:
    from agentplatform.core.models import pricing
    return [{"name": k, "per_m": v} for k, v in sorted(pricing.EMBEDDING_PER_M.items()) if k != current and k in pricing.EMBEDDING_DIMS]


def query(**params) -> str:
    from urllib.parse import urlencode
    return urlencode(params)


def kb_refusal(reason: str, detail: str = "") -> str:
    return KB_REFUSAL.get(reason, "Não foi possível fazer isso.").replace("{detail}", detail)


def text_problem(p) -> str:
    """A rejected automation text, as an instruction for the seller."""
    msg = TEXT_PROBLEM.get(p.rule, "Texto não permitido.")
    quoted = f' "{p.text[:60]}"' if p.text else ""
    return f"{_TEXT_FIELD.get(p.field, p.field)}{quoted}: {msg}"


def owner_label(v: str | None) -> str:
    return OWNER.get(v or "", (v or "—", "muted"))[0]


def owner_tone(v: str | None) -> str:
    return OWNER.get(v or "", ("", "muted"))[1]


def status_label(v: str | None) -> str:
    return STATUS.get(v or "", (humanize(v), "muted"))[0]


def status_tone(v: str | None) -> str:
    return STATUS.get(v or "", ("", "muted"))[1]


def humanize(v) -> str:
    if v is None or v == "":
        return "—"
    s = str(v).replace("_", " ").strip()
    return s[:1].upper() + s[1:]


def usd(v, places: int = 2) -> str:
    try:
        d = Decimal(str(v or 0))
    except Exception:
        return "—"
    return f"US$ {d:,.{places}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_br(d: Decimal, places: int) -> str:
    return f"{d:,.{places}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def brl(v, places: int = 2) -> str:
    """BRL amount (already in reais), e.g. a customer's budget."""
    try:
        return f"R$ {_fmt_br(Decimal(str(v or 0)), places)}"
    except Exception:
        return "—"


def brnum(v) -> str:
    """Decimal as typed, in Brazilian format with thousands: '300000.00' -> '300.000,00', '0.05' -> '0,05'."""
    try:
        d = Decimal(str(v))
    except Exception:
        return str(v)
    places = max(0, -d.as_tuple().exponent) if isinstance(d.as_tuple().exponent, int) else 2
    return _fmt_br(d, places)


def money(usd, places: int | None = None) -> Markup:
    """A USD cost shown in reais (display rate from core/ops/fx.py); the dollar value and the rate go in the tooltip.
    Tiny costs get more decimals so a fraction of a cent does not read as zero."""
    from agentplatform.core.ops import fx
    try:
        u = Decimal(str(usd or 0))
    except Exception:
        return Markup("—")
    rate = fx.current()
    r = u * rate.value
    if places is None:
        places = 2 if r == 0 or abs(r) >= Decimal("0.10") else 4
    tip = f"US$ {_fmt_br(u, max(places, 4))} · cotação {_fmt_br(rate.value, 4)} ({rate.source})"
    return Markup('<span class="money" title="{}">R$ {}</span>').format(tip, _fmt_br(r, places))


def fx_note() -> str:
    from agentplatform.core.ops import fx
    rate = fx.current()
    return f"US$ 1 = R$ {_fmt_br(rate.value, 4)} ({rate.source})"


def stage_label(name: str | None, labels: dict | None = None) -> str:
    return ((labels or {}).get("stages") or {}).get(name or "") or humanize(name)


def slot_label(name: str, labels: dict | None = None) -> str:
    spec = ((labels or {}).get("slots") or {}).get(name)
    return (spec.label if spec and spec.label else None) or humanize(name)


def slot_value(name: str, raw, labels: dict | None = None) -> str:
    """Profile entries are {"value", "confidence", "at", ...}; show the value formatted by the slot type."""
    v = raw.get("value") if isinstance(raw, dict) else raw
    spec = ((labels or {}).get("slots") or {}).get(name)
    if v is None or v == "":
        return "—"
    if spec is not None:
        if spec.type == "money":
            try:
                return brl(v, 0)
            except Exception:
                return str(v)
        if spec.type == "bool":
            return "Sim" if str(v).lower() in ("true", "1", "sim", "yes") else "Não"
        if spec.type == "enum":
            return spec.value_labels.get(str(v), humanize(v))
    return str(v)


def slot_confidence(raw) -> str:
    c = raw.get("confidence") if isinstance(raw, dict) else None
    return f"{float(c) * 100:.0f}%" if isinstance(c, (int, float)) else ""


def contact_name(r) -> str:
    if r.get("first_name"):
        return r["first_name"]
    if r.get("username"):
        return "@" + r["username"]
    ext = str(r.get("external_user_id") or "")
    channel = humanize(r.get("channel")) if r.get("channel") else "Contato"
    return f"{channel} …{ext[-6:]}" if ext else "Contato"


def dt(v: datetime | None, fmt: str = "%d/%m %H:%M") -> str:
    """Console times in the display timezone (the database stores UTC)."""
    if not v:
        return "—"
    if isinstance(v, datetime) and v.tzinfo is not None:
        from zoneinfo import ZoneInfo

        from agentplatform.settings import get_settings
        v = v.astimezone(ZoneInfo(get_settings().display_timezone))
    return v.strftime(fmt)


def initials(v: str | None) -> str:
    parts = [p for p in (v or "").replace("_", " ").replace(".", " ").split() if p]
    return ("".join(p[0] for p in parts[:2]) or "?").upper()


def pct(part, whole) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "—"


_PATHS = {
    "chat": '<path d="M4 5h16v10H8l-4 4z"/>',
    "inbox": '<path d="M4 4h16v16H4z"/><path d="M4 13h4l2 3h4l2-3h4"/>',
    "book": '<path d="M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3z"/><path d="M5 17a3 3 0 0 1 3-3h11"/>',
    "chart": '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    "megaphone": '<path d="M3 10v4l13 5V5z"/><path d="M16 9a3 3 0 0 1 0 6"/><path d="M6 15l1 5h3l-1-4"/>',
    "upload": '<path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 16v4h16v-4"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7 7 0 0 0-2-1.2L14 3h-4l-.5 2.6a7 7 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.6A7 7 0 0 0 5 12c0 .4 0 .8.1 1.2l-2 1.6 2 3.4 2.4-1a7 7 0 0 0 2 1.2L10 21h4l.5-2.6a7 7 0 0 0 2-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2z"/>',
    "wallet": '<path d="M3 7h16a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H3z"/><path d="M3 7l12-4v4"/><circle cx="16" cy="13.5" r="1.2"/>',
    "plug": '<path d="M9 3v5M15 3v5"/><path d="M6 8h12v3a6 6 0 0 1-12 0z"/><path d="M12 17v4"/>',
    "shield": '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
    "users": '<circle cx="9" cy="8" r="3.5"/><path d="M2 20c0-3.5 3-6 7-6s7 2.5 7 6"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7M22 20c0-3-2-5-4.5-5.7"/>',
    "building": '<path d="M4 21V5l8-2v18M12 9h8v12M8 8v.01M8 12v.01M8 16v.01M16 13v.01M16 17v.01"/>',
    "key": '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M17 6l3 3"/>',
    "logout": '<path d="M15 4h4v16h-4M10 8l-4 4 4 4M6 12h11"/>',
    "pause": '<path d="M8 5v14M16 5v14"/>',
    "play": '<path d="M7 5l12 7-12 7z"/>',
    "alert": '<path d="M12 3l10 18H2z"/><path d="M12 10v4M12 17.5v.01"/>',
    "back": '<path d="M15 5l-7 7 7 7"/>',
    "check": '<path d="M5 12l5 5L20 7"/>',
    "search": '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.5-4.5"/>',
    "download": '<path d="M12 4v12M7 11l5 5 5-5"/><path d="M4 20h16"/>',
    "table": '<path d="M3 5h18v14H3z"/><path d="M3 10h18M3 14.5h18M9 5v14"/>',
    "sparkle": '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/>',
    "image": '<path d="M4 5h16v14H4z"/><circle cx="9" cy="10" r="1.6"/><path d="M4 17l5-4 3 2 4-4 4 4"/>',
    "comment": '<path d="M4 5h16v11H9l-5 4z"/><path d="M8 9h8M8 12.5h5"/>',
}


def icon(name: str, size: int = 18) -> Markup:
    return Markup(f'<svg class="i" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                  f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
                  f'{_PATHS.get(name, "")}</svg>')


def install(env) -> None:
    env.filters.update(owner_label=owner_label, owner_tone=owner_tone, status_label=status_label,
                       status_tone=status_tone, humanize=humanize, usd=usd, brl=brl, brnum=brnum, money=money, dt=dt,
                       initials=initials, contact_name=contact_name,
                       entry_label=entry_label, reason_label=reason_label, oauth_error=oauth_error,
                       pause_reason=pause_reason, mode_label=mode_label, trigger_label=trigger_label,
                       media_kind_label=media_kind_label, decision_label=decision_label, decision_tone=decision_tone,
                       text_problem=text_problem)
    env.globals.update(icon=icon, pct=pct, DIRECTION=DIRECTION, MESSAGE_KIND=MESSAGE_KIND, DM_SCOPE=DM_SCOPE, ROLE=ROLE, fx_note=fx_note, stage_label=stage_label,
                       slot_label=slot_label, slot_value=slot_value, slot_confidence=slot_confidence, KB_ERROR=KB_ERROR, KB_FLAG=KB_FLAG, KB_PII=KB_PII,
                       KB_USE=KB_USE, KB_AUDIENCE=KB_AUDIENCE, TONE=TONE, OFF_HOURS_MODE=OFF_HOURS_MODE, NOTIFY_OFF_HOURS=NOTIFY_OFF_HOURS,
                       EVAL_STATUS=EVAL_STATUS, EVAL_TONE=EVAL_TONE, EVAL_LEVEL=EVAL_LEVEL, EVAL_SCORE=EVAL_SCORE, GUARD_RULE=GUARD_RULE)
