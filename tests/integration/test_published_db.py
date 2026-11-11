from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, Table, func, insert, inspect, select
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

from newsdigest.identity import digest_key, part_key
from newsdigest.scheduling import Window
from newsdigest.storage.db import create_db_engine, downgrade, upgrade
from newsdigest.storage.models import Base, PublishedMessage

WINDOW = Window(datetime(2026, 11, 11, 19, tzinfo=UTC), datetime(2026, 11, 12, 6, tzinfo=UTC))
DIGEST = digest_key("news", WINDOW)
TABLE = cast(Table, PublishedMessage.__table__)


def row(index: int = 1, count: int = 2, message_id: int = 100, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "part_key": part_key(DIGEST, index),
        "digest_key": DIGEST,
        "part_index": index,
        "part_count": count,
        "series_manifest_hash": "m" * 64,
        "target_channel_id": "news",
        "destination_peer_id": -1001234567890,
        "destination_message_id": message_id,
        "window_start_utc": WINDOW.start_utc,
        "window_end_utc": WINDOW.end_utc,
        "scheduled_at_utc": WINDOW.end_utc,
        "published_at_utc": WINDOW.end_utc,
        "confirmed_at_utc": WINDOW.end_utc,
        "confirmation_method": "ack",
        "rendered_text": "Новини · 1. Подія — Канал",
        "payload_hash": "p" * 64,
        "random_id": -(2**63) + index,
        "items": [],
        "source_message_ids": [{"type": "telegram", "peer_id": -1002, "message_id": 7}],
        "metadata_recovery": "complete",
    }
    values.update(overrides)
    return values


def put(engine: Engine, values: dict[str, Any]) -> None:
    with engine.begin() as conn:
        conn.execute(insert(TABLE), values)


def test_migration_matches_models(pg_engine: Engine) -> None:
    with pg_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_only_confirmed_publications_table(pg_engine: Engine) -> None:
    tables = set(inspect(pg_engine).get_table_names()) - {"alembic_version"}
    assert tables == {"published_messages"}


def test_multiple_parts_of_one_digest(pg_engine: Engine) -> None:
    put(pg_engine, row(1, message_id=100))
    put(pg_engine, row(2, message_id=101))
    with pg_engine.connect() as conn:
        count = conn.scalar(select(func.count()).where(TABLE.c.digest_key == DIGEST))
    assert count == 2


@pytest.mark.parametrize(
    "second",
    [
        {"message_id": 101},  # той самий part_key
        {"message_id": 101, "part_key": "other"},  # той самий (digest, part_index)
    ],
)
def test_same_part_twice_rejected(pg_engine: Engine, second: dict[str, Any]) -> None:
    put(pg_engine, row(1))
    with pytest.raises(IntegrityError):
        put(pg_engine, row(1, **second))


def test_same_telegram_message_twice_rejected(pg_engine: Engine) -> None:
    put(pg_engine, row(1, message_id=100))
    with pytest.raises(IntegrityError):
        put(pg_engine, row(2, message_id=100))


@pytest.mark.parametrize(
    "overrides",
    [
        {"part_index": 3, "part_count": 2},
        {"part_index": 0, "part_count": 2},
        {"part_count": 0, "part_index": 0},
        {"confirmation_method": "maybe"},
        {"metadata_recovery": "unknown"},
        {"usage_scope": "run"},
        {"items": {"not": "array"}},
        {"source_message_ids": "7"},
        {"window_start_utc": WINDOW.end_utc},
        {"payload_hash": None},
        {"source_message_ids": None},
    ],
)
def test_constraints(pg_engine: Engine, overrides: dict[str, Any]) -> None:
    with pytest.raises(IntegrityError):
        put(pg_engine, row(**overrides))


def test_partial_recovery_allows_unknown_metadata(pg_engine: Engine) -> None:
    put(
        pg_engine,
        row(
            confirmation_method="reconciled",
            metadata_recovery="partial",
            payload_hash=None,
            random_id=None,
            items=None,
            source_message_ids=None,
            series_manifest_hash=None,
        ),
    )


def test_find_published_source_ids(pg_engine: Engine) -> None:
    """D-012: пошук уже опублікованого джерельного допису в межах цільового каналу."""
    put(pg_engine, row(1))
    probe = [{"type": "telegram", "peer_id": -1002, "message_id": 7}]
    with pg_engine.connect() as conn:
        found = conn.scalar(
            select(func.count()).where(
                TABLE.c.target_channel_id == "news",
                TABLE.c.source_message_ids.contains(probe),
            )
        )
        other_channel = conn.scalar(
            select(func.count()).where(
                TABLE.c.target_channel_id == "crypto",
                TABLE.c.source_message_ids.contains(probe),
            )
        )
    assert (found, other_channel) == (1, 0)


def test_timestamps_roundtrip_as_utc(pg_engine: Engine) -> None:
    put(pg_engine, row(1))
    with pg_engine.connect() as conn:
        stored = conn.scalar(select(TABLE.c.window_start_utc))
    assert stored == WINDOW.start_utc


def test_downgrade_and_upgrade_again(migrated_url: URL) -> None:
    downgrade(migrated_url, "base")
    engine = create_db_engine(migrated_url)
    try:
        assert "published_messages" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
        upgrade(migrated_url)


def test_published_source_keys(pg_engine: Engine) -> None:
    from newsdigest.storage.published_repo import published_source_keys

    put(
        pg_engine,
        row(
            1,
            source_message_ids=[
                {"type": "telegram", "peer_id": -1002, "message_id": 7},
                {"type": "site", "url": "https://example.com/a"},
                {"type": "unknown"},
            ],
        ),
    )
    put(pg_engine, row(2, message_id=101, target_channel_id="other"))
    assert published_source_keys(pg_engine, "news") == {
        ("telegram", -1002, 7),
        ("site", "https://example.com/a"),
    }
    assert published_source_keys(pg_engine, "missing") == set()
