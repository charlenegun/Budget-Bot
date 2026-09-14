#!/usr/bin/env bash
# Nightly SQLite backup. Uses .backup, which is safe on a live WAL database
# (plain cp is not). Keeps 30 days locally.
set -euo pipefail

DB=/opt/foodbot/foodbot.db
DEST=/opt/foodbot/backups
STAMP=$(date +%F)

mkdir -p "$DEST"
sqlite3 "$DB" ".backup '$DEST/foodbot-$STAMP.db'"
gzip -f "$DEST/foodbot-$STAMP.db"
find "$DEST" -name 'foodbot-*.db.gz' -mtime +30 -delete

# Optional: copy off the box so a dead VM doesn't take the history with it.
# gcloud storage cp "$DEST/foodbot-$STAMP.db.gz" gs://YOUR-BUCKET/foodbot/
