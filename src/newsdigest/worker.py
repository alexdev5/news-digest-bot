"""Worker: чекає планових меж цільових каналів і сканує їхні вікна (етап 2).

Між межами джерела не читаються (D-004, T11): цикл лише чекає часу, оновлює
heartbeat і прибирає прострочений кеш. На межі — сканування вікна в кеш run.
Обробка AI, рендер і публікація — етапи 3–5; зараз run завершується статусом
`scanned`, а кеш живе до TTL.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta

from newsdigest.cache.run_cache import CacheManager, RunManifest
from newsdigest.config.effective import effective_channel
from newsdigest.config.models import AppConfig, TargetChannel
from newsdigest.delivery import Delivery, PublishMeta, PublishResult
from newsdigest.digest_runner import DigestBuilder, DigestOutcome
from newsdigest.identity import digest_key
from newsdigest.scheduling import (
    Revision,
    Window,
    apply_schedule_change,
    due_boundaries_rev,
    next_boundary_rev,
    window_for_boundary_rev,
)
from newsdigest.state.store import (
    ChannelState,
    Heartbeat,
    RevisionState,
    RunRecord,
    SourceCoverage,
    StateStore,
)
from newsdigest.telegram.rate_limit import RateLimiter
from newsdigest.telegram.scanner import HistoryScanner, ScanSummary, summarize
from newsdigest.telegram.types import FloodWaitPause, TelegramError, TelegramGateway

log = logging.getLogger("newsdigest.worker")

Clock = Callable[[], datetime]
GatewayFactory = Callable[[], AbstractAsyncContextManager[TelegramGateway]]

MAX_IDLE_SECONDS = 30.0
PUBLISHABLE = ("ready", "partial", "uncertain", "held")
RUN_STATUSES = ("published", "partial", "uncertain", "held", "failed")
DEFAULT_RUN_TIMEOUT_SECONDS = 600


@dataclass(frozen=True, slots=True)
class RunOutcome:
    digest_key: str
    window: Window
    status: str
    summary: ScanSummary | None
    error_code: str | None = None


class RunExecutor:
    """Виконує сканування одного вікна: кеш run, підключення, сканер, timeout."""

    def __init__(
        self,
        store: StateStore,
        cache: CacheManager,
        gateway_factory: GatewayFactory,
        limiter: RateLimiter,
        clock: Clock,
        run_timeout_seconds: int = DEFAULT_RUN_TIMEOUT_SECONDS,
    ) -> None:
        self._store = store
        self._cache = cache
        self._gateway_factory = gateway_factory
        self._limiter = limiter
        self._clock = clock
        self._timeout = run_timeout_seconds

    async def scan(self, channel: TargetChannel, window: Window) -> RunOutcome:
        key = digest_key(channel.id, window)
        cache, manifest = self._cache.open_or_create(
            key, channel.id, window.start_utc, window.end_utc, self._clock()
        )
        try:
            async with asyncio.timeout(self._timeout):
                async with self._gateway_factory() as gateway:
                    await self._check_account(gateway)
                    scanner = HistoryScanner(gateway, self._limiter, self._store, self._clock)
                    summary = await scanner.scan(channel, window, cache, manifest)
        except TimeoutError:
            _mark_pending(manifest, "partial", "run_timeout")
            cache.save_manifest(manifest)
            return RunOutcome(key, window, "scanned", summarize(manifest), "run_timeout")
        except FloodWaitPause as exc:
            _mark_pending(manifest, "partial", exc.code)
            cache.save_manifest(manifest)
            return RunOutcome(key, window, "scanned", summarize(manifest), exc.code)
        except TelegramError as exc:
            _mark_pending(manifest, "unavailable", exc.code)
            cache.save_manifest(manifest)
            return RunOutcome(key, window, "failed", summarize(manifest), exc.code)
        return RunOutcome(key, window, "scanned", summary)

    async def _check_account(self, gateway: TelegramGateway) -> None:
        account = await self._limiter.call(gateway.account)
        if account.is_bot:
            raise TelegramError("bot_account", "потрібен звичайний акаунт, не бот")
        runtime = self._store.load_runtime()
        if runtime.account_id is None:
            runtime.account_id = account.account_id
            self._store.save_runtime(runtime)
        elif runtime.account_id != account.account_id:
            raise TelegramError("account_changed", "сесія належить іншому акаунту")

    def discard(self, outcome: RunOutcome) -> None:
        self._cache.run(outcome.digest_key).delete()


def _mark_pending(manifest: RunManifest, status: str, code: str) -> None:
    for progress in manifest.sources.values():
        if progress.status == "pending":
            progress.status = "partial" if status == "partial" else "unavailable"
            progress.error_code = code


class Worker:
    def __init__(
        self,
        config: AppConfig,
        store: StateStore,
        cache: CacheManager,
        executor: RunExecutor,
        clock: Clock,
        builder: DigestBuilder | None = None,
        prompts: dict[str, str] | None = None,
        delivery: Delivery | None = None,
    ) -> None:
        self._delivery = delivery
        self._builder = builder
        self._prompts = prompts or {}
        self._config = config
        self._store = store
        self._cache = cache
        self._executor = executor
        self._clock = clock
        self._channel_locks: dict[str, asyncio.Lock] = {}
        self._active: set[str] = set()
        self._resume: dict[str, list[datetime]] = {}

    def _enabled(self) -> list[TargetChannel]:
        return [c for c in self._config.target_channels if c.enabled]

    def initialize(self) -> None:
        """Активація нових каналів, зміни розкладу, відмітка перерваних runs."""
        now = self._clock()
        runtime = self._store.load_runtime()
        enabled_ids = {c.id for c in self._enabled()}
        for channel in self._config.target_channels:
            state = runtime.channels.get(channel.id)
            if channel.id not in enabled_ids:
                if state is not None:
                    state.paused = True
                continue
            trigger = effective_channel(self._config, channel).trigger
            if state is None:
                # Перша активація: лише реєструємо час, без backfill минулих вікон (v4 §7.4).
                runtime.channels[channel.id] = ChannelState(
                    activated_at_utc=now,
                    scan_start_at_utc=channel.scan_start_at,
                    revisions=[RevisionState.from_revision(Revision(trigger))],
                    last_materialized_utc=now,
                )
                log.info("channel_activated", extra={"channel": channel.id})
                continue
            if state.paused:
                # Повторне ввімкнення: з найближчої майбутньої межі, без лавини випусків.
                state.paused = False
                state.last_materialized_utc = max(state.last_materialized_utc, now)
            state.scan_start_at_utc = channel.scan_start_at
            state.set_revisions(apply_schedule_change(state.revision_tuple(), trigger, now))
            self._plan_resume(channel, state, now)
        self._store.save_runtime(runtime)

    def _plan_resume(self, channel: TargetChannel, state: ChannelState, now: datetime) -> None:
        """Run, перерваний аварією, продовжується в межах grace; старіший — failed (v4 §15)."""
        grace = timedelta(seconds=effective_channel(self._config, channel).misfire_grace_seconds)
        for run in state.runs:
            if run.status not in ("materialized", "scanning") or run.manual:
                continue
            if now - run.boundary_utc <= grace:
                self._resume.setdefault(channel.id, []).append(run.boundary_utc)
            else:
                run.status = "failed"
                run.error_code = "interrupted"
                run.updated_at_utc = now

    def next_wake(self, now: datetime) -> float:
        runtime = self._store.load_runtime()
        soonest = MAX_IDLE_SECONDS
        for channel in self._enabled():
            state = runtime.channels.get(channel.id)
            if state is None:
                continue
            boundary = next_boundary_rev(state.revision_tuple(), state.last_materialized_utc)
            soonest = min(soonest, (boundary - now).total_seconds())
        return max(soonest, 0.5)

    async def tick(self) -> None:
        now = self._clock()
        await asyncio.gather(*(self._process_channel(c, now) for c in self._enabled()))
        self._cleanup_cache(self._clock())
        self._store.save_heartbeat(Heartbeat(at_utc=self._clock(), pid=os.getpid(), phase="idle"))

    async def run_forever(self, stop: asyncio.Event) -> None:
        self.initialize()
        log.info("worker_started", extra={"channels": [c.id for c in self._enabled()]})
        while not stop.is_set():
            await self.tick()
            delay = self.next_wake(self._clock())
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
        log.info("worker_stopped")

    async def _process_channel(self, channel: TargetChannel, now: datetime) -> None:
        lock = self._channel_locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            eff = effective_channel(self._config, channel)
            runtime = self._store.load_runtime()
            state = runtime.channels.get(channel.id)
            if state is None:
                return
            revisions = state.revision_tuple()
            for boundary in sorted(self._resume.pop(channel.id, [])):
                window = window_for_boundary_rev(revisions, boundary, state.scan_start_at_utc)
                if window is not None:
                    log.info("run_resumed", extra={"channel": channel.id})
                    await self._execute(channel, boundary, window)
            runtime = self._store.load_runtime()
            state = runtime.channels[channel.id]
            plan = due_boundaries_rev(
                revisions, state.last_materialized_utc, now, eff.misfire_grace_seconds
            )
            for boundary in plan.skipped:
                if boundary > state.scan_start_at_utc:
                    state.add_skipped(boundary)
                    log.warning(
                        "run_skipped_misfire",
                        extra={"channel": channel.id, "boundary": boundary.isoformat()},
                    )
                state.last_materialized_utc = boundary
            self._store.save_runtime(runtime)
            for boundary in plan.run:
                window = window_for_boundary_rev(revisions, boundary, state.scan_start_at_utc)
                await self._materialize(channel, boundary, window)

    async def _materialize(
        self, channel: TargetChannel, boundary: datetime, window: Window | None
    ) -> None:
        runtime = self._store.load_runtime()
        state = runtime.channels[channel.id]
        state.last_materialized_utc = boundary
        self._store.save_runtime(runtime)
        if window is not None:
            await self._execute(channel, boundary, window)

    async def _execute(self, channel: TargetChannel, boundary: datetime, window: Window) -> None:
        now = self._clock()
        key = digest_key(channel.id, window)
        runtime = self._store.load_runtime()
        state = runtime.channels[channel.id]
        record = state.find_run(key) or RunRecord(
            digest_key=key,
            boundary_utc=boundary,
            window_start_utc=window.start_utc,
            window_end_utc=window.end_utc,
            status="scanning",
            created_at_utc=now,
            updated_at_utc=now,
        )
        record.status = "scanning"
        record.updated_at_utc = now
        state.upsert_run(record)
        self._store.save_runtime(runtime)
        log.info("run_started", extra={"channel": channel.id, "digest_key": key[:12]})

        self._active.add(key)
        digest: DigestOutcome | None = None
        try:
            outcome = await self._executor.scan(channel, window)
            if outcome.status == "scanned" and self._builder is not None:
                eff = effective_channel(self._config, channel)
                prompt = self._prompts.get(channel.id, "")
                digest = await self._builder.build(eff, prompt, window, key)
        except Exception:
            # Непередбачена помилка не зупиняє worker: run позначається failed,
            # інші канали й наступні межі працюють далі.
            log.exception("run_internal_error", extra={"channel": channel.id})
            outcome = RunOutcome(key, window, "failed", None, "internal_error")
            digest = None
        finally:
            self._active.discard(key)

        runtime = self._store.load_runtime()
        state = runtime.channels[channel.id]
        record = state.find_run(key) or record
        record.status = "scanned" if outcome.status == "scanned" else "failed"
        record.error_code = outcome.error_code
        record.updated_at_utc = self._clock()
        if outcome.summary is not None:
            record.coverage = {
                sid: SourceCoverage.model_validate({"status": status})
                for sid, status in outcome.summary.statuses.items()
            }
            record.excluded = dict(outcome.summary.excluded)
        if digest is not None:
            if digest.status == "ready" and digest.built is not None:
                record.status = "ready"
                record.events = len(digest.built.events)
                record.parts = len(digest.built.plan.parts)
                record.ai_tokens = digest.built.tokens or {}
                for reason, count in digest.built.candidates.excluded.items():
                    record.excluded[reason] = record.excluded.get(reason, 0) + count
            elif digest.status == "failed":
                record.status = "failed"
                record.error_code = digest.error_code
            else:
                record.error_code = record.error_code or digest.error_code
        state.upsert_run(record)
        self._store.save_runtime(runtime)
        log.info(
            "run_finished",
            extra={
                "channel": channel.id,
                "digest_key": key[:12],
                "status": record.status,
                "error_code": outcome.error_code,
                "in_window": outcome.summary.in_window if outcome.summary else 0,
                "events": record.events,
                "parts": record.parts,
                "ai_tokens": record.ai_tokens,
                "coverage": outcome.summary.statuses if outcome.summary else {},
            },
        )
        if record.status == "ready" and self._delivery is not None:
            await self.publish_pending(channel, record.boundary_utc)

    async def publish_pending(self, channel: TargetChannel, up_to: datetime) -> None:
        """Публікує готові/незавершені runs каналу хронологічно; зупиняється на першому збої.

        Новіший випуск не обганяє незавершену старішу серію (v4 §7.4, T71).
        """
        assert self._delivery is not None
        state = self._store.load_runtime().channels[channel.id]
        pending = sorted(
            (r for r in state.runs if r.status in PUBLISHABLE and r.boundary_utc <= up_to),
            key=lambda r: r.boundary_utc,
        )
        blocked = False
        for run in pending:
            if blocked:
                self._update_run(channel.id, run.digest_key, status="held")
                continue
            meta = PublishMeta(
                scheduled_at_utc=run.boundary_utc,
                source_order=[s.id for s in channel.source_channels]
                + [s.id for s in channel.source_sites],
                coverage={k: v.status for k, v in run.coverage.items()},
                ai_usage=dict(run.ai_tokens) or None,
            )
            self._active.add(run.digest_key)
            try:
                result = await self._delivery.deliver(channel, run.digest_key, meta)
            except Exception:
                log.exception("publish_internal_error", extra={"channel": channel.id})
                result = PublishResult(status="partial", error_code="internal_error")
            finally:
                self._active.discard(run.digest_key)
            status = result.status if result.status in RUN_STATUSES else "held"
            self._update_run(
                channel.id, run.digest_key, status=status, error_code=result.error_code
            )
            log.info(
                "publish_finished",
                extra={
                    "channel": channel.id,
                    "digest_key": run.digest_key[:12],
                    "status": status,
                    "error_code": result.error_code,
                    "parts": f"{result.committed}/{result.total}",
                },
            )
            if result.status != "published" and result.error_code != "plan_missing":
                blocked = True

    def _update_run(self, channel_id: str, digest_key: str, **changes: object) -> None:
        runtime = self._store.load_runtime()
        record = runtime.channels[channel_id].find_run(digest_key)
        if record is None:
            return
        for name, value in changes.items():
            setattr(record, name, value)
        record.updated_at_utc = self._clock()
        runtime.channels[channel_id].upsert_run(record)
        self._store.save_runtime(runtime)

    def _cleanup_cache(self, now: datetime) -> None:
        removed = set(self._cache.cleanup_expired(now, self._active))
        if removed:
            runtime = self._store.load_runtime()
            for state in runtime.channels.values():
                for run in state.runs:
                    if run.digest_key in removed and run.status in ("scanned", "ready", "held"):
                        run.status = "cache_expired"
            self._store.save_runtime(runtime)
            log.info("cache_expired", extra={"runs": len(removed)})
        if self._cache.over_limit():
            log.error("cache_over_limit", extra={"bytes": self._cache.total_bytes()})
