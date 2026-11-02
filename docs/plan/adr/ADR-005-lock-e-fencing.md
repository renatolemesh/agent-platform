# ADR-005 — Uma execução por conversa: lock e fencing

- **Status:** aceito · **Prioridades afetadas:** 2, 3

## Contexto
Cada conversa pode ter no máximo um run ativo. Uma mensagem nova pode cancelar o run, e o vendedor pode assumir no meio. O Redlock não fornece fencing token (Kleppmann). Advisory locks de sessão não funcionam com PgBouncer em modo transaction.

## Opções
1. Lock no Redis com TTL + fencing token.
2. Advisory lock de sessão do Postgres.
3. Serialização pela fila (`lock` do procrastinate) + `run_epoch` monotônico na conversa, verificado no próprio Postgres, + `pg_advisory_xact_lock` no commit.

## Decisão
Opção 3, com **dois epochs** e um lease por turno (revisado após a revisão independente, achados #5 e #6):
- **`run_epoch`** protege o commit. É incrementado quando um run **não comitado** é cancelado (lote novo, mudança de posse), sempre sob `pg_advisory_xact_lock(hashtext(conversation_id))`. O `commit`, sob o mesmo lock, só grava se `run_epoch = :epoch` **e** `turns.status = 'running' and lease_id = :lease`.
- **`send_epoch`** protege o envio. É incrementado só por mudança de posse, kill switch e opt-out. O despachante envia apenas itens com o `send_epoch` atual, então um lote novo depois do commit não corta as bolhas restantes da resposta anterior.
- **Lease por turno:** cada execução (inclusive a retomada de job stalled pelo procrastinate) gera um `lease_id` novo; um executor antigo que acorda perde o CAS. Não há executor fora do job.
- Cada turno tem seu próprio thread no LangGraph (`tenant_id:conversation_id:turn_id`).

## Consequências
- (+) Correção mesmo com GC pause, worker zumbi, SIGSTOP ou Redis fora.
- (−) O trabalho de LLM de um run cancelado é perdido; o custo fica registrado como `cancelled`.
- Testes obrigatórios: as intercalações (a)–(j) do `07` §3.
