#!/usr/bin/env bash
# Creates docker/secrets/<name> for compose.prod.yaml. Generates the values the platform owns (passwords, KEK, pepper,
# webhook/console secrets, wal-g key) and creates EMPTY files for credentials that come from third parties
# (OpenRouter, Meta, WhatsApp, SMTP, S3), which the operator fills in by hand. Never overwrites an existing file.
#   scripts/deploy/init-secrets.sh            # then: $EDITOR docker/secrets/openrouter_api_key ...
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DIR="${SECRETS_DIR:-$ROOT/docker/secrets}"
umask 077
mkdir -p "$DIR"; chmod 0700 "$DIR"

hex() { od -An -tx1 -N"$1" /dev/urandom | tr -d ' \n'; }
b64() { head -c "$1" /dev/urandom | base64 | tr -d '\n'; }

put() {  # name value
  local f="$DIR/$1"
  if [ -e "$f" ]; then echo "keep     $1"; return; fi
  printf '%s' "$2" > "$f"; chmod 0444 "$f"
  if [ -n "$2" ]; then echo "created  $1"; else echo "EMPTY    $1  <- fill in"; fi
}

put pg_owner_password      "$(hex 32)"
put app_rw_password        "$(hex 32)"
put redis_password         "$(hex 32)"
put kek_b64                "$(b64 32)"
put hash_pepper_b64        "$(b64 32)"
put meta_verify_token      "$(hex 24)"
put console_session_secret "$(hex 32)"
put connect_webhook_secret ""
put walg_libsodium_key     "$(hex 32)"
put grafana_admin_password "$(hex 32)"
put openrouter_api_key     ""
put meta_app_secret        ""
put whatsapp_token         ""
put whatsapp_app_secret    ""
put whatsapp_alert_token   ""
put smtp_url               ""
put walg_s3_access_key     ""
put walg_s3_secret_key     ""
put metrics_token          "$(hex 32)"

cat <<MSG

Secrets in $DIR (files 0444, directory 0700). Next:
  1. fill in the EMPTY ones that you use (OpenRouter, Meta app secret, S3 keys are mandatory in prod);
  2. copy kek_b64, hash_pepper_b64 and walg_libsodium_key to the offline vault NOW — without them the database
     and the backups cannot be decrypted;
  3. cp docker/prod.env.example docker/prod.env and edit it.
MSG
