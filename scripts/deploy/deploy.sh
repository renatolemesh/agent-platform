#!/usr/bin/env bash
# Deploy / rollback (plan 12 §2): pull image -> migrate (expand/contract, forward-only) -> up -d -> health gate.
#   scripts/deploy/deploy.sh ghcr.io/org/agentplatform:<sha>     # deploy that image
#   scripts/deploy/deploy.sh --rollback                          # back to the previously deployed image
# Needs PUBLIC_DOMAIN and ACME_EMAIL in the environment (or in docker/.deploy.env). Keeps a history in
# docker/.deployed (one image per line) so rollback never guesses.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT/docker"
[ -f .deploy.env ] && set -a && . ./.deploy.env && set +a
: "${PUBLIC_DOMAIN:?set PUBLIC_DOMAIN}" "${ACME_EMAIL:?set ACME_EMAIL}"
HIST=.deployed
touch "$HIST"

if [ "${1:-}" = "--rollback" ]; then
  [ "$(wc -l < "$HIST")" -ge 2 ] || { echo "no previous image recorded in docker/$HIST" >&2; exit 1; }
  IMAGE=$(tail -n 2 "$HIST" | head -n 1)
  echo "rolling back to $IMAGE (migrations are NOT reverted: they are backward compatible by policy)"
else
  IMAGE="${1:?usage: deploy.sh <image> | --rollback}"
fi
export APP_IMAGE="$IMAGE"
C=(docker compose -f compose.prod.yaml)

"${C[@]}" pull api worker worker-bg
"${C[@]}" build postgres
"${C[@]}" run --rm migrate
"${C[@]}" up -d --remove-orphans

echo "waiting for the api healthcheck..."
for _ in $(seq 1 40); do
  state=$("${C[@]}" ps --format '{{.Health}}' api 2>/dev/null || true)
  [ "$state" = "healthy" ] && break
  sleep 3
done
if [ "$state" != "healthy" ]; then
  echo "api is not healthy ($state). Logs:" >&2
  "${C[@]}" logs --tail 50 api >&2
  echo "run: scripts/deploy/deploy.sh --rollback" >&2
  exit 1
fi
code=$(curl -s -o /dev/null -w '%{http_code}' "https://${PUBLIC_DOMAIN}/health" || true)
echo "public /health: $code"
[ "${1:-}" = "--rollback" ] && sed -i '$ d' "$HIST" || echo "$IMAGE" >> "$HIST"
echo "deployed $IMAGE"
