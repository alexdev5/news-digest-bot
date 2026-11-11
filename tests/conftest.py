"""Спільні fixtures. Тести БД працюють на тимчасовій базі в локальному PostgreSQL.

Кожна сесія pytest створює окрему базу `newsdigest_test_<random>`, застосовує
міграції й видаляє базу наприкінці. Без доступного PostgreSQL тести БД
пропускаються; з `NEWSDIGEST_REQUIRE_DB=1` (у Docker) — падають.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError

from newsdigest.settings import SettingsError, database_settings_from_env
from newsdigest.storage.db import create_db_engine, upgrade

_CONNECT_TIMEOUT_SECONDS = 3


def _unavailable(reason: str) -> None:
    if os.environ.get("NEWSDIGEST_REQUIRE_DB") == "1":
        pytest.fail(f"PostgreSQL недоступний: {reason}")
    pytest.skip(f"PostgreSQL недоступний: {reason}")


@pytest.fixture(scope="session")
def pg_url() -> Iterator[URL]:
    try:
        settings = database_settings_from_env()
    except SettingsError as exc:
        _unavailable(str(exc))
        raise  # pragma: no cover
    admin = create_engine(
        settings.url("postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": _CONNECT_TIMEOUT_SECONDS},
    )
    name = f"newsdigest_test_{uuid.uuid4().hex[:12]}"
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except OperationalError as exc:
        admin.dispose()
        _unavailable(str(exc.orig))
        raise  # pragma: no cover
    try:
        yield settings.url(name)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def migrated_url(pg_url: URL) -> URL:
    upgrade(pg_url)
    return pg_url


@pytest.fixture
def pg_engine(migrated_url: URL) -> Iterator[Engine]:
    engine = create_db_engine(migrated_url)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE published_messages RESTART IDENTITY"))
    yield engine
    engine.dispose()
