# 14 — Roadmap, tarefas, estimativas e riscos

Cada tarefa é pequena (≤ 2 dias de um agente/dev), tem critério de aceite e teste, e referencia o arquivo do plano. Estimativa relativa em pontos (1 = trivial, 3 = um dia, 5 = dois dias). A ordem respeita as dependências.

## Visão geral

```mermaid
gantt
  dateFormat  YYYY-MM-DD
  title Fases (relativas; datas ilustrativas a partir de 13/10/2026)
  section Fase 1 — Fundação
  Infra, dados, gateway, outbox, orçamento     :f1a, 2026-10-13, 15d
  Testes no app real + App Review (paralelo)   :crit, f1b, 2026-10-13, 30d
  Harness de avaliação e lab (esqueleto)       :f1c, after f1a, 7d
  section Fase 2 — MVP Instagram
  DM: agente, guards, RAG, handoff             :f2a, after f1c, 20d
  Comentários + private reply (pós-review)     :f2b, after f1b, 10d
  Console mínimo, follow-ups, lab              :f2c, after f2a, 8d
  Piloto controlado                            :f2d, after f2c, 10d
  section Fase 3 — Venda e dados
  Tabela de planos, simulação, histórico       :f3, after f2d, 15d
  section Fase 4 — WhatsApp
  Canal WhatsApp, templates, importação, campanhas :f4, after f3, 25d
  section Fase 5 — Escala
  Advanced Access multi-cliente, Connect, novos canais :f5, after f4, 30d
```

## Fase 1 — Fundação (≈ 86 pontos)

| ID | Tarefa | Aceite / teste | Pts | Ref |
| --- | --- | --- | --- | --- |
| F1-REPO-01 | Estrutura do repositório, `uv`, lint (ruff, mypy), pre-commit, CI | CI verde com teste vazio; `CLAUDE.md` aplicado | 2 | `17` |
| F1-INFRA-01 | Compose com perfis dev/staging/prod, Postgres pgvector, Redis, healthchecks e secrets | `docker compose --profile dev up` sobe tudo saudável; portas dev 55432/56379/8100 | 3 | `12` |
| F1-DB-01 | Migrações Alembic do núcleo (`03`) + `pt_unaccent` + RLS + papel `app_rw` | Teste de isolamento por tenant gerado do catálogo passa | 5 | `03` |
| F1-SEC-01 | Envelope encryption (KEK/DEK), `*_hash`, filtro de logs | Unit: cifra/decifra, rotação de DEK; log com token mascarado | 3 | ADR-016 |
| F1-GW-01 | `POST /webhooks/meta`: verificação (`hub.challenge`), assinatura, normalização, `inbound_events` idempotente, 200 rápido | Payloads reais de exemplo → eventos canônicos; duplicado = 1 linha; p99 < 300 ms em teste local | 5 | `05`, `07` |
| F1-Q-01 | procrastinate: app, filas por prioridade, retry, DLQ → `dead_letters`, periódicos | Job que falha N vezes vai para DLQ com alerta | 3 | ADR-004 |
| F1-OUT-01 | Outbox + despachante com as 11 checagens (`send_epoch`, `content_hash`, cliente falou depois do proativo) + quebra por bytes + reconciliação de envio ambíguo (eco → `/conversations` → `failed` + handoff, nunca reenvio às cegas) | Testes de cada checagem e das intercalações (b), (c), (e), (f), (g), (i) do `07` §3; bolhas de 1.000 bytes com acentos | 5 | `07` §5 |
| F1-OWN-01 | Posse + `run_epoch`/`send_epoch` sob advisory lock + lease do turno + `privacy_gate` em qualquer posse + detector de eco (casa `sending`/`unknown`) + reabertura de `CLOSED` | Testes "eco antes do mid", "vendedor fala durante a geração", "opt-out com posse humana" e intercalações (a), (d), (h) do `07` §3 | 5 | ADR-002, ADR-005 |
| F1-DEB-01 | Debouncer Redis + fallback Postgres + `close_batch` | Rajada de 4 mensagens em 6 s com áudio = 1 turno; Redis parado = sem perda | 3 | `07` §2 |
| F1-MG-01 | `ModelGateway` (OpenRouter, ZDR em todo modelo, fallbacks, `require_parameters`, validação Pydantic) + `budget_balances` (reserva atômica, TTL, dia em UTC, liquidação pela estimativa + conciliação) + alertas | Chamada sem saldo → `BudgetDenied`; custo real gravado; teto paralelo respeitado | 5 | `08` §3 |
| F1-CFG-01 | Registro de config (`config_versions`, resolução, monotonicidade de compliance, cache, kill switches) | `validate_config.py` portado para testes; kill switch efetivo em ≤ 5 s | 5 | `08`, ADR-014 |
| F1-PKG-01 | Loader de pacotes + contrato + pacote `auto_insurance` mínimo | Suíte `tests/contract` roda contra o pacote fictício | 3 | `13` |
| F1-OBS-01 | OTel + tabelas de trace + métricas + alertas básicos (perfil `obs`) | Trace de um turno de teste visível; alerta de "sem webhook" dispara | 3 | `10` §1 |
| F1-EVAL-01 | Esqueleto do runner (`run_node`, `run_turn`, `run_conversation`), `FakeChannelAdapter`, graders determinísticos, gate no CI | Um dataset de 5 casos roda no CI e bloqueia uma regressão forçada | 5 | `10` |
| F1-META-01 | **Teste no app real:** permissões concedidas, Standard × Advanced, tipo de token, inscrição de webhooks | Relatório com `debug_token`, escopos, campos recebidos | 2 | `05` |
| F1-META-02 | **Verificação da empresa + App Review** (Advanced Access para `instagram_manage_comments`, `instagram_manage_messages`, `pages_messaging`, recurso Human Agent) com screencast | Submetido na semana 1; aprovado (dependência externa) | 3 | R-01 |
| F1-META-03 | `token_guard` (debug_token diário, reação a 190, tela de reconexão) | Token inválido simulado → kill switch + alerta + reconexão | 3 | `07` §8.8 |
| F1-META-04 | **Teste de eco real:** vendedor responde pelo app; nosso envio; self messaging; segunda conta; GIF/sticker; presença de `app_id` | Relatório que confirma ou ajusta o ADR-002 | 2 | ADR-002 |
| F1-WA-01 | Notificador WhatsApp: número da plataforma, template `handoff_alert_v1`, webhook de botão assinado, fallback por e-mail | Aviso chega; botão "assumi" muda a posse; falha → e-mail | 5 | ADR-010 |
| F1-OPS-01 | Backup `wal-g` + teste de restore automatizado + runbooks | Restore em staging validado | 3 | `12` |
| F1-ING-01 | Ingestão do material (`06`): dedup, PII, OCR de páginas escaneadas, metadados, aprovação | Material carregado com status; nada com PII indexado | 5 | `06` |
| F1-RL-01 | Rate limiters (token bucket Lua por conta e por tipo: private reply/h, texto/s, mídia/s, Conversations/s; por modelo; por contato) + leitura do header `X-Business-Use-Case-Usage` | Testes de bucket; erro 80002 reagenda | 3 | `05` §3 |
| F1-SPOOL-01 | Spool durável de webhooks no `api` + replayer + alerta; job de reinscrição e backfill (`/conversations`, `/{media-id}/comments`) | Postgres parado por 70 min em teste → 0 perda; app segue inscrito | 3 | `07` §1 |
| F1-LG-01 | **Spike:** cancelamento de run no LangGraph 1.2 (task asyncio vs. `RunControl.request_drain`), `durability="sync"` por execução, `EncryptedSerializer` | Nota técnica + teste mínimo; ADR-012 ajustado se preciso | 2 | `07` §3 |
| F1-Q-02 | **Spike:** retry de jobs stalled e heartbeat de worker no procrastinate 3.10; comportamento do `lock` com job agendado | Nota técnica + teste SIGSTOP | 2 | ADR-004 |
| F1-META-05 | **Spike:** confirmar no app real se private reply exige o recurso Human Agent; se `usage.cost` vem em toda resposta do OpenRouter (inclusive com fallback e streaming desligado) | Nota técnica; ajustar F1-META-02 e o ADR-011 | 1 | `05`, `08` |

**Saída da fase 1:** webhooks reais persistidos e deduplicados; outbox enviando DM de teste com checagens; posse por eco validada no app real; orçamento ativo; gate de CI rodando; App Review submetido.

## Fase 2 — MVP Instagram (≈ 121 pontos)

| ID | Tarefa | Aceite / teste | Pts |
| --- | --- | --- | --- |
| F2-AG-01 | Grafo LangGraph com checkpointer Postgres, `load_context`, `commit`, `handoff`, `opt_out`, `degraded_reply` | Retomada após kill do worker sem duplicar | 5 |
| F2-AG-02 | `input_guard` (regras + mascaramento de PII + opt-out + injection) | `core/input-guard-v1` no gate | 5 |
| F2-AG-03 | `analyze_turn` (prompt v1, saída estruturada) + `apply_stage_and_slots` | F1 de slots e acurácia de estágio medidos; transição inválida nunca aplicada | 5 |
| F2-AG-04 | `select_guidelines` + fluxo declarativo + turnos sem progresso | Testes de fluxo do pacote | 3 |
| F2-KB-01 | Knowledge Service (FAQ + híbrido RRF + limiar + `no_basis` + curadoria) | `rag-v1` recall@4 medido; pergunta sem base → `no_basis` | 5 |
| F2-AG-05 | `respond` com ferramentas, teto por turno e contexto mínimo | Rubrica `respond-v1` | 5 |
| F2-AG-06 | `output_guard` (números com proveniência, frases proibidas, PII, bytes, repetição, juiz de outra família) | `compliance-v1`: 100% bloqueado; falso positivo ≤ 5% | 5 |
| F2-PKG-01 | Pacote `consortium` completo (manifesto, slots, estágios, diretrizes, compliance, FAQs semente) | Suíte de contrato + cenários `scenarios-v1` | 5 |
| F2-MEDIA-01 | Áudio (download imediato, transcrição, segurar lote), imagem (descrição opcional) | Rajada com áudio = 1 turno com transcrição | 3 |
| F2-CMT-01 | Automação de comentários (regras, dedup, spam, sensível → humano, variantes A/B, private reply) — **depende do App Review** | Fluxo do `07` §8.1 com adaptador falso e, depois, real | 5 |
| F2-ENTRY-01 | Story reply/mention, referral de anúncio (`ad_id`), ice breakers, atribuição em `lead_sources` | Origem gravada para cada entrada | 3 |
| F2-FORM-01 | `create_lead_form_link` + formulário de pré-cadastro com consentimento (página própria) + entrega ao vendedor | Link único que expira; campos do pacote; nada de CPF | 3 |
| F2-FU-01 | Follow-ups dentro de 24 h e no horário, com paradas | Testes do `09` §5 | 3 |
| F2-CON-01 | Console: login, RBAC, TOTP, lista de conversas (estado, resumo, posse, `inbound_without_reply`), assumir e devolver ao bot | E2E Playwright | 5 |
| F2-CON-02 | Console: trace do turno (contexto mascarado, diretrizes, trechos, chamadas, guard, despacho) | E2E | 3 |
| F2-CON-03 | Console: documentos e FAQs com aprovação e auditoria; fila de curadoria agrupada | E2E | 5 |
| F2-CON-04 | Console: config com diff, dupla aprovação, canário, rollback; kill switches; orçamento e recarga | E2E | 5 |
| F2-LAB-01 | Lab: datasets versionados, variantes como override de config, runner paralelo com estimativa e teto de custo | Experimento reprodutível pelo id | 5 |
| F2-LAB-02 | Lab: placar (Pareto, IC por bootstrap, diff lado a lado), replay de conversa, promoção para canário | E2E | 5 |
| F2-LAB-03 | Lab: varredura de modelos por nó e matriz de RAG; registrar as decisões (ADR-017 confirmado ou revertido) | Decisão registrada com dados | 3 |
| F2-SUM-01 | Resumo rolante assíncrono (a cada N turnos) sem PII | Teste de mascaramento + retenção de informação no cenário | 2 |
| F2-FORM-02 | Segurança da página de pré-cadastro: token de uso único, expiração, rate limit, CSRF, consentimento registrado, sem campos proibidos, cabeçalhos de segurança | Teste de segurança (ZAP baseline) | 3 |
| F2-HO-01 | Handoff completo: off-hours (`bot_keeps_serving`), SLA e re-alerta, reabertura de `CLOSED`, roteamento | Testes do `05` §5–§6 | 3 |
| F2-LGPD-01 | `retention_sweeper` com a tabela completa do `03` §8 | Teste por tabela | 3 |
| F2-LGPD-02 | Direitos do titular no console (busca, exportação, correção, eliminação em cascata com auditoria) | E2E + teste de cascata | 3 |
| F2-LGPD-03 | Pacote jurídico (não é código; dono: operador da plataforma + assessoria): contrato de operador, cláusulas-padrão ANPD com provedores, RIPD, encarregado; `subprocessors[].transfer_mechanism` sem `pending` | Documentos assinados; flag `go_live` liberada pelo schema | 2 |
| F2-CHAOS-01 | Testes de caos e carga (`07` §9) no pipeline | 100% verdes | 5 |
| F2-PILOT-01 | Piloto: canário de 10% → 50% → 100% das conversas, revisão diária com o vendedor | Metas de saída abaixo | 5 |

**Saída da fase 2:** pass^3 ≥ 0,85 nos cenários; zero violação de compliance (gate e revisão); testes de corrida vendedor × bot aprovados (todas as intercalações do `07` §3); custo por conversa ≤ US$ 0,02; p95 do turno ≤ 12 s; **F2-LGPD-01, 02 e 03 concluídas** (sem elas não há go-live).

## Fase 3 — Venda e dados (≈ 30 pontos)

| ID | Tarefa | Aceite / teste | Pts |
| --- | --- | --- | --- |
| F3-DATA-01 | Import validado da tabela de planos (prévia, diff, aprovação, rollback) | Dataset ativo com checksum | 3 |
| F3-SIM-01 | `simulate_consortium` determinística (fórmula do regulamento) + disclosure obrigatório + estágio `simulado` | Casos de teste fornecidos pelo cliente batem ao centavo | 5 |
| F3-GH-01 | Importação do histórico de grupos (sem dados de pessoas) + `group_history` (se aprovado) | Cenário `best_group_question` passa | 5 |
| F3-FORM-01 | Entrega do pré-cadastro ao processo do vendedor (e-mail/WhatsApp/planilha, conforme o cliente) | E2E | 3 |
| F3-DASH-01 | Dashboards de funil, atribuição por origem e custo por lead | Views SQL testadas | 3 |
| F3-OPT-01 | GEPA nos nós estruturados (se houver verba) | Nova versão de prompt passa no gate | 5 |

## Fase 4 — WhatsApp (≈ 45 pontos)

| ID | Tarefa | Pts |
| --- | --- | --- |
| F4-WA-01 | `WhatsAppCloudAdapter` (entrada, saída, janela de 24 h e de 72 h de anúncio, status com `pricing`) | 5 |
| F4-WA-02 | Templates por categoria, aprovação e versionamento; seleção fora da janela | 5 |
| F4-WA-03 | Coexistência (`smb_message_echoes` como sinal de humano) | 3 |
| F4-FU-01 | Cadências fora de 24 h só com template e consentimento | 3 |
| F4-IMP-01 | Importação CSV/XLSX com dry-run, E.164, dedup/merge, LGPD por linha, supressão, rollback | 5 |
| F4-CMP-01 | Campanhas: segmento, agenda, throttling por portfólio, A/B, pausa automática | 5 |
| F4-CMP-02 | Métricas de campanha e custo real | 3 |

## Fase 5 — Escala (≈ 60 pontos)

| ID | Tarefa | Pts |
| --- | --- | --- |
| F5-META-01 | Advanced Access como provedor de tecnologia (multi-cliente) e onboarding de tenant | 5 |
| F5-CON-01 | Connect como `HumanInboxPort` e `ChannelAdapter` (só API e webhooks) | 5 |
| F5-CH-01 | Messenger e webchat | 5 |
| F5-CR-01 | Conversation Routing como sinal adicional de humano | 3 |
| F5-PKG-01 | Segundo segmento real (pacote) | 5 |
| F5-FLOW-01 | Construtor visual de fluxos (ADR de TypeScript, se necessário) | 8 → quebrar |
| F5-COP-01 | Copiloto para vendedores (sugestão de resposta no console) | 5 |

## Riscos

| ID | Risco | Prob. | Impacto | Mitigação |
| --- | --- | --- | --- | --- |
| R-01 | App Review/verificação atrasa ou nega private reply e webhooks de comentário | Alta | Alto | Submeter na semana 1; DM primeiro; screencast claro; escopo mínimo; plano B: só DM + ice breakers + link no perfil até a aprovação |
| R-02 | Eco não distingue humano do bot como esperado (ex.: sem eco para o app nativo) | Média | Alto | F1-META-04 cedo; botão "assumi"; Conversation Routing como alternativa |
| R-03 | Orçamento de US$ 5 acaba antes do piloto dar sinal | Média | Médio | Cenário econômico (≈ US$ 0,77/dia a 100 conversas); piloto com volume menor; degradação; recarga |
| R-04 | Material de um cliente pode trazer promessas ou dados conflitantes | Média | Alto | Revisão antes da aprovação; frases proibidas; fatos-chave com conflito → handoff |
| R-05 | Sem tabela de planos → valor percebido menor | Alta | Médio | Handoff rápido para simulação; fase 3 assim que a tabela chegar |
| R-06 | VPS de 4 GB apertada (Postgres + fila + vetores + observabilidade) | Média | Médio | `mem_limit`; observabilidade enxuta; OCR fora; gatilhos de escala no `02` §6 |
| R-07 | Template do notificador WhatsApp não aprovado ou portfólio sem verificação | Média | Médio | Fallback por e-mail desde o dia 1; Telegram como alternativa |
| R-08 | Mudanças de API/política da Meta | Média | Alto | Versão da Graph API fixada; revisão trimestral do `05` §9; testes de contrato com payloads reais |
| R-09 | LGPD: transferência internacional sem cláusulas-padrão | Média | Alto | ZDR + mascaramento; cláusulas com provedores; RIPD antes do go-live |
| R-10 | Uso da marca da administradora fora das regras para consultores | Média | Médio | Modo restritivo (P7); validar com o cliente |
| R-11 | Qualidade do pt-BR de modelos baratos | Média | Médio | Varredura no lab; cenário equilibrado ainda cabe no teto |
