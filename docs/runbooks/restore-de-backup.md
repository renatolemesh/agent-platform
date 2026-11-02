# Restore de backup (wal-g)

**Como o backup funciona:** o Postgres arquiva cada segmento de WAL com `wal-g wal-push` (`archive_timeout=300`, ou seja, RPO ≤ 5 min) e o sidecar `backup` faz um base backup diário (`BACKUP_HOUR_UTC`), aplica a retenção (`WALG_RETAIN_FULL`, padrão 14) e, aos domingos, um `pg_dump -Fc` no volume `pg_dumps`. Tudo vai cifrado com libsodium (`secrets/walg_libsodium_key`) para o bucket S3 (`WALG_S3_PREFIX`). O Redis não tem backup (nada nele é fonte da verdade).

## Teste mensal de restore (obrigatório)

```bash
scripts/backup/restore_test.sh          # último backup + replay de todo o WAL, num container descartável
```

Ele sobe `ap-restoretest-*`, restaura, espera a recuperação terminar, confere a migração `alembic_version`, conta as tabelas críticas e roda `pg_amcheck`. Imprime uma linha JSON (`"restore_test":"ok"`) e apaga tudo. Para exigir mínimos, use `EXPECT_FILE` (linhas `tabela<TAB>mínimo`). O ciclo completo (backup cifrado → WAL → restore → PITR) também é verificado localmente por `scripts/backup/selftest.sh`.

Registre o resultado (data, segundos, contagens) no log de operação. Se falhar: trate como incidente (o backup não está garantido).

O cron instalado por `scripts/deploy/install-restore-cron.sh` roda `scripts/backup/restore_test_monthly.sh` no dia 3 de cada mês, com log em `/var/log/agentplatform-restore-test.log`. O resultado fica na tabela `restore_tests`, só com contagens, e a plataforma alerta quando o último teste falhou (`restore_test_failed`) ou quando não há teste bom em `operations.alerts.restore_test_max_age_days` dias (`restore_test_overdue`), inclusive se o cron parar.

## Sintomas que pedem restore

- Corrupção (`pg_amcheck`, erros de página no log), disco perdido, `DROP`/`DELETE` acidental.
- O backup deixou de rodar: healthcheck do `backup` `unhealthy` (heartbeat com mais de 26 h) ou `pg_stat_archiver.failed_count` crescendo.

## Diagnóstico

```bash
$C exec postgres psql -U agentplatform -d agentplatform -c "select archived_count, failed_count, last_archived_wal, last_failed_wal, last_failed_time from pg_stat_archiver"
$C exec backup walg-env backup-list
$C logs --since 1h postgres | grep -i "archive"
df -h /var/lib/docker
```

Arquivamento falhando faz o `pg_wal` crescer até encher o disco: resolva antes (credencial S3, bucket, rede `egress`).

## Ação: restore em produção

1. **Pare só os workers** (o `api` continua recebendo webhooks e gravando no spool se o banco sumir; a Meta desinscreve o app depois de cerca de 1 h de falhas):
   ```bash
   $C stop worker worker-bg backup
   ```
2. Ligue o kill switch global se houver risco de o bot agir sobre dados velhos ([kill-switch.md](kill-switch.md)).
3. Escolha o ponto: `LATEST` ou um horário (PITR) **antes** do incidente. Ensaie primeiro num container descartável:
   ```bash
   TARGET_TIME="2026-10-10 14:55:00-03" KEEP=1 scripts/backup/restore_test.sh   # confira os dados no container mantido
   ```
4. Restaure no volume de produção:
   ```bash
   $C stop api
   $C rm -sf postgres backup       # o volume só pode ser removido sem nenhum container ligado a ele
   docker volume create agentplatform_pg_data_quebrado                  # cópia do que havia, antes de apagar
   docker run --rm -v agentplatform_pg_data:/de:ro -v agentplatform_pg_data_quebrado:/para alpine sh -c 'cp -a /de/. /para/'
   docker volume rm agentplatform_pg_data && docker volume create agentplatform_pg_data
   docker run --rm --env-file prod.env -v agentplatform_pg_data:/var/lib/postgresql/data -v "$PWD/secrets:/run/secrets:ro" \
     --entrypoint sh agentplatform-postgres:pg17-walg-v3.0.9 -c '
       chown postgres:postgres /var/lib/postgresql/data && chmod 700 /var/lib/postgresql/data &&
       gosu postgres walg-env backup-fetch /var/lib/postgresql/data LATEST &&
       echo "restore_command = '"'"'/usr/local/bin/walg-env wal-fetch %f %p'"'"'" >> /var/lib/postgresql/data/postgresql.auto.conf &&
       gosu postgres touch /var/lib/postgresql/data/recovery.signal'
   # PITR: acrescente também recovery_target_time e recovery_target_action = 'promote'
   $C up -d postgres && $C logs -f postgres     # espere "database system is ready to accept connections"
   ```
5. Depois da recuperação, retire o `restore_command` do `postgresql.auto.conf` (`ALTER SYSTEM RESET restore_command;`) e confirme `select pg_is_in_recovery()` = `f`.
6. Suba o resto: `$C up -d`. Reprocesse o spool ([spool-crescendo.md](spool-crescendo.md)) e reconcilie o que a Meta mandou durante a janela (conversas recentes via `/conversations`, comentários via `/{media-id}/comments`), e confira `GET /{page}/subscribed_apps`.

## Verificação

- `scripts/backup/restore_test.sh` volta a passar com o backup novo (rode `walg-env backup-push` manualmente: `$C exec backup walg-backup-loop --now`).
- `pg_stat_archiver.failed_count` parado; `backup-list` com um backup de hoje.
- Contagens de `messages`/`leads` coerentes com o ponto escolhido; outbox sem itens `sending` órfãos (o despachante reconcilia os `unknown`).
