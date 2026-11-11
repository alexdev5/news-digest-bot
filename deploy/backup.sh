#!/usr/bin/env bash
# Дамп бази NewsDigest із контейнера `db` проєкту news-digest, з ретенцією.
# Викликається з deploy/deploy.sh перед міграціями; можна додати в cron/systemd timer.
#   bash deploy/backup.sh
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE=(docker compose -f docker-compose.prod.yml)
BACKUP_DIR="${BACKUP_DIR:-/srv/news-digest-backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
STAMP="$(date +%Y%m%d_%H%M%S)"

set -a
# shellcheck disable=SC1091
. ./.env
set +a
DB_NAME="${POSTGRES_DB:-news_digest}"
DB_USER="${POSTGRES_USER:-news_digest}"

mkdir -p "$BACKUP_DIR"
OUT="$BACKUP_DIR/news_digest_${STAMP}.dump"

echo ">> Dumping $DB_NAME to $OUT"
"${COMPOSE[@]}" exec -T db pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$OUT"

if [ ! -s "$OUT" ]; then
  echo ">> Backup is empty — aborting" >&2
  rm -f "$OUT"
  exit 1
fi

echo ">> Pruning backups older than ${RETENTION_DAYS} days"
find "$BACKUP_DIR" -maxdepth 1 -name 'news_digest_*.dump' -mtime +"$RETENTION_DAYS" -delete

echo ">> Backup done: $(du -h "$OUT" | cut -f1) ($OUT)"
