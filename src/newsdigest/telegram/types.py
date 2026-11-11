"""Незалежні від Telethon типи й протоколи Telegram-шару (для тестів із fake-клієнтом)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

# Marked ID каналу: -100XXXXXXXXXX → внутрішній ID XXXXXXXXXX (для t.me/c/...).
_CHANNEL_MARK = 1_000_000_000_000


class TelegramError(RuntimeError):
    """Помилка Telegram-шару з машинним кодом для статусів/логів (без текстів новин)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FloodWaitPause(TelegramError):
    """Telegram вимагає паузу довшу за допустиму в межах run; `pause_until` збережено."""

    def __init__(self, pause_until: datetime) -> None:
        super().__init__("flood_wait", f"FloodWait до {pause_until.isoformat()}")
        self.pause_until = pause_until


@dataclass(frozen=True, slots=True)
class ResolvedPeer:
    peer_id: int
    title: str
    username: str | None
    is_broadcast: bool
    can_post: bool = False

    def message_url(self, message_id: int) -> str:
        if self.username:
            return f"https://t.me/{self.username}/{message_id}"
        internal = -self.peer_id - _CHANNEL_MARK
        return f"https://t.me/c/{internal}/{message_id}"


@dataclass(frozen=True, slots=True)
class RawMessage:
    """Мінімальна проекція Telegram-повідомлення, потрібна сканеру."""

    message_id: int
    date_utc: datetime
    edit_date_utc: datetime | None
    text: str
    is_service: bool = False
    has_media: bool = False
    grouped_id: int | None = None
    forward_from_peer_id: int | None = None


@dataclass(frozen=True, slots=True)
class SentMessage:
    """Підтвердження відправки: ID повідомлення в каналі й серверний час."""

    message_id: int
    date_utc: datetime


@dataclass(frozen=True, slots=True)
class OwnMessage:
    message_id: int
    date_utc: datetime


class SendRejected(TelegramError):
    """Telegram однозначно відхилив send (повідомлення НЕ надіслано)."""


@dataclass(frozen=True, slots=True)
class AccountInfo:
    account_id: int
    is_bot: bool
    display_name: str


@dataclass(frozen=True, slots=True)
class DialogInfo:
    peer_id: int
    title: str
    username: str | None
    is_broadcast: bool
    can_post: bool


class TelegramGateway(Protocol):
    """Операції, які сервіс робить у Telegram. Реалізація — Telethon або fake у тестах."""

    async def account(self) -> AccountInfo: ...

    async def resolve(self, ref: str) -> ResolvedPeer: ...

    async def history_page(
        self, peer_id: int, *, before: datetime, offset_id: int, limit: int
    ) -> Sequence[RawMessage]: ...

    async def dialogs(self) -> Sequence[DialogInfo]: ...

    async def send_message(
        self,
        peer_id: int,
        text: str,
        entities: Sequence[dict[str, object]],
        random_id: int,
    ) -> SentMessage: ...

    async def own_messages(
        self, peer_id: int, since: datetime, until: datetime
    ) -> Sequence[OwnMessage]: ...
