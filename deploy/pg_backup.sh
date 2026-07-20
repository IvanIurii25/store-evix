#!/usr/bin/env bash
# evix-store — PostgreSQL backup (custom-format pg_dump). Keeps the last N dumps.
# Cron (server): 0 3 */3 * *  bash ~/apps/evix-store/deploy/pg_backup.sh
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/apps/evix-store}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/evix-store-backups}"
KEEP="${KEEP:-5}"
COMPOSE="docker compose -f $APP_DIR/docker-compose.prod.yml"

mkdir -p "$BACKUP_DIR"
ts="$(date +%Y%m%d_%H%M%S)"
out="$BACKUP_DIR/evix-store_${ts}.dump"

echo "[$(date -Is)] dumping -> $out" >>"$BACKUP_DIR/backup.log"
$COMPOSE exec -T db pg_dump -U evix -d evix_store -Fc >"$out"

# Rotate: keep the newest $KEEP dumps.
ls -1t "$BACKUP_DIR"/evix-store_*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

echo "[$(date -Is)] done ($(du -h "$out" | cut -f1)); kept $KEEP" >>"$BACKUP_DIR/backup.log"
