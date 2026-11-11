from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from newsdigest.cli import ExitCode, app

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
