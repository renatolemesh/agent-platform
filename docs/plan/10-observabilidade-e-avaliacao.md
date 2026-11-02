# 10 — Observabilidade, avaliação e laboratório de experimentos

Nenhuma mudança de prompt, modelo, config ou código chega à produção sem passar por um conjunto de avaliação automatizado, e toda conversa real é rastreável turno a turno. O laboratório usa a mesma base para comparar variantes.

## 1. Observabilidade (ADR-007)

**Decisão:** OpenTelemetry (SDK Python 1.45.x) + **tabelas próprias no Postgres** como fonte da verdade de trace de negócio e de custo (`turns`, `llm_calls`, `tool_calls`, `guard_results`, `ownership_events`, decisões do despachante). Langfuse self-hosted está fora (pede 4 cores/16 GiB e ClickHouse, incompatível com a VPS de 4 GB). **Langfuse Cloud Hobby** (50 mil unidades/mês, 30 dias) é opcional, só para debug de prompts, com **amostragem** (`operations.trace_sampling`, padrão 0,2) e **texto mascarado**. Com cerca de 150 mil unidades/mês estimadas, sem amostragem a cota acabaria em dias.

- **Trace por turno:** `trace_id` = `turn_id`; spans por nó com atributos `conversation_id`, `tenant_id`, `node`, `model`, `prompt_version`, `tokens_in/out`, `cost_usd`, `latency_ms`, `guard.verdict`, `dispatch.decision`. As convenções GenAI do OTel ainda estão em "Development": usamos os nomes `gen_ai.*` atuais, num módulo único fácil de renomear.
- **Exportação:** OTLP para um coletor leve (`otel-collector` com `memory_limiter`) → Prometheus (métricas) + Loki (logs) + Grafana. No perfil `prod-min`, a observabilidade roda com ≈ 400 MB (Prometheus com retenção de 15 dias, Loki com 7 dias). Se a VPS apertar, o coletor manda para o Grafana Cloud free tier.
- **Visualização de trace de conversa:** página do console "Conversa → turnos" lida das tabelas (contexto montado mascarado, diretrizes, trechos, chamadas, guard e decisões), suficiente para depurar sem ferramenta externa.

**Métricas (Prometheus):**

| Métrica | Tipo | Labels |
| --- | --- | --- |
| `turn_latency_seconds` | histogram | tenant, outcome |
| `node_latency_seconds` | histogram | node, model |
| `queue_lag_seconds` | gauge | queue |
| `llm_cost_usd_total` | counter | tenant, node, model |
| `llm_fallbacks_total` / `llm_errors_total` | counter | model, reason |
| `guard_blocks_total` | counter | guard, rule |
| `dispatch_cancels_total` | counter | reason |
| `handoffs_total` | counter | reason |
| `channel_rate_limited_total` | counter | channel, bucket |
| `optouts_total` | counter | channel |
| `webhook_events_total` / `webhook_last_received_timestamp` | counter/gauge | channel, type |
| `budget_used_ratio` | gauge | tenant, scope |

**Alertas:** fila `turns` com atraso > 30 s; p95 do turno > 20 s por 10 min; erro de canal (190, 80002, 368); nenhum webhook em 30 min no horário humano; custo diário > 80%; salto de handoffs ou bloqueios (> 3σ da média de 7 dias); DLQ > 0; disco > 80%; backup falhou.

**Dashboards de negócio** (Grafana sobre views SQL): funil por estágio; conversão por origem (post, anúncio, campanha); taxa de resposta por variante de private reply; lacunas da base (curadoria); custo por conversa e por lead qualificado.

## 2. Avaliação

Metodologia: datasets pequenos que crescem a partir de falhas reais; graders determinísticos primeiro; LLM-juiz calibrado; **pass^k** para confiabilidade; gate em CI.

### 2.1 Datasets iniciais (versionados em `evals/datasets/`, metadados em `eval_datasets`)

| Dataset | Tamanho inicial | Conteúdo | Fonte |
| --- | --- | --- | --- |
| `core/input-guard-v1` | 80 | opt-out (inteiro × no meio da frase), injection, abuso, PII, pedido de humano | Escrito à mão |
| `consortium/understand-v1` | 50 | Mensagens rotuladas com slots, intenções, objeções e estágio | Escrito com base no material + revisão |
| `consortium/rag-v1` | 60 | Pergunta, resposta esperada, trechos relevantes (spans), 10 sem resposta na base | Material do `06` |
| `consortium/respond-v1` | 40 | Contexto + resposta de referência + rubrica | Escrito à mão |
| `compliance-v1` | 45 | Um caso por regra do `11` (adversariais + "respostas boas" para medir falso positivo) | `11` |
| `consortium/scenarios-v1` | 20 | Cenários multi-turno com persona e objetivo oculto | `13` §2.10 |
| `core/concurrency-v1` | 12 | Corridas e caos do `07` §9 | `07` |
| `auto_insurance/*` | 10 | Pacote fictício (prova de abstração) | `13` §3 |

### 2.2 Graders

**Determinísticos** (pytest, sem custo): frases proibidas; números com proveniência; uma pergunta por mensagem; bytes por bolha; idioma pt-BR; ferramentas chamadas e permitidas; estágio final e slots esperados; handoff na hora certa; nada enviado com posse humana; PII ausente nos traces.

**LLM-juiz** (DeepEval `GEval` ou rubrica própria): uma rubrica por dimensão (correção factual em relação aos trechos, tom, clareza, aderência ao papel, retenção de informação), modelo de **outra família** com versão fixa, **calibrado** contra 50 rótulos humanos (concordância ≥ 0,8 com Cohen's κ ≥ 0,6 antes de valer no gate).

**Conversa:** DeepEval `ConversationSimulator` (persona + `expected_outcome`) chamando `run_conversation` (`04` §10) com o grafo real e o `FakeChannelAdapter`. A nota sai do **estado final** (estágio, slots, pré-cadastro, handoff, opt-out), não só do texto. Métricas `RoleAdherence`, `KnowledgeRetention`, `ConversationCompleteness` e `TopicAdherence` (Ragas) como sinais auxiliares.

### 2.3 Gate de CI e de publicação

| Métrica | Gate |
| --- | --- |
| Violação de compliance (`compliance-v1` adversarial) | 100% bloqueado; **tolerância zero** |
| Falso positivo do guard (respostas boas) | ≤ 5% |
| Opt-out recall | 100% nas mensagens inteiras; ≥ 95% no geral |
| Injection: nenhuma mudança de regra ou ferramenta proibida | 100% |
| F1 de slots (`understand-v1`) | ≥ baseline − 0,02 |
| Acurácia de estágio | ≥ baseline − 0,03 |
| RAG recall@4 | ≥ baseline − 0,03; `no_basis` correto ≥ 90% |
| Cenários: pass^3 | ≥ 0,85 (MVP), sem regressão em cenário crítico |
| Concorrência e caos | 100% |
| Custo por mil turnos | ≤ 120% do baseline (alerta) |

Regressão em métrica crítica bloqueia merge (GitHub Actions) e bloqueia publicação de config no console (o mesmo runner roda do lado do servidor, com teto de custo).

**Dois níveis de gate, por custo (revisão #14):**

| Gate | Quando | Conteúdo | Custo |
| --- | --- | --- | --- |
| Rápido | Todo PR | Graders determinísticos, concorrência, caos e nós com LLM gravado (cassettes); compliance adversarial com o guard real | ≈ US$ 0 |
| Completo | Merge para `main`, publicação de config ou prompt, troca de modelo | Nós ao vivo, RAG, cenários pass^3, juiz | ≈ US$ 0,30–0,80 por rodada, dentro de `budget.ci_daily_cap_usd` |

Mudança de compliance que **só restringe** (frase proibida nova, divulgação nova) passa só no gate rápido + o conjunto de "respostas boas" (falso positivo), sem esperar o gate completo.

**Toda chamada de LLM da avaliação passa pelo `ModelGateway`** (orçamento, ZDR, registro de custo), com a chave e o escopo de orçamento do laboratório. Isso inclui o simulador e o GEval do DeepEval (via adaptador `DeepEvalBaseLLM`), as métricas do Ragas (via LLM wrapper) e o promptfoo (via provider Python). Nenhuma ferramenta usa um cliente de LLM próprio.

### 2.4 Em produção

- Amostra semanal de 20 conversas + todas com bloqueio do guard → fila de revisão humana no console (rótulos: ok, erro factual, tom, compliance, estágio errado).
- Feedback do vendedor (botão "resposta ruim" no console) → cenário novo.
- **Toda falha vira caso de dataset** (botão "adicionar ao dataset", com anonimização).

## 3. Laboratório de experimentos

Objetivo: testar um modelo, uma versão de prompt ou uma estratégia de RAG em minutos, com placar de qualidade × custo × latência.

### 3.1 Componentes

- **Runner** (`lab/runner.py`): recebe `dataset@versão`, lista de **variantes** (cada uma é um override de `PlatformConfig`, nunca código), métricas e teto de custo; estima o custo antes (tokens × preço), pede confirmação acima de US$ 0,50 e roda em paralelo (`asyncio.Semaphore`) chamando `run_node`/`run_turn`/`run_conversation` em sandbox. Registra `experiment_id`, versão do dataset, hash da config, commit, modelos e data.
- **Placar** (console e CLI): variantes × métricas, custo por mil turnos, p50/p95, taxa de erro, fronteira de Pareto qualidade × custo, baseline = produção atual, diff lado a lado das respostas e intervalo de confiança (bootstrap, n repetições).
- **Replay:** reexecuta uma conversa real anonimizada com outra config e mostra, turno a turno, onde as decisões mudam (estágio, ferramenta, guard, texto).
- **Promoção:** variante vencedora → nova versão de config → gate → canário → monitorada → rollback.
- **Teto:** chave do OpenRouter separada para o lab, com limite próprio; `budget.experiment_cap_usd` por experimento.

Tabelas: `eval_datasets(id, name, version, path, sha256)`, `eval_cases(dataset_id, case_id, input, expected, tags)`, `experiments(id, dataset_id, variants, metrics, status, cost_estimate, cost_actual, commit, created_by)`, `eval_results(experiment_id, variant, case_id, rep, outputs, scores, cost_usd, latency_ms)`.

### 3.2 Três níveis de teste

| Nível | Pergunta | Métricas | Custo típico (por variante) |
| --- | --- | --- | --- |
| Nó | Cada chamada faz bem o seu trabalho? | `input_guard`: P/R de opt-out e injection; `analyze_turn`: F1 de slots, acurácia e matriz de confusão de estágio; `respond`: rubrica; `output_guard`: captura × falso positivo | 50 casos × 3 reps ≈ US$ 0,01–0,05 |
| RAG | A base certa chega e a resposta se apoia nela? | recall@k, MRR, nDCG, IoU/precisão/recall por token (chunking), fidelidade e correção (só finalistas), `no_basis` | Recuperação: ≈ US$ 0 (só embeddings) |
| Conversa | O agente fica no trilho? | Conclusão do objetivo, papel e tópico, retenção, estágio certo por turno, turnos até qualificar, repetição, handoff na hora, pass^k | 20 cenários × 3 reps ≈ US$ 0,30–0,80 |

### 3.3 Custo e qualidade

- **Varredura de modelos por nó:** para cada nó, roda o dataset do nó em 5–8 modelos ZDR do OpenRouter e fica com o mais barato que bate a meta; repete a cada trimestre ou quando aparecem modelos novos. Primeira varredura (fase 2): `analyze_turn` ∈ {gemini-2.5-flash-lite, gpt-5-nano, mistral-small-2603, deepseek-v4-flash, gpt-oss-120b}; `respond` ∈ {deepseek-v4-flash, gemini-3.1-flash-lite, gpt-5-mini, llama-4-maverick, claude-haiku-4.5 como teto de qualidade}.
- **Otimização de prompt (fase 3, quando houver verba):** DSPy 3.4 + GEPA nos nós de saída estruturada (`analyze_turn`, guards), com modelo maior só na reflexão; o prompt otimizado volta ao registro como nova versão e passa pelo gate. O adaptador `gepa[langchain]` cobre grafos LangGraph.
- **Contexto enxuto:** ablação de blocos (resumo, histórico, diretrizes, trechos) medida no nível de conversa.
- **Few-shot dinâmico:** exemplos aprovados escolhidos por similaridade (pgvector) com o turno atual.
- **Cache de prompt:** prefixo estável primeiro; medir `cached_tokens` em `llm_calls`.
- **Sem overfitting:** split `dev`/`test` fixo por dataset; decisão só no `test`.

### 3.4 Experimentos de RAG

- Matriz: chunking (fixo 300/500, recursivo por títulos, por seção, pai-filho), embedding (`qwen3-embedding-8b`@1024, `bge-m3`, `text-embedding-3-small`, `gemini-embedding-001`), peso FTS × vetor, top-k, rerank (Voyage `rerank-2.5`, com 200M tokens grátis, × sem rerank), reescrita da consulta.
- **Contextual retrieval:** prefixo de contexto por chunk gerado por LLM barato na indexação. A Anthropic reporta 35% menos falhas no top-20 com embeddings contextuais, 49% com embeddings + BM25 contextuais e 67% com rerank. Medir no nosso dataset; custo único de indexação ≈ centavos.
- **Sem RAG como variante:** uma base aprovada típica (material educativo + FAQs) deve ficar bem abaixo de 200 mil tokens; testar "base inteira no prompt com cache" no `respond` contra RAG, olhando qualidade e custo.
- **Índices versionados:** cada estratégia gera um `kb_indexes` próprio; trocar em produção = mudar `rag.active_index_id`.

### 3.5 Ferramentas (ADR-008)

| Ferramenta | Papel | Por quê |
| --- | --- | --- |
| Runner próprio + pytest | Núcleo do gate e do lab | Chama os nós reais (`run_node`/`run_conversation`); sem cópia do agente |
| promptfoo 0.124 (MIT) | Matriz prompt × modelo por nó em YAML, red team | Provider `openrouter:` nativo; usado via provider Python que chama `run_node` |
| DeepEval 4.2 | Simulador de conversa e métricas multi-turno | `ConversationSimulator` + `RoleAdherence`, `KnowledgeRetention` |
| Ragas 0.4 | Métricas de RAG (context precision/recall, faithfulness) | Só nas finalistas (cadência de release baixa em 2026: dependência opcional) |
| DSPy 3.4 + GEPA | Otimização de prompt | Fase 3 |
| Langfuse Cloud (opcional) | Debug de prompts com amostragem | Sem PII |

Regra: **nenhuma ferramenta roda uma cópia do agente**; todas passam pela interface `run_node`/`run_turn`/`run_conversation`.

### 3.6 Rigor

Repetições (k ≥ 3) e intervalo de confiança; diferença menor que o ruído não decide; juiz com versão fixa, de outra família, calibrado; todo experimento reprodutível pelo `experiment_id` (dataset@versão + hash da config + commit).
