#!/bin/sh
# Prod entrypoint for api/worker containers: turns Docker secrets (/run/secrets/<name>) into the env vars that
# agentplatform.settings reads, so secrets never live in compose files, `docker inspect` or .env on disk.
#   /run/secrets/openrouter_api_key  -> OPENROUTER_API_KEY   (same rule for every non-empty secret file)
#   app_rw_password / pg_owner_password / redis_password -> DATABASE_URL / MIGRATION_DATABASE_URL / REDIS_URL
# Passwords must be URL-safe (generate with `openssl rand -hex 32`). Empty secret files are skipped (optional ones).
set -eu
dir=/run/secrets
if [ -d "$dir" ]; then
  for f in "$dir"/*; do
    [ -f "$f" ] && [ -s "$f" ] || continue
    name=$(basename "$f")
    case "$name" in
      app_rw_password|pg_owner_password|redis_password|walg_*) continue ;;
    esac
    var=$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]')
    eval "already=\${$var:-}"
    [ -n "$already" ] && continue
    value=$(tr -d '\r\n' < "$f")
    export "$var=$value"
  done
  host="${PGHOST_APP:-postgres}"; db="${POSTGRES_DB:-agentplatform}"
  if [ -z "${DATABASE_URL:-}" ] && [ -s "$dir/app_rw_password" ]; then
    DATABASE_URL="postgresql://app_rw:$(tr -d '\r\n' < "$dir/app_rw_password")@${host}:5432/${db}"; export DATABASE_URL
  fi
  if [ -z "${MIGRATION_DATABASE_URL:-}" ] && [ -s "$dir/pg_owner_password" ]; then
    MIGRATION_DATABASE_URL="postgresql://${POSTGRES_USER:-agentplatform}:$(tr -d '\r\n' < "$dir/pg_owner_password")@${host}:5432/${db}"
    export MIGRATION_DATABASE_URL
  fi
  if [ -z "${REDIS_URL:-}" ] && [ -s "$dir/redis_password" ]; then
    REDIS_URL="redis://:$(tr -d '\r\n' < "$dir/redis_password")@${REDIS_HOST:-redis}:6379/0"; export REDIS_URL
  fi
fi
exec "$@"
