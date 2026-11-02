#!/bin/sh
# Runs wal-g with credentials read from Docker secrets (never from the environment of `docker inspect`).
#   archive_command = '/usr/local/bin/walg-env wal-push %p'
#   restore_command = '/usr/local/bin/walg-env wal-fetch %f %p'
# Storage is chosen by the plain env: WALG_S3_PREFIX (+ AWS_ENDPOINT, AWS_REGION, AWS_S3_FORCE_PATH_STYLE)
# or WALG_FILE_PREFIX (local/NFS, used by the restore test).
set -eu
secret() { f="/run/secrets/$1"; [ -s "$f" ] && tr -d '\r\n' < "$f" || true; }

ak=$(secret walg_s3_access_key); sk=$(secret walg_s3_secret_key)
[ -n "$ak" ] && export AWS_ACCESS_KEY_ID="$ak"
[ -n "$sk" ] && export AWS_SECRET_ACCESS_KEY="$sk"
# libsodium encryption key (32 bytes, hex): mandatory outside the restore test
if [ -s /run/secrets/walg_libsodium_key ]; then
  export WALG_LIBSODIUM_KEY_PATH=/run/secrets/walg_libsodium_key WALG_LIBSODIUM_KEY_TRANSFORM="${WALG_LIBSODIUM_KEY_TRANSFORM:-hex}"
elif [ "${WALG_ALLOW_UNENCRYPTED:-0}" != "1" ]; then
  echo "walg-env: missing /run/secrets/walg_libsodium_key (set WALG_ALLOW_UNENCRYPTED=1 only for tests)" >&2
  exit 2
fi
: "${PGHOST:=/var/run/postgresql}"; : "${PGUSER:=${POSTGRES_USER:-postgres}}"; : "${PGDATABASE:=${POSTGRES_DB:-postgres}}"
export PGHOST PGUSER PGDATABASE
if [ -z "${PGPASSWORD:-}" ] && [ -s /run/secrets/pg_owner_password ]; then
  PGPASSWORD=$(secret pg_owner_password); export PGPASSWORD
fi
exec /usr/local/bin/wal-g "$@"
