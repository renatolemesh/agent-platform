# 01 — Requisitos, premissas e perguntas

Fonte: briefing inicial, não versionado (09/10/2026). Este arquivo consolida o que é obrigatório, o que foi decidido, o que foi assumido e o que ainda falta perguntar. A rastreabilidade requisito → componente → dado → config → teste está em [`15-matriz-rastreabilidade.md`](15-matriz-rastreabilidade.md).

## 1. Ordem de prioridade (para resolver conflitos)

1. Segurança, LGPD e compliance do segmento.
2. Nunca enviar mensagem indevida (fora da janela, ativa fora do horário, com humano na conversa, duplicada ou com promessa proibida).
3. Nunca perder mensagem nem lead.
4. Qualidade e coerência da conversa.
5. Conversão.
6. Custo e latência.

Toda decisão de design neste plano que troca um item por outro cita esta ordem.

## 2. Decisões já tomadas

| Tema | Decisão | Origem |
| --- | --- | --- |
| Produto | Plataforma própria, multi-tenant (cada vendedor ou empresa é um `tenant`), multi-segmento | Prompt §2 |
| Primeiro cliente | Consultor de consórcio | Prompt §2 |
| Canal do MVP | Instagram (comentários + direct) pela Graph API, com app Meta próprio | Prompt §2 |
| Login do app Meta | **Facebook Login** (Instagram API with Facebook Login, conta IG ligada a uma página) | Usuário, 09/10/2026 |
| Atendimento humano | O vendedor responde pelo app do Instagram; o eco pausa o bot | Prompt §2 |
| Aviso de handoff | **WhatsApp** (Cloud API oficial, template utility), com e-mail como fallback | Usuário, 09/10/2026 |
| Orçamento LLM | **Recarga conforme o uso**: começa com US$ 5, teto diário configurável, referência de US$ 2/dia | Usuário, 09/10/2026 |
| Hospedagem | **VPS pequena dedicada** (premissa: 2 vCPU / 4 GB / 40 GB SSD) | Usuário, 09/10/2026 |
| Connect | Fora do MVP | Prompt §2 |
| Horário humano | 8h–22h (America/Sao_Paulo); o agente atende 24 h; mensagem ativa só nesse horário | Prompt §2 |
| Volume | Até 100 conversas/dia, com caminho para escalar | Prompt §2 |
| Aprovação de conteúdo | O próprio vendedor (cliente) | Prompt §2 |

## 3. Premissas (perguntas da §19 sem resposta)

Cada premissa tem dono e data para revisão. Se a premissa cair, a seção indicada muda.

| # | Pergunta | Premissa adotada | Afeta |
| --- | --- | --- | --- |
| P1 | Permissões concedidas e se a conta do cliente já está no app | A conta IG do cliente está ligada a uma página e será adicionada ao app como conta gerenciada (Standard Access). Confirmação na fase 1, tarefa F1-META-01 | `05`, ADR-001 |
| P2 | Teto diário aceitável | `daily_cap_usd = 2.00` (dia em UTC), `pilot_total_cap_usd = 4.50` (abaixo do crédito líquido de ≈ US$ 4,74) até a primeira recarga; alertas em 50/80/100% | `08` |
| P3 | Tabela oficial de planos | **Não existe ainda.** Pedido de simulação vira handoff; `simulate_consortium` fica desligada | `13`, `04` |
| P5 | Retorno ao bot por inatividade | Desligado por padrão (`handoff.return_to_bot_after_inactivity_min = null`). Exceção desde 01/11/2026 (ADR-020): handoff causado por falha nossa volta sozinho; o que o vendedor assumiu só volta por ação explícita | `05`, `08`, ADR-020 |
| P6 | 8h–22h em fins de semana e feriados | Vale todos os dias; feriados nacionais desligam mensagens ativas por padrão (configurável) | `09` |
| P7 | Regras da administradora para consultores em redes sociais | Desconhecidas. O pacote começa no modo mais restritivo: sem logo, se apresenta como "assistente virtual de <nome do consultor>, consultor" e não usa material não aprovado | `11` |
| P8 | Histórico de grupos | Sem dados; `group_history` **desligada** até o cliente aprovar | `13`, `06` |
| P10 | Fotos e vídeos | Entram pela mesma ingestão quando chegarem (OCR/transcrição), com o mesmo fluxo de aprovação | `06` |
| P11 | Número de WhatsApp do notificador | A plataforma tem número próprio na Cloud API (não o do vendedor), com verificação do negócio; até ficar pronto, o aviso vai por e-mail | ADR-010 |
| P12 | Quantos vendedores no 1º tenant | Um. O roteamento entre vários fica pronto, mas com uma regra só | `05` |
| P13 | Papéis LGPD | Vendedor (ou a empresa dele) = controlador; plataforma = operadora. Validar com assessoria jurídica antes do go-live | `11` |

## 4. Perguntas que ainda restam (não bloqueiam o plano)

1. **Cliente:** existe tabela de planos ou acesso a simulador da administradora? (destrava a fase 3)
2. **Cliente:** a administradora exige revisão própria de scripts e materiais usados nas redes? Há manual de marca para consultores?
3. **Cliente:** dados e autorização para usar histórico de grupos no atendimento.
4. **Cliente:** horário de atendimento em feriados locais (cidade do consultor).
5. **Plataforma:** dono do número de WhatsApp da plataforma e da conta de Business Manager para a verificação.
6. **Jurídico:** contrato de operador, base legal por finalidade e cláusulas-padrão (Res. CD/ANPD 19/2024) para provedores de LLM no exterior.

## 5. Requisitos funcionais (resumo; detalhe nos arquivos indicados)

| ID | Requisito | Onde |
| --- | --- | --- |
| RF-01 | Responder comentários com regras por post, reel, anúncio ou conta; gatilhos por palavra-chave, regex ou intenção; vigência e prioridade | `05`, `09` |
| RF-02 | Ações por regra: resposta pública, private reply, as duas ou só registrar o lead; variações de texto; sem PII, preço ou simulação na resposta pública | `05` |
| RF-03 | Deduplicação: uma ação por comentário; no máximo uma private reply por pessoa e post numa janela | `03`, `05` |
| RF-04 | Ignorar a própria conta e spam; reclamação, ataque ou tema sensível vai para fila humana | `05` |
| RF-05 | Private reply com pergunta simples, com variantes A/B e taxa de resposta medida | `05`, `10` |
| RF-06 | Entradas: story reply e menção, anúncio para direct (`ad_id`), ice breakers; toda entrada cria ou atualiza o lead com origem | `05`, `03` |
| RF-07 | Abstração de canal; identidade única com merge só por identificador forte | `05`, `03` |
| RF-08 | Texto, áudio (transcrição), imagem, documento, botões e listas com fallback | `05`, `04` |
| RF-09 | Três camadas de conhecimento; fila de curadoria | `06` |
| RF-10 | Estágio, slots, recomendação por ferramenta, objeções, próximo passo | `04`, `13` |
| RF-11 | Arquitetura sem gargalo (poucas chamadas, lock, filas com prioridade) | `02`, `07` |
| RF-12 | Stack fixa em Docker | `12` |
| RF-13 | Harness parametrizável (config versionada, kill switches, gate de avaliação) | `08`, `10` |
| RF-14 | Follow-ups dentro de 24 h e no horário; importação e campanhas (fase 4) | `09` |
| RF-15 | Pré-cadastro mínimo e handoff com resumo | `04`, `05` |
| RF-16 | Console e API de admin sem deploy | `08`, `02` |
| RF-17 | Laboratório de experimentos | `10` |

## 6. Requisitos não funcionais

| ID | Requisito | Meta inicial (configurável) |
| --- | --- | --- |
| RNF-01 | Primeira resposta ao direct | p50 ≤ 8 s, p95 ≤ 20 s (inclui o debounce) |
| RNF-02 | Latência por turno sem debounce | p50 ≤ 5 s, p95 ≤ 12 s |
| RNF-03 | Fast-ack do webhook | p99 ≤ 300 ms |
| RNF-04 | Perda de evento | 0 (todo evento persistido antes do 200) |
| RNF-05 | Mensagem duplicada | 0 em testes de caos |
| RNF-06 | Violação de compliance | 0 (gate de CI e de publicação) |
| RNF-07 | Custo médio | ≤ US$ 0,02 por conversa no cenário econômico (`08` §custo) |
| RNF-08 | Disponibilidade do recebimento | 99,5% no mês; fila absorve falha de worker |
| RNF-09 | Escala | De 100 para 1.000 conversas/dia só com réplicas (sem mudar código) |
| RNF-10 | Isolamento | `tenant_id` + RLS em toda tabela de negócio |

## 7. Métricas de sucesso (fórmula, fonte e meta inicial)

| Métrica | Fórmula | Fonte | Meta inicial |
| --- | --- | --- | --- |
| Tempo até a 1ª resposta | `first_outbound.sent_at − first_inbound.provider_ts` por conversa | `messages`, `outbox` | p50 ≤ 8 s |
| Latência por turno | `turn.finished_at − turn.started_at` | `turns` | p95 ≤ 12 s |
| Resposta à private reply | conversas com inbound ≤ 7 d após a private reply / private replies enviadas | `comment_actions`, `messages` | ≥ 25% (calibrar) |
| Leads qualificados | leads que atingem `min_profile_for_proposal` / leads engajados | `leads`, `lead_stage_history` | ≥ 30% |
| Pré-cadastros | `lead_forms.submitted` por origem | `lead_forms`, `lead_sources` | linha de base no piloto |
| Propostas e vendas | estágios `seller_stage`/`won_stage` do pacote (no consórcio, `com_vendedor`/`cliente`) marcados pelo vendedor, por origem | `lead_stage_history`, `lead_sources` | linha de base |
| Handoff com resumo completo | handoffs com todos os campos obrigatórios do resumo / handoffs | `handoffs` | 100% |
| Tempo até o vendedor assumir | `ownership→HUMANO.at − handoff.created_at` | `ownership_events` | p50 ≤ 30 min no horário humano |
| Opt-outs e bloqueios | opt-outs / conversas; erros de bloqueio do canal | `consents`, `outbox` | ≤ 2% |
| Violações de compliance | bloqueios do `output_guard` em produção que passariam sem o guard + achados na revisão amostral | `guard_results`, `reviews` | 0 achados em revisão |
| Custo de LLM | Σ `llm_calls.cost_usd` / conversas e / leads qualificados | `llm_calls` | ≤ US$ 0,02/conversa |

## 8. Premissa de volume e custo

- 100 conversas/dia ≈ 800 turnos/dia (8 turnos por conversa, como o prompt estima).
- Comentários: até 300/dia num post normal; post viral pode chegar a milhares/h (limitado a 750 private replies/h pela Meta).
- Custo detalhado por nó em [`08-harness-config-seguranca.md`](08-harness-config-seguranca.md#custo).
