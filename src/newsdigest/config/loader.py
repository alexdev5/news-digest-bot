"""Безпечне читання JSON-конфігу: заборона дублікатів ключів, ліміт розміру, валідація."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from newsdigest.config.models import AppConfig

MAX_CONFIG_BYTES = 1_000_000
MAX_PROMPT_BYTES = 100_000


class ConfigError(Exception):
    """Конфігурація не прочитана або не валідна; `problems` — зрозумілі повідомлення."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _reject_constant(name: str) -> None:
    raise ValueError(f"недопустиме JSON-значення {name}")


def parse_config_text(text: str) -> AppConfig:
    """Розбирає JSON-текст у `AppConfig` без звернень до файлів чи мережі."""
    try:
        raw = json.loads(
            text, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant
        )
    except _DuplicateKeyError as exc:
        raise ConfigError([f"повторний ключ у JSON: {exc.args[0]!r}"]) from exc
    except ValueError as exc:
        raise ConfigError([f"некоректний JSON: {exc}"]) from exc
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError([_format_error(err) for err in exc.errors()]) from exc


def _format_error(error: Any) -> str:
    location = ".".join(str(part) for part in error.get("loc", ())) or "<корінь>"
    message = str(error.get("msg", "помилка"))
    message = message.removeprefix("Value error, ")
    return f"{location}: {message}"


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    config: AppConfig
    path: Path
    prompts: dict[str, str]
    """Промпти цільових каналів за id; відсутній промпт — порожній рядок."""


def load_config(path: Path) -> LoadedConfig:
    """Читає конфіг і файли промптів (відносно теки конфігу). Без мережі."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ConfigError([f"не вдалося прочитати {path}: {exc.strerror}"]) from exc
    if size > MAX_CONFIG_BYTES:
        raise ConfigError([f"конфіг завеликий: {size} байт > {MAX_CONFIG_BYTES}"])
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError([f"не вдалося прочитати {path}: {exc}"]) from exc

    config = parse_config_text(text)
    base_dir = path.resolve().parent
    prompts: dict[str, str] = {}
    problems: list[str] = []
    for channel in config.target_channels:
        if channel.ai.prompt is not None:
            prompts[channel.id] = channel.ai.prompt
        elif channel.ai.prompt_file is not None:
            prompt_path = (base_dir / channel.ai.prompt_file).resolve()
            if not prompt_path.is_relative_to(base_dir):
                problems.append(f"{channel.id}: prompt_file має бути всередині теки конфігу")
                continue
            try:
                data = prompt_path.read_bytes()
            except OSError as exc:
                problems.append(f"{channel.id}: не вдалося прочитати prompt_file: {exc.strerror}")
                continue
            if len(data) > MAX_PROMPT_BYTES:
                problems.append(f"{channel.id}: prompt_file завеликий")
                continue
            try:
                prompts[channel.id] = data.decode("utf-8-sig").strip()
            except UnicodeDecodeError:
                problems.append(f"{channel.id}: prompt_file має бути в UTF-8")
        else:
            prompts[channel.id] = ""
    if problems:
        raise ConfigError(problems)
    return LoadedConfig(config=config, path=path, prompts=prompts)
