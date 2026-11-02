#!/bin/sh
# Backup sidecar (plan 12 §3): daily base backup + retention; weekly pg_dump -Fc for portability.
# Env: PGDATA (shared pg_data volume, read-only), PGHOST=postgres, BACKUP_HOUR_UTC (default 4), WALG_RETAIN_FULL (14),
#      DUMP_DIR (/dumps), DUMP_KEEP (4). Writes a heartbeat file the healthcheck reads.
set -eu
HOUR="${BACKUP_HOUR_UTC:-4}"; RETAIN="${WALG_RETAIN_FULL:-14}"; DUMP_DIR="${DUMP_DIR:-/dumps}"; KEEP="${DUMP_KEEP:-4}"
HEARTBEAT=/tmp/backup-heartbeat

run_backup() {
  echo "$(date -u +%FT%TZ) base backup start"
  walg-env backup-push "${PGDATA}"
  walg-env delete retain FULL "${RETAIN}" --confirm
  if [ "$(date -u +%u)" = "7" ]; then
    mkdir -p "${DUMP_DIR}"
    f="${DUMP_DIR}/agentplatform-$(date -u +%Y%m%d).dump"
    PGPASSWORD="$(tr -d '\r\n' < /run/secrets/pg_owner_password)" pg_dump -h "${PGHOST}" -U "${POSTGRES_USER:-agentplatform}" \
      -d "${POSTGRES_DB:-agentplatform}" -Fc -f "${f}.tmp" && mv "${f}.tmp" "${f}"
    ls -1t "${DUMP_DIR}"/agentplatform-*.dump 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
  fi
  date -u +%s > "${HEARTBEAT}"
  echo "$(date -u +%FT%TZ) base backup done"
}

[ "${1:-}" = "--now" ] && { run_backup; exit 0; }
date -u +%s > "${HEARTBEAT}"
while true; do
  now=$(date -u +%s)
  target=$(date -u -d "today ${HOUR}:00" +%s)
  [ "${target}" -le "${now}" ] && target=$((target + 86400))
  sleep $((target - now))
  run_backup || echo "$(date -u +%FT%TZ) base backup FAILED" >&2
done
