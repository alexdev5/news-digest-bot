"""Сканування історії джерел для одного run (v4 §8, §9).

Читає лише джерела цільового каналу і лише вікно `[start, end)`: посторінково від
`end` назад, доки не дійде до `start` або кінця історії. Без фонового збору.
Тексти пишуться тільки в кеш run; у manifest і `/state` — лише статуси й лічильники.
"""

from __future__ import annotations

import functools
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from newsdigest.cache.run_cache import CachedMessage, RunCache, RunManifest, SourceProgress
from newsdigest.config.models import SourceChannel, TargetChannel
from newsdigest.scheduling import Window
from newsdigest.state.store import PinnedPeer, StateStore
from newsdigest.telegram.rate_limit import RateLimiter
from newsdigest.telegram.types import (
    FloodWaitPause,
    RawMessage,
    ResolvedPeer,
    TelegramError,
    TelegramGateway,
)
from newsdigest.telegram_refs import parse_telegram_ref

PAGE_SIZE = 100
MAX_PAGES_PER_SOURCE = 500

Clock = Callable[[], datetime]


def version_hash(message: RawMessage) -> str:
    edit = message.edit_date_utc.isoformat() if message.edit_date_utc else ""
    payload = f"{message.message_id}|{edit}|{message.text}".encode()
    return hashlib.sha256(payload).hexdigest()


def classify(message: RawMessage, window: Window) -> str | None:
    """Причина виключення (машинний код v4 §10) або None, якщо допис придатний."""
    if message.is_service:
        return "service"
    if not message.text.strip():
        return "unsupported_media" if message.has_media else "invalid_content"
    if message.edit_date_utc is not None and message.edit_date_utc >= window.end_utc:
        # Редакцію після cutoff не видаємо за відому до cutoff, а старої версії немає (v4 §9).
        return "post_cutoff_edit"
    return None


@dataclass(frozen=True, slots=True)
class ScanSummary:
    """Безпечний для логів підсумок: лише числа й статуси."""

    statuses: dict[str, str]
    in_window: int
    excluded: dict[str, int]

    @property
    def complete(self) -> bool:
        return all(s == "complete" for s in self.statuses.values())


class HistoryScanner:
    def __init__(
        self,
        gateway: TelegramGateway,
        limiter: RateLimiter,
        store: StateStore,
        clock: Clock,
        page_size: int = PAGE_SIZE,
    ) -> None:
        self._gateway = gateway
        self._limiter = limiter
        self._store = store
        self._clock = clock
        self._page_size = page_size

    async def resolve_pinned(self, ref: str | int) -> ResolvedPeer:
        """Резолвить ref і перевіряє, що він вказує на той самий peer, що й раніше (v4 §6)."""
        key = str(parse_telegram_ref(ref))
        peer = await self._limiter.call(lambda: self._gateway.resolve(key))
        runtime = self._store.load_runtime()
        pinned = runtime.peers.get(key)
        if pinned is not None and pinned.peer_id != peer.peer_id:
            raise TelegramError(
                "peer_changed", f"{key} тепер вказує на інший канал; потрібне підтвердження"
            )
        if pinned is None:
            runtime.peers[key] = PinnedPeer(
                peer_id=peer.peer_id,
                title=peer.title,
                username=peer.username,
                pinned_at_utc=self._clock(),
            )
            self._store.save_runtime(runtime)
        return peer

    async def scan(
        self, channel: TargetChannel, window: Window, cache: RunCache, manifest: RunManifest
    ) -> ScanSummary:
        for site in channel.source_sites:
            if site.id not in manifest.sources:
                # Читач сайтів — етап 2a; до нього сайт явно позначається недоступним.
                manifest.sources[site.id] = SourceProgress(
                    source_type="site",
                    status="excluded" if not site.enabled else "unavailable",
                    error_code=None if not site.enabled else "site_reader_not_implemented",
                )
        for source in channel.source_channels:
            progress = manifest.sources.setdefault(source.id, SourceProgress())
            if not source.enabled:
                progress.status = "excluded"
                progress.error_code = "disabled"
        cache.save_manifest(manifest)

        flood: FloodWaitPause | None = None
        for source in channel.source_channels:
            progress = manifest.sources[source.id]
            # partial — перерване сканування (збій мережі, FloodWait, timeout):
            # продовжуємо з cursor; complete/unavailable/excluded не перечитуємо.
            if progress.status not in ("pending", "partial"):
                continue
            progress.status = "pending"
            progress.error_code = None
            if flood is not None:
                progress.status = "partial"
                progress.error_code = flood.code
                continue
            try:
                await self._scan_source(source, window, cache, manifest, progress)
            except FloodWaitPause as exc:
                flood = exc
                progress.status = "partial"
                progress.error_code = exc.code
            except TelegramError as exc:
                progress.status = "unavailable" if progress.pages == 0 else "partial"
                progress.error_code = exc.code
            cache.save_manifest(manifest)
        cache.save_manifest(manifest)
        return summarize(manifest)

    async def _scan_source(
        self,
        source: SourceChannel,
        window: Window,
        cache: RunCache,
        manifest: RunManifest,
        progress: SourceProgress,
    ) -> None:
        peer = await self.resolve_pinned(source.ref)
        if not peer.is_broadcast:
            raise TelegramError("not_broadcast", f"{source.id}: джерело не є каналом")
        progress.peer_id = peer.peer_id

        while progress.pages < MAX_PAGES_PER_SOURCE:
            cursor = progress.cursor_message_id
            page = await self._limiter.call(
                functools.partial(
                    self._gateway.history_page,
                    peer.peer_id,
                    before=window.end_utc,
                    offset_id=cursor,
                    limit=self._page_size,
                )
            )
            if not page:
                progress.status = "complete"
                cache.save_manifest(manifest)
                return
            fetched_at = self._clock()
            records: list[CachedMessage] = []
            for message in page:
                if not window.contains(message.date_utc):
                    continue
                reason = classify(message, window)
                if reason is not None:
                    progress.excluded[reason] = progress.excluded.get(reason, 0) + 1
                    continue
                records.append(
                    CachedMessage(
                        source_id=source.id,
                        peer_id=peer.peer_id,
                        message_id=message.message_id,
                        date_utc=message.date_utc,
                        edit_date_utc=message.edit_date_utc,
                        fetched_at_utc=fetched_at,
                        text=message.text,
                        grouped_id=message.grouped_id,
                        forward_from_peer_id=message.forward_from_peer_id,
                        version_hash=version_hash(message),
                        url=peer.message_url(message.message_id),
                    )
                )
            oldest_id = min(m.message_id for m in page)
            if cursor and oldest_id >= cursor:
                raise TelegramError("pagination_stalled", f"{source.id}: пагінація не просувається")
            progress.pages += 1
            progress.fetched += len(page)
            progress.in_window += len(records)
            progress.cursor_message_id = oldest_id
            reached_start = min(m.date_utc for m in page) < window.start_utc
            if reached_start:
                progress.status = "complete"
            cache.append_page(manifest, source.id, records)
            if reached_start:
                return
        raise TelegramError("too_many_pages", f"{source.id}: перевищено ліміт сторінок")


def summarize(manifest: RunManifest) -> ScanSummary:
    excluded: dict[str, int] = {}
    for progress in manifest.sources.values():
        for reason, count in progress.excluded.items():
            excluded[reason] = excluded.get(reason, 0) + count
    return ScanSummary(
        statuses={sid: p.status for sid, p in manifest.sources.items()},
        in_window=sum(p.in_window for p in manifest.sources.values()),
        excluded=dict(sorted(excluded.items())),
    )
