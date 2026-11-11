"""Читання підтверджених публікацій: які джерельні дописи вже використано (D-012)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import Engine, Table, select

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
