@echo off
rem Local command wrapper for this project.
rem Run it from the repository root, for example:
rem   .\run.cmd test
rem   .\run.cmd validate
rem
rem Available commands:
rem   start      - start PostgreSQL (db)
rem   stop       - stop PostgreSQL (db)
rem   ps         - show Docker Compose service status
rem   logs       - follow logs (default db); pass a service name to filter
rem   psql       - open psql in the db container
rem   migrate    - apply Alembic migrations (default head)
rem   build      - build (rebuild) the app Docker image
rem   shell      - open a shell in a one-off app container
rem   test       - run pytest; pass args to pytest
rem   lint       - ruff check + ruff format --check
rem   fmt        - format code with ruff
rem   typecheck  - run mypy
rem   check      - lint + typecheck + test
rem   validate   - validate config (default config/config.json or the example)
rem   schedule   - show next windows (default: --all)
rem   digest     - run any digest CLI command
rem   login      - interactive Telegram login; login --qr to scan a QR code
rem   dialogs    - list channels with IDs for the config
rem   doctor     - check session, sources and posting rights
rem   scan       - manual scan of one window: scan --channel <id>
rem   worker     - run the scheduler worker in the foreground
rem   status     - channel state, recent runs, heartbeat
rem
rem This .cmd file keeps Windows usage short and delegates real logic to
rem run.ps1, where Docker legacy builder env vars are set automatically.
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
exit /b %ERRORLEVEL%
