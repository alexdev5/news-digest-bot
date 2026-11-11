"""Спільні для команд CLI коди виходу й опції."""

from __future__ import annotations

from enum import IntEnum
from pathlib import Path
from typing import Annotated

import typer


class ExitCode(IntEnum):
    OK = 0
    CONFIG_ERROR = 2
    USAGE_ERROR = 3
    DATABASE_ERROR = 4
    TELEGRAM_ERROR = 5
    LOCK_BUSY = 6
    STATE_ERROR = 7
    UNHEALTHY = 8
    AI_ERROR = 9


ConfigOption = Annotated[
    Path,
    typer.Option("--config", "-c", help="Шлях до JSON-конфігу.", dir_okay=False),
]

DEFAULT_CONFIG = Path("config/config.json")
