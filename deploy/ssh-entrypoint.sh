#!/usr/bin/env bash
# Forced command SSH-ключа GitHub Actions. Встановлюється ПОЗА робочою копією:
#   install -m 0755 deploy/ssh-entrypoint.sh /root/bin/news-digest-deploy-entrypoint
# і прописується в authorized_keys (див. docs/deployment.md, «Автодеплой»).
#
# Приймає зі stdin один рядок — повний SHA коміту. Перевіряє, що це поточний
# origin/main, бере deploy/deploy.sh саме з цього коміту й запускає його.
# Старий deploy.sh із робочої копії не виконується.
set -euo pipefail

APP_DIR="${NEWS_DIGEST_APP_DIR:-/srv/news-digest}"

IFS= read -r DEPLOY_SHA || { echo "Deploy blocked: missing revision" >&2; exit 1; }
if [[ ! "$DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Deploy blocked: invalid revision" >&2
  exit 1
fi
export DEPLOY_SHA

cd "$APP_DIR"

# Один деплой одночасно, навіть якщо ручний і автоматичний збіглися.
exec 9>"$APP_DIR/.deploy.lock"
flock -n 9 || { echo "Deploy blocked: another deploy is running" >&2; exit 1; }

git fetch --prune origin main
if [[ "$(git rev-parse origin/main)" != "$DEPLOY_SHA" ]]; then
  echo "Deploy blocked: requested revision is not current main" >&2
  exit 1
fi

driver=$(mktemp)
trap 'rm -f -- "$driver"' EXIT
git show "$DEPLOY_SHA:deploy/deploy.sh" > "$driver"
bash "$driver"
