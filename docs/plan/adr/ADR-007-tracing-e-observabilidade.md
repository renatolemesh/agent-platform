# ADR-007 — Tracing e observabilidade

- **Status:** aceito

## Contexto
Orçamento mínimo, VPS de 4 GB, nenhuma PII em traces. São cerca de 800 turnos/dia, o que dá ~150 mil unidades/mês no Langfuse.

## Opções
1. Langfuse self-hosted v4 (Postgres + ClickHouse + Redis + S3; recomenda 4 cores e 16 GiB).
2. Langfuse Cloud Hobby (50 mil unidades/mês, 30 dias) ou Core (US$ 29/mês).
3. OTel + tabelas próprias no Postgres + Prometheus/Loki/Grafana.
4. Arize Phoenix (container único; licença Elastic-2.0; ~2–3 GB).

## Decisão
A opção 3 é a base. As tabelas `turns`, `llm_calls` e afins já são necessárias para orçamento e auditoria. A opção 2 entra como complemento opcional, no Hobby, com amostragem de 20% e texto mascarado, para depurar prompts. A 1 foi descartada pela memória; a 4, pela licença e pela RAM.

## Consequências
- (+) Custo zero e dados sob nosso controle; o console mostra o trace de cada turno.
- (−) Menos UI pronta para prompts; o laboratório próprio cobre os experimentos.
