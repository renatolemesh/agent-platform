#!/bin/sh
# First-boot only (docker-entrypoint-initdb.d): create app_rw with the password from the Docker secret, so migration
# 0002 (`create role ... if not exists`) keeps it instead of the dev default. Existing clusters: see
# docs/runbooks/rotacao-de-chaves.md (alter role app_rw password ...).
set -eu
f=/run/secrets/app_rw_password
[ -s "$f" ] || { echo "10-app-role: no app_rw_password secret, migration will create the dev role" >&2; exit 0; }
pw=$(tr -d '\r\n' < "$f")
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -v pw="$pw" <<'SQL'
select format('create role app_rw login password %L nosuperuser nocreatedb nocreaterole nobypassrls', :'pw') \gexec
SQL
