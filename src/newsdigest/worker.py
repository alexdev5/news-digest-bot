"""Планове сканування, побудова й доставка; між межами — тільки recovery і heartbeat."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

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
    previous_boundary_rev,
    window_for_boundary_rev,
)
from newsdigest.state.store import (
    ChannelState,
    Heartbeat,
    RevisionState,
    RunRecord,
    RuntimeState,
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
HEARTBEAT_SECONDS = 30.0
PUBLISHABLE = ("ready", "partial", "uncertain", "held")
# Доставку зупинила причина, яку усуває лише власник (права в каналі, вхід в акаунт).
# Worker повторює спроби (після відновлення прав продовжить сам), але healthcheck
# і status показують проблему, щоб вона не була тихою.
OWNER_ACTION_CODES = frozenset({"no_post_rights", "auth_required"})
COVERING_STATUSES = ("ready", "published", "partial", "uncertain", "held")
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

    async def scan(
        self, channel: TargetChannel, window: Window, *, run_key: str | None = None
    ) -> RunOutcome:
        key = run_key or digest_key(channel.id, window)
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
        published_end: Callable[[str], datetime | None] | None = None,
    ) -> None:
        self._delivery = delivery
        self._published_end = published_end
        self._builder = builder
        self._prompts = prompts or {}
        self._config = config
        self._store = store
        self._cache = cache
        self._executor = executor
        self._clock = clock
        self._channel_locks: dict[str, asyncio.Lock] = {}
        self._active: set[str] = set()
        self._resume: dict[str, list[RunRecord]] = {}

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
            if run.status not in ("materialized", "scanning"):
                continue
            if now - run.boundary_utc <= grace:
                self._resume.setdefault(channel.id, []).append(run)
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
        pulse = asyncio.create_task(self._heartbeat())
        try:
            await self._handle_manual_requests()
            now = self._clock()
            await asyncio.gather(*(self._process_channel(c, now) for c in self._enabled()))
            self._cleanup_cache(self._clock())
        finally:
            pulse.cancel()
            with suppress(asyncio.CancelledError):
                await pulse
            self._store.save_heartbeat(
                Heartbeat(at_utc=self._clock(), pid=os.getpid(), phase="idle")
            )

    async def _heartbeat(self) -> None:
        while True:
            self._store.save_heartbeat(
                Heartbeat(at_utc=self._clock(), pid=os.getpid(), phase="processing")
            )
            await asyncio.sleep(HEARTBEAT_SECONDS)

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
            if self._delivery is not None:
                await self.publish_pending(channel, now)
            eff = effective_channel(self._config, channel)
            runtime = self._store.load_runtime()
            state = runtime.channels.get(channel.id)
            if state is None:
                return
            revisions = state.revision_tuple()
            for run in sorted(self._resume.pop(channel.id, []), key=lambda r: r.boundary_utc):
                current = self._store.load_runtime().channels[channel.id].find_run(run.digest_key)
                if current is None or current.status not in ("materialized", "scanning"):
                    continue
                log.info("run_resumed", extra={"channel": channel.id})
                await self._execute(
                    channel,
                    run.boundary_utc,
                    Window(run.window_start_utc, run.window_end_utc),
                    manual=run.manual,
                    request_id=run.manual_request_id,
                    ignore_published=run.ignore_published,
                )
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
                planned = window_for_boundary_rev(revisions, boundary, state.scan_start_at_utc)
                clipped = await self._after_covered(channel.id, planned) if planned else None
                await self._materialize(channel, boundary, clipped)

    async def covered_until(self, channel_id: str) -> datetime | None:
        """D-029: до якого моменту новини вже опубліковані або в роботі.

        Максимум із кінця останнього опублікованого вікна (БД) і кінців вікон runs,
        що вже зібрані або публікуються. Невдалі runs (failed/skipped) не рахуються,
        тож пропуск ніколи не розширює вікно назад.
        """
        candidates: list[datetime] = []
        if self._published_end is not None:
            from_db = await asyncio.to_thread(self._published_end, channel_id)
            if from_db is not None:
                candidates.append(from_db)
        state = self._store.load_runtime().channels.get(channel_id)
        if state is not None:
            candidates.extend(
                r.window_end_utc
                for r in state.runs
                if r.status in COVERING_STATUSES and r.manual_request_id is None
            )
        return max(candidates) if candidates else None

    async def _after_covered(self, channel_id: str, window: Window) -> Window | None:
        covered = await self.covered_until(channel_id)
        if covered is None or covered <= window.start_utc:
            return window
        if covered >= window.end_utc:
            log.info("window_already_covered", extra={"channel": channel_id})
            return None
        return Window(covered, window.end_utc)

    def manual_window(
        self, channel: TargetChannel, now: datetime, covered: datetime | None
    ) -> Window | None:
        """Останній завершений інтервал schedule, обрізаний уже покритим (D-031)."""
        state = self._store.load_runtime().channels.get(channel.id)
        trigger = effective_channel(self._config, channel).trigger
        revisions = state.revision_tuple() if state is not None else (Revision(trigger),)
        end = previous_boundary_rev(revisions, now + timedelta(microseconds=1))
        planned = window_for_boundary_rev(revisions, end, channel.scan_start_at)
        if planned is None:
            return None
        start = max(planned.start_utc, covered or planned.start_utc)
        return Window(start, end) if start < end else None

    async def run_now(self, channel: TargetChannel) -> RunRecord | None:
        """Ручний прогін останнього завершеного планового інтервалу (D-031)."""
        lock = self._channel_locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            now = self._clock()
            if self._delivery is not None:
                await self.publish_pending(channel, now)
            window = self.manual_window(channel, now, await self.covered_until(channel.id))
            if window is None:
                log.info("manual_run_nothing_new", extra={"channel": channel.id})
                return None
            log.info("manual_run_started", extra={"channel": channel.id})
            await self._execute(channel, window.end_utc, window, manual=True)
            key = digest_key(channel.id, window)
            return self._store.load_runtime().channels[channel.id].find_run(key)

    @staticmethod
    def validate_manual_window(channel: TargetChannel, window: Window, now: datetime) -> None:
        if not channel.enabled:
            raise ValueError("цільовий канал вимкнений")
        if window.start_utc < channel.scan_start_at:
            raise ValueError("--from раніше scan_start_at каналу")
        if window.end_utc > now:
            raise ValueError("--to не може бути в майбутньому")

    async def run_window(
        self,
        channel: TargetChannel,
        window: Window,
        *,
        ignore_published: bool = False,
        request_id: str | None = None,
    ) -> RunRecord:
        """Explicit scan → AI → publication, independent of scheduled coverage (D-033)."""
        self.validate_manual_window(channel, window, self._clock())
        request_id = request_id or uuid4().hex
        key = digest_key(channel.id, window, request_id=request_id)
        lock = self._channel_locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            if self._delivery is not None:
                await self.publish_pending(channel, self._clock())
            state = self._store.load_runtime().channels[channel.id]
            existing = state.find_run(key)
            if existing is not None and existing.status not in ("materialized", "scanning"):
                return existing  # a queued request replay must never regenerate a sent plan
            await self._execute(
                channel,
                existing.boundary_utc if existing else self._clock(),
                window,
                manual=True,
                request_id=request_id,
                ignore_published=ignore_published,
            )
            record = self._store.load_runtime().channels[channel.id].find_run(key)
            assert record is not None
            return record

    async def _handle_manual_requests(self) -> None:
        enabled = {c.id: c for c in self._enabled()}
        for path, request in self._store.pending_manual_requests():
            channel = enabled.get(request.channel_id)
            if channel is None:
                log.warning("manual_request_unknown_channel", extra={"channel": request.channel_id})
                path.unlink(missing_ok=True)
                continue
            try:
                if request.window_start_utc is not None and request.window_end_utc is not None:
                    await self.run_window(
                        channel,
                        Window(request.window_start_utc, request.window_end_utc),
                        ignore_published=request.ignore_published,
                        request_id=request.request_id,
                    )
                else:
                    await self.run_now(channel)
            except ValueError:
                log.warning("manual_request_invalid_window", extra={"channel": channel.id})
            path.unlink(missing_ok=True)

    async def _materialize(
        self, channel: TargetChannel, boundary: datetime, window: Window | None
    ) -> None:
        runtime = self._store.load_runtime()
        state = runtime.channels[channel.id]
        state.last_materialized_utc = boundary
        self._store.save_runtime(runtime)
        if window is not None:
            await self._execute(channel, boundary, window)

    async def _execute(
        self,
        channel: TargetChannel,
        boundary: datetime,
        window: Window,
        manual: bool = False,
        *,
        request_id: str | None = None,
        ignore_published: bool = False,
    ) -> None:
        now = self._clock()
        key = digest_key(channel.id, window, request_id=request_id)
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
            manual=manual,
            manual_request_id=request_id,
            ignore_published=ignore_published,
        )
        record.status = "scanning"
        record.updated_at_utc = now
        state.upsert_run(record)
        self._store.save_runtime(runtime)
        log.info("run_started", extra={"channel": channel.id, "digest_key": key[:12]})

        self._active.add(key)
        digest: DigestOutcome | None = None
        try:
            outcome = await self._executor.scan(channel, window, run_key=key)
            if outcome.status == "scanned" and self._builder is not None:
                eff = effective_channel(self._config, channel)
                prompt = self._prompts.get(channel.id, "")
                digest = await self._builder.build(
                    eff,
                    prompt,
                    window,
                    key,
                    ignore_published=ignore_published,
                    require_published_history=request_id is not None,
                )
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
                sid: SourceCoverage.model_validate(detail)
                for sid, detail in outcome.summary.details.items()
            }
            record.excluded = dict(outcome.summary.excluded)
            record.excluded_sources = list(outcome.summary.excluded_sources)
        if digest is not None:
            if digest.status == "ready" and digest.built is not None:
                record.status = "ready"
                record.events = len(digest.built.events)
                record.parts = len(digest.built.plan.parts)
                record.ai_tokens = digest.built.tokens or {}
                record.excluded_sources.extend(digest.built.candidates.excluded_sources)
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
                "error_code": record.error_code,
                "in_window": outcome.summary.in_window if outcome.summary else 0,
                "events": record.events,
                "parts": record.parts,
                "ai_tokens": record.ai_tokens,
                "coverage": outcome.summary.statuses if outcome.summary else {},
            },
        )
        if record.status == "ready" and self._delivery is not None:
            await self.publish_pending(channel, record.boundary_utc)

    async def publish_pending(
        self, channel: TargetChannel, up_to: datetime, *, force: bool = False
    ) -> None:
        """Публікує готові/незавершені runs каналу хронологічно; зупиняється на першому збої.

        Новіший випуск не обганяє незавершену старішу серію (v4 §7.4, T71).
        """
        assert self._delivery is not None
        state = self._store.load_runtime().channels[channel.id]
        pending = sorted(
            (r for r in state.runs if r.status in PUBLISHABLE and r.boundary_utc <= up_to),
            key=lambda r: r.boundary_utc,
        )
        guards = self._store.load_guards().guards.values()
        started = {g.digest_key for g in guards if g.phase != "prepared"}
        unresolved = {
            g.digest_key
            for g in guards
            if g.channel_id == channel.id and g.phase != "committed" and g.digest_key in started
        }
        # Guards remain authoritative even if a run record is missing/legacy-failed.
        pending_keys = {r.digest_key for r in pending}
        blocked = bool(unresolved - pending_keys)
        for run in pending:
            if blocked:
                self._update_run(channel.id, run.digest_key, status="held")
                continue
            if (
                not force
                and run.next_delivery_attempt_utc
                and run.next_delivery_attempt_utc > self._clock()
            ):
                blocked = True
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
                channel.id,
                run.digest_key,
                status=status,
                error_code=result.error_code,
                delivery_attempts=run.delivery_attempts + 1,
                next_delivery_attempt_utc=(
                    self._clock()
                    + timedelta(seconds=min(60 * 2 ** min(run.delivery_attempts, 4), 900))
                    if status in PUBLISHABLE
                    else None
                ),
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
            if result.error_code in OWNER_ACTION_CODES:
                log.error(
                    "publish_needs_owner_action",
                    extra={"channel": channel.id, "error_code": result.error_code},
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
        self._cache.inspection().cleanup_expired(now)
        if removed:
            runtime = self._store.load_runtime()
            started = {
                g.digest_key
                for g in self._store.load_guards().guards.values()
                if g.phase != "prepared"
            }
            for state in runtime.channels.values():
                for run in state.runs:
                    if run.digest_key in removed and run.status != "published":
                        if run.digest_key in started:
                            run.status = "held"
                            run.error_code = "plan_missing_after_send"
                        else:
                            run.status = "cache_expired"
            self._store.save_runtime(runtime)
            log.info("cache_expired", extra={"runs": len(removed)})
        if self._cache.over_limit():
            log.error("cache_over_limit", extra={"bytes": self._cache.total_bytes()})


def owner_action_needed(runtime: RuntimeState) -> list[str]:
    """Незавершені випуски, доставку яких блокує причина для власника (без мережі)."""
    return [
        f"{channel_id}: {run.error_code}"
        for channel_id, state in runtime.channels.items()
        for run in state.runs
        if run.status in PUBLISHABLE and run.error_code in OWNER_ACTION_CODES
    ]
