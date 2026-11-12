"""Рендер випуску: текст, entities і поділ на частини (D-002, D-014–D-016, D-025).

Формат пункту (D-025):

    N. <емодзі> <жирний заголовок>
    Стислий виклад зі збереженням суті. — Назва джерела

Рівно назва джерела — text-url посилання на конкретний допис; заголовок — bold.
Заголовок випуску теж жирний; для N>1 частин до нього додається `· Частина i/N`.
Нумерація наскрізна для всієї серії. Поділ — між цілими пунктами; пункт, довший за
повідомлення, продовжується в наступній частині (між абзацами/реченнями). N
перераховується, доки заголовки не перестануть змінювати розкладку (9→10, 99→100).
Довжина — у code points, offsets — у UTF-16. Чистий модуль: без мережі й файлів.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from newsdigest.pipeline.events import Event, visible_source

ITEM_SEPARATOR = "\n\n"
UPDATE_PREFIX = "Оновлення:"
MONTHS_GENITIVE = (
    "січня",
    "лютого",
    "березня",
    "квітня",
    "травня",
    "червня",
    "липня",
    "серпня",
    "вересня",
    "жовтня",
    "листопада",
    "грудня",
)
MAX_PACKING_ROUNDS = 20


class RenderError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Entity:
    kind: Literal["bold", "text_url"]
    offset: int
    length: int
    url: str | None = None

    def shifted(self, delta: int) -> Entity:
        return Entity(self.kind, self.offset + delta, self.length, self.url)

    def as_json(self) -> dict[str, object]:
        data: dict[str, object] = {"type": self.kind, "offset": self.offset, "length": self.length}
        if self.url is not None:
            data["url"] = self.url
        return data


@dataclass(frozen=True, slots=True)
class RenderedPart:
    index: int
    count: int
    text: str
    entities: tuple[Entity, ...]
    event_ids: tuple[str, ...]

    @property
    def payload_hash(self) -> str:
        payload = {"text": self.text, "entities": [e.as_json() for e in self.entities]}
        canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def links(self) -> list[str]:
        return [e.url for e in self.entities if e.kind == "text_url" and e.url]


@dataclass(frozen=True, slots=True)
class _Block:
    """Фрагмент тексту з entities відносно його початку."""

    event_id: str
    text: str
    entities: tuple[Entity, ...]


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def one_line(text: str) -> str:
    """Один рядок без керувальних символів і зайвих пробілів."""
    cleaned = "".join(" " if unicodedata.category(ch) in {"Cc", "Zl", "Zp"} else ch for ch in text)
    return " ".join(cleaned.split())


def clean_label(label: str) -> str:
    """Назва джерела одним рядком (не змінює ідентичність каналу)."""
    return one_line(label) or "Джерело"


class _Builder:
    def __init__(self) -> None:
        self.text = ""
        self.entities: list[Entity] = []

    def plain(self, fragment: str) -> None:
        self.text += fragment

    def styled(
        self, fragment: str, kind: Literal["bold", "text_url"], url: str | None = None
    ) -> None:
        self.entities.append(Entity(kind, utf16_len(self.text), utf16_len(fragment), url))
        self.text += fragment


def _item(number: int, event: Event) -> _Block:
    source = visible_source(event)
    title = one_line(event.title)
    # D-043: позначки «Оновлення:» у пості немає; is_update лишається лише в метаданих.
    if title.startswith(UPDATE_PREFIX):
        title = title[len(UPDATE_PREFIX) :].strip() or title
    emoji = one_line(event.emoji)
    summary = "\n\n".join(one_line(p) for p in event.summary.split("\n\n") if p.strip())

    out = _Builder()
    out.plain(f"{number}. " + (f"{emoji} " if emoji else ""))
    if event.layout == "compact":
        out.plain(title)
    else:
        out.styled(title, "bold")
    if summary:
        out.plain(f"\n{summary}")
    if event.bullets:
        out.plain("\n\n" + "\n".join("▪️ " + one_line(b) for b in event.bullets))
    if event.quote:
        out.plain(f"\n\n«{one_line(event.quote)}»")
    out.plain("\n\n— " if event.bullets or event.quote else " — ")
    out.styled(clean_label(source.label), "text_url", source.url)
    return _Block(event.event_id, out.text, tuple(out.entities))


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
            date_long=f"{end.day} {MONTHS_GENITIVE[end.month - 1]}",
            time=f"{end:%H:%M}",
            period=f"{start:%d.%m %H:%M} — {end:%d.%m %H:%M}",
        )
        if count > 1:
            line += f" · Частина {index}/{count}"
        return one_line(line)


def _length(head: str, items: Sequence[_Block]) -> int:
    return len(head) + len(ITEM_SEPARATOR) + len(ITEM_SEPARATOR.join(i.text for i in items))


_CUTS = ("\n\n", "\n", ". ", "; ", ", ", " ")


def _cp_spans(text: str, entities: Sequence[Entity]) -> list[tuple[int, int]]:
    """Межі entities у code points (offsets entities — у UTF-16)."""
    to_cp: dict[int, int] = {}
    position = 0
    for index, char in enumerate(text):
        to_cp[position] = index
        position += utf16_len(char)
    to_cp[position] = len(text)
    return [(to_cp[e.offset], to_cp[e.offset + e.length]) for e in entities]


def _clip(entities: Sequence[Entity], start: int, end: int) -> list[Entity]:
    """Entities у межах [start,end) UTF-16, зсунуті на початок фрагмента."""
    clipped: list[Entity] = []
    for e in entities:
        lo, hi = max(e.offset, start), min(e.offset + e.length, end)
        if hi > lo:
            clipped.append(Entity(e.kind, lo - start, hi - lo, e.url))
    return clipped


def _split_block(block: _Block, room: int) -> list[_Block]:
    """Ділить завеликий пункт між абзацами → рядками → реченнями → словами.

    Розріз не проходить усередині заголовка чи посилання, якщо є інший варіант.
    """
    pieces: list[_Block] = []
    text, entities = block.text, list(block.entities)
    while len(text) > room:
        spans = _cp_spans(text, entities)
        cut = room
        for separator in _CUTS:
            found = text.rfind(separator, room // 3, room)
            while found > 0 and any(lo < found < hi for lo, hi in spans):
                found = text.rfind(separator, room // 3, found)
            if found > 0:
                cut = found + (len(separator) if separator.strip() else 0)
                break
        head = text[:cut].rstrip()
        tail = text[cut:].lstrip()
        start = len(text) - len(tail)
        pieces.append(_Block(block.event_id, head, tuple(_clip(entities, 0, utf16_len(head)))))
        offset = utf16_len(text[:start])
        entities = _clip(entities, offset, utf16_len(text))
        text = tail
    pieces.append(_Block(block.event_id, text, tuple(entities)))
    return pieces


def _fit(items: Sequence[_Block], header: Header, count: int, limit: int) -> list[_Block]:
    head = max(len(header.render(count, count)), len(header.render(1, count)))
    room = limit - head - len(ITEM_SEPARATOR)
    if room < 1:
        raise RenderError("header_too_long", f"заголовок не лишає місця для тексту ({limit})")
    fitted: list[_Block] = []
    for item in items:
        fitted.extend(_split_block(item, room) if len(item.text) > room else [item])
    return fitted


def _pack(items: Sequence[_Block], header: Header, count: int, limit: int) -> list[list[_Block]]:
    parts: list[list[_Block]] = []
    current: list[_Block] = []
    for item in _fit(items, header, count, limit):
        head = header.render(len(parts) + 1, count)
        candidate = [*current, item]
        if _length(head, candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        current = [item]
    if current:
        parts.append(current)
    return parts


def render_series(events: Sequence[Event], header: Header, max_chars: int) -> list[RenderedPart]:
    """Усі події → упорядкований повний набір частин. Порожній список подій — без частин.

    Пункт, що не вміщується в одне повідомлення, продовжується в наступній частині.
    """
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
    placed = [eid for i, eid in enumerate(placed) if i == 0 or placed[i - 1] != eid]
    if placed != [e.event_id for e in events]:
        raise RenderError("coverage_mismatch", "подія відсутня або продубльована в частинах")
    return parts


def _assemble(index: int, count: int, header: Header, items: Sequence[_Block]) -> RenderedPart:
    out = _Builder()
    out.styled(header.render(index, count), "bold")
    for item in items:
        out.plain(ITEM_SEPARATOR)
        base = utf16_len(out.text)
        out.entities.extend(e.shifted(base) for e in item.entities)
        out.text += item.text
    return RenderedPart(
        index=index,
        count=count,
        text=out.text,
        entities=tuple(out.entities),
        event_ids=tuple(dict.fromkeys(i.event_id for i in items)),
    )
