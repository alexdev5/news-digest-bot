#!/usr/bin/env bash
# Одноразова підготовка /srv/news-digest на сервері (повторний запуск безпечний):
# каталоги даних для користувача контейнера, пароль PostgreSQL, .env.
#
#   sudo bash deploy/init-server.sh
#
# Пароль генерується (hex, 64 символи) у secrets/postgres_password. Його читають
# два контейнери з різними UID (postgres і бот), тому файл 0444, а захищає його
# каталог secrets/ (0750 root:docker). Наявний пароль не перезаписується:
# база вже ініціалізована з ним.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_UID=10001
SECRET="$APP_DIR/secrets/postgres_password"

if [ "$(id -u)" -ne 0 ]; then
  echo "Запускайте від root: sudo bash deploy/init-server.sh" >&2
  exit 1
fi

cd "$APP_DIR"

echo ">> Каталоги даних (власник UID $APP_UID)"
mkdir -p state cache session
chown "$APP_UID:$APP_UID" state cache session
chmod 0700 state cache session

echo ">> Секрети"
mkdir -p secrets
# Група docker (користувач деплою) може пройти в каталог — compose перевіряє наявність
# файлу секрету; решта користувачів хоста — ні.
if getent group docker >/dev/null; then
  chown root:docker secrets
  chmod 0750 secrets
else
  chown root:root secrets
  chmod 0700 secrets
fi
if [ -s "$SECRET" ]; then
  echo "   Пароль PostgreSQL уже є — не змінюю"
else
  (umask 077 && openssl rand -hex 32 > "$SECRET")
  echo "   Згенеровано пароль PostgreSQL"
fi
chmod 0444 "$SECRET"

if [ ! -f .env ]; then
  cp .env.prod.example .env
  echo ">> Створено .env із .env.prod.example"
fi

echo ">> Готово. Далі: покласти config/config.json і виконати bash deploy/deploy.sh"
