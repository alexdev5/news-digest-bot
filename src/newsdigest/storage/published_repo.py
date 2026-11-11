"""Читання підтверджених публікацій: які джерельні дописи вже використано (D-012)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Engine, Table, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from newsdigest.storage.models import PublishedMessage

_TABLE = cast(Table, PublishedMessage.__table__)


def _key(entry: Any) -> tuple[object, ...] | None:
    if not isinstance(entry, dict):
        return None
    if entry.get("type") == "site" and isinstance(entry.get("url"), str):
        return ("site", entry["url"])
    peer, message = entry.get("peer_id"), entry.get("message_id")
    if entry.get("type") == "telegram" and isinstance(peer, int) and isinstance(message, int):
        return ("telegram", peer, message)
    return None


def published_source_keys(engine: Engine, channel_id: str) -> set[tuple[object, ...]]:
    """Ключі всіх джерельних дописів, уже використаних у підтверджених частинах каналу."""
    query = select(_TABLE.c.source_message_ids).where(
        _TABLE.c.target_channel_id == channel_id,
        _TABLE.c.source_message_ids.is_not(None),
    )
    keys: set[tuple[object, ...]] = set()
    with engine.connect() as conn:
        for (entries,) in conn.execute(query):
            for entry in entries or ():
                key = _key(entry)
                if key is not None:
                    keys.add(key)
    return keys


def recent_items(
    engine: Engine, channel_id: str, since: datetime, limit: int = 200
) -> list[tuple[str, str, datetime]]:
    """Власні опубліковані пункти каналу після `since`: (заголовок, виклад, час).

    Міжвипускна пам'ять (v4 §11) — лише з уже підтверджених публікацій.
    """
    query = (
        select(_TABLE.c["items"], _TABLE.c.published_at_utc)
        .where(
            _TABLE.c.target_channel_id == channel_id,
            _TABLE.c.published_at_utc >= since,
            _TABLE.c["items"].is_not(None),
        )
        .order_by(_TABLE.c.published_at_utc)
    )
    result: list[tuple[str, str, datetime]] = []
    with engine.connect() as conn:
        for items, published_at in conn.execute(query):
            for item in items or ():
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                summary = str(item.get("short_summary") or "").strip()
                if title or summary:
                    result.append((title, summary, published_at))
    return result[-limit:]


@dataclass(frozen=True, slots=True)
class ConfirmedPart:
    """Підтверджена Telegram-повідомленням частина випуску — єдине, що пишеться в БД."""

    part_key: str
    digest_key: str
    part_index: int
    part_count: int
    series_manifest_hash: str
    target_channel_id: str
    destination_peer_id: int
    destination_message_id: int
    window_start_utc: datetime
    window_end_utc: datetime
    scheduled_at_utc: datetime
    published_at_utc: datetime
    confirmed_at_utc: datetime
    confirmation_method: str
    rendered_text: str
    entities: list[dict[str, object]]
    payload_hash: str
    random_id: int
    items: list[dict[str, Any]]
    source_message_ids: list[dict[str, object]]
    source_order_snapshot: list[str]
    coverage_summary: dict[str, Any] | None
    ai_usage_summary: dict[str, Any] | None
    usage_scope: str | None


def insert_confirmed_part(engine: Engine, part: ConfirmedPart) -> bool:
    """Ідемпотентний insert: повтор того самого part_key нічого не змінює. True — вставлено."""
    values = {name: getattr(part, name) for name in ConfirmedPart.__slots__}
    values["metadata_recovery"] = "complete"
    statement = (
        pg_insert(_TABLE)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["part_key"])
        .returning(_TABLE.c.publication_id)
    )
    with engine.begin() as conn:
        inserted = conn.execute(statement).first()
    return inserted is not None


def committed_part_keys(engine: Engine, digest_key: str) -> set[str]:
    query = select(_TABLE.c.part_key).where(_TABLE.c.digest_key == digest_key)
    with engine.connect() as conn:
        return {row[0] for row in conn.execute(query)}


def used_message_ids(engine: Engine, destination_peer_id: int) -> set[int]:
    query = select(_TABLE.c.destination_message_id).where(
        _TABLE.c.destination_peer_id == destination_peer_id
    )
    with engine.connect() as conn:
        return {int(row[0]) for row in conn.execute(query)}


def last_published_window_end(engine: Engine, channel_id: str) -> datetime | None:
    """Кінець вікна останнього опублікованого випуску каналу (D-029) або None."""
    from sqlalchemy import func

    query = select(func.max(_TABLE.c.window_end_utc)).where(
        _TABLE.c.target_channel_id == channel_id
    )
    with engine.connect() as conn:
        value = conn.execute(query).scalar()
    return value if isinstance(value, datetime) else None
