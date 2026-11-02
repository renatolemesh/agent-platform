# ADR-006 — Banco vetorial e busca híbrida

- **Status:** aceito

## Contexto
Menos de 100 mil chunks no horizonte de 12 meses, VPS de 4 GB, conteúdo em português, metadados e filtros por tenant, vigência e status.

## Critérios
| Critério | pgvector 0.8.7 | Qdrant 1.19 |
| --- | --- | --- |
| RAM extra | ~0 (usa o Postgres) | +1 processo; ~0,6–0,9 GB para 100k × 1024–1536 dims |
| Consistência com os dados de negócio | Transacional | Eventual (dois stores) |
| Busca lexical em português | `tsvector` `portuguese` + `unaccent`, maduros | BM25 nativo (cobertura de PT a verificar) |
| Filtro + HNSW | `iterative_scan` resolve filtro que devolve menos de k | Nativo |
| Backup | Um só | Snapshot separado |
| Multi-tenant | RLS | Payload `is_tenant` |

## Decisão
pgvector no Postgres:
- HNSW por índice versionado (expressão `embedding::vector(N)` com `WHERE index_id`).
- `halfvec` se as dimensões passarem de 2.000.
- FTS `pt_unaccent` e fusão RRF (k=60) em SQL.
- Acesso sempre pela interface `VectorStore`.
- Rerank opcional via API (Voyage `rerank-2.5`), decidido no laboratório. Reranker local está fora: não cabe em 2 vCPU/4 GB.

## Consequências
- (+) Uma transação, um backup e RLS.
- (−) Se passar de ~1M chunks ou a latência da busca subir, rever (adaptador Qdrant).
