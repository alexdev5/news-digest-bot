"""Події, вибір видимого посилання (D-015) і хронологічний порядок (D-016).

Подію (групу дописів про одне й те саме) формує модель на етапі 4. Тут — модель
даних і детермінована логіка, яка від моделі не залежить: посилання й порядок
обирає код, а не LLM.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from newsdigest.pipeline.candidates import Candidate, SourceRef

MAX_PREVIEW_SUMMARY_CHARS = 280
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_WS = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Event:
    """Одна новина випуску (D-025): емодзі, короткий заголовок, суть у 1–2 реченнях
    і всі джерельні дописи-учасники."""

    event_id: str
    title: str
    summary: str
    participants: tuple[SourceRef, ...]
    emoji: str = ""
    is_update: bool = False

    def __post_init__(self) -> None:
        if not self.participants:
            raise ValueError("подія без джерел")
        if not self.title.strip():
            raise ValueError("подія без заголовка")


def visible_source(event: Event) -> SourceRef:
    """D-015: допис, де новина з'явилася найраніше; рівний час — вище в списку джерел."""
    return min(event.participants, key=lambda r: (r.date_utc, r.position, r.message_id))


def ordered(events: Iterable[Event]) -> list[Event]:
    """D-016: за зростанням часу найновішого допису групи; найновіші — внизу."""
    return sorted(events, key=lambda e: (max(r.date_utc for r in e.participants), e.event_id))


def preview_summary(text: str, limit: int = MAX_PREVIEW_SUMMARY_CHARS) -> str:
    """Чорновий виклад без ШІ для preview: 1–2 перші речення, обрізані за межею.

    Це НЕ редакторський виклад і не публікується (D-011): потрібен лише щоб
    перевірити рендер, посилання й поділ на частини до появи AI-проходу.
    """
    first_paragraph = next((p for p in text.split("\n") if p.strip()), text)
    sentences = _SENTENCE_END.split(_WS.sub(" ", first_paragraph).strip())
    summary = " ".join(sentences[:2])
    if len(summary) > limit:
        cut = summary[: limit - 1].rsplit(" ", 1)[0]
        summary = cut.rstrip(",;:—- ") + "…"
    return summary


MAX_PREVIEW_TITLE_CHARS = 90


def preview_title_and_summary(text: str) -> tuple[str, str]:
    """Чорнові заголовок (перше речення) і суть (наступні речення) без ШІ."""
    first_paragraph = next((p for p in text.splitlines() if p.strip()), text)
    sentences = _SENTENCE_END.split(_WS.sub(" ", first_paragraph).strip())
    title = sentences[0]
    if len(title) > MAX_PREVIEW_TITLE_CHARS:
        title = title[: MAX_PREVIEW_TITLE_CHARS - 1].rsplit(" ", 1)[0].rstrip(",;:—- ") + "…"
    rest = " ".join(sentences[1:])
    return title, preview_summary(rest) if rest else ""


def preview_events(candidates: Sequence[Candidate]) -> list[Event]:
    """Кожен кандидат — окрема подія з чорновим текстом (до AI-групування, етап 4)."""
    events: list[Event] = []
    for candidate in candidates:
        title, summary = preview_title_and_summary(candidate.text)
        events.append(
            Event(
                event_id=candidate.candidate_id,
                title=title,
                summary=summary,
                participants=candidate.refs,
                emoji="📰",
            )
        )
    return events
