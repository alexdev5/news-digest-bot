"""Тимчасовий файловий кеш run: `/cache/runs/<digest_key>/` (v4 §8, §18.2).

- `manifest.json` — безконтентний прогрес: вікно, TTL, статуси й cursor кожного джерела.
- `messages.jsonl` — отримані тексти джерел; живуть лише до TTL/завершення run.

Crash-safety (T16): сторінка спершу дописується в JSONL із fsync, потім атомарно
оновлюється cursor у manifest. Після падіння сторінку можна прочитати повторно;
дублікати рядків прибираються ключем `(source_id, message_id, version_hash)`.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from newsdigest.state.atomic import (
    StateCorruptError,
    fsync_directory,
    read_json_checked,
    write_json_atomic,
)

DEFAULT_TTL_SECONDS = 86_400
DEFAULT_MAX_BYTES = 512 * 1024 * 1024

ProgressStatus = Literal["pending", "complete", "partial", "unavailable", "excluded"]


class CacheModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class SourceProgress(CacheModel):
    source_type: Literal["telegram", "site"] = "telegram"
    peer_id: int | None = None
    status: ProgressStatus = "pending"
    cursor_message_id: int = 0
    pages: int = 0
    fetched: int = 0
    in_window: int = 0
    excluded: dict[str, int] = Field(default_factory=dict)
    edited_after_cutoff: int = 0
    error_code: str | None = None


class RunManifest(CacheModel):
    digest_key: str
    channel_id: str
    window_start_utc: AwareDatetime
    window_end_utc: AwareDatetime
    created_at_utc: AwareDatetime
    expires_at_utc: AwareDatetime
    sources: dict[str, SourceProgress] = Field(default_factory=dict)
    excluded_sources: list[dict[str, str | int]] = Field(default_factory=list)


class CachedMessage(CacheModel):
    """Отриманий джерельний допис. ТІЛЬКИ в кеші, ніколи в БД чи `/state`."""

    source_id: str
    source_type: Literal["telegram", "site"] = "telegram"
    peer_id: int
    message_id: int
    date_utc: AwareDatetime
    edit_date_utc: AwareDatetime | None = None
    fetched_at_utc: AwareDatetime
    text: str
    grouped_id: int | None = None
    forward_from_peer_id: int | None = None
    version_hash: str
    url: str

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.source_id, self.message_id, self.version_hash)


class RunCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @property
    def manifest_path(self) -> Path:
        return self.directory / "manifest.json"

    @property
    def messages_path(self) -> Path:
        return self.directory / "messages.jsonl"

    def exists(self) -> bool:
        return self.manifest_path.exists()

    def load_manifest(self) -> RunManifest:
        data = read_json_checked(self.manifest_path)
        if data is None:
            raise FileNotFoundError(self.manifest_path)
        try:
            return RunManifest.model_validate(data)
        except ValidationError as exc:
            raise StateCorruptError(f"{self.manifest_path}: не відповідає схемі") from exc

    def save_manifest(self, manifest: RunManifest) -> None:
        write_json_atomic(self.manifest_path, manifest.model_dump(mode="json"))

    def append_page(
        self, manifest: RunManifest, source_id: str, records: Iterable[CachedMessage]
    ) -> None:
        """Дописує сторінку й лише потім фіксує прогрес джерела в manifest."""
        lines = [r.model_dump_json() + "\n" for r in records]
        if lines:
            self.directory.mkdir(parents=True, exist_ok=True)
            with open(self.messages_path, "a", encoding="utf-8") as handle:
                handle.writelines(lines)
                handle.flush()
                os.fsync(handle.fileno())
            fsync_directory(self.directory)
        self.save_manifest(manifest)

    def read_messages(self) -> list[CachedMessage]:
        if not self.messages_path.exists():
            return []
        seen: dict[tuple[str, int, str], CachedMessage] = {}
        with open(self.messages_path, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    message = CachedMessage.model_validate_json(line)
                except ValidationError:
                    # Обірваний останній рядок після аварії: сторінку перечитаємо за cursor.
                    continue
                seen.setdefault(message.key, message)
        return sorted(seen.values(), key=lambda m: (m.date_utc, m.source_id, m.message_id))

    def size_bytes(self) -> int:
        if not self.directory.exists():
            return 0
        return sum(p.stat().st_size for p in self.directory.rglob("*") if p.is_file())

    def delete(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


@dataclass(frozen=True, slots=True)
class CacheEntry:
    digest_key: str
    channel_id: str
    expires_at_utc: datetime
    size_bytes: int


class CacheManager:
    """Каталог `/cache/runs`: створення run-кешу, TTL, ліміт розміру."""

    def __init__(
        self,
        root: Path,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.root = root
        self.ttl = timedelta(seconds=ttl_seconds)
        self.max_bytes = max_bytes

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    def inspection(self) -> CacheManager:
        """CLI scan/preview never share payloads with production delivery."""
        return CacheManager(self.root / "inspection", int(self.ttl.total_seconds()), self.max_bytes)

    def run(self, digest_key: str) -> RunCache:
        if not digest_key.isalnum():
            raise ValueError("digest_key має містити тільки літери й цифри")
        return RunCache(self.runs_dir / digest_key)

    def open_or_create(
        self,
        digest_key: str,
        channel_id: str,
        window_start: datetime,
        window_end: datetime,
        now: datetime,
    ) -> tuple[RunCache, RunManifest]:
        """Відкриває чинний кеш run (для продовження) або створює новий.

        TTL відлічується від створення й не подовжується повторним відкриттям (v4 §8.4).
        """
        cache = self.run(digest_key)
        if cache.exists():
            manifest = cache.load_manifest()
            if manifest.expires_at_utc > now:
                return cache, manifest
            cache.delete()
        manifest = RunManifest(
            digest_key=digest_key,
            channel_id=channel_id,
            window_start_utc=window_start,
            window_end_utc=window_end,
            created_at_utc=now,
            expires_at_utc=now + self.ttl,
        )
        cache.directory.mkdir(parents=True, exist_ok=True)
        cache.save_manifest(manifest)
        return cache, manifest

    def entries(self) -> list[CacheEntry]:
        if not self.runs_dir.exists():
            return []
        result: list[CacheEntry] = []
        for directory in sorted(self.runs_dir.iterdir()):
            cache = RunCache(directory)
            if not directory.is_dir() or not cache.exists():
                continue
            try:
                manifest = cache.load_manifest()
            except (StateCorruptError, FileNotFoundError):
                continue
            result.append(
                CacheEntry(
                    digest_key=manifest.digest_key,
                    channel_id=manifest.channel_id,
                    expires_at_utc=manifest.expires_at_utc,
                    size_bytes=cache.size_bytes(),
                )
            )
        return result

    def total_bytes(self) -> int:
        return sum(e.size_bytes for e in self.entries())

    def cleanup_expired(self, now: datetime, active: set[str] | None = None) -> list[str]:
        """Видаляє прострочені run-кеші, крім активних читачів. Повертає digest_key."""
        protected = active or set()
        removed: list[str] = []
        for entry in self.entries():
            if entry.expires_at_utc <= now and entry.digest_key not in protected:
                self.run(entry.digest_key).delete()
                removed.append(entry.digest_key)
        return removed

    def over_limit(self) -> bool:
        return self.total_bytes() > self.max_bytes


def dump_counts(messages: Iterable[CachedMessage]) -> dict[str, int]:
    """Кількість дописів за джерелами — безпечні для логів числа без текстів."""
    counts: dict[str, int] = {}
    for message in messages:
        counts[message.source_id] = counts.get(message.source_id, 0) + 1
    return dict(sorted(counts.items()))


__all__ = [
    "CacheEntry",
    "CacheManager",
    "CachedMessage",
    "RunCache",
    "RunManifest",
    "SourceProgress",
    "dump_counts",
]
