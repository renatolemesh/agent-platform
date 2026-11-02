# Postgres ou Redis fora

**O que depende de cada um:** o Postgres é a fonte da verdade (dados, fila, checkpoints, outbox, orçamento). Sem ele, o `api` só grava no spool e nada é processado nem enviado. O Redis guarda **apenas** rate limit (`noeviction`, sem persistência). Sem ele, o despachante usa limitador local por processo (mais conservador) e nada se perde.

## Sintomas

- `GET /health` → 503 `{"ok":false,...}`; healthcheck do `api` falhando; workers reiniciando com erro de conexão.
- Alerta `spool`; logs com `connection refused`, `too many connections`, `could not extend file` (disco).
- Redis fora: avisos de fallback do rate limiter no log; envios um pouco mais lentos.

## Diagnóstico

```bash
$C ps postgres redis
$C logs --since 30m postgres | tail -100
df -h /var/lib/docker && free -m
$C exec postgres psql -U agentplatform -d agentplatform -c "select count(*), state from pg_stat_activity group by 2"
$C exec postgres psql -U agentplatform -d agentplatform -c "select archived_count, failed_count from pg_stat_archiver"
$C exec redis sh -c 'REDISCLI_AUTH="$(cat /run/secrets/redis_password)" redis-cli info memory | grep used_memory_human'
```

Causas frequentes:
- **Disco cheio** por WAL não arquivado (arquivamento falhando, ver [restore-de-backup.md](restore-de-backup.md)) ou por logs.
- **OOM** do container (`mem_limit` de 1 GB): veja `docker inspect agentplatform-postgres-1 --format '{{.State.OOMKilled}}'`.
- **Conexões esgotadas** (`max_connections=60`; cada processo usa um pool de até 10).

## Ação

1. O `api` continua no ar (spool). Não derrube o `api`.
2. Disco: libere espaço (`docker image prune -a` com cuidado, `journalctl --vacuum-size=200M`); se o `pg_wal` está enorme, corrija o arquivamento (credencial S3) e deixe o Postgres drenar. **Nunca** apague arquivos de `pg_wal` à mão.
3. Reinicie: `$C up -d postgres` e espere `healthy`; depois `$C restart worker worker-bg`.
4. Corrupção ou perda do volume: [restore-de-backup.md](restore-de-backup.md).
5. Redis: `$C up -d redis`. Os buckets recomeçam vazios (o primeiro envio pode esperar até 2 s); não há dado a recuperar.
6. Depois: reprocesse o spool ([spool-crescendo.md](spool-crescendo.md)).

## Verificação

- `/health` 200 com `spool_bytes: 0`; `$C ps` todos `healthy`.
- `procrastinate_jobs` drenando ([fila-travada-dlq.md](fila-travada-dlq.md)); outbox sem `sending` velho.
