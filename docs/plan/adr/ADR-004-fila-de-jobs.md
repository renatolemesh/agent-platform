# ADR-004 — Fila de jobs

- **Status:** aceito · **Prioridades afetadas:** 3

## Contexto
Requisitos: ordem por conversa, retry com backoff, DLQ, jobs agendados e periódicos, visibilidade e suporte a async. A VPS tem 4 GB, e o Postgres já é a fonte da verdade.

## Opções (versões em 09/10/2026)
| Opção | Serialização por chave | Retry | DLQ | Agendados | Observação |
| --- | --- | --- | --- | --- | --- |
| procrastinate 3.10 (Postgres) | **Sim** (`lock`, `queueing_lock`) | `RetryStrategy` exponencial | Jobs `failed` consultáveis | `schedule_at`, `@periodic` | Zero RAM extra |
| pgqueuer 1.5 | Parcial (por entrypoint) | Via executor | Não | Cron | |
| Tabela própria com `SKIP LOCKED` | A implementar | A implementar | A implementar | A implementar | Mais código |
| Redis Streams | Não (exige particionar) | Manual | Manual | Não | Estado crítico no Redis |
| arq 0.28 | Não | Sim | Não | Sim | Em modo só manutenção |
| Taskiq 0.13 / Dramatiq 2.2 / Celery 5.6 | Não | Sim | Só o Dramatiq | Sim | Broker extra |

## Decisão
**procrastinate:**
- `run_turn` usa `lock=conversation_id` e `queueing_lock=conversation_id`.
- Filas por prioridade: `turns`, `comments`, `media`, `dispatch`, `echo`, `followups`, `campaigns`, `ingest`, `lab` e `maintenance`.
- `RetryStrategy` com backoff exponencial + jitter.
- Jobs `failed` são espelhados em `dead_letters`, com alerta.

**Cuidado documentado:** um job agendado que segura um `lock` bloqueia os seguintes até rodar. Por isso **só `run_turn` usa o lock da conversa**. Follow-ups, eco, mídia e despacho não usam; a correção deles vem do `send_epoch`, do advisory lock nas transições e das checagens do despachante. Um job de eco preso atrás de um turno longo atrasaria a pausa do bot.

**Jobs travados:** worker com heartbeat; job `doing` sem heartbeat é reenfileirado pelo retry de jobs stalled da biblioteca, com lease novo no turno (ADR-005). A API exata na 3.10 é confirmada no spike F1-Q-02.

**Argumentos dos jobs:** só IDs, nunca texto ou PII (as tabelas do procrastinate não têm RLS nem cifra).

## Consequências
- (+) Fila durável e transacional com os dados; escala com mais workers.
- (−) O Postgres faz papel de fila: monitorar `queue_lag` e bloat. O gatilho para separar está no `02` §6.
