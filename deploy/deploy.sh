#!/usr/bin/env bash
# evix-store — deploy/update on the server. Pulls both repos, rebuilds, migrates
# (via the one-off `migrate` service), and rolls the stack. Run on the server:
#   bash ~/apps/evix-store/deploy/deploy.sh
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/apps/evix-store}"
FRONT_DIR="${FRONT_DIR:-$HOME/apps/evix-store-front}"
COMPOSE="docker compose -f $APP_DIR/docker-compose.prod.yml"

echo "== pull backend =="; git -C "$APP_DIR" pull --ff-only
echo "== pull frontend =="; git -C "$FRONT_DIR" pull --ff-only

cd "$APP_DIR"
[ -f .env ] || { echo "missing $APP_DIR/.env (copy .env.prod.example)"; exit 1; }

echo "== build + migrate + up (app/front) =="
$COMPOSE up -d --build
echo "== ensure tunnel up =="
$COMPOSE --profile tunnel up -d
# Recreating app/front gives them new IPs; cloudflared caches the old ones and
# starts returning "origin unreachable". Restart it so it re-resolves.
echo "== restart cloudflared (re-resolve origin IPs) =="
$COMPOSE --profile tunnel restart cloudflared

echo "== status =="; $COMPOSE ps
# cloudflared just restarted; give the tunnel a few seconds to reconnect and
# re-resolve the new app/front IPs before smoking, or the curls catch it
# mid-reconnect and print false 502/530.
echo "== wait for tunnel to settle =="; sleep 8
echo "== smoke =="
curl -sS -o /dev/null -w "https://shop.evix.md/api/v1/health -> %{http_code}\n" https://shop.evix.md/api/v1/health || true
curl -sS -o /dev/null -w "https://shop.evix.md/ro -> %{http_code}\n" https://shop.evix.md/ro || true
