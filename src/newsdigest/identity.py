"""Стабільні ідентичності випуску й частин (architecture.md, «Моделі й ідентичність»)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from newsdigest.scheduling import Window, to_utc

_KEY_VERSION = "v1"
MANUAL_KEY_PREFIX = "manual"


def format_utc(moment: datetime) -> str:
    """Канонічний UTC-рядок фіксованої довжини: лексикографічний порядок = хронологічний."""
    return to_utc(moment).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_utc(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def digest_key(channel_id: str, window: Window, *, request_id: str | None = None) -> str:
    """Канал + межі; для явного ручного випуску також request ID. Без ревізії конфігу."""
    canonical = "|".join(
        (_KEY_VERSION, channel_id, format_utc(window.start_utc), format_utc(window.end_utc))
    )
    if request_id is not None:
        # Explicit reprocessing gets a separate identity; retries reuse the request ID.
        return MANUAL_KEY_PREFIX + hashlib.sha256(f"{canonical}|{request_id}".encode()).hexdigest()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def part_key(digest: str, part_index: int) -> str:
    if part_index < 1:
        raise ValueError("part_index починається з 1")
    return f"{digest}:{part_index}"
