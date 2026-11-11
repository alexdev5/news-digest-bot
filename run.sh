#!/usr/bin/env bash
# Linux/macOS command wrapper for this project (mirror of run.ps1).
# Run it from the repository root, for example:
#   ./run.sh test
#   ./run.sh validate config/config.json
#
# Everything runs inside the app container, so behavior is identical on every OS.
# Unlike the Windows wrapper this does NOT force the legacy Docker builder.
set -euo pipefail

cd "$(dirname "$0")"

app() {
  docker compose run --rm app "$@"
}

app_no_db() {
  docker compose run --rm --no-deps app "$@"
}

config_path() {
  if [ -f config/config.json ]; then
    echo "config/config.json"
  else
    echo "config/config.json not found, using config/config.example.json" >&2
    echo "config/config.example.json"
  fi
}

show_help() {
  cat <<'EOF'
Usage: ./run.sh <command> [args]

Docker:
  start            Start PostgreSQL (db)
  stop             Stop PostgreSQL (db)
  ps               Show compose service status
  logs [svc]       Follow logs, optionally for one service (default db)
  build            Build (rebuild) the app image
  shell            Open shell in a one-off app container
  psql             Open psql in the db container

Database:
  migrate [rev]    Apply Alembic migrations (default head)

Checks:
  test [args]      Run pytest; args are passed to pytest
  lint             Run ruff check and ruff format --check
  fmt              Format code with ruff
  typecheck        Run mypy
  check            lint + typecheck + test

App:
  validate [path]  Validate config (default config/config.json or the example)
  schedule [args]  Show next windows (default: --all)
  digest [args]    Run any digest CLI command

Telegram (needs TELEGRAM_API_ID/HASH in .env):
  login [--qr]     Interactive Telegram login (session in ./session)
  dialogs          List channels with IDs for the config
  doctor           Check session, sources and posting rights
  scan [args]      Manual scan of one window, e.g. scan --channel dnipro_news
  worker           Run the scheduler worker in the foreground
  status           Channel state, recent runs, heartbeat
EOF
}

command="${1:-help}"
shift || true

case "$command" in
  help) show_help ;;
  start) docker compose up -d db ;;
  stop) docker compose stop db ;;
  ps | status) docker compose ps ;;
  logs)
    if [ "$#" -gt 0 ]; then
      docker compose logs -f --tail=120 "$@"
    else
      docker compose logs -f --tail=120 db
    fi
    ;;
  psql) docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' ;;
  migrate) app digest migrate "$@" ;;
  build | rebuild) docker compose build app ;;
  shell) docker compose run --rm app sh ;;
  test) app pytest "$@" ;;
  lint) app_no_db sh -c "ruff check src tests && ruff format --check src tests" ;;
  fmt) app_no_db ruff format src tests ;;
  typecheck) app_no_db mypy src tests ;;
  check) app sh -c "ruff check src tests && ruff format --check src tests && mypy src tests && pytest" ;;
  validate)
    if [ "$#" -gt 0 ]; then
      app_no_db digest validate-config --config "$@"
    else
      app_no_db digest validate-config --config "$(config_path)"
    fi
    ;;
  schedule)
    if [ "$#" -gt 0 ]; then
      app_no_db digest schedule "$@"
    else
      app_no_db digest schedule --config "$(config_path)" --all
    fi
    ;;
  digest) app digest "$@" ;;
  login) app_no_db digest login "$@" ;;
  dialogs) app_no_db digest dialogs ;;
  doctor) app_no_db digest doctor --config "$(config_path)" ;;
  scan) app_no_db digest scan --config "$(config_path)" "$@" ;;
  worker) app digest run --config "$(config_path)" ;;
  status) app_no_db digest status --config "$(config_path)" ;;
  *)
    echo "Unknown command: $command" >&2
    show_help
    exit 1
    ;;
esac
