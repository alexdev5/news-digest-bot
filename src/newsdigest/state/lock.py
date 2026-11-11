"""Ексклюзивний process lock: один власник Telegram-сесії та писач стану.

Worker тримає lock увесь час роботи; login/dialogs/doctor/scan беруть той самий lock,
тож не можуть відкрити сесію паралельно з worker (v4 §5).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import TracebackType
from typing import IO, Self


class LockBusyError(RuntimeError):
    """Lock уже тримає інший процес (найчастіше — запущений worker)."""


class ProcessLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: IO[bytes] | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")  # noqa: SIM115 - тримаємо відкритим до release()
        try:
            _lock(handle)
        except OSError as exc:
            handle.close()
            raise LockBusyError(
                f"{self.path} зайнятий іншим процесом (зупиніть worker або дочекайтесь завершення)"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode("ascii"))
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            _unlock(self._handle)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


if sys.platform != "win32":
    import fcntl

    def _lock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

else:  # pragma: no cover - локальна розробка на Windows
    import msvcrt

    def _lock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
