#!/usr/bin/env bash
# Restore test (plan 12 §3): restores a wal-g backup (base + WAL replay) into a THROWAWAY container, waits for
# recovery to finish, runs sanity checks and removes everything. Never touches the running database.
#
# Usage (on the server, from the repo root):
#   scripts/backup/restore_test.sh                                  # LATEST backup, storage from docker/prod.env
#   BACKUP_NAME=base_000000010000000000000004 scripts/backup/restore_test.sh
#   TARGET_TIME="2026-10-10 03:00:00+00" scripts/backup/restore_test.sh   # point-in-time recovery
#   EXPECT_FILE=/tmp/expected.tsv scripts/backup/restore_test.sh   # "table<TAB>min_rows" lines; fail if fewer
#
# Env:
#   IMAGE        postgres+wal-g image (default agentplatform-postgres:pg17-walg-v3.0.9)
#   ENV_FILE     storage settings: WALG_S3_PREFIX/AWS_* or WALG_FILE_PREFIX (default docker/prod.env)
#   SECRETS_DIR  dir with walg_libsodium_key, walg_s3_access_key, walg_s3_secret_key (default docker/secrets)
#   EXTRA_DOCKER_ARGS  e.g. "-v ap-backuptest-store:/backups" when WALG_FILE_PREFIX points to a volume
#   EXPECTED_HEAD alembic revision that must be current (default: the head of migrations/versions)
#   KEEP=1       keep the restored container for inspection (prints its name)
# Exit code 0 = restore OK; prints one JSON summary line (no data, only counts) for the monitoring log.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE="${IMAGE:-agentplatform-postgres:pg17-walg-v3.0.9}"
ENV_FILE="${ENV_FILE:-$ROOT/docker/prod.env}"
SECRETS_DIR="${SECRETS_DIR:-$ROOT/docker/secrets}"
BACKUP_NAME="${BACKUP_NAME:-LATEST}"
TARGET_TIME="${TARGET_TIME:-}"
DB="${POSTGRES_DB:-agentplatform}"
DBUSER="${POSTGRES_USER:-agentplatform}"
NAME="${NAME_PREFIX:-ap-restoretest}-$(date -u +%Y%m%d%H%M%S)-$$"
VOL="${NAME}-data"
started=$(date -u +%s)

# Docker Desktop on Windows (Git Bash) needs Windows paths for bind mounts.
hostpath() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }
export MSYS_NO_PATHCONV=1

cleanup() {
  if [ "${KEEP:-0}" = "1" ]; then echo "kept: container $NAME, volume $VOL" >&2; return; fi
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker volume rm "$VOL" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if [ -z "${EXPECTED_HEAD:-}" ]; then
  # Alembic head = the revision no other revision points to as down_revision
  revs=$(sed -n 's/^revision *= *"\([^"]*\)".*/\1/p' "$ROOT"/migrations/versions/*.py | sort -u)
  downs=$(sed -n 's/^down_revision *= *"\([^"]*\)".*/\1/p' "$ROOT"/migrations/versions/*.py | sort -u)
  EXPECTED_HEAD=$(comm -23 <(printf '%s\n' "$revs") <(printf '%s\n' "$downs"))
  if [ -z "$EXPECTED_HEAD" ] || [ "$(printf '%s\n' "$EXPECTED_HEAD" | wc -l)" -ne 1 ]; then
    echo "multiple or no Alembic heads: $EXPECTED_HEAD (set EXPECTED_HEAD)" >&2; exit 2
  fi
fi

recovery_conf="restore_command = '/usr/local/bin/walg-env wal-fetch %f %p'"
[ -n "$TARGET_TIME" ] && recovery_conf="$recovery_conf
recovery_target_time = '$TARGET_TIME'
recovery_target_action = 'promote'"

docker volume create "$VOL" >/dev/null
# shellcheck disable=SC2086  # EXTRA_DOCKER_ARGS is intentionally word-split
docker run -d --name "$NAME" \
  --env-file "$(hostpath "$ENV_FILE")" \
  -e PGDATA=/var/lib/postgresql/data -e POSTGRES_PASSWORD=restore-test-only -e POSTGRES_HOST_AUTH_METHOD=trust \
  -e BACKUP_NAME="$BACKUP_NAME" -e RECOVERY_CONF="$recovery_conf" \
  -v "$VOL:/var/lib/postgresql/data" \
  -v "$(hostpath "$SECRETS_DIR"):/run/secrets:ro" \
  ${EXTRA_DOCKER_ARGS:-} \
  --entrypoint sh "$IMAGE" -c '
    set -eu
    chown postgres:postgres "$PGDATA"; chmod 0700 "$PGDATA"
    gosu postgres /usr/local/bin/walg-env backup-fetch "$PGDATA" "$BACKUP_NAME"
    printf "%s\n" "$RECOVERY_CONF" >> "$PGDATA/postgresql.auto.conf"
    gosu postgres touch "$PGDATA/recovery.signal"
    # archive_mode=off: the throwaway cluster must never push WAL into the production prefix
    exec docker-entrypoint.sh postgres -c archive_mode=off -c listen_addresses=localhost
  ' >/dev/null

echo "restoring $BACKUP_NAME into $NAME ..." >&2
psqlq() { docker exec "$NAME" psql -U "$DBUSER" -d "$DB" -v ON_ERROR_STOP=1 -At -c "$1"; }
deadline=$(( $(date -u +%s) + ${TIMEOUT_S:-900} ))
until docker exec "$NAME" pg_isready -q -U "$DBUSER" -d "$DB" 2>/dev/null && [ "$(psqlq 'select pg_is_in_recovery()' 2>/dev/null)" = "f" ]; do
  if ! docker ps -q --filter "name=^${NAME}$" | grep -q .; then
    docker logs --tail 40 "$NAME" >&2 || true
    echo '{"restore_test":"failed","reason":"container exited"}'; exit 1
  fi
  [ "$(date -u +%s)" -gt "$deadline" ] && { docker logs --tail 40 "$NAME" >&2; echo '{"restore_test":"failed","reason":"timeout"}'; exit 1; }
  sleep 2
done

fail=0
head=$(psqlq "select version_num from alembic_version")
[ "$head" = "$EXPECTED_HEAD" ] || { echo "alembic head $head != expected $EXPECTED_HEAD" >&2; fail=1; }

counts=""
for t in tenants channel_accounts contacts conversations messages leads outbox inbound_events handoffs config_versions audit_log procrastinate_jobs; do
  n=$(psqlq "select count(*) from $t" 2>/dev/null || echo "missing")
  [ "$n" = "missing" ] && { echo "table $t missing" >&2; fail=1; }
  counts="$counts\"$t\":\"$n\","
  if [ -n "${EXPECT_FILE:-}" ]; then
    min=$(awk -F'\t' -v t="$t" '$1==t {print $2}' "$EXPECT_FILE")
    if [ -n "$min" ] && [ "$n" != "missing" ] && [ "$n" -lt "$min" ]; then echo "$t: $n rows < expected $min" >&2; fail=1; fi
  fi
done

# structural check of every heap and btree index (amcheck ships with Postgres; created only in the throwaway copy)
psqlq "create extension if not exists amcheck" >/dev/null
if ! docker exec "$NAME" pg_amcheck -U "$DBUSER" -d "$DB" --heapallindexed --no-strict-names >/dev/null 2>"/tmp/${NAME}.amcheck"; then
  echo "pg_amcheck reported corruption:" >&2; cat "/tmp/${NAME}.amcheck" >&2; fail=1
fi
rm -f "/tmp/${NAME}.amcheck"

last_tx=$(psqlq "select coalesce(max(created_at)::text, '') from messages" 2>/dev/null || echo "")
status=$([ "$fail" = 0 ] && echo ok || echo failed)
echo "{\"restore_test\":\"$status\",\"backup\":\"$BACKUP_NAME\",\"target_time\":\"$TARGET_TIME\",\"alembic_head\":\"$head\",${counts}\"last_message_at\":\"$last_tx\",\"seconds\":$(( $(date -u +%s) - started ))}"
exit "$fail"
