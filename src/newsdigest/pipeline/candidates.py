"""Підготовка кандидатів із кешу run (v4 §9–§11, D-012, D-014).

Кроки: альбоми (`grouped_id`) → один матеріал; фільтри `exclude_patterns`;
пропуск уже опублікованих джерельних дописів цього цільового каналу (D-012);
об'єднання ТОЧНИХ копій тексту зі збереженням усіх посилань-походжень.
Схожі, але не однакові тексти не видаляються — лише позначаються як підказка
для змістового групування (D-009): різниця в числі, місці чи запереченні
не повинна губитися через локальну евристику.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from newsdigest.cache.run_cache import CachedMessage

NEAR_DUPLICATE_THRESHOLD = 0.85
_WS = re.compile(r"\s+")
_URL = re.compile(r"https?://\S+")
_WORD = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class SourceRef:
    """Посилання на конкретний джерельний допис (без тексту)."""

    source_id: str
    source_type: str
    peer_id: int
    message_id: int
    url: str
    date_utc: datetime
    label: str
    position: int
    """Позиція джерела в конфігу: `source_channels` по порядку, потім `source_sites`."""

    @property
    def published_id(self) -> dict[str, object]:
        """Ідентифікатор для `source_message_ids` у БД (D-014)."""
        if self.source_type == "site":
            return {"type": "site", "url": self.url}
        return {"type": "telegram", "peer_id": self.peer_id, "message_id": self.message_id}


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: str
    text: str
    refs: tuple[SourceRef, ...]
    similar_to: tuple[str, ...] = ()

    @property
    def earliest(self) -> datetime:
        return min(r.date_utc for r in self.refs)

    @property
    def latest(self) -> datetime:
        return max(r.date_utc for r in self.refs)


@dataclass(slots=True)
class CandidateSet:
    candidates: list[Candidate]
    excluded: dict[str, int] = field(default_factory=dict)

    def count(self, reason: str, amount: int = 1) -> None:
        self.excluded[reason] = self.excluded.get(reason, 0) + amount


def normalize_text(text: str) -> str:
    """Представлення для точного порівняння: регістр, пробіли, без URL. Не для публікації."""
    return _WS.sub(" ", _URL.sub(" ", text)).strip().casefold()


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def shingles(text: str, size: int = 3) -> frozenset[tuple[str, ...]]:
    words = _WORD.findall(normalize_text(text))
    if len(words) < size:
        return frozenset({tuple(words)}) if words else frozenset()
    return frozenset(tuple(words[i : i + size]) for i in range(len(words) - size + 1))


def jaccard(a: frozenset[tuple[str, ...]], b: frozenset[tuple[str, ...]]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def published_key(ref: SourceRef) -> tuple[object, ...]:
    if ref.source_type == "site":
        return ("site", ref.url)
    return ("telegram", ref.peer_id, ref.message_id)


def _merge_albums(messages: Sequence[CachedMessage]) -> list[list[CachedMessage]]:
    """Дописи одного альбому (той самий source + grouped_id) — один матеріал."""
    albums: dict[tuple[str, int], list[CachedMessage]] = defaultdict(list)
    singles: list[list[CachedMessage]] = []
    for message in messages:
        if message.grouped_id is not None:
            albums[(message.source_id, message.grouped_id)].append(message)
        else:
            singles.append([message])
    grouped = [sorted(group, key=lambda m: m.message_id) for group in albums.values()]
    return singles + grouped


def build_candidates(
    messages: Iterable[CachedMessage],
    *,
    positions: dict[str, int],
    labels: dict[str, str],
    exclude_patterns: Sequence[str] = (),
    published: set[tuple[object, ...]] | None = None,
) -> CandidateSet:
    """Кандидати з кешованих дописів. `positions`/`labels` — за source_id із конфігу."""
    already = published or set()
    patterns = [p.casefold() for p in exclude_patterns]
    result = CandidateSet(candidates=[])
    by_hash: dict[str, tuple[str, list[SourceRef]]] = {}
    order: list[str] = []

    for group in _merge_albums(list(messages)):
        first = group[0]
        if first.source_id not in positions:
            result.count("unknown_source", len(group))
            continue
        text = "\n".join(m.text for m in group if m.text.strip())
        ref = SourceRef(
            source_id=first.source_id,
            source_type=first.source_type,
            peer_id=first.peer_id,
            message_id=first.message_id,
            url=first.url,
            date_utc=min(m.date_utc for m in group),
            label=labels.get(first.source_id, first.source_id),
            position=positions[first.source_id],
        )
        if any(_message_key(m) in already for m in group):
            result.count("already_published")
            continue
        folded = text.casefold()
        if any(p in folded for p in patterns):
            result.count("filtered")
            continue
        digest = text_hash(text)
        if digest in by_hash:
            by_hash[digest][1].append(ref)
            result.count("exact_duplicate")
            continue
        by_hash[digest] = (text, [ref])
        order.append(digest)

    candidates = [
        Candidate(
            candidate_id=digest[:16],
            text=by_hash[digest][0],
            refs=tuple(sorted(by_hash[digest][1], key=lambda r: (r.date_utc, r.position))),
        )
        for digest in order
    ]
    result.candidates = _with_similarity_hints(candidates)
    result.candidates.sort(key=lambda c: (c.earliest, c.candidate_id))
    return result


def _message_key(message: CachedMessage) -> tuple[object, ...]:
    if message.source_type == "site":
        return ("site", message.url)
    return ("telegram", message.peer_id, message.message_id)


def _with_similarity_hints(candidates: list[Candidate]) -> list[Candidate]:
    sets = [shingles(c.text) for c in candidates]
    hints: dict[int, list[str]] = defaultdict(list)
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            if jaccard(sets[i], sets[j]) >= NEAR_DUPLICATE_THRESHOLD:
                hints[i].append(candidates[j].candidate_id)
                hints[j].append(candidates[i].candidate_id)
    return [
        Candidate(c.candidate_id, c.text, c.refs, tuple(hints.get(i, ()))) if i in hints else c
        for i, c in enumerate(candidates)
    ]
