"""AI-групування кандидатів у події (D-009, D-011, D-036).

Модель групує й пише виклад. Код:
- блокує лише структурні помилки (невідомі/двічі вжиті candidate_id, порожній пункт);
- виправляє оформлення сам (`editorial.normalize_event`);
- щодо змісту (числа, довжина, атрибуція каналу) дає моделі одну спробу виправити,
  далі приймає пункт із попередженням у лічильниках.

Невраховані після спроб дописи окремо повторюються по одному; що не вдалося —
публікується без них, а їхні ID потрапляють у `ai_unprocessed` (не позначаються
використаними). Випуск зупиняється (D-011) лише коли AI не дав жодної придатної
відповіді або впав API. Після кількох пачок чи повторів окремий запит лише
знаходить дублікати (групи ID); переписуються з оригіналів тільки ці групи.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from newsdigest.ai.editorial import (
    SHORT_SOURCE_CHARS,
    body_text,
    content_issues,
    normalize_event,
    originals_for,
    quote_in,
    source_budget,
    target_chars,
)
from newsdigest.ai.prompts import (
    DEDUPE_INSTRUCTIONS,
    EXCLUSION_REASONS,
    PROMPT_VERSION,
    SYSTEM_INSTRUCTIONS,
    dedupe_schema,
    response_schema,
)
from newsdigest.ai.provider import AiError, AiUsage, OpenAIProvider
from newsdigest.pipeline.candidates import Candidate, SourceRef
from newsdigest.pipeline.events import Event

MAX_ATTEMPTS = 3
RESCUE_ATTEMPTS = 2
MAX_BATCH_CHARS = 60_000
TraceSink = Callable[[dict[str, Any]], Awaitable[None]]
Originals = Mapping[str, tuple[Candidate, ...]]


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
    unfinished_refs: set[SourceRef] = field(default_factory=set)
    """Дописи з необробленим змістом: не позначаються використаними в БД."""
    usage: list[AiUsage] = field(default_factory=list)
    batches: int = 0
    retried: bool = False
    excluded_sources: list[dict[str, str | int]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    """Зауваження, що лишились після всіх спроб (без текстів новин)."""
    members: dict[str, tuple[Candidate, ...]] = field(default_factory=dict)
    """event_id → кандидати, з яких подію написано (для переписування дублікатів)."""
    keep: set[str] = field(default_factory=set)
    """candidate_id, які модель уже подала як новину: виключати їх не можна."""
    second_chance: set[str] = field(default_factory=set)
    """candidate_id, що вже отримали змістові зауваження (далі — попередження)."""
    accepted_titles: dict[str, list[str]] = field(default_factory=dict)
    """candidate_id → заголовки вже прийнятих з нього пунктів (допис-підбірка, D-037)."""

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


def batches(candidates: Sequence[Candidate], limit: int = MAX_BATCH_CHARS) -> list[list[Candidate]]:
    result: list[list[Candidate]] = []
    current: list[Candidate] = []
    size = 0
    for candidate in candidates:
        length = len(candidate.text) + 200
        if current and size + length > limit:
            result.append(current)
            current, size = [], 0
        current.append(candidate)
        size += length
    if current:
        result.append(current)
    return result


def split_long_candidates(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Every character survives; fragment IDs retain all original source references."""
    result: list[Candidate] = []
    limit = MAX_BATCH_CHARS - 200
    for c in candidates:
        if len(c.text) <= limit:
            result.append(c)
            continue
        for index, start in enumerate(range(0, len(c.text), limit)):
            result.append(
                Candidate(
                    f"{c.candidate_id}:fragment{index}", c.text[start : start + limit], c.refs
                )
            )
    return result


def build_payload(
    batch: Sequence[Candidate],
    context: ChannelContext,
    feedback: Sequence[str] = (),
    originals: Originals | None = None,
) -> dict[str, Any]:
    tz = context.timezone
    evidence = originals or {}
    budgets = {c.candidate_id: source_budget(evidence.get(c.candidate_id, (c,))) for c in batch}
    payload: dict[str, Any] = {
        "prompt_version": PROMPT_VERSION,
        "channel_description": context.topic,
        "short_source_chars": SHORT_SOURCE_CHARS,
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
                "text": c.text,
                "similar_to": list(c.similar_to),
                "source_chars": budgets[c.candidate_id],
                "target_chars": target_chars(budgets[c.candidate_id]),
            }
            for c in batch
        ],
    }
    if feedback:
        payload["previous_answer_problems"] = list(feedback)
    return payload


@dataclass(slots=True)
class Review:
    """Розбір відповіді: загальні проблеми, прийняті й повторювані події."""

    global_problems: list[str] = field(default_factory=list)
    good_events: list[dict[str, Any]] = field(default_factory=list)
    bad_events: list[tuple[dict[str, Any], list[str]]] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)
    rejected_exclusions: list[str] = field(default_factory=list)
    rejected_ids: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    """Коди змістових зауважень, з якими подію прийнято (лічильники `ai_warning_*`)."""

    @property
    def problems(self) -> list[str]:
        return self.global_problems + [p for _, items in self.bad_events for p in items]

    @property
    def covered(self) -> set[str]:
        ids = {cid for event in self.good_events for cid in event["candidate_ids"]}
        return ids | {item["candidate_id"] for item in self.excluded}


def review(
    data: dict[str, Any],
    batch: Sequence[Candidate],
    *,
    originals: Originals | None = None,
    timezone: ZoneInfo | None = None,
    second_chance: set[str] | frozenset[str] = frozenset(),
    allow_exclusions: bool = True,
    keep: set[str] | frozenset[str] = frozenset(),
) -> Review:
    """Перевіряє відповідь. Одна погана подія не скасовує решту придатних.

    `second_chance` — candidate_id, що вже отримали змістові зауваження: тепер їх
    приймаємо з попередженням. `keep` — кандидати, яких не можна виключати.
    """
    by_id = {c.candidate_id: c for c in batch}
    evidence = originals or {}
    result = Review()
    events = data.get("events")
    excluded = data.get("excluded")
    if not isinstance(events, list) or not isinstance(excluded, list):
        result.global_problems.append("відповідь не відповідає схемі")
        return result
    if any(not isinstance(e, dict) for e in events + excluded):
        result.global_problems.append("елемент відповіді не є об'єктом")
        return result

    # Один допис може дати кілька РІЗНИХ пунктів (підбірка новин, D-037), але не
    # може бути водночас у пункті й у виключеннях або виключений двічі.
    in_events: set[str] = set()
    for event in events:
        in_events.update(str(c) for c in event.get("candidate_ids") or [])
    excluded_count: dict[str, int] = {}
    for item in excluded:
        cid = str(item.get("candidate_id"))
        excluded_count[cid] = excluded_count.get(cid, 0) + 1
    unknown = sorted(c for c in in_events | set(excluded_count) if c not in by_id)
    conflict = sorted(
        c for c, n in excluded_count.items() if c in by_id and (n > 1 or c in in_events)
    )
    if unknown:
        result.global_problems.append(f"невідомі candidate_id: {', '.join(unknown)}")
    if conflict:
        result.global_problems.append(
            f"кандидати і в події, і у виключеннях (або виключені двічі): {', '.join(conflict)}"
        )
    if result.global_problems:
        return result

    for raw in events:
        ids = list(dict.fromkeys(c for c in raw.get("candidate_ids") or [] if c in by_id))
        label = f"подія ({', '.join(ids) or 'без candidate_ids'})"
        if not ids:
            result.bad_events.append((raw, [f"{label}: немає candidate_ids"]))
            continue
        selected = [by_id[c] for c in ids]
        event, _notes = normalize_event(raw, selected, evidence)
        if not event["title"]:
            result.bad_events.append((raw, [f"{label}: порожній пункт — напиши суть новини"]))
            continue
        issues = content_issues(event, selected, evidence, timezone)
        if issues and not set(ids) <= second_chance:
            result.bad_events.append((event, [f"{label}: {text}" for _, text in issues]))
            continue
        result.warnings.extend(code for code, _ in issues)
        result.good_events.append(event)
    for item in excluded:
        cid = str(item["candidate_id"])
        if item.get("reason") not in EXCLUSION_REASONS or not allow_exclusions or cid in keep:
            result.rejected_ids.add(cid)
        if item.get("reason") not in EXCLUSION_REASONS:
            result.rejected_exclusions.append(
                f"{cid}: невідома причина виключення {item.get('reason')!r} — це новина, "
                "поверни її подією"
            )
        elif not allow_exclusions or cid in keep:
            result.rejected_exclusions.append(
                f"{cid}: не можна виключати новину замість виправлення — поверни її подією"
            )
        else:
            result.excluded.append(item)
    return result


def validate(data: dict[str, Any], batch: Sequence[Candidate]) -> tuple[list[str], set[str]]:
    """Сумісний короткий вигляд `review`: усі проблеми й враховані candidate_id."""
    checked = review(data, batch)
    return checked.problems + checked.rejected_exclusions, checked.covered


def _event(data: dict[str, Any], batch: Sequence[Candidate], originals: Originals) -> Event:
    by_id = {c.candidate_id: c for c in batch}
    ids = sorted(data["candidate_ids"])
    refs = sorted(
        {ref for cid in ids for ref in by_id[cid].refs},
        key=lambda r: (r.date_utc, r.position, r.message_id),
    )
    quote = str(data.get("quote", ""))
    quote_id = str(data.get("quote_candidate_id", ""))
    return Event(
        event_id=hashlib.sha256(("|".join(ids) + "\n" + str(data["title"])).encode()).hexdigest()[
            :16
        ],
        title=str(data["title"]),
        summary=str(data["summary"]),
        participants=tuple(refs),
        emoji=str(data.get("emoji", "")),
        is_update=bool(data.get("is_update", False)),
        layout=str(data.get("layout", "detailed")),
        bullets=tuple(b["text"] for b in data.get("bullets", [])),
        quote=quote,
        quote_sources=tuple(
            r
            for c in originals_for([quote_id], by_id, originals)
            if quote_in(quote, c)
            for r in c.refs
        )
        if quote and quote_id in by_id
        else (),
    )


async def _trace(trace: TraceSink | None, record: dict[str, Any]) -> None:
    if trace is not None:
        await trace(record)


async def _group_batch(
    batch: list[Candidate],
    provider: OpenAIProvider,
    context: ChannelContext,
    result: GroupingResult,
    *,
    originals: Originals,
    trace: TraceSink | None = None,
    attempts: int = MAX_ATTEMPTS,
    allow_exclusions: bool = True,
    same_event_hint: bool = False,
    merge_drafts: Sequence[str] = (),
    phase: str = "group",
) -> tuple[list[Candidate], bool]:
    """Приймає придатні події одразу; решту повторно надсилає з поясненням.

    Повертає (невраховані кандидати, чи прийнято хоч щось). AiError API — далі.
    """
    pending = batch
    feedback: list[str] = []
    corrections: list[dict[str, Any]] = []
    # Спільні для основного проходу й rescue: уже визнана новиною не стає not_news,
    # а вже отримане змістове зауваження не повторюється.
    second_chance = result.second_chance
    keep = result.keep
    accepted_any = False
    for attempt in range(attempts):
        payload = build_payload(pending, context, feedback, originals)
        if same_event_hint:
            payload["same_event_hint"] = True
        if merge_drafts:
            payload["merge_drafts"] = list(merge_drafts)
        accepted_before = {
            c.candidate_id: result.accepted_titles[c.candidate_id]
            for c in pending
            if c.candidate_id in result.accepted_titles
        }
        if accepted_before and not merge_drafts:
            # Інші новини цих дописів уже прийняті — не повторювати їх.
            payload["already_accepted"] = accepted_before
        if corrections:
            payload["corrections"] = corrections
        record: dict[str, Any] = {
            "phase": phase,
            "attempt": attempt + 1,
            "instructions": SYSTEM_INSTRUCTIONS,
            "payload": payload,
        }
        try:
            data, usage = await provider.complete_json(
                SYSTEM_INSTRUCTIONS,
                payload,
                response_schema(allow_exclusions=allow_exclusions),
                "digest_events",
            )
        except AiError as exc:
            await _trace(trace, record | {"error_code": exc.code, "detail": str(exc)})
            raise
        result.usage.append(usage)
        checked = review(
            data,
            pending,
            originals=originals,
            timezone=context.timezone,
            second_chance=second_chance,
            allow_exclusions=allow_exclusions,
            keep=keep,
        )
        await _trace(
            trace,
            record
            | {
                "response": data,
                "problems": checked.problems + checked.rejected_exclusions,
                "covered": sorted(checked.covered),
                "usage": {"input": usage.input_tokens, "output": usage.output_tokens},
            },
        )
        if checked.global_problems:
            feedback, corrections = checked.global_problems, []
            result.retried = True
            continue
        for event in checked.good_events:
            accepted = _event(event, pending, originals)
            while accepted.event_id in result.members:  # той самий допис, той самий заголовок
                accepted = replace(
                    accepted,
                    event_id=hashlib.sha256(accepted.event_id.encode()).hexdigest()[:16],
                )
            result.events.append(accepted)
            result.members[accepted.event_id] = tuple(
                c for c in pending if c.candidate_id in event["candidate_ids"]
            )
            for cid in event["candidate_ids"]:
                result.accepted_titles.setdefault(cid, []).append(accepted.title)
        for code in checked.warnings:
            result.count(f"ai_warning_{code}")
        for item in checked.excluded:
            result.count(f"ai_{item['reason']}")
            c = next(c for c in pending if c.candidate_id == item["candidate_id"])
            result.excluded_sources.extend(
                {
                    "source_id": r.source_id,
                    "peer_id": r.peer_id,
                    "message_id": r.message_id,
                    "candidate_id": c.candidate_id,
                    "reason": f"ai_{item['reason']}",
                }
                for r in c.refs
            )
        accepted_any = accepted_any or bool(checked.good_events or checked.excluded)
        covered = checked.covered
        bad_ids = {
            str(cid) for event, _ in checked.bad_events for cid in event.get("candidate_ids") or []
        }
        # Допис-підбірка: одна його новина прийнята, інша потребує виправлення —
        # допис лишається в обробці, доки не прийнято й ту новину.
        pending = [c for c in pending if c.candidate_id not in covered or c.candidate_id in bad_ids]
        if not pending:
            return [], accepted_any
        result.retried = True
        second_chance |= bad_ids
        keep |= bad_ids
        corrections = [
            {"previous_event": event, "problems": issues} for event, issues in checked.bad_events
        ]
        feedback = [p for _, issues in checked.bad_events for p in issues]
        feedback += checked.rejected_exclusions
        mentioned = bad_ids | checked.rejected_ids
        missed = [c.candidate_id for c in pending if c.candidate_id not in mentioned]
        if missed:
            feedback.append(f"не враховано кандидатів: {', '.join(missed)}")
    result.problems.extend(feedback)
    return pending, accepted_any


def _mark_unprocessed(result: GroupingResult, candidates: Sequence[Candidate]) -> None:
    for c in candidates:
        result.unprocessed.append(c.candidate_id)
        result.unfinished_refs.update(c.refs)
        result.count("ai_unprocessed")
        result.excluded_sources.extend(
            {
                "source_id": r.source_id,
                "peer_id": r.peer_id,
                "message_id": r.message_id,
                "candidate_id": c.candidate_id,
                "reason": "ai_unprocessed",
            }
            for r in c.refs
        )


async def group_with_ai(
    candidates: Sequence[Candidate],
    provider: OpenAIProvider,
    context: ChannelContext,
    *,
    trace: TraceSink | None = None,
) -> GroupingResult:
    result = GroupingResult()
    fragments = split_long_candidates(candidates)
    original_by_ref = {r: c for c in candidates for r in c.refs}
    originals = {
        c.candidate_id: tuple(dict.fromkeys(original_by_ref[r] for r in c.refs)) for c in fragments
    }
    for batch in batches(fragments):
        result.batches += 1
        left, accepted_any = await _group_batch(
            list(batch), provider, context, result, originals=originals, trace=trace
        )
        if not left:
            continue
        if not accepted_any:
            # Жодної придатної відповіді на пачку: AI не працює, випуск не виходить (D-011).
            raise AiError(
                "invalid_answer", "; ".join(result.problems[-5:]) or "немає валідної відповіді"
            )
        for candidate in left:  # окремий маленький запит на кожен проблемний допис
            rest, _ = await _group_batch(
                [candidate],
                provider,
                context,
                result,
                originals=originals,
                trace=trace,
                attempts=RESCUE_ATTEMPTS,
                phase="rescue",
            )
            _mark_unprocessed(result, rest)
    if len(result.events) > 1 and (result.batches > 1 or result.retried):
        await _deduplicate(result, provider, context, originals, trace)
    _unique_ids(result.events)
    return result


def _unique_ids(events: list[Event]) -> None:
    """event_id унікальні в межах випуску (той самий допис і заголовок двічі)."""
    seen: set[str] = set()
    for index, event in enumerate(events):
        event_id = event.event_id
        while event_id in seen:
            event_id = hashlib.sha256(event_id.encode()).hexdigest()[:16]
        seen.add(event_id)
        if event_id != event.event_id:
            events[index] = replace(event, event_id=event_id)


async def _deduplicate(
    result: GroupingResult,
    provider: OpenAIProvider,
    context: ChannelContext,
    originals: Originals,
    trace: TraceSink | None,
) -> None:
    """Знаходить дублікати між пачками/повторами; переписує з оригіналів лише їх."""
    tz = context.timezone
    drafts = {f"d{i}": event for i, event in enumerate(result.events, start=1)}
    payload = {
        "drafts": [
            {
                "id": key,
                "time": f"{e.participants[0].date_utc.astimezone(tz):%d.%m %H:%M}",
                "text": body_text(
                    {
                        "title": e.title,
                        "summary": e.summary,
                        "bullets": [{"text": b} for b in e.bullets],
                    }
                ),
            }
            for key, e in drafts.items()
        ]
    }
    record: dict[str, Any] = {
        "phase": "dedupe",
        "attempt": 1,
        "instructions": DEDUPE_INSTRUCTIONS,
        "payload": payload,
    }
    try:
        data, usage = await provider.complete_json(
            DEDUPE_INSTRUCTIONS, payload, dedupe_schema(), "digest_duplicates"
        )
    except AiError as exc:
        # Чернетки вже перевірені; можливий дубль кращий за зупинку всього випуску.
        await _trace(trace, record | {"error_code": exc.code, "detail": str(exc)})
        result.problems.append(f"перевірка дублікатів не вдалася: {exc.code}")
        result.count("ai_dedupe_failed")
        return
    result.usage.append(usage)
    await _trace(trace, record | {"response": data})
    used: set[str] = set()
    groups: list[list[str]] = []
    for group in data.get("duplicates") or []:
        ids = list(dict.fromkeys(str(i) for i in (group or {}).get("draft_ids") or []))
        if len(ids) < 2 or any(i not in drafts or i in used for i in ids):
            continue
        used.update(ids)
        groups.append(ids)
    replaced: dict[str, list[Event]] = {}
    for ids in groups:
        group_events = [drafts[i] for i in ids]
        sources = list(
            dict.fromkeys(c for e in group_events for c in result.members.get(e.event_id, ()))
        )
        merged = GroupingResult()
        try:
            left, _ = await _group_batch(
                sources,
                provider,
                context,
                merged,
                originals=originals,
                trace=trace,
                attempts=RESCUE_ATTEMPTS,
                allow_exclusions=False,
                same_event_hint=True,
                merge_drafts=[payload["drafts"][int(i[1:]) - 1]["text"] for i in ids],
                phase="merge",
            )
        except AiError as exc:
            result.problems.append(f"злиття дублікатів не вдалося: {exc.code}")
            result.count("ai_dedupe_failed")
            left = sources
        result.usage.extend(merged.usage)
        if left or len(merged.events) != 1:
            # Злиття має дати рівно одну подію; інакше (зайва сусідня новина з
            # підбірки, втрата) лишаємо перевірені чернетки як є.
            continue
        update = any(e.is_update for e in group_events)
        replaced[ids[0]] = [replace(e, is_update=e.is_update or update) for e in merged.events]
        for i in ids[1:]:
            replaced[i] = []
        for reason, amount in merged.excluded.items():
            result.count(reason, amount)
        result.members.update(merged.members)
    if not replaced:
        return
    events: list[Event] = []
    for key, event in drafts.items():
        events.extend(replaced.get(key, [event]))
    result.events = events
