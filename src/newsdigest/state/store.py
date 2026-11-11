"""Моделі й сховище технічного стану `/state` (v4 §18.3).

Тільки безконтентні дані: розклад, ревізії, межі, статуси й лічильники runs,
закріплені peer ID, rate limits, heartbeat. Новинних текстів тут немає й бути не
може: моделі мають `extra="forbid"`, а поля — лише ідентифікатори, час і числа.
"""

from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from newsdigest.scheduling import DailySlots, FixedInterval, Revision, Trigger
from newsdigest.state.atomic import StateCorruptError, read_json_checked, write_json_atomic

STATE_SCHEMA_VERSION = 1
MAX_RUN_HISTORY = 60
MAX_SKIPPED_HISTORY = 60
MAX_REVISIONS = 3

RunStatus = Literal["materialized", "scanning", "scanned", "failed", "cache_expired"]
CoverageStatus = Literal["pending", "complete", "partial", "unavailable", "excluded"]


class StateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class TriggerSpec(StateModel):
    """Серіалізований тригер розкладу."""

    kind: Literal["daily", "interval"]
    timezone: str | None = None
    times: tuple[str, ...] = ()
    every_seconds: int | None = None
    anchor_utc: AwareDatetime | None = None

    @classmethod
    def from_trigger(cls, trigger: Trigger) -> TriggerSpec:
        if isinstance(trigger, DailySlots):
            return cls(
                kind="daily",
                timezone=trigger.timezone.key,
                times=tuple(t.strftime("%H:%M") for t in trigger.times),
            )
        return cls(
            kind="interval", every_seconds=trigger.every_seconds, anchor_utc=trigger.anchor_utc
        )

    def to_trigger(self) -> Trigger:
        if self.kind == "daily":
            assert self.timezone is not None
            return DailySlots(
                timezone=ZoneInfo(self.timezone),
                times=tuple(time.fromisoformat(t) for t in self.times),
            )
        assert self.every_seconds is not None and self.anchor_utc is not None
        return FixedInterval(every_seconds=self.every_seconds, anchor_utc=self.anchor_utc)


class RevisionState(StateModel):
    trigger: TriggerSpec
    effective_from_utc: AwareDatetime | None = None

    def to_revision(self) -> Revision:
        return Revision(self.trigger.to_trigger(), self.effective_from_utc)

    @classmethod
    def from_revision(cls, revision: Revision) -> RevisionState:
        return cls(
            trigger=TriggerSpec.from_trigger(revision.trigger),
            effective_from_utc=revision.effective_from,
        )


class SourceCoverage(StateModel):
    status: CoverageStatus
    fetched: int = 0
    in_window: int = 0
    error_code: str | None = None


class RunRecord(StateModel):
    digest_key: str
    boundary_utc: AwareDatetime
    window_start_utc: AwareDatetime
    window_end_utc: AwareDatetime
    status: RunStatus
    created_at_utc: AwareDatetime
    updated_at_utc: AwareDatetime
    manual: bool = False
    error_code: str | None = None
    coverage: dict[str, SourceCoverage] = Field(default_factory=dict)
    excluded: dict[str, int] = Field(default_factory=dict)


class ChannelState(StateModel):
    activated_at_utc: AwareDatetime
    scan_start_at_utc: AwareDatetime
    revisions: list[RevisionState]
    last_materialized_utc: AwareDatetime
    paused: bool = False
    runs: list[RunRecord] = Field(default_factory=list)
    skipped_utc: list[AwareDatetime] = Field(default_factory=list)

    def revision_tuple(self) -> tuple[Revision, ...]:
        return tuple(r.to_revision() for r in self.revisions)

    def set_revisions(self, revisions: tuple[Revision, ...]) -> None:
        self.revisions = [RevisionState.from_revision(r) for r in revisions[-MAX_REVISIONS:]]
        if self.revisions[0].effective_from_utc is not None:
            # Найстаріша збережена ревізія стає безстроковою лише тоді, коли попередні
            # межі вже матеріалізовані; MAX_REVISIONS достатньо для рідкісних змін.
            self.revisions[0] = RevisionState(trigger=self.revisions[0].trigger)

    def upsert_run(self, record: RunRecord) -> None:
        self.runs = [r for r in self.runs if r.digest_key != record.digest_key]
        self.runs.append(record)
        self.runs = self.runs[-MAX_RUN_HISTORY:]

    def find_run(self, digest_key: str) -> RunRecord | None:
        return next((r for r in self.runs if r.digest_key == digest_key), None)

    def add_skipped(self, boundary: datetime) -> None:
        self.skipped_utc = [*self.skipped_utc, boundary][-MAX_SKIPPED_HISTORY:]


class PinnedPeer(StateModel):
    """Закріплений Telegram peer для ref із конфігу (зміна username не перемикає peer)."""

    peer_id: int
    title: str
    username: str | None = None
    pinned_at_utc: AwareDatetime


class RuntimeState(StateModel):
    schema_version: Literal[1] = 1
    account_id: int | None = None
    channels: dict[str, ChannelState] = Field(default_factory=dict)
    peers: dict[str, PinnedPeer] = Field(default_factory=dict)


class RateLimitState(StateModel):
    schema_version: Literal[1] = 1
    pause_until_utc: AwareDatetime | None = None
    reason: str | None = None


class Heartbeat(StateModel):
    at_utc: AwareDatetime
    pid: int
    phase: str


class StateStore:
    """Читання/запис файлів `/state`. Писач один — процес із ProcessLock."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @property
    def lock_path(self) -> Path:
        return self.directory / "process.lock"

    def _load_optional[M: StateModel](self, name: str, model: type[M]) -> M | None:
        path = self.directory / name
        data = read_json_checked(path)
        if data is None:
            return None
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise StateCorruptError(f"{path}: не відповідає схемі стану") from exc

    def _load[M: StateModel](self, name: str, model: type[M], default: M) -> M:
        loaded = self._load_optional(name, model)
        return default if loaded is None else loaded

    def _save(self, name: str, value: StateModel) -> None:
        write_json_atomic(self.directory / name, value.model_dump(mode="json"))

    def load_runtime(self) -> RuntimeState:
        return self._load("runtime.json", RuntimeState, RuntimeState())

    def save_runtime(self, state: RuntimeState) -> None:
        self._save("runtime.json", state)

    def load_rate_limits(self) -> RateLimitState:
        return self._load("rate_limits.json", RateLimitState, RateLimitState())

    def save_rate_limits(self, state: RateLimitState) -> None:
        self._save("rate_limits.json", state)

    def load_heartbeat(self) -> Heartbeat | None:
        return self._load_optional("heartbeat.json", Heartbeat)

    def save_heartbeat(self, heartbeat: Heartbeat) -> None:
        self._save("heartbeat.json", heartbeat)
