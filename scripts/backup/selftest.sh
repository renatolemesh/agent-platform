#!/usr/bin/env bash
# End-to-end self-test of the backup chain with wal-g FILE storage (no cloud, no secrets of the real deployment):
#   source Postgres (archive_mode=on, libsodium-encrypted WAL + base backup)  ->  migrate + bootstrap
#   -> base backup -> rows written AFTER the base backup -> restore_test.sh (full replay) -> PITR restore
# Proves: archive_command works, backups are encrypted and restorable, WAL replay brings post-backup rows, PITR stops
# at the target, the initdb script creates app_rw with the secret password.
# Everything is named ap-backuptest-* and removed at the end. Needs: docker, the agentplatform venv (for migrate).
#
#   scripts/backup/selftest.sh            # builds agentplatform-postgres image if missing
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE="${IMAGE:-agentplatform-postgres:pg17-walg-v3.0.9}"
PORT="${SELFTEST_PORT:-55499}"
SRC=ap-backuptest-src
STORE=ap-backuptest-store
SRCDATA=ap-backuptest-srcdata
TMP="$(mktemp -d)"
export MSYS_NO_PATHCONV=1
hostpath() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY="$ROOT/.venv/Scripts/python"

cleanup() {
  docker rm -f "$SRC" >/dev/null 2>&1 || true
  docker ps -aq --filter "name=^ap-backuptest-" | xargs -r docker rm -f >/dev/null 2>&1 || true
  docker volume ls -q --filter "name=^ap-backuptest-" | xargs -r docker volume rm >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT
cleanup_before() { docker rm -f "$SRC" >/dev/null 2>&1 || true; docker volume rm "$STORE" "$SRCDATA" >/dev/null 2>&1 || true; }
cleanup_before

docker image inspect "$IMAGE" >/dev/null 2>&1 || docker build -f "$ROOT/docker/postgres/Dockerfile" -t "$IMAGE" "$ROOT/docker"

mkdir -p "$TMP/secrets"
rnd() { "$PY" -c "import secrets;print(secrets.token_hex($1))"; }
rnd 32 > "$TMP/secrets/walg_libsodium_key"
rnd 16 > "$TMP/secrets/pg_owner_password"
rnd 16 > "$TMP/secrets/app_rw_password"
chmod 0444 "$TMP/secrets"/*
OWNER_PW=$(cat "$TMP/secrets/pg_owner_password"); APP_PW=$(cat "$TMP/secrets/app_rw_password")
cat > "$TMP/storage.env" <<EOF
WALG_FILE_PREFIX=/backups
POSTGRES_USER=agentplatform
POSTGRES_DB=agentplatform
EOF

docker volume create "$STORE" >/dev/null
docker run --rm -v "$STORE:/backups" --entrypoint chown "$IMAGE" postgres:postgres /backups
docker run -d --name "$SRC" \
  --env-file "$(hostpath "$TMP/storage.env")" -e POSTGRES_PASSWORD_FILE=/run/secrets/pg_owner_password \
  -v "$SRCDATA:/var/lib/postgresql/data" -v "$STORE:/backups" -v "$(hostpath "$TMP/secrets"):/run/secrets:ro" \
  -p "127.0.0.1:$PORT:5432" "$IMAGE" \
  postgres -c wal_level=replica -c archive_mode=on -c "archive_command=/usr/local/bin/walg-env wal-push %p" -c archive_timeout=60 \
  >/dev/null
psrc() { docker exec "$SRC" psql -U agentplatform -d agentplatform -v ON_ERROR_STOP=1 -At -c "$1"; }
for _ in $(seq 1 60); do
  # wait for the real server (the entrypoint runs a temporary one during initdb)
  docker exec "$SRC" pg_isready -q -h 127.0.0.1 -U agentplatform 2>/dev/null && break; sleep 1
done
sleep 2

echo "== migrate + bootstrap against the source (app_rw created by the initdb script with the secret password)"
export MIGRATION_DATABASE_URL="postgresql://agentplatform:${OWNER_PW}@127.0.0.1:${PORT}/agentplatform"
export DATABASE_URL="postgresql://app_rw:${APP_PW}@127.0.0.1:${PORT}/agentplatform"
(cd "$ROOT" && "$PY" -m agentplatform.cli migrate >/dev/null && "$PY" -m agentplatform.cli bootstrap --slug dev-consortium >/dev/null)
"$PY" -c "import psycopg,os; psycopg.connect(os.environ['DATABASE_URL']).execute('select 1')" \
  && echo "   app_rw login with secret password: ok"

echo "== base backup (encrypted)"
docker exec -u postgres "$SRC" /usr/local/bin/walg-env backup-push /var/lib/postgresql/data 2>&1 | tail -n 1
docker exec -u postgres "$SRC" /usr/local/bin/walg-env backup-list 2>/dev/null | tail -n 1
# wal-g retries failed parts forever and survives SIGTERM, so bound it with SIGKILL; the failure must be a
# decryption/format error, nothing else
nokey=$(docker run --rm -v "$STORE:/backups" -e WALG_FILE_PREFIX=/backups -e WALG_ALLOW_UNENCRYPTED=1 -u postgres \
    --entrypoint sh "$IMAGE" -c 'mkdir -p /tmp/x && timeout -s KILL 30 /usr/local/bin/walg-env backup-fetch /tmp/x LATEST' 2>&1) \
  && { echo "backup restorable WITHOUT the libsodium key: NOT encrypted" >&2; exit 1; }
printf '%s' "$nokey" | grep -qiE "magic number|decrypt|cipher|authenticat" \
  || { echo "no-key fetch failed for an unexpected reason:" >&2; printf '%s\n' "$nokey" | tail -n 5 >&2; exit 1; }
echo "   backup cannot be restored without the key (encrypted): ok"

echo "== writes after the base backup"
psrc "insert into audit_log (actor_kind, action, target) select 'system', 'backup.selftest.before_target', 'n'||g from generate_series(1,3) g" >/dev/null
sleep 2
TARGET=$(psrc "select now()::text")
sleep 2
psrc "insert into audit_log (actor_kind, action, target) select 'system', 'backup.selftest.after_target', 'n'||g from generate_series(1,2) g" >/dev/null
psrc "select pg_switch_wal()" >/dev/null
for _ in $(seq 1 30); do
  pending=$(psrc "select count(*) from pg_ls_dir('pg_wal/archive_status') f where f like '%.ready'")
  [ "$pending" = "0" ] && break; sleep 1
done
total=$(psrc "select count(*) from audit_log")
before_target=$(psrc "select count(*) from audit_log where action <> 'backup.selftest.after_target'")
printf 'audit_log\t%s\ntenants\t1\n' "$total" > "$TMP/expect.tsv"

common=(env IMAGE="$IMAGE" ENV_FILE="$TMP/storage.env" SECRETS_DIR="$TMP/secrets" NAME_PREFIX=ap-backuptest-restore
        EXTRA_DOCKER_ARGS="-v $STORE:/backups")

echo "== restore LATEST + full WAL replay (expect audit_log = $total)"
out=$("${common[@]}" EXPECT_FILE="$TMP/expect.tsv" bash "$ROOT/scripts/backup/restore_test.sh")
echo "   $out"
got=$(printf '%s' "$out" | sed -n 's/.*"audit_log":"\([0-9]*\)".*/\1/p')
[ "$got" = "$total" ] || { echo "full replay: audit_log $got != $total" >&2; exit 1; }

echo "== point-in-time restore to $TARGET (expect audit_log = $before_target)"
out=$("${common[@]}" TARGET_TIME="$TARGET" bash "$ROOT/scripts/backup/restore_test.sh")
echo "   $out"
got=$(printf '%s' "$out" | sed -n 's/.*"audit_log":"\([0-9]*\)".*/\1/p')
[ "$got" = "$before_target" ] || { echo "PITR: audit_log $got != $before_target" >&2; exit 1; }

echo "SELFTEST OK"
