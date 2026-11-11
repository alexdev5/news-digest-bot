"""Доставка серії: guards, random_id, confirmed-only insert, звірка (T61–T70, M07–M10)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from newsdigest.delivery import Publisher, PublishMeta
from newsdigest.pipeline.candidates import SourceRef
from newsdigest.pipeline.events import Event
from newsdigest.pipeline.plan import PublicationPlan, build_plan
from newsdigest.pipeline.render import Header, render_series
from newsdigest.state import StateStore
from newsdigest.storage.published_repo import ConfirmedPart
from newsdigest.telegram.rate_limit import RateLimiter
from newsdigest.telegram.types import OwnMessage, SendRejected
from tests.fakes import FakeClock, FakeFloodWaitError, FakeGateway, channel_peer

T0 = datetime(2026, 11, 11, 13, 0, tzinfo=UTC)
TARGET = channel_peer(-1004402354838, "ok_news_digest_ua")
TARGET = type(TARGET)(TARGET.peer_id, "News Digest UA", "ok_news_digest_ua", True, True)
META = PublishMeta(scheduled_at_utc=T0, source_order=["alpha"])


class MemoryParts:
    def __init__(self) -> None:
        self.rows: dict[str, ConfirmedPart] = {}
        self.fail_inserts = 0

    def committed_part_keys(self, digest_key: str) -> set[str]:
        return {k for k, r in self.rows.items() if r.digest_key == digest_key}

    def used_message_ids(self, destination_peer_id: int) -> set[int]:
        return {
            r.destination_message_id
            for r in self.rows.values()
            if r.destination_peer_id == destination_peer_id
        }

    def close(self) -> None:
        pass

    def insert(self, part: ConfirmedPart) -> None:
        if self.fail_inserts:
            self.fail_inserts -= 1
            raise RuntimeError("db down")
        self.rows.setdefault(part.part_key, part)


def plan(parts: int = 3) -> PublicationPlan:
    ref = SourceRef("alpha", "telegram", -1001, 1, "https://t.me/alpha/1", T0, "Альфа", 0)
    events = [Event(f"e{i}", f"Подія {i}", "y" * 150, (ref,), emoji="📰") for i in range(parts)]
    header = Header(
        "{name} · {date} {time}", "Новини", T0 - timedelta(hours=7), T0, ZoneInfo("UTC")
    )
    rendered = render_series(events, header, 230)
    assert len(rendered) == parts
    ids = iter(range(101, 200))
    return build_plan(
        "d" * 64, "news", T0 - timedelta(hours=7), T0, events, rendered, lambda: next(ids)
    )


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.clock = FakeClock(T0 + timedelta(minutes=1))
        self.gateway = FakeGateway(clock=self.clock)
        self.store = StateStore(tmp_path / "state")
        self.parts = MemoryParts()
        self.limiter = RateLimiter(self.store, self.clock, self.clock.sleep, min_gap_seconds=0)

    def publisher(self) -> Publisher:
        return Publisher(self.gateway, self.limiter, self.store, self.parts, self.clock)

    def phases(self) -> list[str]:
        guards = self.store.load_guards().guards
        return [g.phase for g in sorted(guards.values(), key=lambda g: g.part_index)]


async def test_series_published_in_order_with_planned_random_ids(tmp_path: Path) -> None:
    env = Env(tmp_path)
    p = plan()
    result = await env.publisher().publish(p, TARGET, META)
    assert (result.status, result.committed, result.total) == ("published", 3, 3)
    assert [rid for _, _, rid in env.gateway.sent] == [101, 102, 103]
    assert [text for _, text, _ in env.gateway.sent] == [part.text for part in p.parts]
    assert env.phases() == ["committed"] * 3
    row = env.parts.rows[p.parts[0].part_key]
    assert row.confirmation_method == "ack" and row.destination_message_id == 1001
    assert row.items and row.source_message_ids
    assert env.parts.rows[p.parts[1].part_key].ai_usage_summary is None


async def test_republish_does_not_resend_m08(tmp_path: Path) -> None:
    env = Env(tmp_path)
    p = plan()
    await env.publisher().publish(p, TARGET, META)
    result = await env.publisher().publish(p, TARGET, META)
    assert result.status == "published"
    assert len(env.gateway.sent) == 3


async def test_lost_response_blocks_next_parts_m09_t64(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.gateway.send_errors.append(TimeoutError("no response"))
    result = await env.publisher().publish(plan(), TARGET, META)
    assert result.status == "uncertain"
    assert env.phases() == ["uncertain", "prepared", "prepared"]
    assert env.gateway.sent == [] and env.parts.rows == {}


async def test_reconcile_finds_delivered_part_t63_t65(tmp_path: Path) -> None:
    env = Env(tmp_path)
    p = plan()
    env.gateway.deliver_then_fail = True  # пост у каналі є, відповідь загубилась
    first = await env.publisher().publish(p, TARGET, META)
    assert first.status == "uncertain" and len(env.gateway.sent) == 1
    env.clock.advance(60)
    second = await env.publisher().publish(p, TARGET, META)
    assert second.status == "published"
    assert len(env.gateway.sent) == 3  # частина 1 не надсилалась повторно
    first_row = env.parts.rows[p.parts[0].part_key]
    assert first_row.confirmation_method == "reconciled"
    assert first_row.destination_message_id == 1001


async def test_reconcile_ambiguous_stays_uncertain_t66(tmp_path: Path) -> None:
    env = Env(tmp_path)
    p = plan()
    env.gateway.deliver_then_fail = True
    await env.publisher().publish(p, TARGET, META)
    # Власник опублікував щось руками в той самий час — двоє кандидатів.
    env.gateway.channel_posts.append(OwnMessage(5000, env.clock()))
    result = await env.publisher().publish(p, TARGET, META)
    assert (result.status, result.error_code) == ("uncertain", "reconcile_ambiguous")
    assert len(env.gateway.sent) == 1


async def test_nothing_found_stays_uncertain(tmp_path: Path) -> None:
    env = Env(tmp_path)
    p = plan()
    env.gateway.send_errors.append(ConnectionError("lost"))
    await env.publisher().publish(p, TARGET, META)
    result = await env.publisher().publish(p, TARGET, META)
    assert result.status == "uncertain"
    assert env.gateway.sent == []


async def test_db_failure_after_ack_no_resend_m10_t69(tmp_path: Path) -> None:
    env = Env(tmp_path)
    p = plan()
    env.parts.fail_inserts = 1
    first = await env.publisher().publish(p, TARGET, META)
    assert (first.status, first.error_code) == ("partial", "db_insert_failed")
    assert env.phases()[0] == "confirmed"
    second = await env.publisher().publish(p, TARGET, META)
    assert second.status == "published"
    assert len(env.gateway.sent) == 3
    assert len(env.parts.rows) == 3


async def test_rejected_send_is_not_uncertain(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.gateway.send_errors.append(SendRejected("send_rejected", "ChatWriteForbiddenError"))
    result = await env.publisher().publish(plan(), TARGET, META)
    assert (result.status, result.error_code) == ("failed", "send_rejected")
    assert env.phases() == ["prepared", "prepared", "prepared"]


async def test_flood_wait_keeps_same_random_id(tmp_path: Path) -> None:
    env = Env(tmp_path)
    p = plan()
    env.gateway.send_errors.append(FakeFloodWaitError(3600))
    first = await env.publisher().publish(p, TARGET, META)
    assert first.status == "partial" and env.phases()[0] == "prepared"
    env.clock.advance(3700)
    second = await env.publisher().publish(p, TARGET, META)
    assert second.status == "published"
    assert env.gateway.sent[0][2] == 101


async def test_changed_plan_after_start_rejected(tmp_path: Path) -> None:
    """Після початку доставки інший payload для тієї ж частини заборонений (T67)."""
    env = Env(tmp_path)
    p = plan()
    env.gateway.send_errors.append(TimeoutError())
    await env.publisher().publish(p, TARGET, META)
    changed = replace(p, parts=(replace(p.parts[0], payload_hash="0" * 64), *p.parts[1:]))
    with pytest.raises(RuntimeError):
        await env.publisher().publish(changed, TARGET, META)


async def test_empty_plan_publishes_nothing(tmp_path: Path) -> None:
    env = Env(tmp_path)
    empty = build_plan("e" * 64, "news", T0 - timedelta(hours=7), T0, [], [])
    result = await env.publisher().publish(empty, TARGET, META)
    assert (result.status, result.total) == ("published", 0)
    assert env.gateway.sent == []
