"""Атомарний запис JSON-файлів стану з версією та контрольною сумою.

temporary file → flush/fsync → atomic replace → fsync каталогу (де підтримується).
Пошкоджений або чужий файл не «скидається» мовчки: читання кидає `StateCorruptError`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

ENVELOPE_VERSION = 1


class StateCorruptError(RuntimeError):
    """Файл стану пошкоджений: потрібне ручне відновлення, а не автоматичний reset."""


def _canonical(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def fsync_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
    fsync_directory(path.parent)


def write_json_atomic(path: Path, data: Any) -> None:
    body = _canonical(data)
    envelope = {
        "version": ENVELOPE_VERSION,
        "checksum": hashlib.sha256(body).hexdigest(),
        "data": data,
    }
    write_bytes_atomic(path, json.dumps(envelope, ensure_ascii=False, indent=2).encode("utf-8"))


def read_json_checked(path: Path) -> Any | None:
    """Дані з конверта або None, якщо файлу ще немає."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateCorruptError(f"{path}: некоректний JSON") from exc
    if not isinstance(envelope, dict) or set(envelope) != {"version", "checksum", "data"}:
        raise StateCorruptError(f"{path}: невідомий формат файлу стану")
    if envelope["version"] != ENVELOPE_VERSION:
        raise StateCorruptError(f"{path}: непідтримувана версія {envelope['version']!r}")
    if hashlib.sha256(_canonical(envelope["data"])).hexdigest() != envelope["checksum"]:
        raise StateCorruptError(f"{path}: контрольна сума не збігається")
    return envelope["data"]
