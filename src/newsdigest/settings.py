"""Налаштування середовища (не JSON-конфіг каналів).

PostgreSQL, Telegram API та робочі каталоги. Секрет `X` береться з файлу `X_FILE`
(production, compose secrets) або зі змінної `X` (локальна розробка).
У JSON-конфіг секрети не потрапляють.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.engine import URL


class SettingsError(RuntimeError):
    """Не задані або некоректні змінні середовища."""


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    host: str
    port: int
    name: str
    user: str
    password: str = field(repr=False)

    def url(self, database: str | None = None) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=database or self.name,
        )


def read_secret(values: Mapping[str, str], name: str) -> str:
    """Значення секрету з `<name>_FILE` або `<name>`; порожній рядок, якщо не задано."""
    secret_file = values.get(f"{name}_FILE")
    if secret_file:
        try:
            return Path(secret_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SettingsError(f"не вдалося прочитати {name}_FILE: {exc.strerror}") from exc
    return values.get(name, "").strip()


def database_settings_from_env(env: Mapping[str, str] | None = None) -> DatabaseSettings:
    values = os.environ if env is None else env
    password = read_secret(values, "POSTGRES_PASSWORD")
    if not password:
        raise SettingsError("не задано POSTGRES_PASSWORD або POSTGRES_PASSWORD_FILE")
    raw_port = values.get("POSTGRES_PORT", "5432")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise SettingsError(f"POSTGRES_PORT має бути числом: {raw_port!r}") from exc
    return DatabaseSettings(
        host=values.get("POSTGRES_HOST", "localhost"),
        port=port,
        name=values.get("POSTGRES_DB", "news_digest"),
        user=values.get("POSTGRES_USER", "news_digest"),
        password=password,
    )


@dataclass(frozen=True, slots=True)
class TelegramSettings:
    api_id: int
    api_hash: str = field(repr=False)


def telegram_settings_from_env(env: Mapping[str, str] | None = None) -> TelegramSettings:
    """`TELEGRAM_API_ID(_FILE)` і `TELEGRAM_API_HASH(_FILE)` з my.telegram.org."""
    values = os.environ if env is None else env
    raw_id = read_secret(values, "TELEGRAM_API_ID")
    api_hash = read_secret(values, "TELEGRAM_API_HASH")
    if not raw_id or not api_hash:
        raise SettingsError("не задано TELEGRAM_API_ID/TELEGRAM_API_HASH (або відповідні *_FILE)")
    try:
        api_id = int(raw_id)
    except ValueError as exc:
        raise SettingsError("TELEGRAM_API_ID має бути числом") from exc
    return TelegramSettings(api_id=api_id, api_hash=api_hash)


@dataclass(frozen=True, slots=True)
class Paths:
    """Робочі каталоги: технічний стан, тимчасовий кеш, сесія Telegram."""

    state_dir: Path
    cache_dir: Path
    session_dir: Path

    @property
    def session_file(self) -> Path:
        return self.session_dir / "newsdigest.session"


def paths_from_env(env: Mapping[str, str] | None = None) -> Paths:
    values = os.environ if env is None else env
    return Paths(
        state_dir=Path(values.get("NEWSDIGEST_STATE_DIR", "/state")),
        cache_dir=Path(values.get("NEWSDIGEST_CACHE_DIR", "/cache")),
        session_dir=Path(values.get("NEWSDIGEST_SESSION_DIR", "/session")),
    )
