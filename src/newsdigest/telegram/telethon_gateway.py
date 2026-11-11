"""Реалізація `TelegramGateway` на Telethon.

Клієнт без фонового приймання оновлень (`receive_updates=False`, `catch_up=False`)
і без автоматичного очікування FloodWait (`flood_sleep_threshold=0`): паузами
керує `RateLimiter`. Підключення відкривається лише на час операції (idle disconnect).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from getpass import getpass
from typing import Any

from telethon import TelegramClient, types, utils

from newsdigest.settings import Paths, TelegramSettings
from newsdigest.telegram.types import (
    AccountInfo,
    DialogInfo,
    RawMessage,
    ResolvedPeer,
    TelegramError,
)


def build_client(paths: Paths, settings: TelegramSettings) -> TelegramClient:
    paths.session_dir.mkdir(parents=True, exist_ok=True)
    return TelegramClient(
        str(paths.session_file.with_suffix("")),
        settings.api_id,
        settings.api_hash,
        receive_updates=False,
        catch_up=False,
        flood_sleep_threshold=0,
        request_retries=2,
        connection_retries=3,
        auto_reconnect=False,
    )


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _can_post(entity: Any) -> bool:
    if getattr(entity, "creator", False):
        return True
    rights = getattr(entity, "admin_rights", None)
    return bool(rights is not None and getattr(rights, "post_messages", False))


def _resolved(entity: Any) -> ResolvedPeer:
    is_channel = isinstance(entity, types.Channel)
    return ResolvedPeer(
        peer_id=utils.get_peer_id(entity),
        title=(getattr(entity, "title", None) or getattr(entity, "first_name", None) or "?"),
        username=getattr(entity, "username", None),
        is_broadcast=bool(is_channel and getattr(entity, "broadcast", False)),
        can_post=_can_post(entity),
    )


def _raw(message: Any) -> RawMessage:
    forward_peer: int | None = None
    fwd = getattr(message, "fwd_from", None)
    if fwd is not None and getattr(fwd, "from_id", None) is not None:
        forward_peer = utils.get_peer_id(fwd.from_id)
    date = _utc(message.date)
    assert date is not None
    return RawMessage(
        message_id=int(message.id),
        date_utc=date,
        edit_date_utc=_utc(getattr(message, "edit_date", None)),
        text=getattr(message, "message", None) or "",
        is_service=isinstance(message, types.MessageService),
        has_media=getattr(message, "media", None) is not None,
        grouped_id=getattr(message, "grouped_id", None),
        forward_from_peer_id=forward_peer,
    )


class TelethonGateway:
    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    async def account(self) -> AccountInfo:
        me = await self._client.get_me()
        if me is None:
            raise TelegramError("auth_required", "сесія не авторизована: виконайте digest login")
        name = " ".join(p for p in (me.first_name, me.last_name) if p) or str(me.id)
        return AccountInfo(account_id=int(me.id), is_bot=bool(me.bot), display_name=name)

    async def resolve(self, ref: str) -> ResolvedPeer:
        target: str | int = int(ref) if ref.lstrip("-").isdigit() else ref
        try:
            entity = await self._client.get_entity(target)
        except ValueError as exc:
            raise TelegramError(
                "peer_not_found",
                f"{ref}: канал не знайдено (для ID спершу виконайте digest dialogs)",
            ) from exc
        return _resolved(entity)

    async def history_page(
        self, peer_id: int, *, before: datetime, offset_id: int, limit: int
    ) -> Sequence[RawMessage]:
        messages = await self._client.get_messages(
            peer_id, limit=limit, offset_date=before, offset_id=offset_id
        )
        return [_raw(m) for m in messages]

    async def dialogs(self) -> Sequence[DialogInfo]:
        result: list[DialogInfo] = []
        async for dialog in self._client.iter_dialogs():
            entity = dialog.entity
            if not isinstance(entity, types.Channel):
                continue
            peer = _resolved(entity)
            result.append(
                DialogInfo(
                    peer_id=peer.peer_id,
                    title=peer.title,
                    username=peer.username,
                    is_broadcast=peer.is_broadcast,
                    can_post=peer.can_post,
                )
            )
        return result


@asynccontextmanager
async def connected_gateway(
    paths: Paths, settings: TelegramSettings
) -> AsyncIterator[TelethonGateway]:
    """Підключення на час операції; після виходу клієнт від'єднується."""
    client = build_client(paths, settings)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise TelegramError("auth_required", "сесія не авторизована: виконайте digest login")
        yield TelethonGateway(client)
    finally:
        await client.disconnect()


async def interactive_login(
    paths: Paths,
    settings: TelegramSettings,
    ask: Callable[[str], str] = input,
    ask_secret: Callable[[str], str] = getpass,
) -> AccountInfo:
    """Інтерактивний вхід: телефон, код Telegram і за потреби пароль 2FA (не зберігаються)."""
    client = build_client(paths, settings)
    try:
        await client.start(
            phone=lambda: ask("Номер телефону (+380...): "),
            code_callback=lambda: ask("Код із Telegram: "),
            password=lambda: ask_secret("Пароль 2FA: "),
            max_attempts=3,
        )
        return await TelethonGateway(client).account()
    finally:
        await client.disconnect()
