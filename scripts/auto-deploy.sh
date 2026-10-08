#!/usr/bin/env bash
# prehrana-plus auto-deploy: pull main and rebuild if origin/main advanced.
# Called by systemd timer prehrana-plus-deploy.timer every minute.
set -euo pipefail

REPO=/home/pajp/prehrana-plus
LOG=/home/pajp/prehrana-plus/deploy.log

cd "$REPO"

git fetch --quiet origin main
LOCAL=$(git rev-parse @)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
  exit 0
fi

{
  echo "=== $(date -Is) — updating $LOCAL → $REMOTE ==="
  git pull --ff-only origin main
  docker-compose up -d --build
  echo "=== done ==="
} >> "$LOG" 2>&1
