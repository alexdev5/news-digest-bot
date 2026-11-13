"""Реалізація `TelegramGateway` на Telethon.

Клієнт без фонового приймання оновлень (`receive_updates=False`, `catch_up=False`)
і без автоматичного очікування FloodWait (`flood_sleep_threshold=0`): паузами
керує `RateLimiter`. Підключення відкривається лише на час операції (idle disconnect).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from getpass import getpass
from typing import Any

from telethon import TelegramClient, errors, types, utils

from newsdigest.settings import Paths, TelegramSettings
from newsdigest.telegram.rate_limit import flood_wait_seconds
from newsdigest.telegram.types import (
    AccountInfo,
    AccountUnavailable,
    DialogInfo,
    OwnMessage,
    RawMessage,
    ResolvedPeer,
    SendRejected,
    SentMessage,
    TelegramError,
)


async def _read[T](operation: Awaitable[T]) -> T:
    """Normalize read failures; FloodWait still belongs to the shared limiter."""
    try:
        return await operation
    except (errors.AuthKeyError, errors.UnauthorizedError) as exc:
        raise AccountUnavailable("auth_required", type(exc).__name__) from exc
    except errors.RPCError as exc:
        if flood_wait_seconds(exc) is not None:
            raise
        raise TelegramError("source_unavailable", type(exc).__name__) from exc
    except (OSError, TimeoutError) as exc:
        raise TelegramError("network", type(exc).__name__) from exc


# Помилки, після яких повідомлення гарантовано НЕ надіслано (Telegram відхилив запит).
_REJECTED_ERRORS = {
    "ChatWriteForbiddenError",
    "ChatAdminRequiredError",
    "ChannelPrivateError",
    "MessageTooLongError",
    "MessageEmptyError",
    "EntityBoundsInvalidError",
    "EntitiesTooLongError",
    "PeerIdInvalidError",
    "UserBannedInChannelError",
    "ChatRestrictedError",
}


def _entity(data: dict[str, object]) -> Any:
    offset = int(data["offset"])  # type: ignore[call-overload]
    length = int(data["length"])  # type: ignore[call-overload]
    if data.get("type") == "bold":
        return types.MessageEntityBold(offset=offset, length=length)
    if data.get("type") == "text_url":
        return types.MessageEntityTextUrl(offset=offset, length=length, url=str(data["url"]))
    raise ValueError(f"невідомий тип entity: {data.get('type')!r}")


def _sent_from_result(result: Any, random_id: int) -> SentMessage:
    """Дістає ID і час повідомлення з відповіді messages.sendMessage."""
    if isinstance(result, types.UpdateShortSentMessage):
        date = _utc(result.date)
        assert date is not None
        return SentMessage(int(result.id), date)
    message_id: int | None = None
    updates = list(getattr(result, "updates", None) or ())
    for update in updates:
        if isinstance(update, types.UpdateMessageID) and update.random_id == random_id:
            message_id = int(update.id)
    for update in updates:
        if isinstance(update, (types.UpdateNewChannelMessage, types.UpdateNewMessage)):
            message = update.message
            if message_id is None or int(message.id) == message_id:
                date = _utc(message.date)
                if date is not None:
                    return SentMessage(int(message.id), date)
    raise TelegramError("no_receipt", "у відповіді Telegram немає ID надісланого повідомлення")


def build_client(
    paths: Paths, settings: TelegramSettings, *, receive_updates: bool = False
) -> TelegramClient:
    """Клієнт без фонових оновлень. `receive_updates=True` — лише для входу через QR:
    Telegram повідомляє про скан оновленням UpdateLoginToken; новини при цьому не читаються.
    """
    paths.session_dir.mkdir(parents=True, exist_ok=True)
    return TelegramClient(
        str(paths.session_file.with_suffix("")),
        settings.api_id,
        settings.api_hash,
        receive_updates=receive_updates,
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
        photo=isinstance(getattr(message, "media", None), types.MessageMediaPhoto),
        grouped_id=getattr(message, "grouped_id", None),
        forward_from_peer_id=forward_peer,
    )


class TelethonGateway:
    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    async def account(self) -> AccountInfo:
        me = await _read(self._client.get_me())
        if me is None:
            raise AccountUnavailable(
                "auth_required", "сесія не авторизована: виконайте digest login"
            )
        name = " ".join(p for p in (me.first_name, me.last_name) if p) or str(me.id)
        return AccountInfo(account_id=int(me.id), is_bot=bool(me.bot), display_name=name)

    async def resolve(self, ref: str) -> ResolvedPeer:
        target: str | int = int(ref) if ref.lstrip("-").isdigit() else ref
        try:
            entity = await _read(self._client.get_entity(target))
        except ValueError as exc:
            raise TelegramError(
                "peer_not_found",
                f"{ref}: канал не знайдено (для ID спершу виконайте digest dialogs)",
            ) from exc
        return _resolved(entity)

    async def history_page(
        self, peer_id: int, *, before: datetime, offset_id: int, limit: int
    ) -> Sequence[RawMessage]:
        messages = await _read(
            self._client.get_messages(peer_id, limit=limit, offset_date=before, offset_id=offset_id)
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

    async def send_message(
        self,
        peer_id: int,
        text: str,
        entities: Sequence[dict[str, object]],
        random_id: int,
    ) -> SentMessage:
        """Низькорівневий messages.sendMessage зі ЗБЕРЕЖЕНИМ random_id (v4 §14.2)."""
        from telethon.tl.functions.messages import SendMessageRequest

        peer = await self._client.get_input_entity(peer_id)
        request = SendMessageRequest(
            peer=peer,
            message=text,
            no_webpage=True,
            random_id=random_id,
            entities=[_entity(e) for e in entities],
        )
        try:
            result = await self._client(request)
        except Exception as exc:
            if type(exc).__name__ in _REJECTED_ERRORS:
                raise SendRejected("send_rejected", type(exc).__name__) from exc
            raise
        return _sent_from_result(result, random_id)

    async def own_messages(
        self, peer_id: int, since: datetime, until: datetime
    ) -> Sequence[OwnMessage]:
        """Власні (`out`) повідомлення каналу з датою в [since, until) — для звірки."""
        messages = await _read(self._client.get_messages(peer_id, limit=100, offset_date=until))
        result: list[OwnMessage] = []
        for message in messages:
            date = _utc(message.date)
            if date is not None and since <= date < until and getattr(message, "out", False):
                result.append(OwnMessage(int(message.id), date))
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


_CODE_DELIVERY = {
    "SentCodeTypeApp": "у застосунку Telegram: чат «Telegram» (офіційний, з галочкою) "
    "на телефоні або в Desktop, де ви вже залогінені",
    "SentCodeTypeSms": "SMS на номер",
    "SentCodeTypeSmsWord": "SMS на номер (слово)",
    "SentCodeTypeSmsPhrase": "SMS на номер (фраза)",
    "SentCodeTypeFirebaseSms": "SMS на номер",
    "SentCodeTypeFragmentSms": "через Fragment (анонімний номер)",
    "SentCodeTypeCall": "голосовим дзвінком",
    "SentCodeTypeFlashCall": "дзвінком (код — останні цифри номера, що дзвонить)",
    "SentCodeTypeMissedCall": "пропущеним дзвінком (код — останні цифри номера)",
    "SentCodeTypeEmailCode": "на пошту, прив'язану до Telegram для входу",
    "SentCodeTypeSetUpEmailRequired": "Telegram вимагає спершу прив'язати пошту для входу "
    "(Налаштування → Конфіденційність → Пошта для входу) в офіційному застосунку",
}


def describe_code_delivery(sent: Any) -> str:
    """Людський опис, куди Telegram надіслав код входу (за типом SentCode)."""
    kind = type(getattr(sent, "type", None)).__name__
    where = _CODE_DELIVERY.get(kind, f"невідомий спосіб ({kind})")
    parts = [f"Код надіслано {where}."]
    next_type = getattr(sent, "next_type", None)
    if next_type is not None:
        next_kind = type(next_type).__name__.removeprefix("CodeType")
        parts.append(f"Повторний запит (порожній Enter) надішле інакше: {next_kind}.")
    timeout = getattr(sent, "timeout", None)
    if timeout:
        parts.append(f"Повторити можна через {timeout} с.")
    return " ".join(parts)


async def interactive_login(
    paths: Paths,
    settings: TelegramSettings,
    ask: Callable[[str], str] = input,
    ask_secret: Callable[[str], str] = getpass,
    say: Callable[[str], None] = print,
) -> AccountInfo:
    """Інтерактивний вхід: телефон, код Telegram і за потреби пароль 2FA (не зберігаються).

    Показує, куди саме Telegram надіслав код, і дозволяє запросити його повторно
    порожнім Enter (Telegram тоді зазвичай обирає інший спосіб доставки).
    """
    from telethon import errors

    client = build_client(paths, settings)
    await client.connect()
    try:
        if await client.is_user_authorized():
            say("Сесія вже авторизована.")
            return await TelethonGateway(client).account()
        phone = ask("Номер телефону (+380...): ").strip()
        sent = await client.send_code_request(phone)
        say(describe_code_delivery(sent))
        for _ in range(5):
            code = ask("Код (порожній Enter — надіслати повторно): ").strip().replace(" ", "")
            if not code:
                sent = await client.send_code_request(phone)
                say(describe_code_delivery(sent))
                continue
            try:
                await client.sign_in(phone=phone, code=code)
            except errors.PhoneCodeInvalidError:
                say("Невірний код, спробуйте ще раз.")
                continue
            except errors.PhoneCodeExpiredError:
                say("Код прострочений, надсилаю новий.")
                sent = await client.send_code_request(phone)
                say(describe_code_delivery(sent))
                continue
            except errors.SessionPasswordNeededError:
                for _ in range(3):
                    try:
                        await client.sign_in(password=ask_secret("Пароль 2FA: "))
                        break
                    except errors.PasswordHashInvalidError:
                        say("Невірний пароль 2FA.")
                else:
                    raise TelegramError("auth_failed", "забагато спроб пароля 2FA") from None
            return await TelethonGateway(client).account()
        raise TelegramError("auth_failed", "вхід не завершено: забагато спроб")
    finally:
        await client.disconnect()


def render_qr(url: str) -> str:
    """QR-код посилання входу у вигляді тексту для термінала."""
    import io

    import qrcode

    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    buffer = io.StringIO()
    qr.print_ascii(out=buffer, invert=True)
    return buffer.getvalue()


async def interactive_qr_login(
    paths: Paths,
    settings: TelegramSettings,
    ask_secret: Callable[[str], str] = getpass,
    say: Callable[[str], None] = print,
    attempts: int = 5,
) -> AccountInfo:
    """Вхід через QR без коду: скан телефоном у Налаштування → Пристрої → Підключити пристрій."""
    from telethon import errors

    client = build_client(paths, settings, receive_updates=True)
    await client.connect()
    try:
        if await client.is_user_authorized():
            say("Сесія вже авторизована.")
            return await TelethonGateway(client).account()
        qr = await client.qr_login()
        for _ in range(attempts):
            say(render_qr(qr.url))
            say(
                "Відскануйте QR телефоном: Telegram → Налаштування → Пристрої → "
                f"Підключити пристрій. Код діє до {qr.expires.astimezone():%H:%M:%S}."
            )
            try:
                await qr.wait()
            except TimeoutError:
                say("Код прострочився, показую новий.")
                await qr.recreate()
                continue
            except errors.SessionPasswordNeededError:
                for _ in range(3):
                    try:
                        await client.sign_in(password=ask_secret("Пароль 2FA: "))
                        break
                    except errors.PasswordHashInvalidError:
                        say("Невірний пароль 2FA.")
                else:
                    raise TelegramError("auth_failed", "забагато спроб пароля 2FA") from None
            return await TelethonGateway(client).account()
        raise TelegramError("auth_failed", "QR не відскановано вчасно")
    finally:
        await client.disconnect()
