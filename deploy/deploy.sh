#!/usr/bin/env bash
# Деплой на сервері (/srv/news-digest).
#   Автоматично: GitHub Actions → deploy/ssh-entrypoint.sh → цей скрипт із DEPLOY_SHA.
#   Вручну:      cd /srv/news-digest && bash deploy/deploy.sh   (бере поточний origin/main)
#
# Порядок: перевірка файлів → оновлення коду до точного коміту → build → db →
# backup → міграції → перевірка конфігу → (пере)запуск worker, якщо вже виконано
# `digest login` і заповнені Telegram-секрети. Інакше працює лише db.
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

# Compose вимагає наявності всіх файлів секретів сервісу app. Порожні заготовки
# Telegram-секретів не дають деплою впасти; worker без значень не запускається.
for name in telegram_api_id telegram_api_hash; do
  if [ ! -e "secrets/$name" ]; then
    : > "secrets/$name"
    chmod 0444 "secrets/$name"
    echo ">> Створено порожній secrets/$name (заповніть значенням із my.telegram.org)"
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

if [ -s secrets/telegram_api_id ] && [ -s secrets/telegram_api_hash ]   && [ -s session/newsdigest.session ]; then
  echo ">> Starting worker"
  "${COMPOSE[@]}" up -d --no-deps app
  echo ">> Done. Worker запущено."
else
  echo ">> Done. Worker НЕ запущено: потрібні secrets/telegram_api_* і digest login"
  echo "   (див. docs/deployment.md, «Telegram»)."
fi
