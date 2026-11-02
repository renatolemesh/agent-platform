# ADR-008 — Ferramentas de avaliação e otimização

- **Status:** aceito

## Contexto
O gate de CI e o laboratório precisam testar **os nós reais**, com custo baixo e preferência por open source.

## Opções (versões em 09/10/2026)
- Runner próprio + pytest.
- promptfoo 0.124 (MIT, parte da OpenAI desde mar/2026; segue open source).
- DeepEval 4.2 (simulador de conversa e métricas multi-turno).
- Ragas 0.4 (métricas de RAG; último release em jan/2026).
- DSPy 3.4 + GEPA (otimização de prompt).
- AutoRAG (busca automática de combinação de RAG).
- Langfuse experiments.

## Decisão
- **Núcleo:** runner próprio com `run_node`/`run_turn`/`run_conversation` + graders em pytest. É o que roda no gate.
- **promptfoo** para matrizes prompt × modelo e red team, via provider Python que chama `run_node`.
- **DeepEval** para o simulador de cliente e as métricas `RoleAdherence`, `KnowledgeRetention` e `ConversationCompleteness`.
- **Ragas** opcional, só nas finalistas de RAG.
- **DSPy/GEPA** na fase 3, quando houver verba.
- **AutoRAG** descartado: a matriz própria basta.

**Toda chamada de LLM das ferramentas de avaliação passa pelo `ModelGateway`** (adaptador `DeepEvalBaseLLM`, wrapper de LLM do Ragas e provider Python do promptfoo), com a chave do laboratório e o escopo de orçamento `ci:`/`experiment:`. Gate em dois níveis: rápido (determinístico + cassettes) em todo PR; completo (ao vivo, pass^3) no merge e na publicação de config.

## Consequências
- (+) O que é testado é o que vai para produção; o custo fica sob controle, com ZDR também na avaliação.
- (−) Integração das ferramentas com a interface `run_*`, tarefa da fase 1.
