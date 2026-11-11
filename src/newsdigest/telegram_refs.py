"""Нормалізація посилань на Telegram-канали з конфігурації (без мережі)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_MARKED_CHANNEL_RE = re.compile(r"^-100[1-9][0-9]{0,15}$")
_TME_HOSTS = frozenset({"t.me", "www.t.me", "telegram.me", "www.telegram.me"})


class TelegramRefError(ValueError):
    """Непідтримуване або некоректне посилання на канал."""


@dataclass(frozen=True, slots=True)
class TelegramRef:
    """Нормалізований ref: username у нижньому регістрі або marked ID каналу."""

    kind: Literal["username", "marked_id"]
    value: str

    def __str__(self) -> str:
        return f"@{self.value}" if self.kind == "username" else self.value


def _username(raw: str) -> TelegramRef:
    if not _USERNAME_RE.fullmatch(raw):
        raise TelegramRefError(f"некоректний username каналу: {raw!r}")
    return TelegramRef("username", raw.lower())


def parse_telegram_ref(ref: str | int) -> TelegramRef:
    """Приймає `@username`, `https://t.me/<username>` або marked ID `-100…`.

    Посилання на окремий допис, invite link, `t.me/c/…` та інші форми
    відхиляються, а не перетворюються мовчки на інший канал (v4 §6).
    """
    if isinstance(ref, bool):
        raise TelegramRefError("ref не може бути bool")
    if isinstance(ref, int):
        text = str(ref)
        if not _MARKED_CHANNEL_RE.fullmatch(text):
            raise TelegramRefError(f"числовий ref має бути marked ID каналу -100…: {ref}")
        return TelegramRef("marked_id", text)

    text = ref.strip()
    if not text:
        raise TelegramRefError("порожній ref")
    if _MARKED_CHANNEL_RE.fullmatch(text):
        return TelegramRef("marked_id", text)
    if text.startswith("@"):
        return _username(text[1:])
    if "://" in text:
        parts = urlsplit(text)
        if parts.scheme != "https" or parts.hostname not in _TME_HOSTS:
            raise TelegramRefError(f"підтримуються лише https://t.me/<username>: {text!r}")
        if parts.query or parts.fragment:
            raise TelegramRefError(f"URL каналу не має містити параметрів: {text!r}")
        segments = [s for s in parts.path.split("/") if s]
        if len(segments) != 1 or segments[0].startswith("+") or segments[0] == "joinchat":
            raise TelegramRefError(f"потрібен URL самого каналу, не допису чи запрошення: {text!r}")
        return _username(segments[0])
    raise TelegramRefError(f"ref має бути @username, https://t.me/<username> або -100…: {text!r}")
