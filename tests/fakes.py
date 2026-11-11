"""Синтетичні fake-реалізації Telegram і годинника для офлайн-тестів."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from newsdigest.telegram.types import (
    AccountInfo,
    DialogInfo,
    RawMessage,
    ResolvedPeer,
    TelegramError,
)


class SimulatedCrash(BaseException):
    """Імітує аварійне завершення процесу (kill), яке worker не перехоплює."""


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)


class FakeFloodWaitError(Exception):
    """Імітує telethon.errors.FloodWaitError (визначається за назвою класу)."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"wait {seconds}")
        self.seconds = seconds


FakeFloodWaitError.__name__ = "FloodWaitError"


@dataclass
class FakeGateway:
    peers: dict[str, ResolvedPeer] = field(default_factory=dict)
    history: dict[int, list[RawMessage]] = field(default_factory=dict)
    account_id: int = 42
    history_calls: int = 0
    resolve_calls: int = 0
    fail_after_pages: int | None = None
    flood_on_history: list[int] = field(default_factory=list)
    crash_on_account: bool = False

    async def account(self) -> AccountInfo:
        if self.crash_on_account:
            raise SimulatedCrash
        return AccountInfo(account_id=self.account_id, is_bot=False, display_name="Test")

    async def resolve(self, ref: str) -> ResolvedPeer:
        self.resolve_calls += 1
        try:
            return self.peers[ref]
        except KeyError:
            raise TelegramError("peer_not_found", ref) from None

    async def history_page(
        self, peer_id: int, *, before: datetime, offset_id: int, limit: int
    ) -> Sequence[RawMessage]:
        if self.flood_on_history:
            raise FakeFloodWaitError(self.flood_on_history.pop(0))
        if self.fail_after_pages is not None and self.history_calls >= self.fail_after_pages:
            raise TelegramError("network", "connection lost")
        self.history_calls += 1
        messages = sorted(self.history.get(peer_id, []), key=lambda m: -m.message_id)
        selected = [
            m
            for m in messages
            if m.date_utc < before and (offset_id == 0 or m.message_id < offset_id)
        ]
        return selected[:limit]

    async def dialogs(self) -> Sequence[DialogInfo]:
        return []


def factory(gateway: FakeGateway):  # type: ignore[no-untyped-def]
    @asynccontextmanager
    async def connect() -> AsyncIterator[FakeGateway]:
        yield gateway

    return connect


def channel_peer(peer_id: int, username: str, title: str = "Канал") -> ResolvedPeer:
    return ResolvedPeer(peer_id=peer_id, title=title, username=username, is_broadcast=True)


def messages_every(
    start: datetime, count: int, step_minutes: int, first_id: int = 1, text: str = "Новина"
) -> list[RawMessage]:
    return [
        RawMessage(
            message_id=first_id + i,
            date_utc=start + timedelta(minutes=step_minutes * i),
            edit_date_utc=None,
            text=f"{text} {first_id + i}",
        )
        for i in range(count)
    ]


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)
