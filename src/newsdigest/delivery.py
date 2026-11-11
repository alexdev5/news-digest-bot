"""Доставка серії в цільовий канал (етап 5; v4 §14, architecture.md «Стани та доставка частин»).

Для кожної частини по черзі:
1. уже є в БД → пропустити (повторно не надсилається);
2. guard `confirmed` → лише дописати в БД (send не повторюється, D-обмеження v4 §14.4);
3. guard `sending`/`uncertain` → звірка в каналі за часом (D-017): рівно один власний
   пост в інтервалі → підтверджено; інакше `uncertain` і зупинка серії (hold);
4. `prepared` → надійно записати `sending`, надіслати зі збереженим random_id,
   записати receipt (`confirmed`), insert у БД, `committed`.

Немає атомарності всієї серії й «exactly-once» за будь-якого збою: ціль — не робити
сліпих повторів. Підтверджені частини не видаляються для імітації відкату.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import Engine

from newsdigest.cache.run_cache import CacheManager
from newsdigest.config.models import TargetChannel
from newsdigest.pipeline.plan import PlannedPart, PublicationPlan
from newsdigest.state.store import DeliveryGuard, StateStore
from newsdigest.storage.published_repo import (
    ConfirmedPart,
    committed_part_keys,
    insert_confirmed_part,
    used_message_ids,
)
from newsdigest.telegram.rate_limit import RateLimiter
from newsdigest.telegram.scanner import HistoryScanner
from newsdigest.telegram.types import (
    FloodWaitPause,
    ResolvedPeer,
    SendRejected,
    SentMessage,
    TelegramError,
    TelegramGateway,
)

log = logging.getLogger("newsdigest.delivery")

RECONCILE_BEFORE = timedelta(seconds=60)
RECONCILE_AFTER = timedelta(minutes=5)

Clock = Callable[[], datetime]


class PartStore(Protocol):
    def committed_part_keys(self, digest_key: str) -> set[str]: ...

    def used_message_ids(self, destination_peer_id: int) -> set[int]: ...

    def insert(self, part: ConfirmedPart) -> None: ...

    def close(self) -> None: ...


class SqlPartStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def committed_part_keys(self, digest_key: str) -> set[str]:
        return committed_part_keys(self._engine, digest_key)

    def used_message_ids(self, destination_peer_id: int) -> set[int]:
        return used_message_ids(self._engine, destination_peer_id)

    def insert(self, part: ConfirmedPart) -> None:
        insert_confirmed_part(self._engine, part)

    def close(self) -> None:
        self._engine.dispose()


@dataclass(frozen=True, slots=True)
class PublishMeta:
    """Безконтентні metadata випуску для рядків БД."""

    scheduled_at_utc: datetime
    source_order: list[str]
    coverage: dict[str, Any] | None = None
    ai_usage: dict[str, Any] | None = None


@dataclass(slots=True)
class PublishResult:
    status: str
    """`published` — усі частини в БД; `partial` — зупинились без невизначеності;
    `uncertain` — результат send невідомий, серія на hold до звірки; `failed` — send відхилено."""
    committed: int = 0
    total: int = 0
    error_code: str | None = None
    message_ids: list[int] = field(default_factory=list)


class Publisher:
    def __init__(
        self,
        gateway: TelegramGateway,
        limiter: RateLimiter,
        store: StateStore,
        parts: PartStore,
        clock: Clock,
    ) -> None:
        self._gateway = gateway
        self._limiter = limiter
        self._store = store
        self._parts = parts
        self._clock = clock

    async def publish(
        self, plan: PublicationPlan, destination: ResolvedPeer, meta: PublishMeta
    ) -> PublishResult:
        result = PublishResult(status="published", total=len(plan.parts))
        if not plan.parts:
            return result
        self._prepare_guards(plan, destination)
        committed = await asyncio.to_thread(self._parts.committed_part_keys, plan.digest_key)

        for part in plan.parts:
            guard = self._guard(part.part_key)
            if part.part_key in committed:
                self._set(guard, phase="committed")
                result.committed += 1
                continue
            if guard.phase in ("sending", "uncertain"):
                found = await self._reconcile(guard)
                if found is None:
                    self._set(guard, phase="uncertain", error_code="reconcile_ambiguous")
                    log.error("part_uncertain", extra={"part": part.part_key[-20:]})
                    result.status, result.error_code = "uncertain", "reconcile_ambiguous"
                    return result
                self._confirm(guard, found, "reconciled")
            elif guard.phase == "prepared":
                outcome = await self._send(part, guard, destination)
                if outcome is not None:
                    result.status, result.error_code = outcome
                    return result
            guard = self._guard(part.part_key)
            try:
                await asyncio.to_thread(
                    self._parts.insert, self._confirmed_part(plan, part, guard, meta)
                )
            except Exception:
                # Повідомлення вже в каналі: наступна спроба лише допише БД, без нового send.
                log.exception("db_insert_failed", extra={"part": part.part_key[-20:]})
                result.status, result.error_code = "partial", "db_insert_failed"
                return result
            self._set(guard, phase="committed")
            result.committed += 1
            if guard.message_id is not None:
                result.message_ids.append(guard.message_id)
        return result

    # --- guards ---------------------------------------------------------------

    def _prepare_guards(self, plan: PublicationPlan, destination: ResolvedPeer) -> None:
        """До першого send фіксуємо безконтентний manifest серії для всіх частин."""
        state = self._store.load_guards()
        now = self._clock()
        for part in plan.parts:
            if part.part_key in state.guards:
                existing = state.guards[part.part_key]
                if existing.payload_hash != part.payload_hash:
                    raise RuntimeError(
                        f"план частини {part.part_key} змінився після початку доставки"
                    )
                continue
            state.guards[part.part_key] = DeliveryGuard(
                part_key=part.part_key,
                digest_key=plan.digest_key,
                channel_id=plan.channel_id,
                destination_peer_id=destination.peer_id,
                part_index=part.index,
                part_count=part.count,
                manifest_hash=plan.manifest_hash,
                payload_hash=part.payload_hash,
                random_id=part.random_id,
                phase="prepared",
                updated_at_utc=now,
            )
        self._store.save_guards(state)

    def _guard(self, part_key: str) -> DeliveryGuard:
        return self._store.load_guards().guards[part_key]

    def _set(self, guard: DeliveryGuard, **changes: Any) -> DeliveryGuard:
        state = self._store.load_guards()
        current = state.guards[guard.part_key]
        for name, value in changes.items():
            setattr(current, name, value)
        current.updated_at_utc = self._clock()
        self._store.save_guards(state)
        return current

    def _confirm(self, guard: DeliveryGuard, sent: SentMessage, method: str) -> None:
        self._set(
            guard,
            phase="confirmed",
            message_id=sent.message_id,
            message_date_utc=sent.date_utc,
            confirmation_method=method,
            error_code=None,
        )

    # --- send / reconcile -----------------------------------------------------

    async def _send(
        self, part: PlannedPart, guard: DeliveryGuard, destination: ResolvedPeer
    ) -> tuple[str, str] | None:
        self._set(guard, phase="sending", sending_at_utc=self._clock())
        try:
            sent = await self._limiter.call(
                functools.partial(
                    self._gateway.send_message,
                    destination.peer_id,
                    part.text,
                    list(part.entities),
                    part.random_id,
                )
            )
        except (FloodWaitPause, SendRejected) as exc:
            # Telegram запит відхилив або ми його не відправляли — повідомлення немає.
            self._set(guard, phase="prepared", sending_at_utc=None, error_code=exc.code)
            status = "partial" if isinstance(exc, FloodWaitPause) else "failed"
            return status, exc.code
        except Exception as exc:
            # Невідомо, чи дійшло (обрив, timeout): лише звірка, без повторного send.
            self._set(guard, phase="uncertain", error_code=type(exc).__name__)
            log.error("send_uncertain", extra={"part": part.part_key[-20:]})
            return "uncertain", "send_uncertain"
        self._confirm(guard, sent, "ack")
        log.info(
            "part_sent",
            extra={"part": part.part_key[-20:], "message_id": sent.message_id},
        )
        return None

    async def _reconcile(self, guard: DeliveryGuard) -> SentMessage | None:
        """D-017: рівно один власний пост в інтервалі після `sending` і ще не зайнятий."""
        if guard.message_id is not None and guard.message_date_utc is not None:
            return SentMessage(guard.message_id, guard.message_date_utc)
        if guard.sending_at_utc is None:
            return None
        since = guard.sending_at_utc - RECONCILE_BEFORE
        until = guard.sending_at_utc + RECONCILE_AFTER
        messages = await self._limiter.call(
            functools.partial(self._gateway.own_messages, guard.destination_peer_id, since, until)
        )
        taken = await asyncio.to_thread(self._parts.used_message_ids, guard.destination_peer_id)
        taken |= {
            g.message_id
            for g in self._store.load_guards().guards.values()
            if g.message_id is not None and g.part_key != guard.part_key
        }
        candidates = [m for m in messages if m.message_id not in taken]
        if len(candidates) != 1:
            return None
        found = candidates[0]
        return SentMessage(found.message_id, found.date_utc)

    def _confirmed_part(
        self, plan: PublicationPlan, part: PlannedPart, guard: DeliveryGuard, meta: PublishMeta
    ) -> ConfirmedPart:
        from newsdigest.identity import parse_utc

        assert guard.message_id is not None and guard.message_date_utc is not None
        now = self._clock()
        return ConfirmedPart(
            part_key=part.part_key,
            digest_key=plan.digest_key,
            part_index=part.index,
            part_count=part.count,
            series_manifest_hash=plan.manifest_hash,
            target_channel_id=plan.channel_id,
            destination_peer_id=guard.destination_peer_id,
            destination_message_id=guard.message_id,
            window_start_utc=parse_utc(plan.window_start_utc),
            window_end_utc=parse_utc(plan.window_end_utc),
            scheduled_at_utc=meta.scheduled_at_utc,
            published_at_utc=guard.message_date_utc,
            confirmed_at_utc=now,
            confirmation_method=guard.confirmation_method or "ack",
            rendered_text=part.text,
            entities=list(part.entities),
            payload_hash=part.payload_hash,
            random_id=part.random_id,
            items=list(part.items),
            source_message_ids=list(part.source_message_ids),
            source_order_snapshot=meta.source_order,
            coverage_summary=meta.coverage if part.index == 1 else None,
            ai_usage_summary=meta.ai_usage if part.index == 1 else None,
            usage_scope="series" if part.index == 1 and meta.ai_usage else None,
        )


GatewayFactory = Callable[[], AbstractAsyncContextManager[TelegramGateway]]
PartStoreFactory = Callable[[], PartStore | None]


def sql_part_store_factory(engine_factory: Callable[[], Engine | None]) -> PartStoreFactory:
    def factory() -> PartStore | None:
        engine = engine_factory()
        return SqlPartStore(engine) if engine is not None else None

    return factory


class Delivery:
    """Публікація плану run: план із кешу, ціль, Publisher, очищення кешу після успіху."""

    def __init__(
        self,
        store: StateStore,
        cache: CacheManager,
        gateway_factory: GatewayFactory,
        limiter: RateLimiter,
        part_store_factory: PartStoreFactory,
        clock: Clock,
    ) -> None:
        self._store = store
        self._cache = cache
        self._gateway_factory = gateway_factory
        self._limiter = limiter
        self._part_store_factory = part_store_factory
        self._clock = clock

    async def deliver(
        self, channel: TargetChannel, digest_key: str, meta: PublishMeta
    ) -> PublishResult:
        run_cache = self._cache.run(digest_key)
        plan = PublicationPlan.load(run_cache.directory)
        if plan is None:
            started = any(
                g.digest_key == digest_key and g.phase != "prepared"
                for g in self._store.load_guards().guards.values()
            )
            if started:
                # Кеш зник після початку відправки: лише звірка й ручне рішення (v4 §14.3).
                return PublishResult(status="held", error_code="plan_missing_after_send")
            return PublishResult(status="failed", error_code="plan_missing")
        if not plan.parts:
            run_cache.delete()
            return PublishResult(status="published")
        parts = self._part_store_factory()
        if parts is None:
            # Без БД не можна зафіксувати підтверджене — тому й не надсилаємо.
            return PublishResult(status="partial", error_code="db_unavailable")
        try:
            async with self._gateway_factory() as gateway:
                scanner = HistoryScanner(gateway, self._limiter, self._store, self._clock)
                destination = await scanner.resolve_pinned(channel.telegram)
                if not destination.is_broadcast or not destination.can_post:
                    return PublishResult(status="failed", error_code="no_post_rights")
                publisher = Publisher(gateway, self._limiter, self._store, parts, self._clock)
                result = await publisher.publish(plan, destination, meta)
        except TelegramError as exc:
            return PublishResult(status="partial", error_code=exc.code)
        finally:
            parts.close()
        if result.status == "published":
            run_cache.delete()
        return result
