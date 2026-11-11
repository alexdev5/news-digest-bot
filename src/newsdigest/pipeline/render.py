"""Рендер випуску: текст, text-url entities і поділ на частини (D-002, D-014–D-016).

Пункт: `N. Короткий зміст — Назва джерела`, де рівно назва — посилання на допис.
Нумерація наскрізна для всієї серії. Поділ — лише між цілими пунктами; кількість
частин N перераховується, доки заголовки `Частина i/N` не перестануть змінювати
розкладку (переходи 9→10, 99→100). Довжина — у code points, offsets — у UTF-16.
Чистий модуль: без мережі й файлів.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from newsdigest.pipeline.events import Event, visible_source

ITEM_SEPARATOR = "\n\n"
UPDATE_PREFIX = "Оновлення: "
MAX_PACKING_ROUNDS = 20


class RenderError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class TextUrlEntity:
    offset: int
    length: int
    url: str


@dataclass(frozen=True, slots=True)
class RenderedPart:
    index: int
    count: int
    text: str
    entities: tuple[TextUrlEntity, ...]
    event_ids: tuple[str, ...]

    @property
    def payload_hash(self) -> str:
        payload = {
            "text": self.text,
            "entities": [[e.offset, e.length, e.url] for e in self.entities],
        }
        canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _Item:
    event_id: str
    text: str
    label_offset: int
    label_length: int
    url: str


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def clean_label(label: str) -> str:
    """Назва джерела одним рядком без керувальних символів (не змінює ідентичність)."""
    cleaned = "".join(" " if unicodedata.category(ch) in {"Cc", "Zl", "Zp"} else ch for ch in label)
    return " ".join(cleaned.split()) or "Джерело"


def _item(number: int, event: Event) -> _Item:
    source = visible_source(event)
    summary = " ".join(event.summary.split())
    if event.is_update and not summary.startswith(UPDATE_PREFIX):
        summary = UPDATE_PREFIX + summary
    body = f"{number}. {summary} — "
    label = clean_label(source.label)
    return _Item(
        event_id=event.event_id,
        text=body + label,
        label_offset=utf16_len(body),
        label_length=utf16_len(label),
        url=source.url,
    )


@dataclass(frozen=True, slots=True)
class Header:
    template: str
    name: str
    window_start: datetime
    window_end: datetime
    timezone: ZoneInfo

    def render(self, index: int, count: int) -> str:
        start = self.window_start.astimezone(self.timezone)
        end = self.window_end.astimezone(self.timezone)
        line = self.template.format(
            name=self.name,
            date=f"{end:%d.%m}",
            time=f"{end:%H:%M}",
            period=f"{start:%d.%m %H:%M} — {end:%d.%m %H:%M}",
        )
        if count > 1:
            line += f" · Частина {index}/{count}"
        return line


def _pack(items: Sequence[_Item], header: Header, count: int, limit: int) -> list[list[_Item]]:
    parts: list[list[_Item]] = []
    current: list[_Item] = []
    for item in items:
        index = len(parts) + 1
        head = header.render(index, count)
        candidate = [*current, item]
        if _length(head, candidate) <= limit:
            current = candidate
            continue
        if not current:
            raise RenderError(
                "item_too_long",
                f"пункт {item.event_id} не вміщується в повідомлення навіть сам "
                f"({_length(head, [item])} > {limit})",
            )
        parts.append(current)
        index = len(parts) + 1
        head = header.render(index, count)
        if _length(head, [item]) > limit:
            raise RenderError(
                "item_too_long", f"пункт {item.event_id} не вміщується в повідомлення"
            )
        current = [item]
    if current:
        parts.append(current)
    return parts


def _length(head: str, items: Sequence[_Item]) -> int:
    return len(head) + len(ITEM_SEPARATOR) + len(ITEM_SEPARATOR.join(i.text for i in items))


def render_series(events: Sequence[Event], header: Header, max_chars: int) -> list[RenderedPart]:
    """Усі події → упорядкований повний набір частин. Порожній список подій — без частин."""
    if not events:
        return []
    items = [_item(number, event) for number, event in enumerate(events, start=1)]
    count = 1
    for _ in range(MAX_PACKING_ROUNDS):
        packed = _pack(items, header, count, max_chars)
        if len(packed) == count:
            break
        count = len(packed)
    else:  # pragma: no cover - захист від нескінченного циклу
        raise RenderError("unstable_packing", "кількість частин не стабілізувалась")

    parts = [_assemble(i + 1, count, header, group) for i, group in enumerate(packed)]
    placed = [eid for part in parts for eid in part.event_ids]
    expected = [e.event_id for e in events]
    if placed != expected:
        raise RenderError("coverage_mismatch", "подія відсутня або продубльована в частинах")
    return parts


def _assemble(index: int, count: int, header: Header, items: Sequence[_Item]) -> RenderedPart:
    text = header.render(index, count) + ITEM_SEPARATOR
    entities: list[TextUrlEntity] = []
    for position, item in enumerate(items):
        if position:
            text += ITEM_SEPARATOR
        base = utf16_len(text)
        entities.append(TextUrlEntity(base + item.label_offset, item.label_length, item.url))
        text += item.text
    return RenderedPart(
        index=index,
        count=count,
        text=text,
        entities=tuple(entities),
        event_ids=tuple(i.event_id for i in items),
    )
