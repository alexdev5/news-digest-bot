"""Єдиний контролер FloodWait і пауз між запитами (v4 §8.3, §15; T75).

Клієнт Telethon створюється з `flood_sleep_threshold=0`, тож FloodWait завжди
доходить сюди. Короткі паузи відчікуються в межах run; довгі зберігаються в
`/state/rate_limits.json` (`pause_until`) і переживають restart.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from newsdigest.state.store import RateLimitState, StateStore
from newsdigest.telegram.types import FloodWaitPause

MAX_INLINE_WAIT_SECONDS = 60
MAX_INLINE_RETRIES = 3

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


def flood_wait_seconds(exc: BaseException) -> int | None:
    """Секунди FloodWait, якщо це FloodWait-помилка Telethon; інакше None."""
    if type(exc).__name__ in {"FloodWaitError", "FloodPremiumWaitError", "SlowModeWaitError"}:
        seconds = getattr(exc, "seconds", None)
        if isinstance(seconds, int):
            return seconds
    return None


class RateLimiter:
    def __init__(
        self,
        store: StateStore,
        clock: Clock,
        sleep: Sleep = asyncio.sleep,
        min_gap_seconds: float = 1.0,
    ) -> None:
        self._store = store
        self._clock = clock
        self._sleep = sleep
        self._min_gap = min_gap_seconds
        self._last_request: datetime | None = None
        self._lock = asyncio.Lock()

    def paused_until(self) -> datetime | None:
        state = self._store.load_rate_limits()
        if state.pause_until_utc is not None and state.pause_until_utc > self._clock():
            return state.pause_until_utc
        return None

    async def call[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        """Виконує один Telegram-запит: один одночасно, з мінімальною паузою між запитами."""
        async with self._lock:
            pause = self.paused_until()
            if pause is not None:
                raise FloodWaitPause(pause)
            for attempt in range(MAX_INLINE_RETRIES + 1):
                await self._respect_gap()
                try:
                    result = await operation()
                except Exception as exc:
                    seconds = flood_wait_seconds(exc)
                    if seconds is None:
                        raise
                    if seconds <= MAX_INLINE_WAIT_SECONDS and attempt < MAX_INLINE_RETRIES:
                        await self._sleep(seconds + 1)
                        continue
                    until = self._clock() + timedelta(seconds=seconds + 1)
                    self._store.save_rate_limits(
                        RateLimitState(pause_until_utc=until, reason="flood_wait")
                    )
                    raise FloodWaitPause(until) from exc
                finally:
                    self._last_request = self._clock()
                return result
            raise AssertionError("unreachable")  # pragma: no cover

    async def _respect_gap(self) -> None:
        if self._last_request is None:
            return
        elapsed = (self._clock() - self._last_request).total_seconds()
        if elapsed < self._min_gap:
            await self._sleep(self._min_gap - elapsed)
