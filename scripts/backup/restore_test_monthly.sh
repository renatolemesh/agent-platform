#!/usr/bin/env bash
# Monthly restore test (plan 12 §3), run by cron (scripts/deploy/install-restore-cron.sh). Runs restore_test.sh and records the outcome in
# the database: that record is what lets the platform alert when the test fails or stops running. Stores counts only, never data.
#
# Env:
#   RESTORE_CMD  what to run (default: scripts/backup/restore_test.sh)
#   PSQL_CMD     how to reach the production database as its owner (default: psql inside the compose postgres service)
# Exit code: the restore test's, or 3 when the result could not be recorded.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
if [ -z "${PSQL_CMD:-}" ]; then
  cd "$ROOT/docker" || exit 3
  # shellcheck disable=SC1091  # exists only on the server
  [ -f .deploy.env ] && set -a && . ./.deploy.env && set +a
  PSQL_CMD="docker compose -f compose.prod.yaml exec -T postgres psql -U ${POSTGRES_USER:-agentplatform} -d ${POSTGRES_DB:-agentplatform}"
fi
RESTORE_CMD="${RESTORE_CMD:-bash $ROOT/scripts/backup/restore_test.sh}"

# shellcheck disable=SC2086  # both are command lines
out=$($RESTORE_CMD)
code=$?
summary=$(printf '%s\n' "$out" | tail -n 1)
[ -n "$summary" ] || summary="{\"restore_test\":\"failed\",\"reason\":\"no output\",\"exit\":$code}"
status=failed
[ "$code" = 0 ] && status=ok

# shellcheck disable=SC2086
if ! printf "insert into restore_tests (status, summary) values (:'status', :'summary');\n" \
    | $PSQL_CMD -v ON_ERROR_STOP=1 -v status="$status" -v summary="$summary" -f - >/dev/null; then
  echo "restore test finished with status $status, but the result could not be recorded" >&2
  printf '%s\n' "$summary"
  exit 3
fi
printf '%s\n' "$summary"
exit "$code"
