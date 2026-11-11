"""Temporary AI diagnostics; never written to logs, /state or the publication DB."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from newsdigest.state.atomic import StateCorruptError, read_json_checked, write_json_atomic

log = logging.getLogger("newsdigest.ai")


class AiTrace:
    def __init__(self, run_directory: Path) -> None:
        self.directory = run_directory / "ai"
        self.sequence = max(
            (int(p.stem) for p in self.directory.glob("*.json") if p.stem.isdigit()),
            default=0,
        )

    async def record(self, data: dict[str, Any]) -> None:
        self.sequence += 1
        await self._write(self.directory / f"{self.sequence:04d}.json", data)

    async def failure(self, code: str, detail: str) -> None:
        await self._write(self.directory / "failure.json", {"code": code, "detail": detail})

    @staticmethod
    async def _write(path: Path, data: dict[str, Any]) -> None:
        try:
            await asyncio.to_thread(write_json_atomic, path, data)
        except OSError:
            # A diagnostic failure must not change delivery or leak a response into logs.
            log.warning("ai_trace_write_failed")


def failure_detail(run_directory: Path) -> str | None:
    try:
        data = read_json_checked(run_directory / "ai" / "failure.json")
    except (OSError, StateCorruptError):
        return None
    return data["detail"] if data and isinstance(data.get("detail"), str) else None
