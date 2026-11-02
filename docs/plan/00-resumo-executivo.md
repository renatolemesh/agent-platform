# 00 — Resumo executivo

**Plataforma de agentes de atendimento e vendas por mensagem.** Multi-tenant e multi-segmento. Primeiro cliente: um consultor de consórcio no Instagram. Plano elaborado em 09/10/2026.

## O que vamos construir

Um **workflow determinístico com LLM em quatro pontos de decisão**, não um agente autônomo:

- **Entrada:** comentários e mensagens do Instagram entram pela Graph API (Facebook Login), viram eventos canônicos persistidos e passam por debounce.
- **Processamento:** rodam um grafo LangGraph fixo, uma execução por conversa: entender e classificar → regras → conhecimento → responder → guard.
- **Saída:** tudo sai **só** por um outbox, cujo despachante revalida posse, janela, horário, opt-out, idade da mensagem e rate limit antes de cada bolha.
- **Vendedor:** atende pelo app do Instagram; o eco da fala dele pausa o bot.
- **Aviso:** o handoff chega ao vendedor por WhatsApp, com os botões "assumi" e "devolver".
- **Segmento:** tudo o que é de consórcio vive num pacote versionado; o núcleo é genérico, e um pacote fictício de seguro auto prova isso.

## Decisões principais

| Tema | Decisão | ADR |
| --- | --- | --- |
| Canal e humano | Graph API direto (Facebook Login); portas `ChannelAdapter`/`HumanInboxPort`; eco como sinal de humano | 001, 002 |
| Runtime | Python 3.12, FastAPI, Pydantic v2, LangGraph 1.2 + checkpointer Postgres; console Jinja/HTMX | 003 |
| Fila e concorrência | procrastinate (Postgres) com `lock=conversation_id` só no turno + `run_epoch` (commit) e `send_epoch` (envio) como fencing + lease por turno | 004, 005, 012 |
| Conhecimento | pgvector 0.8.7 + FTS português + RRF; FAQs canônicas; dados estruturados só por ferramenta | 006, 015 |
| Modelos e custo | `ModelGateway` com ZDR, fallback, orçamento com reserva/liquidação + trava no OpenRouter | 011 |
| Turno | `analyze_turn` fundido (1 chamada) + `respond` + guard com juiz de outra família | 017 |
| Observabilidade | OTel + tabelas no Postgres; Langfuse Cloud opcional com amostragem | 007 |
| Avaliação | Runner próprio sobre os nós reais + promptfoo + DeepEval; gate no CI e na publicação de config | 008 |
| Notificador | WhatsApp Cloud API (template utility) + e-mail de fallback | 010 |
| Config | Documentos versionados no Postgres, schema completo e validado, canário e rollback | 014 |
| Pacotes | Pacote Python + manifesto YAML; compliance do tenant só pode ficar mais restritivo | 013 |

## O que a pesquisa mudou em relação ao prompt

1. **App Review entra no caminho crítico (risco R-01).** Private replies e webhooks de comentário exigem **Advanced Access** (com verificação da empresa e, para private reply, o recurso Human Agent), mesmo para a conta própria. O pedido é submetido na semana 1, e o DM vai para produção primeiro.
2. **Token:** com Facebook Login, o Page token **não expira por prazo**; ele é invalidado por eventos (erro 190). O job de renovação de 60 dias vira um job diário de validação, com reconexão.
3. **Limite de envio de mídia:** 10/s para áudio e vídeo (além dos 100/s de texto).
4. **Webhooks:** a Messenger Platform **desinscreve o app depois de 1 h de falhas**, então entra um alerta de "nenhum webhook recebido".
5. **WhatsApp:** desde 01/10/2026, mensagens de serviço e utility dentro da janela **são cobradas** (oficial). A franquia de 1.000 grátis por mês não foi confirmada na doc oficial.
6. **Custo:** o piso realista com ZDR é ≈ **US$ 0,77/dia** a 100 conversas/dia, incluindo a taxa de 5,5% (≈ US$ 0,008 por conversa). O prompt estimava US$ 0,40 a 2. US$ 5 duram ≈ 6 dias nesse volume ou ≈ 30 dias com 20 conversas/dia.
7. **Langfuse self-hosted** pede 16 GiB, o que não cabe na VPS. A observabilidade fica em OTel + Postgres.
8. **Regulação:** a vedação de prometer contemplação vem da Res. BCB 155 (informação clara), do CDC e do contrato. Não há proibição textual na Res. BCB 285.

## Fases

1. **Fundação:** infra, dados, gateway, outbox, posse, orçamento, config, gate e testes no app real, com o App Review em paralelo.
2. **MVP Instagram:** agente completo no DM, comentários depois do review, console mínimo, follow-ups em 24 h, laboratório e piloto em canário.
3. **Venda e dados:** tabela de planos, simulação e histórico de assembleia.
4. **WhatsApp:** canal, templates, importação e campanhas.
5. **Escala:** multi-cliente, Connect e novos canais.

Detalhe, pontos e riscos estão no `14`.

## Critérios de aceite do plano (§18 do prompt)

| Critério | Status | Onde |
| --- | --- | --- |
| Matriz de rastreabilidade | ✅ | `15` |
| Cada modo de falha da §10 com mitigação e teste | ✅ | `07` §9, `15` |
| Prova de abstração (núcleo sem consórcio + pacote fictício) | ✅ definido (teste de contrato + grep) | `13`, `15` |
| Todo fato externo com link e data | ✅ | `18` (consulta em 09/10/2026) |
| Schema de config completo e validável | ✅ validado (13/13 checagens) | `08`, `anexos/` |
| Custo por conversa e por dia dentro do orçamento, com a premissa de volume | ✅ | `08` §4 |
| Estimativa relativa por fase e riscos com mitigação | ✅ | `14` |
| Revisão por subagente independente, com críticas e respostas | ✅ | `16` |

## Índice

`01` requisitos e premissas · `02` arquitetura (C4) · `03` dados · `04` agente · `05` canais e inbox humano · `06` conhecimento e material · `07` confiabilidade e sequências · `08` harness, custo e segurança · `09` follow-ups · `10` observabilidade, avaliação e laboratório · `11` compliance · `12` Docker e operação · `13` pacotes · `14` roadmap · `15` rastreabilidade · `16` revisão independente · `17` `CLAUDE.md` · `18` fontes · `adr/` (17 ADRs) · `anexos/` (schema da config).
