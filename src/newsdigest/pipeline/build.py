"""Збирання випуску з кешу run: кандидати → події → порядок → частини → план."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from newsdigest.cache.run_cache import CachedMessage
from newsdigest.config.models import TargetChannel
from newsdigest.identity import digest_key
from newsdigest.pipeline.candidates import Candidate, CandidateSet, SourceRef, build_candidates
from newsdigest.pipeline.events import Event, ordered, preview_events
from newsdigest.pipeline.plan import PublicationPlan, build_plan
from newsdigest.pipeline.render import Header, render_series
from newsdigest.scheduling import Window
from newsdigest.state.store import RuntimeState
from newsdigest.telegram_refs import parse_telegram_ref

Grouper = Callable[[Sequence[Candidate]], Awaitable["Grouped"]]


@dataclass(frozen=True, slots=True)
class Grouped:
    """Результат групування: події + причини виключень (числа) + технічні metadata."""

    events: list[Event]
    excluded: dict[str, int]
    tokens: dict[str, int] | None = None
    excluded_sources: list[dict[str, str | int]] = field(default_factory=list)
    unfinished_refs: frozenset[SourceRef] = frozenset()
    """Дописи, частину змісту яких AI не обробив (`ai_unprocessed`)."""


async def draft_grouper(candidates: Sequence[Candidate]) -> Grouped:
    """Без ШІ: кожен кандидат — окрема подія з чорновим текстом (лише preview, D-011)."""
    return Grouped(events=preview_events(candidates), excluded={})


def source_maps(
    channel: TargetChannel, runtime: RuntimeState | None = None
) -> tuple[dict[str, int], dict[str, str]]:
    """Позиції (порядок у конфігу) і видимі назви джерел за source_id.

    Назва: `name` з конфігу → назва каналу, закріплена під час сканування → id.
    """
    positions: dict[str, int] = {}
    labels: dict[str, str] = {}
    peers = runtime.peers if runtime is not None else {}
    for source in channel.source_channels:
        if not source.enabled:
            continue
        positions[source.id] = len(positions)
        pinned = peers.get(str(parse_telegram_ref(source.ref)))
        labels[source.id] = source.name or (pinned.title if pinned else source.id)
    for site in channel.source_sites:
        if not site.enabled:
            continue
        positions[site.id] = len(positions)
        labels[site.id] = site.name
    return positions, labels


@dataclass(frozen=True, slots=True)
class BuiltDigest:
    candidates: CandidateSet
    events: list[Event]
    plan: PublicationPlan
    tokens: dict[str, int] | None = None


async def build_digest(
    channel: TargetChannel,
    window: Window,
    timezone: ZoneInfo,
    max_chars: int,
    messages: Sequence[CachedMessage],
    *,
    runtime: RuntimeState | None = None,
    published: set[tuple[object, ...]] | None = None,
    grouper: Grouper = draft_grouper,
    run_key: str | None = None,
) -> BuiltDigest:
    positions, labels = source_maps(channel, runtime)
    candidates = build_candidates(
        messages,
        positions=positions,
        labels=labels,
        exclude_patterns=channel.filters.exclude_patterns,
        published=published,
    )
    grouped = await grouper(candidates.candidates) if candidates.candidates else Grouped([], {})
    for reason, count in grouped.excluded.items():
        candidates.count(reason, count)
    candidates.excluded_sources.extend(grouped.excluded_sources)
    events = ordered(grouped.events)
    header = Header(
        template=channel.format.header,
        name=channel.name,
        window_start=window.start_utc,
        window_end=window.end_utc,
        timezone=timezone,
    )
    parts = render_series(events, header, max_chars)
    plan = build_plan(
        run_key or digest_key(channel.id, window),
        channel.id,
        window.start_utc,
        window.end_utc,
        events,
        parts,
        unfinished=grouped.unfinished_refs,
    )
    return BuiltDigest(candidates=candidates, events=events, plan=plan, tokens=grouped.tokens)
