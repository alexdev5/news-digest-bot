"""Незмінний план публікації серії (architecture.md, «Побудова серії», крок 7).

До першої відправки фіксуються всі частини: тексти, entities, payload hashes,
random ID кожної частини й безконтентні metadata пунктів для БД. Після початку
відправки план не перегенеровується (стан відправки — етап 5).
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable, Collection, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from newsdigest.identity import format_utc, part_key
from newsdigest.pipeline.candidates import SourceRef
from newsdigest.pipeline.events import Event, visible_source
from newsdigest.pipeline.render import RenderedPart
from newsdigest.state.atomic import write_json_atomic

PLAN_VERSION = 1


def new_random_id() -> int:
    """Випадковий signed 64-bit MTProto random_id (не нуль)."""
    while True:
        value = secrets.randbits(64) - 2**63
        if value != 0:
            return value


@dataclass(frozen=True, slots=True)
class PlannedPart:
    part_key: str
    index: int
    count: int
    text: str
    entities: tuple[dict[str, object], ...]
    payload_hash: str
    random_id: int
    items: tuple[dict[str, Any], ...]
    source_message_ids: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class PublicationPlan:
    version: int
    digest_key: str
    channel_id: str
    window_start_utc: str
    window_end_utc: str
    manifest_hash: str
    parts: tuple[PlannedPart, ...]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, directory: Path) -> Path:
        path = directory / "plan.json"
        write_json_atomic(path, self.to_json())
        return path

    @classmethod
    def load(cls, directory: Path) -> PublicationPlan | None:
        """План із кешу run або None, якщо його немає (кеш очищено/прострочено)."""
        from newsdigest.state.atomic import read_json_checked

        data = read_json_checked(directory / "plan.json")
        if data is None:
            return None
        parts = tuple(
            PlannedPart(
                **{
                    **part,
                    "entities": tuple(part["entities"]),
                    "items": tuple(part["items"]),
                    "source_message_ids": tuple(part["source_message_ids"]),
                }
            )
            for part in data["parts"]
        )
        return cls(**{**data, "parts": parts})


def _item_metadata(number: int, event: Event) -> dict[str, Any]:
    source = visible_source(event)
    return {
        "position": number,
        "event_id": event.event_id,
        "title": event.title,
        "emoji": event.emoji,
        "short_summary": event.factual_summary,
        "layout": event.layout,
        "bullets": list(event.bullets),
        "quote": event.quote,
        "quote_source_refs": [r.published_id for r in event.quote_sources],
        "is_update": event.is_update,
        "displayed_source_id": source.source_id,
        "displayed_url": source.url,
        "displayed_label": source.label,
        "used_source_refs": [r.published_id | {"url": r.url} for r in event.participants],
    }


def _ref_key(published_id: dict[str, object]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((k, str(v)) for k, v in published_id.items()))


def build_plan(
    digest_key: str,
    channel_id: str,
    window_start: datetime,
    window_end: datetime,
    events: Sequence[Event],
    parts: Sequence[RenderedPart],
    random_id: Callable[[], int] = new_random_id,
    unfinished: Collection[SourceRef] = (),
) -> PublicationPlan:
    """`unfinished` — дописи з необробленим AI залишком: їхні новини публікуються,
    але сам допис не записується використаним, щоб ручний перезапуск періоду
    міг обробити залишок (D-038)."""
    by_id = {e.event_id: e for e in events}
    numbers = {e.event_id: n for n, e in enumerate(events, start=1)}
    used_ids: set[int] = set()
    # Пункт, розбитий між частинами, вважається опублікованим (memory, використані
    # джерела) лише разом з ОСТАННЬОЮ своєю частиною.
    last_part = {eid: part.index for part in parts for eid in part.event_ids}
    # Допис-підбірка (D-037) стає використаним лише з частиною, де його остання новина.
    skipped = {_ref_key(r.published_id) for r in unfinished}
    last_ref: dict[tuple[tuple[str, str], ...], int] = {}
    for eid, index in last_part.items():
        for ref in by_id[eid].participants:
            key = _ref_key(ref.published_id)
            last_ref[key] = max(last_ref.get(key, 0), index)
    planned: list[PlannedPart] = []
    for part in parts:
        rid = random_id()
        while rid in used_ids:
            rid = random_id()
        used_ids.add(rid)
        part_events = [by_id[eid] for eid in part.event_ids if last_part[eid] == part.index]
        planned.append(
            PlannedPart(
                part_key=part_key(digest_key, part.index),
                index=part.index,
                count=part.count,
                text=part.text,
                entities=tuple(e.as_json() for e in part.entities),
                payload_hash=part.payload_hash,
                random_id=rid,
                items=tuple(_item_metadata(numbers[e.event_id], e) for e in part_events),
                source_message_ids=tuple(
                    ref.published_id
                    for e in part_events
                    for ref in e.participants
                    if last_ref[_ref_key(ref.published_id)] == part.index
                    and _ref_key(ref.published_id) not in skipped
                ),
            )
        )
    manifest = json.dumps(
        [digest_key, [p.payload_hash for p in planned]], separators=(",", ":")
    ).encode("utf-8")
    return PublicationPlan(
        version=PLAN_VERSION,
        digest_key=digest_key,
        channel_id=channel_id,
        window_start_utc=format_utc(window_start),
        window_end_utc=format_utc(window_end),
        manifest_hash=hashlib.sha256(manifest).hexdigest(),
        parts=tuple(planned),
    )
