from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from newsdigest.identity import digest_key, format_utc, parse_utc, part_key
from newsdigest.scheduling import Window
from newsdigest.settings import SettingsError, database_settings_from_env

WINDOW = Window(datetime(2026, 11, 11, 19, tzinfo=UTC), datetime(2026, 11, 12, 6, tzinfo=UTC))


def test_database_settings_defaults() -> None:
    settings = database_settings_from_env({"POSTGRES_PASSWORD": "p@ss:w/rd"})
    url = settings.url()
    assert (url.host, url.port, url.database, url.username) == (
        "localhost",
        5432,
        "news_digest",
        "news_digest",
    )
    assert url.drivername == "postgresql+psycopg"
    assert "p@ss" not in repr(settings)
    assert "p%40ss%3Aw%2Frd" in url.render_as_string(hide_password=False)


def test_password_file(tmp_path: Path) -> None:
    secret = tmp_path / "pg_password"
    secret.write_text("from-file\n", encoding="utf-8")
    settings = database_settings_from_env(
        {"POSTGRES_PASSWORD_FILE": str(secret), "POSTGRES_PASSWORD": "ignored"}
    )
    assert settings.password == "from-file"


@pytest.mark.parametrize(
    "env",
    [{}, {"POSTGRES_PASSWORD": "x", "POSTGRES_PORT": "abc"}, {"POSTGRES_PASSWORD_FILE": "/nope"}],
)
def test_bad_settings(env: dict[str, str]) -> None:
    with pytest.raises(SettingsError):
        database_settings_from_env(env)


def test_identity_helpers() -> None:
    digest = digest_key("news", WINDOW)
    assert len(digest) == 64
    assert digest_key("news", WINDOW) == digest
    assert digest_key("other", WINDOW) != digest
    assert part_key(digest, 2) == f"{digest}:2"
    with pytest.raises(ValueError):
        part_key(digest, 0)
    moment = datetime(2026, 11, 12, 6, 0, 0, 123, tzinfo=UTC)
    assert parse_utc(format_utc(moment)) == moment
    assert format_utc(moment) == "2026-11-12T06:00:00.000123Z"
