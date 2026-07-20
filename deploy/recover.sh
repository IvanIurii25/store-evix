#!/usr/bin/env bash
# evix-store — idempotent, non-destructive recovery (after power loss / reboot).
# Brings the whole stack + tunnel up and prints a health summary. No build/pull.
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/apps/evix-store}"
COMPOSE="docker compose -f $APP_DIR/docker-compose.prod.yml"
cd "$APP_DIR"

echo "== docker =="; docker info >/dev/null 2>&1 || { echo "docker not running"; exit 1; }

echo "== bringing stack + tunnel up =="
$COMPOSE up -d
$COMPOSE --profile tunnel up -d

echo "== waiting for app/front health =="
for _ in $(seq 1 30); do
  app=$($COMPOSE ps --format '{{.Service}} {{.Health}}' | awk '/^app /{print $2}')
  front=$($COMPOSE ps --format '{{.Service}} {{.Health}}' | awk '/^front /{print $2}')
  [ "$app" = "healthy" ] && [ "$front" = "healthy" ] && break
  sleep 3
done

echo "== containers =="; $COMPOSE ps
echo "== public check =="
curl -sS -o /dev/null -w "https://shop.evix.md/ro -> %{http_code}\n" https://shop.evix.md/ro || true
curl -sS -o /dev/null -w "https://shop.evix.md/api/v1/health -> %{http_code}\n" https://shop.evix.md/api/v1/health || true
