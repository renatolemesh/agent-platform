# Fila travada / DLQ

**Peças:** a fila é o procrastinate no Postgres (`procrastinate_jobs`). O `worker` atende `turns, comments, media, dispatch, echo, ingress`; o `worker-bg` atende `followups, ingest, lab, maintenance` e roda os periódicos (`periodic_minute`: varredura de entrada, ticks dos tenants, alertas, sweeper de orçamento, retry de jobs travados; `periodic_daily` às 03:30 UTC: retenção LGPD e `token_guard`). Jobs que esgotam as tentativas ficam `failed`; falhas de negócio vão para `dead_letters`. Argumentos de job só levam IDs, então reprocessar é seguro: cada job relê o estado e as cercas (`run_epoch`, `send_epoch`, lease do turno) impedem efeito duplicado.

## Sintomas

- Alertas `inbound_backlog:<tenant>`, `outbox_stuck:<tenant>`, `turn_failures:<tenant>`, `jobs_failed`, `dead_letters`.
- Cliente sem resposta; console mostra `inbound_without_reply`.

## Diagnóstico

```bash
$C ps worker worker-bg                                          # healthy? reiniciando (OOM)?
$C logs --since 30m worker | grep -iE "error|exception|stalled" | tail -50
P="$C exec postgres psql -U agentplatform -d agentplatform -c"
$P "select queue_name, status, count(*) from procrastinate_jobs where status in ('todo','doing','failed') group by 1, 2 order by 1, 2"
$P "select id, task_name, queue_name, attempts, scheduled_at from procrastinate_jobs where status = 'doing' order by id limit 20"
$P "select id, task_name, args, attempts from procrastinate_jobs where status = 'failed' order by id desc limit 20"
$P "select source, ref, left(error, 120), at from dead_letters where reprocessed_at is null order by at desc limit 20"
$P "select tenant_id, status, cancel_reason, count(*) from outbox where created_at > now() - interval '1 hour' group by 1, 2, 3"
$P "select status, count(*) from inbound_events where received_at > now() - interval '1 hour' group by 1"
```

Leitura rápida:
- Muitos `todo` antigos e nenhum `doing`: worker parado ou sem conexão. Veja `$C logs worker`.
- `doing` antigos: worker preso. O `retry_stalled` reenfileira jobs sem heartbeat há mais de 60 s; se repetir, reinicie.
- `outbox` em `unknown`: envio ambíguo em reconciliação (eco → API de conversas → `failed` + handoff). **Não reenvie à mão.**
- `outbox` cancelado por `kill_switch`, `window_closed`, `stale`: comportamento esperado, não é fila travada.

## Ação

1. Worker preso ou sem memória: `$C restart worker` (o `stop_grace_period` de 45 s deixa os turnos em curso terminarem; o lease do turno impede commit duplo).
2. Corrija a causa raiz (erro no log, credencial, migração faltando) **antes** de reprocessar.
3. Reprocessar jobs `failed` (um a um, ou em lote depois de corrigir):
   ```bash
   $P "select procrastinate_retry_job_v2(<id>, now(), null, null, null)"
   $P "select procrastinate_retry_job_v2(id, now(), null, null, null) from procrastinate_jobs where status = 'failed' and task_name = 'run_turn'"
   ```
   Ou pelo shell interativo: `$C exec worker sh /usr/local/bin/secrets-entrypoint.sh procrastinate --app=agentplatform.jobs.app.app shell` → `list_jobs status=failed`, `retry <id>`.
4. Eventos de entrada parados (`inbound_events` em `pending`/`failed`): o `sweep_inbound` do periódico os reenfileira. Para forçar, reinicie o `worker-bg`.
5. Dead letters: depois de corrigir, reprocesse pelo console e marque `reprocessed_at`.

## Verificação

- `procrastinate_jobs` sem `failed` novos e `todo` drenando; `inbound_events` recentes `done`.
- Conversas afetadas receberam resposta ou foram para handoff. As mensagens que ficaram velhas são canceladas por design (`max_staleness_s`): avise o vendedor para retomar essas conversas.
