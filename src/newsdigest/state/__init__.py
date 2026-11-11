"""Технічний стан `/state`: атомарні JSON-файли без новинних текстів і process lock."""

from newsdigest.state.atomic import StateCorruptError
from newsdigest.state.lock import LockBusyError, ProcessLock
from newsdigest.state.store import (
    ChannelState,
    Heartbeat,
    PinnedPeer,
    RateLimitState,
    RunRecord,
    RuntimeState,
    SourceCoverage,
    StateStore,
)

__all__ = [
    "ChannelState",
    "Heartbeat",
    "LockBusyError",
    "PinnedPeer",
    "ProcessLock",
    "RateLimitState",
    "RunRecord",
    "RuntimeState",
    "SourceCoverage",
    "StateCorruptError",
    "StateStore",
]
