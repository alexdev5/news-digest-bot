"""Збирання випуску з кешу run: кандидати → події → порядок → частини → план."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from newsdigest.cache.run_cache import CachedMessage
from newsdigest.config.models import TargetChannel
from newsdigest.identity import digest_key
from newsdigest.pipeline.candidates import Candidate, CandidateSet, build_candidates
from newsdigest.pipeline.events import Event, ordered, preview_events
from newsdigest.pipeline.plan import PublicationPlan, build_plan
from newsdigest.pipeline.render import Header, render_series
from newsdigest.scheduling import Window
from newsdigest.state.store import RuntimeState
from newsdigest.telegram_refs import parse_telegram_ref

Grouper = Callable[[Sequence[Candidate]], list[Event]]


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
        positions[source.id] = len(positions)
        pinned = peers.get(str(parse_telegram_ref(source.ref)))
        labels[source.id] = source.name or (pinned.title if pinned else source.id)
    for site in channel.source_sites:
        positions[site.id] = len(positions)
        labels[site.id] = site.name
    return positions, labels


@dataclass(frozen=True, slots=True)
class BuiltDigest:
    candidates: CandidateSet
    events: list[Event]
    plan: PublicationPlan


def build_digest(
    channel: TargetChannel,
    window: Window,
    timezone: ZoneInfo,
    max_chars: int,
    messages: Sequence[CachedMessage],
    *,
    runtime: RuntimeState | None = None,
    published: set[tuple[object, ...]] | None = None,
    grouper: Grouper = preview_events,
) -> BuiltDigest:
    positions, labels = source_maps(channel, runtime)
    candidates = build_candidates(
        messages,
        positions=positions,
        labels=labels,
        exclude_patterns=channel.filters.exclude_patterns,
        published=published,
    )
    events = ordered(grouper(candidates.candidates))
    header = Header(
        template=channel.format.header,
        name=channel.name,
        window_start=window.start_utc,
        window_end=window.end_utc,
        timezone=timezone,
    )
    parts = render_series(events, header, max_chars)
    plan = build_plan(
        digest_key(channel.id, window),
        channel.id,
        window.start_utc,
        window.end_utc,
        events,
        parts,
    )
    return BuiltDigest(candidates=candidates, events=events, plan=plan)
