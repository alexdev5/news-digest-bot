param(
    [Parameter(Position = 0)]
    [string]$Command = "help",

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

$env:DOCKER_BUILDKIT = "0"
$env:COMPOSE_DOCKER_CLI_BUILD = "0"

if ($null -eq $Rest) {
    $Rest = @()
}

function Invoke-App {
    docker compose run --rm app @args
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

function Invoke-AppNoDb {
    docker compose run --rm --no-deps app @args
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

function Get-ConfigPath {
    if (Test-Path "config/config.json") {
        return "config/config.json"
    }
    Write-Host "config/config.json not found, using config/config.example.json"
    return "config/config.example.json"
}

function Show-Help {
    Write-Host "Usage: .\run <command> [args]"
    Write-Host ""
    Write-Host "Docker:"
    Write-Host "  start          Start PostgreSQL (db)"
    Write-Host "  stop           Stop PostgreSQL (db)"
    Write-Host "  ps             Show compose service status"
    Write-Host "  logs [svc]     Follow logs, optionally for one service (default db)"
    Write-Host "  build          Build (rebuild) the app image"
    Write-Host "  shell          Open shell in a one-off app container"
    Write-Host "  psql           Open psql in the db container"
    Write-Host ""
    Write-Host "Database:"
    Write-Host "  migrate [rev]  Apply Alembic migrations (default head)"
    Write-Host ""
    Write-Host "Checks:"
    Write-Host "  test [args]    Run pytest; args are passed to pytest"
    Write-Host "  lint           Run ruff check and ruff format --check"
    Write-Host "  fmt            Format code with ruff"
    Write-Host "  typecheck      Run mypy"
    Write-Host "  check          lint + typecheck + test"
    Write-Host ""
    Write-Host "App:"
    Write-Host "  validate [path]  Validate config (default config/config.json or the example)"
    Write-Host "  schedule [args]  Show next windows (default: --all)"
    Write-Host "  digest [args]    Run any digest CLI command"
    Write-Host ""
    Write-Host "Telegram (needs TELEGRAM_API_ID/HASH in .env):"
    Write-Host "  login [--qr]     Interactive Telegram login (session in ./session)"
    Write-Host "  dialogs          List channels with IDs for the config"
    Write-Host "  doctor           Check session, sources and posting rights"
    Write-Host "  scan [args]      Manual scan of one window, e.g. scan --channel dnipro_news"
    Write-Host "  worker           Run the scheduler worker in the foreground"
    Write-Host "  status           Channel state, recent runs, heartbeat"
}

$Command = $Command.ToLowerInvariant()

switch ($Command) {
    "help" {
        Show-Help
    }
    "start" {
        docker compose up -d db
        exit $LASTEXITCODE
    }
    "stop" {
        docker compose stop db
        exit $LASTEXITCODE
    }
    "ps" {
        docker compose ps
    }
    "status" {
        docker compose ps
    }
    "logs" {
        if ($Rest.Count -gt 0) {
            docker compose logs -f --tail=120 @Rest
        }
        else {
            docker compose logs -f --tail=120 db
        }
    }
    "psql" {
        docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
    }
    "migrate" {
        Invoke-App digest migrate @Rest
    }
    "build" {
        docker compose build app
        exit $LASTEXITCODE
    }
    "rebuild" {
        docker compose build app
        exit $LASTEXITCODE
    }
    "shell" {
        docker compose run --rm app sh
    }
    "test" {
        Invoke-App pytest @Rest
    }
    "lint" {
        Invoke-AppNoDb sh -c "ruff check src tests && ruff format --check src tests"
    }
    "fmt" {
        Invoke-AppNoDb ruff format src tests
    }
    "typecheck" {
        Invoke-AppNoDb mypy src tests
    }
    "check" {
        Invoke-App sh -c "ruff check src tests && ruff format --check src tests && mypy src tests && pytest"
    }
    "validate" {
        if ($Rest.Count -gt 0) {
            Invoke-AppNoDb digest validate-config --config @Rest
        }
        else {
            $ConfigPath = Get-ConfigPath
            Invoke-AppNoDb digest validate-config --config $ConfigPath
        }
    }
    "schedule" {
        if ($Rest.Count -gt 0) {
            Invoke-AppNoDb digest schedule @Rest
        }
        else {
            $ConfigPath = Get-ConfigPath
            Invoke-AppNoDb digest schedule --config $ConfigPath --all
        }
    }
    "digest" {
        Invoke-App digest @Rest
    }
    "login" {
        Invoke-AppNoDb digest login @Rest
    }
    "dialogs" {
        Invoke-AppNoDb digest dialogs
    }
    "doctor" {
        $ConfigPath = Get-ConfigPath
        Invoke-AppNoDb digest doctor --config $ConfigPath
    }
    "scan" {
        $ConfigPath = Get-ConfigPath
        Invoke-AppNoDb digest scan --config $ConfigPath @Rest
    }
    "worker" {
        $ConfigPath = Get-ConfigPath
        Invoke-App digest run --config $ConfigPath
    }
    "status" {
        $ConfigPath = Get-ConfigPath
        Invoke-AppNoDb digest status --config $ConfigPath
    }
    default {
        Write-Host "Unknown command: $Command"
        Show-Help
        exit 1
    }
}
