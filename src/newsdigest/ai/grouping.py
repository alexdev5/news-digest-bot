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

MAX_ATTEMPTS = 3
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
    problems: list[str] = field(default_factory=list)
    """Зауваження до відповіді моделі, що лишились після всіх спроб (без текстів новин)."""

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


def _source_numbers(text: str) -> set[str]:
    """Числа джерела, включно з частинами дат/дробів: «11.11» дає 11.11 і 11."""
    numbers = _numbers(text)
    for number in list(numbers):
        numbers.update(part for part in number.split(".") if part)
    return numbers


@dataclass(slots=True)
class Review:
    """Розбір відповіді моделі: загальні проблеми, придатні й непридатні події."""

    global_problems: list[str] = field(default_factory=list)
    good_events: list[dict[str, Any]] = field(default_factory=list)
    bad_events: list[tuple[dict[str, Any], list[str]]] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)

    @property
    def problems(self) -> list[str]:
        return self.global_problems + [p for _, items in self.bad_events for p in items]

    @property
    def covered(self) -> set[str]:
        ids = {cid for event in self.good_events for cid in event["candidate_ids"]}
        return ids | {item["candidate_id"] for item in self.excluded}


def review(data: dict[str, Any], batch: Sequence[Candidate]) -> Review:
    """Перевіряє відповідь. Одна погана подія не скасовує решту придатних."""
    by_id = {c.candidate_id: c for c in batch}
    result = Review()
    events = data.get("events")
    excluded = data.get("excluded")
    if not isinstance(events, list) or not isinstance(excluded, list):
        result.global_problems.append("відповідь не відповідає схемі")
        return result

    usage: dict[str, int] = {}
    for event in events:
        for cid in event.get("candidate_ids") or []:
            usage[cid] = usage.get(cid, 0) + 1
    for item in excluded:
        cid = item.get("candidate_id")
        usage[cid] = usage.get(cid, 0) + 1
    unknown = sorted(str(c) for c in usage if c not in by_id)
    twice = sorted(c for c, n in usage.items() if n > 1 and c in by_id)
    if unknown:
        result.global_problems.append(f"невідомі candidate_id: {', '.join(unknown)}")
    if twice:
        result.global_problems.append(f"кандидати ужито двічі: {', '.join(twice)}")
    if result.global_problems:
        return result

    for event in events:
        ids = [c for c in event.get("candidate_ids") or [] if c in by_id]
        title = str(event.get("title", "")).strip()
        summary = str(event.get("summary", "")).strip()
        label = f"подія «{title[:40]}» ({', '.join(ids) or 'без candidate_ids'})"
        issues: list[str] = []
        if not ids:
            issues.append(f"{label}: немає candidate_ids")
        if not title or len(title) > MAX_TITLE_CHARS:
            issues.append(f"{label}: заголовок порожній або довший за {MAX_TITLE_CHARS}")
        if len(summary) > MAX_SUMMARY_CHARS:
            issues.append(f"{label}: summary довший за {MAX_SUMMARY_CHARS}")
        if len(str(event.get("emoji", ""))) > MAX_EMOJI_CHARS:
            issues.append(f"{label}: emoji має бути одним символом")
        sources = " ".join(_compact(by_id[c].text) for c in ids)
        missing = _numbers(_compact(f"{title} {summary}")) - _source_numbers(sources)
        if missing:
            issues.append(
                f"{label}: числа {sorted(missing)} відсутні в текстах джерел — "
                "напиши їх точно як у джерелі або прибери"
            )
        if issues:
            result.bad_events.append((event, issues))
        else:
            result.good_events.append(event)
    for item in excluded:
        if item.get("reason") in EXCLUSION_REASONS:
            result.excluded.append(item)
        else:
            result.global_problems.append(f"невідома причина виключення {item.get('reason')!r}")
    return result


def validate(data: dict[str, Any], batch: Sequence[Candidate]) -> tuple[list[str], set[str]]:
    """Сумісний короткий вигляд `review`: усі проблеми й враховані candidate_id."""
    checked = review(data, batch)
    return checked.problems, checked.covered


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
    """Приймає придатні події одразу; решту кандидатів повторно надсилає з поясненням."""
    pending = batch
    feedback: list[str] = []
    accepted_any = False
    for _attempt in range(MAX_ATTEMPTS):
        data, usage = await provider.complete_json(
            SYSTEM_INSTRUCTIONS,
            build_payload(pending, context, feedback),
            response_schema(),
            "digest_events",
        )
        result.usage.append(usage)
        checked = review(data, pending)
        if checked.global_problems:
            feedback = checked.global_problems[:20]
            continue
        for event in checked.good_events:
            result.events.append(_event(event, pending))
        for item in checked.excluded:
            result.count(f"ai_{item['reason']}")
        accepted_any = accepted_any or bool(checked.good_events or checked.excluded)
        covered = checked.covered
        pending = [c for c in pending if c.candidate_id not in covered]
        if not pending:
            return
        feedback = [p for _, issues in checked.bad_events for p in issues][:20]
        missed = [c.candidate_id for c in pending if not _mentioned(c.candidate_id, checked)]
        if missed:
            feedback.append(f"не враховано кандидатів: {', '.join(missed)}")
    result.problems.extend(feedback)
    if not accepted_any:
        # Жодної придатної відповіді для пачки: AI-прохід не вдався, випуск не публікується.
        raise AiError("invalid_answer", "; ".join(feedback[:5]) or "немає валідної відповіді")
    result.unprocessed.extend(c.candidate_id for c in pending)
    result.count("ai_unprocessed", len(pending))


def _mentioned(candidate_id: str, checked: Review) -> bool:
    return any(
        candidate_id in (event.get("candidate_ids") or []) for event, _ in checked.bad_events
    )
