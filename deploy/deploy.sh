#!/usr/bin/env bash
# Деплой на сервері (/srv/news-digest).
#   Автоматично: GitHub Actions → deploy/ssh-entrypoint.sh → цей скрипт із DEPLOY_SHA.
#   Вручну:      cd /srv/news-digest && bash deploy/deploy.sh   (бере поточний origin/main)
#
# Порядок: перевірка файлів → оновлення коду до точного коміту → build → db →
# backup → міграції → перевірка конфігу. Worker (`digest run`) ще не реалізований
# (етап 2), тому бот не запускається; контейнер db працює постійно.
set -euo pipefail

APP_DIR="${NEWS_DIGEST_APP_DIR:-/srv/news-digest}"
cd "$APP_DIR"
COMPOSE=(docker compose -f docker-compose.prod.yml --profile worker)

for path in .env config/config.json secrets/postgres_password; do
  if [ ! -e "$path" ]; then
    echo "Немає $path — див. docs/deployment.md (первинне налаштування)" >&2
    exit 1
  fi
done

echo ">> Updating code"
git fetch --prune origin main
TARGET="${DEPLOY_SHA:-$(git rev-parse origin/main)}"
if [[ ! "$TARGET" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Invalid revision: $TARGET" >&2
  exit 1
fi
git checkout -q main
git merge --ff-only "$TARGET"
if [[ "$(git rev-parse HEAD)" != "$TARGET" ]]; then
  echo "Checkout mismatch: HEAD is not $TARGET" >&2
  exit 1
fi
echo ">> Revision $(git rev-parse --short HEAD)"

echo ">> Building image"
"${COMPOSE[@]}" build app

echo ">> Starting database"
"${COMPOSE[@]}" up -d --wait db

echo ">> Backup before migrations"
bash deploy/backup.sh

echo ">> Migrations"
"${COMPOSE[@]}" run --rm app digest migrate

echo ">> Config check"
"${COMPOSE[@]}" run --rm app digest validate-config --config /config/config.json

echo ">> Done. Worker ще не реалізований (етап 2) — бот не запускається, db працює."
