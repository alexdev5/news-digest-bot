from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from newsdigest.cli import app
from newsdigest.cli_common import ExitCode

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "config.example.json"
runner = CliRunner()


def test_validate_example() -> None:
    result = runner.invoke(app, ["validate-config", "--config", str(EXAMPLE)])
    assert result.exit_code == ExitCode.OK, result.output
    assert "dnipro_news" in result.output


def test_validate_reports_problems(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"schema_version": 4, "target_channels": []}), encoding="utf-8")
    result = runner.invoke(app, ["validate-config", "--config", str(path)])
    assert result.exit_code == ExitCode.CONFIG_ERROR
    assert "target_channels" in result.output


def test_schedule_one_channel() -> None:
    result = runner.invoke(
        app, ["schedule", "--config", str(EXAMPLE), "--channel", "dnipro_news", "--next", "2"]
    )
    assert result.exit_code == ExitCode.OK, result.output
    assert result.output.count("→") == 2


def test_schedule_requires_selection() -> None:
    result = runner.invoke(app, ["schedule", "--config", str(EXAMPLE)])
    assert result.exit_code == ExitCode.USAGE_ERROR


def test_schedule_unknown_channel() -> None:
    result = runner.invoke(app, ["schedule", "--config", str(EXAMPLE), "--channel", "nope"])
    assert result.exit_code == ExitCode.USAGE_ERROR


def test_healthcheck_without_heartbeat(tmp_path: Path) -> None:
    result = runner.invoke(app, ["healthcheck"], env={"NEWSDIGEST_STATE_DIR": str(tmp_path)})
    assert result.exit_code == ExitCode.UNHEALTHY


def test_status_without_state(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["status", "--config", str(EXAMPLE)], env={"NEWSDIGEST_STATE_DIR": str(tmp_path)}
    )
    assert result.exit_code == ExitCode.OK, result.output
    assert "не виконано login" in result.output


def test_scan_requires_channel(tmp_path: Path) -> None:
    result = runner.invoke(app, ["scan", "--config", str(EXAMPLE)])
    assert result.exit_code == ExitCode.USAGE_ERROR


def test_telegram_commands_need_credentials(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["dialogs"],
        env={"NEWSDIGEST_STATE_DIR": str(tmp_path), "TELEGRAM_API_ID": "", "TELEGRAM_API_HASH": ""},
    )
    assert result.exit_code == ExitCode.TELEGRAM_ERROR


def test_healthcheck_reports_delivery_blocked_by_missing_rights(tmp_path: Path) -> None:
    import os
    from datetime import UTC, datetime

    from newsdigest.state.store import (
        ChannelState,
        Heartbeat,
        RevisionState,
        RunRecord,
        RuntimeState,
        StateStore,
    )

    store = StateStore(tmp_path)
    now = datetime.now(UTC)
    store.save_heartbeat(Heartbeat(at_utc=now, pid=os.getpid(), phase="idle"))
    run = RunRecord(
        digest_key="d" * 64,
        boundary_utc=now,
        window_start_utc=now,
        window_end_utc=now,
        status="partial",
        created_at_utc=now,
        updated_at_utc=now,
        error_code="no_post_rights",
    )
    from datetime import time
    from zoneinfo import ZoneInfo

    from newsdigest.scheduling import DailySlots, Revision

    slots = DailySlots(timezone=ZoneInfo("Europe/Kyiv"), times=(time(8),))
    revision = RevisionState.from_revision(Revision(slots))
    store.save_runtime(
        RuntimeState(
            channels={
                "news": ChannelState(
                    activated_at_utc=now,
                    scan_start_at_utc=now,
                    revisions=[revision],
                    last_materialized_utc=now,
                    runs=[run],
                )
            }
        )
    )
    env = {"NEWSDIGEST_STATE_DIR": str(tmp_path)}
    result = runner.invoke(app, ["healthcheck"], env=env)
    assert result.exit_code == ExitCode.UNHEALTHY
    assert "no_post_rights" in result.output
    status = runner.invoke(app, ["status", "--config", str(EXAMPLE)], env=env)
    assert "ПОТРІБНА ДІЯ ВЛАСНИКА" in status.output
