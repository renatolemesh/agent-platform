#!/usr/bin/env bash
# Installs the monthly restore test on the server (day 3, 04:17 in the server's clock). Run once, as root, from the repo on the server.
# The platform alerts if no good test is recorded for operations.alerts.restore_test_max_age_days, so a cron that dies does not go unnoticed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CRON_FILE="${CRON_FILE:-/etc/cron.d/agentplatform-restore-test}"
LOG_FILE="${LOG_FILE:-/var/log/agentplatform-restore-test.log}"
{
  echo "SHELL=/bin/bash"
  echo "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  echo "17 4 3 * * root bash $ROOT/scripts/backup/restore_test_monthly.sh >> $LOG_FILE 2>&1"
} > "$CRON_FILE"
chmod 0644 "$CRON_FILE"
echo "installed $CRON_FILE; first run now with: bash $ROOT/scripts/backup/restore_test_monthly.sh"
