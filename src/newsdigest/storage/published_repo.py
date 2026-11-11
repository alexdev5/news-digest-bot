"""Читання підтверджених публікацій: які джерельні дописи вже використано (D-012)."""

from __future__ import annotations

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
