from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from newsdigest.ai.grouping import group_with_ai, review
from newsdigest.ai.provider import AiError, OpenAIProvider
from newsdigest.ai.trace import AiTrace, failure_detail
from newsdigest.cli import app
from newsdigest.state.atomic import read_json_checked
from tests.unit.test_ai import CONTEXT, SETTINGS, FakeCreate, response
from tests.unit.test_editorial import answer, candidate, event
from tests.unit.test_manual_window import cli_setup, setup
from tests.unit.test_worker import Harness, kyiv


async def test_retry_receives_rejected_draft_and_exact_problem(tmp_path: Path) -> None:
    c = candidate("a", "У селищі відкрили 2 бібліотеки.")
    bad = event(["a"], "Відкрили 3 бібліотеки.")
    create = FakeCreate(answer(bad), answer(event(["a"], "Відкрили 2 бібліотеки.")))
    trace = AiTrace(tmp_path)
    result = await group_with_ai([c], OpenAIProvider(create, SETTINGS), CONTEXT, trace=trace.record)
    assert len(result.events) == 1
    correction = create.payload(1)["corrections"][0]
    assert correction["previous_event"]["title"] == bad["title"]
    assert any("числа ['3']" in p for p in correction["problems"])
    first = read_json_checked(tmp_path / "ai/0001.json")
    assert first is not None
    assert first["response"]["events"][0] == bad and first["problems"]
    second = read_json_checked(tmp_path / "ai/0002.json")
    assert second is not None
    assert second["covered"] == ["a"] and second["problems"] == []


def test_failed_manual_command_prints_reason_and_keeps_trace_only_in_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness(tmp_path, kyiv(2026, 11, 10, 12))
    setup(h)

    async def invalid(**kwargs: Any) -> Any:
        ids = [c["id"] for c in json.loads(kwargs["input"])["candidates"]]
        assert ids
        return response(answer(event(["unknown"], "Непридатний виклад")))

    assert h.worker._builder is not None
    h.worker._builder._provider_factory = lambda s: OpenAIProvider(invalid, s)
    result = CliRunner().invoke(
        app,
        [
            *cli_setup(h, monkeypatch),
            "--from",
            "2026-11-10T10:00:00+02:00",
            "--to",
            "2026-11-10T11:00:00+02:00",
            "--ignore-published",
        ],
    )
    assert result.exit_code != 0 and "Причина AI-збою" in result.output
    assert "невідомі candidate_id" in result.output
    run = h.runs()[0]
    directory = h.cache.run(run.digest_key).directory
    assert len(list((directory / "ai").glob("[0-9]*.json"))) == 3
    assert failure_detail(directory) and h.gateway.sent == []
    assert "Непридатний виклад" not in (h.store.directory / "runtime.json").read_text(
        encoding="utf-8"
    )
    h.cache.cleanup_expired(h.clock() + timedelta(days=2), set())
    assert not directory.exists()  # diagnostics share the run TTL


async def test_api_failure_is_traced_without_changing_error_code(tmp_path: Path) -> None:
    create = FakeCreate(RuntimeError("private transport data"))
    with pytest.raises(AiError) as info:
        await group_with_ai(
            [candidate("a", "Відкрили бібліотеку.")],
            OpenAIProvider(create, SETTINGS),
            CONTEXT,
            trace=AiTrace(tmp_path).record,
        )
    data = read_json_checked(tmp_path / "ai/0001.json")
    assert data is not None
    assert data["error_code"] == info.value.code
    assert "private transport data" not in str(data)


async def test_trace_write_failure_does_not_break_valid_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: Any) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr("newsdigest.ai.trace.write_json_atomic", fail)
    result = await group_with_ai(
        [candidate("a", "Відкрили бібліотеку.")],
        OpenAIProvider(FakeCreate(answer(event(["a"], "Бібліотеку відкрили."))), SETTINGS),
        CONTEXT,
        trace=AiTrace(tmp_path).record,
    )
    assert len(result.events) == 1


@pytest.mark.parametrize("timezone", ["Europe/Kyiv", "UTC"])
async def test_supplied_publication_time_is_not_an_invented_number(timezone: str) -> None:
    from zoneinfo import ZoneInfo

    c = candidate("a", "У селищі повідомили про відкриття нової бібліотеки для мешканців.")
    context = replace(CONTEXT, timezone=ZoneInfo(timezone))
    stamp = f"{c.earliest.astimezone(context.timezone):%H:%M}"
    data = event(["a"], f"О {stamp} повідомили про відкриття бібліотеки.")
    result = await group_with_ai([c], OpenAIProvider(FakeCreate(answer(data)), SETTINGS), context)
    assert stamp in result.events[0].title


def test_publication_time_does_not_allow_its_numbers_as_counts_or_other_times() -> None:
    c = candidate("a", "У селищі повідомили про відкриття нової бібліотеки для мешканців.")
    stamp = c.earliest.astimezone(CONTEXT.timezone)
    for title in (f"Відкрили {stamp.hour} бібліотек.", "О 23:59 відкрили бібліотеку."):
        checked = review(answer(event(["a"], title)), [c], timezone=CONTEXT.timezone)
        assert any("числа" in p for p in checked.problems)
