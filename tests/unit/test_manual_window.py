from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from newsdigest.ai.grouping import PublishedItem
from newsdigest.ai.provider import OpenAIProvider
from newsdigest.cli import app
from newsdigest.scheduling import Window
from newsdigest.settings import Paths, TelegramSettings
from newsdigest.state import ProcessLock
from newsdigest.state.store import ManualRequest
from newsdigest.worker import Worker
from tests.fakes import SimulatedCrash
from tests.unit.test_delivery import MemoryParts
from tests.unit.test_worker import Harness, ai_answer_all, config, kyiv, with_delivery

WINDOW = Window(kyiv(2026, 11, 10, 10), kyiv(2026, 11, 10, 11))


def setup(h: Harness) -> tuple[MemoryParts, list[dict[str, Any]]]:
    parts: MemoryParts = with_delivery(h)
    calls: list[dict[str, Any]] = []
    builder = h.worker._builder
    assert builder is not None

    def memory(channel_id: str) -> tuple[set[tuple[object, ...]] | None, list[PublishedItem]]:
        keys: set[tuple[object, ...]] = {
            ("telegram", ref["peer_id"], ref["message_id"])
            for row in parts.rows.values()
            for ref in row.source_message_ids
        }
        return keys, [PublishedItem("Попередній випуск", "Стислий текст", h.clock())]

    builder._load_memory = memory  # type: ignore[method-assign]
    builder._provider_factory = lambda s: OpenAIProvider(ai_answer_all(calls), s)
    h.worker.initialize()
    return parts, calls


async def test_repeat_flag_includes_used_ids_and_disables_ai_memory(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    parts, calls = setup(h)
    target = config().channel("news")
    first = await h.worker.run_window(target, WINDOW)
    assert first.status == "published" and first.events == 4
    assert json.loads(calls[0]["input"])["published_recent"]
    sent = len(h.gateway.sent)

    second = await h.worker.run_window(target, WINDOW)
    assert second.status == "published" and second.events == 0
    assert len(h.gateway.sent) == sent and len(calls) == 1

    third = await h.worker.run_window(target, WINDOW, ignore_published=True)
    assert third.status == "published" and third.events == 4
    assert len(h.gateway.sent) == sent * 2
    assert json.loads(calls[-1]["input"])["published_recent"] == []
    assert len({r.digest_key for r in (first, second, third)}) == 3
    assert {r.digest_key for r in parts.rows.values()} == {first.digest_key, third.digest_key}
    assert all(not h.cache.run(r.digest_key).exists() for r in (first, second, third))


async def test_explicit_window_does_not_move_scheduled_coverage(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    setup(h)
    await h.worker.run_window(config().channel("news"), WINDOW)
    assert await h.worker.covered_until("news") is None
    h.clock.now = kyiv(2026, 11, 10, 21, 1)
    await h.worker.tick()
    scheduled = next(r for r in h.runs() if not r.manual)
    assert scheduled.window_start_utc == kyiv(2026, 11, 10, 8)
    assert scheduled.window_end_utc == kyiv(2026, 11, 10, 21)


async def test_history_unavailable_requires_explicit_override(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    with_delivery(h)  # no DB history backend
    h.worker.initialize()
    record = await h.worker.run_window(config().channel("news"), WINDOW)
    assert record.error_code == "published_history_unavailable" and not h.gateway.sent
    record = await h.worker.run_window(config().channel("news"), WINDOW, ignore_published=True)
    assert record.status == "published"


async def test_replayed_request_and_db_retry_never_resend(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    parts, calls = setup(h)
    parts.fail_inserts = 1
    request_id = "a" * 32
    first = await h.worker.run_window(
        config().channel("news"),
        WINDOW,
        ignore_published=True,
        request_id=request_id,
    )
    assert first.status == "partial"
    sent, reads, ai_calls = len(h.gateway.sent), h.gateway.history_calls, len(calls)
    h.clock.advance(60)
    second = await h.worker.run_window(
        config().channel("news"),
        WINDOW,
        ignore_published=True,
        request_id=request_id,
    )
    assert second.digest_key == first.digest_key and second.status == "published"
    assert (len(h.gateway.sent), h.gateway.history_calls, len(calls)) == (sent, reads, ai_calls)


async def test_historical_run_cannot_overtake_unresolved_send(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    setup(h)
    h.gateway.send_errors.append(TimeoutError())
    first = await h.worker.run_window(config().channel("news"), WINDOW)
    assert first.status == "uncertain"
    h.clock.advance(5)
    old = Window(kyiv(2026, 11, 9, 10), kyiv(2026, 11, 9, 11))
    second = await h.worker.run_window(config().channel("news"), old, ignore_published=True)
    assert second.status == "held" and h.gateway.sent == []


async def test_queued_request_survives_crash_and_resumes_same_run(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    setup(h)
    req = ManualRequest(
        channel_id="news",
        requested_at_utc=h.clock(),
        window_start_utc=WINDOW.start_utc,
        window_end_utc=WINDOW.end_utc,
        ignore_published=True,
    )
    path = h.store.queue_manual_request(req)
    h.gateway.crash_on_account = True
    with pytest.raises(SimulatedCrash):
        await h.worker.tick()
    assert path.exists() and h.runs()[0].status == "scanning"
    key = h.runs()[0].digest_key
    h.gateway.crash_on_account = False
    setup(h)  # new worker, same persisted request/cache/state
    await h.worker.tick()
    assert not path.exists()
    assert len(h.runs()) == 1 and h.runs()[0].digest_key == key
    assert h.runs()[0].status == "published" and h.runs()[0].ignore_published
    assert h.gateway.history_calls == 1


def cli_setup(h: Harness, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    cfg = h.tmp / "config.json"
    cfg.write_text(config().model_dump_json(), encoding="utf-8")
    paths = Paths(h.store.directory, h.cache.root, h.tmp / "session")
    monkeypatch.setattr("newsdigest.cli_telegram.utcnow", h.clock)
    monkeypatch.setattr(
        "newsdigest.cli_telegram.telegram_env", lambda: (paths, TelegramSettings(1, "fake"))
    )
    monkeypatch.setattr("newsdigest.cli_telegram.build_worker", lambda *_: h.worker)
    return ["run-window", "--config", str(cfg), "--channel", "news"]


def test_cli_runs_locally_with_exact_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    setup(h)
    args = [
        *cli_setup(h, monkeypatch),
        "--from",
        "2026-11-10T10:00:00+02:00",
        "--to",
        "2026-11-10T11:00:00+02:00",
        "--ignore-published",
    ]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert h.runs()[0].status == "published" and h.runs()[0].ignore_published
    assert h.runs()[0].window_start_utc == WINDOW.start_utc


def test_cli_queues_separate_requests_while_worker_holds_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    setup(h)
    args = [
        *cli_setup(h, monkeypatch),
        "--from",
        "2026-11-10T10:00:00+02:00",
        "--to",
        "2026-11-10T11:00:00+02:00",
        "--ignore-published",
    ]
    lock = ProcessLock(h.store.lock_path)
    lock.acquire()
    try:
        for _ in range(2):
            result = CliRunner().invoke(app, args)
            assert result.exit_code == 0 and "передано worker" in result.output
        requests = h.store.pending_manual_requests()
        assert len(requests) == 2 and all(r.ignore_published for _, r in requests)
        assert h.gateway.history_calls == 0
        asyncio.run(h.worker.tick())
        assert h.store.pending_manual_requests() == []
        assert len(h.runs()) == 2 and all(r.status == "published" for r in h.runs())
    finally:
        lock.release()


@pytest.mark.parametrize(
    "start,end",
    [
        ("bad", "2026-11-10T11:00:00+02:00"),
        ("2026-11-10T10:00:00", "2026-11-10T11:00:00+02:00"),
        ("2026-11-10T11:00:00+02:00", "2026-11-10T10:00:00+02:00"),
        ("2026-11-10T10:00:00+02:00", "2026-11-10T13:00:00+02:00"),
        ("2026-10-31T10:00:00+02:00", "2026-11-10T11:00:00+02:00"),
    ],
)
def test_invalid_window_rejected_before_queue_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start: str,
    end: str,
) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    setup(h)
    result = CliRunner().invoke(app, [*cli_setup(h, monkeypatch), "--from", start, "--to", end])
    assert result.exit_code == 3, result.output
    assert not h.store.pending_manual_requests() and h.gateway.history_calls == 0


def test_disabled_channel_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="вимкнений"):
        Worker.validate_manual_window(config(enabled=False).channel("news"), WINDOW, WINDOW.end_utc)
