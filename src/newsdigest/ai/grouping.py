"""AI-групування кандидатів у події з перевіркою відповіді моделі (D-009, D-001, D-011).

Модель групує й пише виклад; код перевіряє: усі candidate_id відомі, кожен
кандидат ужито рівно один раз, поля в межах, числа у викладі є в текстах
джерел цієї події. Невдала перевірка → до 2 спроб із поясненням помилок (v4 §12.3).
Кандидати, яких модель так і не врахувала, рахуються як `ai_unprocessed` і не
публікуються (D-019) — але й не губляться мовчки: їх видно в лічильниках.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from newsdigest.ai.prompts import (
    EXCLUSION_REASONS,
    PROMPT_VERSION,
    SYSTEM_INSTRUCTIONS,
    response_schema,
)
from newsdigest.ai.provider import AiError, AiUsage, OpenAIProvider
from newsdigest.pipeline.candidates import Candidate
from newsdigest.pipeline.events import Event

MAX_ATTEMPTS = 2
MAX_BATCH_CHARS = 60_000
MAX_CANDIDATE_CHARS = 4_000
MAX_TITLE_CHARS = 120
MAX_SUMMARY_CHARS = 450
MAX_EMOJI_CHARS = 8
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


@dataclass(frozen=True, slots=True)
class PublishedItem:
    """Пункт, уже опублікований у цьому каналі (міжвипускна пам'ять, v4 §11)."""

    title: str
    summary: str
    published_at: datetime


@dataclass(slots=True)
class GroupingResult:
    events: list[Event] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)
    unprocessed: list[str] = field(default_factory=list)
    usage: list[AiUsage] = field(default_factory=list)
    batches: int = 0

    def count(self, reason: str, amount: int = 1) -> None:
        self.excluded[reason] = self.excluded.get(reason, 0) + amount

    @property
    def tokens(self) -> dict[str, int]:
        return {
            "input": sum(u.input_tokens for u in self.usage),
            "output": sum(u.output_tokens for u in self.usage),
            "requests": len(self.usage),
        }


@dataclass(frozen=True, slots=True)
class ChannelContext:
    topic: str
    channel_prompt: str
    timezone: ZoneInfo
    published_recent: tuple[PublishedItem, ...] = ()


def _numbers(text: str) -> set[str]:
    return {m.group(0).replace(",", ".") for m in _NUMBER.finditer(text.replace(" ", ""))}


def _compact(text: str) -> str:
    return re.sub(r"(?<=\d)[\s ](?=\d{3}\b)", "", text)


def batches(candidates: Sequence[Candidate], limit: int = MAX_BATCH_CHARS) -> list[list[Candidate]]:
    result: list[list[Candidate]] = []
    current: list[Candidate] = []
    size = 0
    for candidate in candidates:
        length = min(len(candidate.text), MAX_CANDIDATE_CHARS) + 200
        if current and size + length > limit:
            result.append(current)
            current, size = [], 0
        current.append(candidate)
        size += length
    if current:
        result.append(current)
    return result


def build_payload(
    batch: Sequence[Candidate], context: ChannelContext, feedback: Sequence[str] = ()
) -> dict[str, Any]:
    tz = context.timezone
    payload: dict[str, Any] = {
        "prompt_version": PROMPT_VERSION,
        "topic": context.topic,
        "channel_prompt": context.channel_prompt,
        "published_recent": [
            {
                "title": p.title,
                "summary": p.summary,
                "published": f"{p.published_at.astimezone(tz):%d.%m %H:%M}",
            }
            for p in context.published_recent
        ],
        "candidates": [
            {
                "id": c.candidate_id,
                "sources": sorted({r.label for r in c.refs}),
                "time": f"{c.earliest.astimezone(tz):%d.%m %H:%M}",
                "text": c.text[:MAX_CANDIDATE_CHARS],
                "similar_to": list(c.similar_to),
            }
            for c in batch
        ],
    }
    if feedback:
        payload["previous_answer_problems"] = list(feedback)
    return payload


def validate(data: dict[str, Any], batch: Sequence[Candidate]) -> tuple[list[str], set[str]]:
    """Помилки відповіді й множина врахованих candidate_id."""
    by_id = {c.candidate_id: c for c in batch}
    seen: dict[str, str] = {}
    problems: list[str] = []
    events = data.get("events")
    excluded = data.get("excluded")
    if not isinstance(events, list) or not isinstance(excluded, list):
        return ["відповідь не відповідає схемі"], set()

    def mark(cid: object, where: str) -> None:
        if not isinstance(cid, str) or cid not in by_id:
            problems.append(f"невідомий candidate_id {cid!r}")
        elif cid in seen:
            problems.append(f"кандидат {cid} ужито двічі ({seen[cid]} і {where})")
        else:
            seen[cid] = where

    for index, event in enumerate(events):
        ids = event.get("candidate_ids") or []
        if not ids:
            problems.append(f"подія {index}: немає candidate_ids")
        for cid in ids:
            mark(cid, f"подія {index}")
        title = str(event.get("title", "")).strip()
        summary = str(event.get("summary", "")).strip()
        if not title or len(title) > MAX_TITLE_CHARS:
            problems.append(f"подія {index}: заголовок порожній або довший за {MAX_TITLE_CHARS}")
        if len(summary) > MAX_SUMMARY_CHARS:
            problems.append(f"подія {index}: summary довший за {MAX_SUMMARY_CHARS}")
        if len(str(event.get("emoji", ""))) > MAX_EMOJI_CHARS:
            problems.append(f"подія {index}: emoji має бути одним символом")
        sources = " ".join(
            _compact(by_id[c].text) for c in ids if isinstance(c, str) and c in by_id
        )
        missing = _numbers(_compact(f"{title} {summary}")) - _numbers(sources)
        if missing:
            problems.append(f"подія {index}: числа {sorted(missing)} відсутні в текстах джерел")
    for item in excluded:
        mark(item.get("candidate_id"), "excluded")
        if item.get("reason") not in EXCLUSION_REASONS:
            problems.append(f"невідома причина виключення {item.get('reason')!r}")
    return problems, set(seen)


def _event(data: dict[str, Any], batch: Sequence[Candidate]) -> Event:
    by_id = {c.candidate_id: c for c in batch}
    ids = sorted(data["candidate_ids"])
    refs = sorted(
        {ref for cid in ids for ref in by_id[cid].refs},
        key=lambda r: (r.date_utc, r.position, r.message_id),
    )
    return Event(
        event_id=hashlib.sha256("|".join(ids).encode()).hexdigest()[:16],
        title=str(data["title"]).strip().rstrip("."),
        summary=str(data["summary"]).strip(),
        participants=tuple(refs),
        emoji=str(data.get("emoji", "")).strip(),
        is_update=bool(data.get("is_update", False)),
    )


async def group_with_ai(
    candidates: Sequence[Candidate], provider: OpenAIProvider, context: ChannelContext
) -> GroupingResult:
    result = GroupingResult()
    for batch in batches(candidates):
        result.batches += 1
        await _group_batch(list(batch), provider, context, result)
    return result


async def _group_batch(
    batch: list[Candidate],
    provider: OpenAIProvider,
    context: ChannelContext,
    result: GroupingResult,
) -> None:
    pending = batch
    feedback: list[str] = []
    for _attempt in range(MAX_ATTEMPTS):
        data, usage = await provider.complete_json(
            SYSTEM_INSTRUCTIONS,
            build_payload(pending, context, feedback),
            response_schema(),
            "digest_events",
        )
        result.usage.append(usage)
        problems, covered = validate(data, pending)
        if problems:
            feedback = problems[:20]
            continue
        for event in data["events"]:
            result.events.append(_event(event, pending))
        for item in data["excluded"]:
            result.count(f"ai_{item['reason']}")
        pending = [c for c in pending if c.candidate_id not in covered]
        if not pending:
            return
        feedback = [f"не враховано кандидатів: {', '.join(c.candidate_id for c in pending)}"]
    if len(pending) == len(batch):
        # Жодної придатної відповіді для пачки: AI-прохід не вдався, випуск не публікується.
        raise AiError("invalid_answer", "; ".join(feedback[:5]) or "немає валідної відповіді")
    result.unprocessed.extend(c.candidate_id for c in pending)
    result.count("ai_unprocessed", len(pending))
