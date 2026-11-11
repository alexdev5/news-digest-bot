"""Налаштування середовища (не JSON-конфіг каналів): підключення до PostgreSQL.

Пароль береться з `POSTGRES_PASSWORD_FILE` (secret file, production) або
`POSTGRES_PASSWORD` (локальна розробка). У JSON-конфіг секрети не потрапляють.
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


def database_settings_from_env(env: Mapping[str, str] | None = None) -> DatabaseSettings:
    values = os.environ if env is None else env
    password_file = values.get("POSTGRES_PASSWORD_FILE")
    if password_file:
        try:
            password = Path(password_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SettingsError(f"не вдалося прочитати POSTGRES_PASSWORD_FILE: {exc}") from exc
    else:
        password = values.get("POSTGRES_PASSWORD", "")
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
