"""Підключення до PostgreSQL і застосування міграцій Alembic."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def create_db_engine(url: URL | str) -> Engine:
    """Синхронний engine (psycopg 3). В asyncio-коді викликати поза event loop."""
    return create_engine(url, pool_pre_ping=True)


def alembic_config(url: URL | str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    rendered = url.render_as_string(hide_password=False) if isinstance(url, URL) else url
    # Alembic інтерпретує `%` у значеннях ini-конфігу.
    config.set_main_option("sqlalchemy.url", rendered.replace("%", "%%"))
    return config


def upgrade(url: URL | str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: URL | str, revision: str) -> None:
    command.downgrade(alembic_config(url), revision)
