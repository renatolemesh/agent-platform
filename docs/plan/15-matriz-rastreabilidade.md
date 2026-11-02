# 15 — Matriz de rastreabilidade

Cada requisito das seções 3 a 16 do prompt → componente responsável → dados → config → teste. Testes com prefixo: `unit/`, `contract/`, `integration/`, `eval/` (datasets do `10`), `chaos/`, `e2e/`.

## Escopo funcional (§3)

| Req. | Componente | Dados | Config | Teste |
| --- | --- | --- | --- | --- |
| 3.1 Regras de comentário (palavra-chave, regex, intenção; vigência, prioridade) | `comments.automation` | `comment_rules`, `comment_actions` | `comments.rules[]` | `unit/comments/test_rule_matching.py` |
| 3.1 Ações (pública, private, ambas, só lead) | `comments.automation` → `outbox` | `outbox`, `leads`, `lead_sources` | `rules[].actions` | `integration/test_comment_flow.py` (`07` §8.1) |
| 3.1 Variações sem PII, preço ou simulação | `comments.automation` + `output_guard` (regras) | `comment_actions.public_variant` | `public_variants` (≥ 3, validado) | `unit/comments/test_public_reply_guard.py` |
| 3.1 Dedup (1 ação por comentário; 1 private por pessoa e post na janela) | `comments.automation` | `unique(comment_id)`; claim atômico em `private_reply_claims` | `private_reply_dedup_window_days` | `unit/comments/test_dedup.py` |
| 3.1 Ignorar própria conta e spam; sensível → humano | `comments.automation` | `comment_actions.decision` | `sensitive_topics_to_human`, `hide_spam` | `eval/core/comments-triage-v1` |
| 3.2 Private reply provoca resposta; A/B | `comments.automation` | `comment_actions.private_variant` | `private_variants`, `private_weights` | Métrica de resposta por variante (`10`) |
| 3.2 Story, anúncio (`ad_id`), ice breakers | `channels.instagram` (eventos) | `lead_sources` | `meta.*` | `contract/test_instagram_payloads.py` |
| 3.2 Toda entrada cria/atualiza lead com origem | `orchestration` + `commit` | `lead_sources` | — | `integration/test_attribution.py` |
| 3.3 Abstração de canal | `channels.ports` | eventos canônicos | — | `contract/test_channel_adapter.py` (fake + IG) |
| 3.3 Identidade única; merge só forte | `contacts.identity` | `contact_identities`, `contact_merge_suggestions` | — | `unit/test_identity_merge.py` |
| 3.3 Áudio, imagem, documento, botões, fallback | `media` worker + `ChannelCapabilities` | `messages.media` | `media.*`, `concurrency.media_max_wait_s` | `chaos/test_burst_with_audio.py` |
| 3.4 RAG só aprovado; FAQ canônica; dados estruturados | `knowledge.service`, `tools` | `kb_*`, `faqs`, `domain_*` | `rag.*` | `eval/consortium/rag-v1` |
| 3.4 Sem resposta → curadoria | `knowledge.service` | `curation_queue` | `rag.min_score` | `unit/test_no_basis.py` |
| 3.5 Estágio com evidência; slots; recomendação por ferramenta; objeções; próximo passo | `agent` (`analyze_turn`, `apply_stage_and_slots`, `select_guidelines`) | `leads`, `lead_stage_history` | `funnel.*`, pacote | `eval/consortium/understand-v1`, `scenarios-v1` |
| 3.6 Sem gargalos | Fila serializada, prioridades, ≤ 4 LLM/turno | `turns.llm_calls` | `agent.max_llm_calls_per_turn` | `chaos/test_viral_post.py` |
| 3.7 Stack | `12` | — | — | `docker compose config` no CI |
| 3.8 Harness | `config.registry`, `tools.registry`, kill switches, gate | `config_versions`, `kill_switches` | todo o schema | `anexos/validate_config.py` → `unit/test_config_schema.py` |
| 3.9 Follow-ups, importação, campanhas | `followups.engine` (MVP); importação e campanhas na fase 4 | `followup_runs`; fase 4: `import_*`, `campaign_*` | `followups.*`, `sending.*` | `09` §5 |
| 3.10 Pré-cadastro mínimo; sem CPF etc.; handoff com resumo | `tools.create_lead_form_link`, `handoff` | `lead_forms`, `handoffs` | pacote `lead_form_model`, `handoff.*` | `e2e/test_lead_form.py`, `eval/compliance-v1` |
| 3.11 Console sem deploy | `admin.api` + console | `audit_log` | — | `e2e/console/*` (Playwright) |

## Stack, dados e Docker (§4)

| Req. | Componente | Teste |
| --- | --- | --- |
| 4.2 ADR-001; portas; token cifrado e renovado; nunca em log | `channels.*`, `token_guard`, ADR-016 | `unit/test_log_redaction.py`, `integration/test_token_invalid.py` |
| 4.3 Python, LangGraph OSS, `AsyncPostgresSaver`, `interrupt()` só para aprovações | `agent.graph` | `chaos/test_worker_kill_resume.py` |
| 4.4 `ModelGateway`; ZDR; fallback; Pydantic; modelo por nó; orçamento | `models.gateway` | `unit/test_gateway_budget.py`, `unit/test_provider_policy.py` |
| 4.5 Postgres fonte da verdade; pgvector; Redis reconstruível; fila | `03`, ADR-004/006 | `chaos/test_redis_down.py` |
| 4.6 Compose, perfis, healthchecks, limites, backup testado | `12` | `ops/test_restore.sh` (mensal) |

## Canais Meta (§5)

| Req. | Componente | Teste |
| --- | --- | --- |
| Acesso e App Review | F1-META-01/02 | Relatório de permissões |
| Token (Facebook Login: page token, 190) | `token_guard` | `integration/test_token_invalid.py` |
| Private reply (1/comentário, 7 dias, follow-up só se responder) | `comments.automation`, despachante | `unit/test_private_reply_rules.py` |
| Limites (750/h, 2/s, 100/s, 10/s mídia) | `ratelimit` | `unit/test_buckets.py`, `chaos/test_viral_post.py` |
| Janela 24 h; nunca `HUMAN_AGENT` em automação | despachante, adaptador | `unit/test_no_human_agent_tag.py` |
| 1.000 bytes | `bubbles.split` | `unit/test_bubble_split_bytes.py` |
| Eco | `human_detector` | `chaos/test_echo_before_mid.py`, `chaos/test_seller_during_generation.py` |
| Webhooks: assinatura, 200 rápido, dedup, ordem | `channels.gateway` | `integration/test_webhook_fastack.py`, `chaos/test_duplicate_out_of_order.py` |
| WhatsApp (fase 4): janela, templates, preço, limites, IA, coexistência | adaptador WhatsApp (fase 4); notificador (MVP) | `contract/test_whatsapp_notifier.py` |

## Princípios (§6)

| Princípio | Onde é garantido | Teste |
| --- | --- | --- |
| 1 Código controla o fluxo | Transições, despachante e fluxos em código | `unit/test_stage_transitions.py` |
| 2 Um agente por turno | Grafo fixo; ≤ 4 chamadas | Métrica `turns.llm_calls` + teste |
| 3 Contexto mínimo | `context_builder` | `unit/test_context_budget.py` (tokens máximos) |
| 4 Regras condicionais | Catálogo de diretrizes + `select_guidelines` | `unit/test_guideline_selection.py` |
| 5 Fluxo declarativo | `FlowSpec` do pacote | `contract/test_flows.py` |
| 6 Números só de ferramentas | `output_guard` numérico | `eval/compliance-v1:invent_installment` |
| 7 Estado tipado e persistido | Pydantic + checkpointer + tabelas | `chaos/test_worker_kill_resume.py` |
| 8 Efeito só via outbox idempotente | `outbox` + `effect_key` | `chaos/test_no_duplicate_send.py` |
| 9 Uma execução por conversa | procrastinate `lock` + `run_epoch` | `chaos/test_concurrent_batches.py` |
| 10 Falhar para o lado seguro | Degradação + handoff | `chaos/test_openrouter_down.py` |
| 11 Conteúdo é dado | Delimitação com nonce; guard | `eval/core/input-guard-v1:injection_*` |
| 12 Configurável, versionado, testado | Registro + gate | `unit/test_publish_requires_eval.py` |

## Confiabilidade (§10), modos de falha → mitigação → teste

| Modo de falha | Mitigação | Teste |
| --- | --- | --- |
| Webhook duplicado / fora de ordem | `unique(idempotency_key)`; ordem por `provider_ts` | `chaos/test_duplicate_out_of_order.py` |
| Rajada | Debounce com teto | `chaos/test_burst_with_audio.py` |
| Mensagem nova durante o run | ADR-012 | `chaos/test_new_message_during_run.py` |
| Worker cai ou pausa antes/depois do commit | Checkpoint + retry de job stalled do procrastinate + lease por turno + `effect_key` | `chaos/test_worker_kill_resume.py`, `chaos/test_worker_sigstop.py` |
| Opt-out com posse humana | `privacy_gate` antes do desvio por posse | `chaos/test_optout_while_human.py` |
| Envio ambíguo sem eco | Reconciliação por eco e `/conversations`; nunca reenvio às cegas | `chaos/test_send_timeout_reconcile.py` |
| Lote novo entre bolhas | `send_epoch` separado do `run_epoch` | `chaos/test_new_batch_between_bubbles.py` |
| Postgres fora > 1 h | Spool local no `api` + replay | `chaos/test_postgres_down_spool.py` |
| Timeout ambíguo no envio | `unknown` + reconciliação por eco | `chaos/test_send_timeout_reconcile.py` |
| Item velho no outbox | `max_staleness` | `unit/test_dispatcher_staleness.py` |
| OpenRouter lento ou fora | Timeout, retry, fallback, breaker, msg de espera, handoff | `chaos/test_openrouter_down.py` |
| Orçamento esgotado | Degradação | `integration/test_budget_exhausted.py` |
| Redis fora | Fallbacks | `chaos/test_redis_down.py` |
| Postgres fora | 503 + reentrega da Meta | `chaos/test_postgres_down.py` |
| Token inválido | Kill switch + reconexão | `integration/test_token_invalid.py` |
| Post viral | Buckets, prioridade, backlog | `chaos/test_viral_post.py` |
| DLQ | Alerta e reprocessamento | `integration/test_dlq_reprocess.py` |
| Kill switch | Cache de 5 s; checagem por bolha | `chaos/test_kill_switch_mid_send.py` |

## Humano, follow-ups, harness, observabilidade, lab, compliance (§11–§16)

| Req. | Componente | Teste |
| --- | --- | --- |
| 11 Posse explícita; checagem no início e no despachante | `orchestrator`, despachante | `chaos/test_seller_during_generation.py` |
| 11 Gatilhos de handoff; aviso com mínimo de PII; SLA | `handoff`, `WhatsAppCloudNotifier` | `unit/test_handoff_summary_no_pii.py`, `integration/test_sla_breach.py` |
| 11 Volta ao bot explícita | console + botão assinado | `e2e/test_return_to_bot.py` |
| 12 Cadências conscientes de janela, horário e opt-out | `followups.engine`, despachante | `09` §5 |
| 12 Importação com LGPD e supressão (fase 4) | `imports` | `integration/test_import_suppression.py` |
| 13 Config versionada, dupla aprovação, canário, rollback | `config.registry` | `integration/test_config_canary_rollback.py` |
| 13 Orçamento total, diário e por conversa; trava do OpenRouter | `models.gateway`, ADR-011 | `unit/test_budget_reserve_parallel.py` |
| 13 Threat model, RLS, RBAC, segredos | `08` §7 | `integration/test_rls_isolation.py`, `unit/test_rbac.py` |
| 14 Trace por turno sem PII; métricas; alertas | `observability` | `unit/test_trace_masking.py` |
| 14 Dataset, graders, juiz calibrado, pass^k, gate | `lab`, CI | Gate do `10` §2.3 |
| 15 Lab: variantes como config, placar, replay, promoção | `lab.runner` + console | `e2e/test_lab_experiment.py` |
| 16 Regras de consórcio, seguro, LGPD e Meta | pacote + núcleo + guard | `eval/compliance-v1` (1 cenário por regra) |
| 2.1 Núcleo agnóstico + pacote fictício | `packages.loader` | `contract/test_core_is_segment_agnostic.py`, `contract/test_segment_package.py[auto_insurance]` |
