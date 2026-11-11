from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from newsdigest.cache.run_cache import CacheManager
from newsdigest.config.loader import parse_config_text
from newsdigest.config.models import AppConfig
from newsdigest.state import StateStore
from newsdigest.telegram.rate_limit import RateLimiter
from newsdigest.worker import RunExecutor, Worker
from tests.fakes import (
    FakeClock,
    FakeGateway,
    SimulatedCrash,
    channel_peer,
    factory,
    messages_every,
)

KYIV = ZoneInfo("Europe/Kyiv")
PEER = -1001000000001


def kyiv(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=KYIV).astimezone(UTC)


def config(enabled: bool = True, times: tuple[str, ...] = ("08:00", "21:00")) -> AppConfig:
    data: dict[str, Any] = {
        "schema_version": 4,
        "target_channels": [
            {
                "id": "news",
                "enabled": enabled,
                "name": "Новини",
                "telegram": "@my_digest",
                "schedule": {"times": list(times)},
                "scan_start_at": "2026-11-01T00:00:00+02:00",
                "source_channels": [{"id": "alpha", "ref": "@alpha_news"}],
            }
        ],
    }
    return parse_config_text(json.dumps(data))


class Harness:
    def __init__(self, tmp_path: Path, now: datetime, cfg: AppConfig | None = None) -> None:
        self.tmp = tmp_path
        self.clock = FakeClock(now)
        self.gateway = FakeGateway(
            peers={"@alpha_news": channel_peer(PEER, "alpha_news")},
            history={PEER: messages_every(kyiv(2026, 11, 9), 400, 15)},
        )
        self.store = StateStore(tmp_path / "state")
        self.cache = CacheManager(tmp_path / "cache", ttl_seconds=86_400)
        self.worker = self.build(cfg or config())

    def build(self, cfg: AppConfig) -> Worker:
        limiter = RateLimiter(self.store, self.clock, self.clock.sleep, min_gap_seconds=0)
        executor = RunExecutor(self.store, self.cache, factory(self.gateway), limiter, self.clock)
        return Worker(cfg, self.store, self.cache, executor, self.clock)

    def runs(self) -> list[Any]:
        return self.store.load_runtime().channels["news"].runs


async def test_activation_has_no_backfill_and_no_scan_t12(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    h.worker.initialize()
    await h.worker.tick()
    assert h.gateway.history_calls == 0 and h.gateway.resolve_calls == 0
    assert h.runs() == []
    assert h.store.load_heartbeat() is not None


async def test_no_source_reads_between_boundaries_t11(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    h.worker.initialize()
    for _ in range(20):
        h.clock.advance(15 * 60)  # 12:00 → 17:00
        await h.worker.tick()
    assert h.gateway.history_calls == 0


async def test_scan_on_boundary_t13(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    h.worker.initialize()
    h.clock.now = kyiv(
        2026,
        11,
        10,
        21,
        0,
    ) + timedelta(seconds=2)
    await h.worker.tick()
    runs = h.runs()
    assert len(runs) == 1
    assert runs[0].status == "scanned"
    assert runs[0].window_start_utc == kyiv(2026, 11, 10, 8)
    assert runs[0].window_end_utc == kyiv(2026, 11, 10, 21)
    assert runs[0].coverage["alpha"].status == "complete"
    calls = h.gateway.history_calls
    await h.worker.tick()
    assert h.gateway.history_calls == calls  # той самий слот удруге не сканується


async def test_late_restart_keeps_original_window_t14(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 7))
    h.worker.initialize()
    h.clock.now = kyiv(2026, 11, 10, 8, 12)
    h.worker = h.build(config())
    h.worker.initialize()
    await h.worker.tick()
    assert [r.window_end_utc for r in h.runs()] == [kyiv(2026, 11, 10, 8)]


async def test_old_misfires_skipped_t08(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 7))
    h.worker.initialize()
    h.clock.now = kyiv(2026, 11, 11, 9)  # пропущено 08:00 і 21:00, 08:00 наступного дня в grace
    await h.worker.tick()
    runs = h.runs()
    assert [r.window_end_utc for r in runs] == [kyiv(2026, 11, 11, 8)]
    assert runs[0].window_start_utc == kyiv(2026, 11, 10, 21)  # вікно не розширене
    skipped = h.store.load_runtime().channels["news"].skipped_utc
    assert skipped == [kyiv(2026, 11, 10, 8), kyiv(2026, 11, 10, 21)]


async def test_interrupted_run_resumed_within_grace(tmp_path: Path) -> None:
    """Аварія посеред run: після restart у межах grace run продовжується, не дублюється."""
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    h.worker.initialize()
    h.clock.now = kyiv(2026, 11, 10, 21, 1)
    h.gateway.crash_on_account = True
    with pytest.raises(SimulatedCrash):
        await h.worker.tick()
    assert h.runs()[0].status == "scanning"
    h.gateway.crash_on_account = False
    h.clock.advance(60)
    h.worker = h.build(config())
    h.worker.initialize()
    await h.worker.tick()
    assert [r.status for r in h.runs()] == ["scanned"]


async def test_interrupted_run_beyond_grace_failed(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    h.worker.initialize()
    h.clock.now = kyiv(2026, 11, 10, 21, 1)
    h.gateway.crash_on_account = True
    with pytest.raises(SimulatedCrash):
        await h.worker.tick()
    h.gateway.crash_on_account = False
    h.clock.now = kyiv(2026, 11, 11, 3)
    h.worker = h.build(config())
    h.worker.initialize()
    await h.worker.tick()
    run = h.runs()[0]
    assert (run.status, run.error_code) == ("failed", "interrupted")


async def test_disable_enable_no_avalanche_t10(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    h.worker.initialize()
    h.worker = h.build(config(enabled=False))
    h.worker.initialize()
    h.clock.now = kyiv(2026, 11, 15, 12)
    h.worker = h.build(config(enabled=True))
    h.worker.initialize()
    await h.worker.tick()
    state = h.store.load_runtime().channels["news"]
    assert state.runs == [] and state.skipped_utc == []
    assert h.gateway.history_calls == 0


async def test_schedule_change_creates_transition_window_t06(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    h.worker.initialize()
    h.worker = h.build(config(times=("09:00", "23:00")))
    h.worker.initialize()
    for moment in (kyiv(2026, 11, 10, 21, 1), kyiv(2026, 11, 10, 23, 1)):
        h.clock.now = moment
        await h.worker.tick()
    windows = [(r.window_start_utc, r.window_end_utc) for r in h.runs()]
    assert windows == [
        (kyiv(2026, 11, 10, 8), kyiv(2026, 11, 10, 21)),
        (kyiv(2026, 11, 10, 21), kyiv(2026, 11, 10, 23)),
    ]


async def test_cache_expiry_marks_runs(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    h.worker.initialize()
    h.clock.now = kyiv(2026, 11, 10, 21, 1)
    await h.worker.tick()
    assert h.cache.entries()
    h.clock.now = kyiv(2026, 11, 11, 21, 30)
    await h.worker.tick()
    statuses = {r.window_end_utc: r.status for r in h.runs()}
    assert statuses[kyiv(2026, 11, 10, 21)] == "cache_expired"


async def test_next_wake_bounded(tmp_path: Path) -> None:
    h = Harness(
        tmp_path,
        kyiv(
            2026,
            11,
            10,
            20,
            59,
        ),
    )
    h.worker.initialize()
    assert 0.5 <= h.worker.next_wake(h.clock()) <= 60


async def test_unexpected_error_marks_run_failed_and_worker_continues(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    h.worker.initialize()
    h.clock.now = kyiv(2026, 11, 10, 21, 1)

    async def boom() -> None:
        raise ValueError("unexpected")

    h.gateway.account = boom  # type: ignore[method-assign,assignment]
    await h.worker.tick()
    run = h.runs()[0]
    assert (run.status, run.error_code) == ("failed", "internal_error")


def ai_answer_all(create_calls: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
    import json as _json
    from types import SimpleNamespace

    async def create(**kwargs: Any) -> Any:
        create_calls.append(kwargs)
        candidates = _json.loads(kwargs["input"])["candidates"]
        data = {
            "events": [
                {
                    "candidate_ids": [c["id"]],
                    "emoji": "📰",
                    "title": c["text"][:60],
                    "summary": "",
                    "is_update": False,
                }
                for i, c in enumerate(candidates)
            ],
            "excluded": [],
        }
        return SimpleNamespace(
            status="completed",
            model="gpt-6.1-terra",
            output=[
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="output_text", text=_json.dumps(data))],
                )
            ],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )

    return create


def with_builder(h: Harness, provider: bool) -> None:
    from newsdigest.ai.provider import OpenAIProvider
    from newsdigest.digest_runner import DigestBuilder

    calls: list[dict[str, Any]] = []
    h.ai_calls = calls  # type: ignore[attr-defined]
    factory_ai = (
        (lambda s: OpenAIProvider(ai_answer_all(calls), s)) if provider else (lambda s: None)
    )
    builder = DigestBuilder(h.store, h.cache, factory_ai, lambda: None, h.clock)
    limiter = RateLimiter(h.store, h.clock, h.clock.sleep, min_gap_seconds=0)
    executor = RunExecutor(h.store, h.cache, factory(h.gateway), limiter, h.clock)
    h.worker = Worker(config(), h.store, h.cache, executor, h.clock, builder=builder)


async def test_ai_builds_ready_plan(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    with_builder(h, provider=True)
    h.worker.initialize()
    h.clock.now = kyiv(2026, 11, 10, 21, 1)
    await h.worker.tick()
    run = h.runs()[0]
    assert run.status == "ready"
    assert run.events and run.events > 0 and run.parts and run.parts >= 1
    assert run.ai_tokens["requests"] == len(h.ai_calls)  # type: ignore[attr-defined]
    plan = json.loads(
        (h.cache.run(run.digest_key).directory / "plan.json").read_text(encoding="utf-8")
    )["data"]
    assert plan["parts"][0]["text"].startswith("10 листопада\n\n1. ")


async def test_without_ai_key_no_plan_d011(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    with_builder(h, provider=False)
    h.worker.initialize()
    h.clock.now = kyiv(2026, 11, 10, 21, 1)
    await h.worker.tick()
    run = h.runs()[0]
    assert (run.status, run.error_code) == ("scanned", "ai_not_configured")
    assert not (h.cache.run(run.digest_key).directory / "plan.json").exists()


def with_delivery(h: Harness) -> Any:
    from newsdigest.ai.provider import OpenAIProvider
    from newsdigest.delivery import Delivery
    from newsdigest.digest_runner import DigestBuilder
    from tests.unit.test_delivery import MemoryParts

    target = channel_peer(-1004402354838, "my_digest")
    h.gateway.peers["@my_digest"] = type(target)(target.peer_id, "Digest", "my_digest", True, True)
    h.gateway.clock = h.clock
    parts = MemoryParts()
    calls: list[dict[str, Any]] = []
    builder = DigestBuilder(
        h.store, h.cache, lambda s: OpenAIProvider(ai_answer_all(calls), s), lambda: None, h.clock
    )
    limiter = RateLimiter(h.store, h.clock, h.clock.sleep, min_gap_seconds=0)
    executor = RunExecutor(h.store, h.cache, factory(h.gateway), limiter, h.clock)
    delivery = Delivery(h.store, h.cache, factory(h.gateway), limiter, lambda: parts, h.clock)
    h.worker = Worker(
        config(), h.store, h.cache, executor, h.clock, builder=builder, delivery=delivery
    )
    return parts


async def test_ready_run_is_published_and_cache_cleaned(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    parts = with_delivery(h)
    h.worker.initialize()
    h.clock.now = kyiv(2026, 11, 10, 21, 1)
    await h.worker.tick()
    run = h.runs()[0]
    assert run.status == "published"
    assert len(parts.rows) == run.parts and len(h.gateway.sent) == run.parts
    assert not h.cache.run(run.digest_key).directory.exists()
    assert h.store.load_guards().unresolved() == []


async def test_newer_run_held_behind_uncertain_t71(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    with_delivery(h)
    h.worker.initialize()
    h.clock.now = kyiv(2026, 11, 10, 21, 1)
    h.gateway.send_errors.append(TimeoutError("lost"))
    await h.worker.tick()
    assert h.runs()[0].status == "uncertain"
    h.clock.now = kyiv(2026, 11, 11, 8, 1)
    await h.worker.tick()
    statuses = [r.status for r in h.runs()]
    assert statuses == ["uncertain", "held"]
    assert h.gateway.sent == []


async def test_manual_run_without_publications_starts_at_last_boundary_d029(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    with_delivery(h)
    h.worker.initialize()
    record = await h.worker.run_now(config().target_channels[0])
    assert record is not None and record.manual
    assert (record.window_start_utc, record.window_end_utc) == (
        kyiv(2026, 11, 10, 8),
        kyiv(2026, 11, 10, 12),
    )
    assert record.status == "published"


async def test_next_scheduled_window_starts_after_manual_run_d029(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    with_delivery(h)
    h.worker.initialize()
    await h.worker.run_now(config().target_channels[0])
    h.clock.now = kyiv(2026, 11, 10, 21, 1)
    await h.worker.tick()
    scheduled = [r for r in h.runs() if not r.manual]
    assert [(r.window_start_utc, r.window_end_utc) for r in scheduled] == [
        (kyiv(2026, 11, 10, 12), kyiv(2026, 11, 10, 21))
    ]


async def test_manual_run_uses_db_last_published_end(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    with_delivery(h)
    h.worker._published_end = lambda _cid: kyiv(2026, 11, 10, 10, 30)
    h.worker.initialize()
    record = await h.worker.run_now(config().target_channels[0])
    assert record is not None
    assert record.window_start_utc == kyiv(2026, 11, 10, 10, 30)


async def test_manual_run_nothing_new(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    with_delivery(h)
    h.worker._published_end = lambda _cid: kyiv(2026, 11, 10, 12)
    h.worker.initialize()
    assert await h.worker.run_now(config().target_channels[0]) is None
    assert h.gateway.history_calls == 0


async def test_failed_run_does_not_extend_next_window(tmp_path: Path) -> None:
    """Невдалий run не рахується «покритим», але й не тягне вікно назад (D-008/D-029)."""
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    with_builder(h, provider=False)
    h.worker.initialize()
    h.clock.now = kyiv(2026, 11, 10, 21, 1)
    await h.worker.tick()
    h.clock.now = kyiv(2026, 11, 11, 8, 1)
    await h.worker.tick()
    windows = [(r.window_start_utc, r.window_end_utc) for r in h.runs()]
    assert windows[-1] == (kyiv(2026, 11, 10, 21), kyiv(2026, 11, 11, 8))


async def test_manual_request_file_is_picked_by_worker(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    with_delivery(h)
    h.worker.initialize()
    h.store.request_manual_run("news", h.clock())
    await h.worker.tick()
    assert [r.manual for r in h.runs()] == [True]
    assert h.store.take_manual_requests() == []
