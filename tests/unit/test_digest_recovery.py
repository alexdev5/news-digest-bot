"""Regressions reproduced during the November 11 review; no live services."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from telethon.errors import ChannelPrivateError, SessionRevokedError
from typer.testing import CliRunner

from newsdigest.cli import app
from newsdigest.config.models import Defaults, PublicationDefaults
from newsdigest.settings import Paths, TelegramSettings
from newsdigest.telegram.rate_limit import RateLimiter
from newsdigest.telegram.telethon_gateway import _read
from newsdigest.telegram.types import AccountUnavailable, SendRejected
from tests.fakes import factory, messages_every
from tests.unit.test_delivery import META, TARGET, Env, plan
from tests.unit.test_scanner import START, A, B, gateway, target
from tests.unit.test_scanner import Env as ScanEnv
from tests.unit.test_worker import Harness, config, kyiv, with_delivery


@pytest.mark.parametrize("hour,minute", [(21, 0), (21, 31), (23, 59)])
def test_first_manual_window_is_last_completed_schedule_interval(
    tmp_path: Path, hour: int, minute: int
) -> None:
    cfg = config(times=("08:00", "15:00", "21:00"))
    h = Harness(tmp_path, kyiv(2026, 11, 11, hour, minute), cfg)
    h.worker.initialize()
    window = h.worker.manual_window(cfg.channel("news"), h.clock(), None)
    assert window is not None
    assert (window.start_utc, window.end_utc) == (kyiv(2026, 11, 11, 15), kyiv(2026, 11, 11, 21))


async def test_db_receipt_recovered_on_idle_tick_without_resend(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    parts = with_delivery(h)
    parts.fail_inserts = 1
    h.worker.initialize()
    h.clock.now = kyiv(2026, 11, 10, 21, 1)
    await h.worker.tick()
    assert h.runs()[0].status == "partial"
    calls, sent = h.gateway.history_calls, len(h.gateway.sent)
    h.clock.advance(30)
    await h.worker.tick()
    assert parts.rows == {}  # backoff
    h.clock.advance(30)
    await h.worker.tick()
    assert h.runs()[0].status == "published"
    assert len(parts.rows) == sent == len(h.gateway.sent)
    assert h.gateway.history_calls == calls


async def test_restart_recovers_ready_series_without_rescan(tmp_path: Path) -> None:
    from tests.unit.test_worker import with_builder

    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    with_builder(h, provider=True)
    h.worker.initialize()
    h.clock.now = kyiv(2026, 11, 10, 21, 1)
    await h.worker.tick()
    assert h.runs()[0].status == "ready"
    reads = h.gateway.history_calls
    parts = with_delivery(h)  # new Worker over the same persisted state/cache
    h.worker.initialize()
    await h.worker.tick()
    assert h.runs()[0].status == "published" and parts.rows
    assert h.gateway.history_calls == reads


async def test_rejected_second_part_blocks_newer_digest(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    parts = with_delivery(h)
    h.worker._config = config().model_copy(
        update={"defaults": Defaults(publication=PublicationDefaults(max_chars_per_message=500))}
    )
    send = h.gateway.send_message

    async def reject_after_first(*args: Any, **kwargs: Any) -> Any:
        if h.gateway.sent:
            raise SendRejected("send_rejected", "ChatWriteForbiddenError")
        return await send(*args, **kwargs)

    h.gateway.send_message = reject_after_first  # type: ignore[method-assign]
    h.worker.initialize()
    h.clock.now = kyiv(2026, 11, 10, 21, 1)
    await h.worker.tick()
    assert h.runs()[0].status == "partial" and len(parts.rows) == 1
    h.clock.now = kyiv(2026, 11, 11, 8, 1)
    await h.worker.tick()
    assert [r.status for r in h.runs()] == ["partial", "held"]
    assert len(h.gateway.sent) == 1
    h.gateway.send_message = send  # type: ignore[method-assign]
    h.clock.advance(900)
    await h.worker.tick()
    assert all(r.status == "published" for r in h.runs())
    assert h.store.load_guards().unresolved() == []


@pytest.mark.parametrize("command", ["preview", "scan"])
def test_inspection_does_not_overwrite_or_delete_delivery_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    parts = with_delivery(h)
    parts.fail_inserts = 1
    h.worker.initialize()
    h.clock.now = kyiv(2026, 11, 10, 21, 1)
    asyncio.run(h.worker.tick())
    key = h.runs()[0].digest_key
    plan_path = h.cache.run(key).directory / "plan.json"
    before = plan_path.read_bytes()
    cfg = tmp_path / "config.json"
    cfg.write_text(config().model_dump_json(), encoding="utf-8")
    paths = Paths(h.store.directory, h.cache.root, tmp_path / "session")
    monkeypatch.setattr(
        "newsdigest.cli_telegram.telegram_env", lambda: (paths, TelegramSettings(1, "fake"))
    )
    monkeypatch.setattr("newsdigest.cli_telegram.gateway_factory", lambda *_: factory(h.gateway))
    monkeypatch.setattr("newsdigest.cli_telegram.utcnow", h.clock)
    monkeypatch.setattr(
        "newsdigest.cli_telegram.RateLimiter",
        lambda s, c: RateLimiter(s, c, h.clock.sleep, min_gap_seconds=0),
    )
    monkeypatch.setattr("newsdigest.digest_runner.default_engine_factory", lambda: lambda: None)
    args = [
        command,
        "--config",
        str(cfg),
        "--channel",
        "news",
        "--window-end",
        "2026-11-10T21:00:00+02:00",
    ]
    if command == "preview":
        args.append("--no-ai")
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert plan_path.read_bytes() == before
    asyncio.run(h.worker.publish_pending(config().channel("news"), h.clock(), force=True))
    assert h.runs()[0].status == "published"


async def test_telethon_private_source_does_not_abort_other_sources(tmp_path: Path) -> None:
    fake = gateway([], messages_every(START, 2, 5))
    history = fake.history_page

    async def failing() -> Any:
        raise ChannelPrivateError(request=None)

    async def read(peer_id: int, **kwargs: Any) -> Any:
        return await _read(failing()) if peer_id == A else await history(peer_id, **kwargs)

    fake.history_page = read  # type: ignore[method-assign]
    env = ScanEnv(tmp_path, fake)
    summary, cache = await env.scan(target())
    assert summary.statuses == {"alpha": "unavailable", "beta": "complete"}
    assert summary.details["alpha"]["error_code"] == "source_unavailable"
    assert summary.details["beta"]["in_window"] == 2
    assert {m.peer_id for m in cache.read_messages()} == {B}


async def test_revoked_session_is_fatal() -> None:
    async def failed() -> Any:
        raise SessionRevokedError(request=None)

    with pytest.raises(AccountUnavailable):
        await _read(failed())


async def test_changed_destination_or_random_id_cannot_resume(tmp_path: Path) -> None:
    env = Env(tmp_path)
    p = plan()
    env.gateway.send_errors.append(TimeoutError())
    await env.publisher().publish(p, TARGET, META)
    with pytest.raises(RuntimeError):
        await env.publisher().publish(p, replace(TARGET, peer_id=TARGET.peer_id - 1), META)
    changed = replace(p, parts=(replace(p.parts[0], random_id=999), *p.parts[1:]))
    with pytest.raises(RuntimeError):
        await env.publisher().publish(changed, TARGET, META)


async def test_restart_after_cache_cleanup_does_not_hold_committed_series(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    with_delivery(h)
    h.worker.initialize()
    h.clock.now = kyiv(2026, 11, 10, 21, 1)
    await h.worker.tick()
    sent = len(h.gateway.sent)
    state = h.store.load_runtime()
    state.channels["news"].runs[0].status = "ready"  # crash before final state write
    h.store.save_runtime(state)
    await h.worker.tick()
    assert h.runs()[0].status == "published" and len(h.gateway.sent) == sent


async def test_orphan_guard_blocks_new_publication(tmp_path: Path) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    with_delivery(h)
    h.worker.initialize()
    h.clock.now = kyiv(2026, 11, 10, 21, 1)
    h.gateway.send_errors.append(TimeoutError())
    await h.worker.tick()
    state = h.store.load_runtime()
    state.channels["news"].runs = []  # legacy truncated history; guards survive
    h.store.save_runtime(state)
    h.clock.now = kyiv(2026, 11, 11, 8, 1)
    await h.worker.tick()
    assert h.runs()[0].status == "held"
    assert h.gateway.sent == []


async def test_heartbeat_keeps_advancing_during_long_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    h.worker.initialize()
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(*args: Any) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(h.worker, "_process_channel", slow)
    monkeypatch.setattr("newsdigest.worker.HEARTBEAT_SECONDS", 0.01)
    task = asyncio.create_task(h.worker.tick())
    await started.wait()
    h.clock.advance(180)
    await asyncio.sleep(0.03)
    heartbeat = h.store.load_heartbeat()
    assert heartbeat and heartbeat.at_utc == h.clock() and heartbeat.phase == "processing"
    release.set()
    await task
    assert h.store.load_heartbeat().phase == "idle"  # type: ignore[union-attr]
