"""CLI `digest`. На етапі 1 — команди без Telegram/OpenAI: конфіг, розклад, міграції БД."""

from __future__ import annotations

import io
import sys
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path
from typing import Annotated

import typer

from newsdigest.config import ConfigError, LoadedConfig, effective_channel, load_config
from newsdigest.scheduling import upcoming_boundaries, window_for_boundary


class ExitCode(IntEnum):
    OK = 0
    CONFIG_ERROR = 2
    USAGE_ERROR = 3
    DATABASE_ERROR = 4


app = typer.Typer(
    name="digest",
    help="NewsDigest: тематичні дайджести з Telegram-каналів і сайтів.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _setup() -> None:
    # На Windows перенаправлений вивід за замовчуванням у cp1251: кирилиця й символи
    # на кшталт «→» мають виводитися однаково в терміналі, файлі та Docker-логах.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper) and stream.encoding.lower() != "utf-8":
            stream.reconfigure(encoding="utf-8", errors="replace")


ConfigOption = Annotated[
    Path,
    typer.Option("--config", "-c", help="Шлях до JSON-конфігу.", dir_okay=False),
]


def _load_or_exit(path: Path) -> LoadedConfig:
    try:
        return load_config(path)
    except ConfigError as exc:
        typer.echo("Конфігурація невалідна:", err=True)
        for problem in exc.problems:
            typer.echo(f"  - {problem}", err=True)
        raise typer.Exit(ExitCode.CONFIG_ERROR) from exc


@app.command("validate-config")
def validate_config(config: ConfigOption = Path("config/config.json")) -> None:
    """Локальна перевірка конфігу без мережі, платних викликів і публікацій."""
    loaded = _load_or_exit(config)
    channels = loaded.config.target_channels
    typer.echo(f"OK: {config} — цільових каналів: {len(channels)}")
    for channel in channels:
        state = "увімкнено" if channel.enabled else "вимкнено"
        telegram = sum(s.enabled for s in channel.source_channels)
        sites = sum(s.enabled for s in channel.source_sites)
        typer.echo(
            f"  {channel.id} ({state}): Telegram-джерел {telegram}, сайтів {sites}, "
            f"ціль {channel.parsed_telegram()}"
        )


@app.command("schedule")
def schedule(
    config: ConfigOption = Path("config/config.json"),
    channel: Annotated[str | None, typer.Option(help="id цільового каналу.")] = None,
    all_channels: Annotated[bool, typer.Option("--all", help="Усі цільові канали.")] = False,
    next_count: Annotated[int, typer.Option("--next", min=1, max=100)] = 5,
) -> None:
    """Найближчі планові межі та їхні вікна (з урахуванням scan_start_at)."""
    loaded = _load_or_exit(config)
    if all_channels == (channel is not None):
        typer.echo("Вкажіть або --channel <id>, або --all.", err=True)
        raise typer.Exit(ExitCode.USAGE_ERROR)
    if channel is not None:
        try:
            selected = [loaded.config.channel(channel)]
        except KeyError:
            typer.echo(f"Невідомий цільовий канал: {channel}", err=True)
            raise typer.Exit(ExitCode.USAGE_ERROR) from None
    else:
        selected = list(loaded.config.target_channels)

    now = datetime.now(UTC)
    for target in selected:
        eff = effective_channel(loaded.config, target)
        typer.echo(f"{target.id} — {target.name} ({eff.timezone.key})")
        cursor = max(now, target.scan_start_at)
        for boundary in upcoming_boundaries(eff.trigger, cursor, next_count):
            window = window_for_boundary(eff.trigger, boundary, target.scan_start_at)
            if window is None:  # pragma: no cover - cursor >= scan_start_at
                continue
            start = window.start_utc.astimezone(eff.timezone)
            end = window.end_utc.astimezone(eff.timezone)
            typer.echo(f"  {start:%Y-%m-%d %H:%M %Z} → {end:%Y-%m-%d %H:%M %Z}")


@app.command("migrate")
def migrate(
    revision: Annotated[str, typer.Argument(help="Цільова ревізія Alembic.")] = "head",
) -> None:
    """Застосувати міграції схеми PostgreSQL (підключення — зі змінних POSTGRES_*)."""
    from sqlalchemy.exc import SQLAlchemyError

    from newsdigest.settings import SettingsError, database_settings_from_env
    from newsdigest.storage.db import upgrade

    try:
        settings = database_settings_from_env()
        upgrade(settings.url(), revision)
    except SettingsError as exc:
        typer.echo(f"Налаштування БД: {exc}", err=True)
        raise typer.Exit(ExitCode.DATABASE_ERROR) from exc
    except SQLAlchemyError as exc:
        typer.echo(f"Помилка БД: {exc.__class__.__name__}: {exc}", err=True)
        raise typer.Exit(ExitCode.DATABASE_ERROR) from exc
    typer.echo(f"OK: схема БД оновлена до {revision}")


def main() -> None:  # pragma: no cover
    app()
