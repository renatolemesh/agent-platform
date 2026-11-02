#!/usr/bin/env bash
# Exposes the local API (dev) through a server's nginx: reverse SSH tunnel that reopens itself when it drops.
#   TUNNEL_HOST=deploy@203.0.113.10 TUNNEL_BIND=127.0.0.1:18100 scripts/dev-tunnel.sh
# TUNNEL_BIND is the address:port nginx proxies to on the server; LOCAL is the API here.
set -u
: "${TUNNEL_HOST:?set TUNNEL_HOST (user@server)}"
: "${TUNNEL_BIND:?set TUNNEL_BIND (address:port on the server)}"
LOCAL="${LOCAL:-127.0.0.1:8100}"
while true; do
  echo "[$(date '+%F %T')] tunnel ${TUNNEL_HOST} ${TUNNEL_BIND} -> ${LOCAL}"
  ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 -o ServerAliveCountMax=3 \
      -R "${TUNNEL_BIND}:${LOCAL}" "${TUNNEL_HOST}"
  echo "[$(date '+%F %T')] tunnel closed (exit $?); reconnecting in 5 s"
  sleep 5
done
