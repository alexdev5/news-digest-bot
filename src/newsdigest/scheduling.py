"""Чиста логіка розкладів і фіксованих вікон `[start, end)` (v4 §7, D-013).

Модуль не читає годинник і не робить I/O: поточний час передається явно.
Усі моменти — timezone-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_MAX_DST_SHIFT_MINUTES = 24 * 60
_MAX_SEARCH_DAYS = 3


class ScheduleError(ValueError):
    """Некоректні вхідні дані для розрахунку розкладу."""


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ScheduleError(f"{name} має містити часову зону")


def to_utc(value: datetime) -> datetime:
    _require_aware(value, "момент часу")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class DailySlots:
    """Щоденні локальні слоти; DST: ambiguous=first_occurrence, nonexistent=shift_forward."""

    timezone: ZoneInfo
    times: tuple[time, ...]

    def __post_init__(self) -> None:
        if not self.times:
            raise ScheduleError("daily_slots потребує хоча б одного часу")
        if len(set(self.times)) != len(self.times):
            raise ScheduleError("daily_slots містить повтори")
        object.__setattr__(self, "times", tuple(sorted(self.times)))


@dataclass(frozen=True, slots=True)
class FixedInterval:
    """Межі `anchor + k * every_seconds` у реальних секундах UTC."""

    every_seconds: int
    anchor_utc: datetime

    def __post_init__(self) -> None:
        if self.every_seconds < 60:
            raise ScheduleError("every_seconds має бути не менше 60")
        object.__setattr__(self, "anchor_utc", to_utc(self.anchor_utc))


type Trigger = DailySlots | FixedInterval


@dataclass(frozen=True, slots=True)
class Window:
    """Інформаційне вікно випуску: `start <= t < end`."""

    start_utc: datetime
    end_utc: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_utc", to_utc(self.start_utc))
        object.__setattr__(self, "end_utc", to_utc(self.end_utc))
        if self.start_utc >= self.end_utc:
            raise ScheduleError("початок вікна має бути раніше кінця")

    def contains(self, moment: datetime) -> bool:
        return self.start_utc <= to_utc(moment) < self.end_utc


def local_slot_to_utc(day: date, slot: time, tz: ZoneInfo) -> datetime:
    """Переводить локальний слот у UTC з політиками DST v4 §7.3.

    Неоднозначний час → перша поява (`fold=0`). Неіснуючий час → перша валідна
    локальна хвилина після пропуску.
    """
    naive = datetime.combine(day, slot)
    for minutes in range(_MAX_DST_SHIFT_MINUTES + 1):
        probe = naive + timedelta(minutes=minutes)
        candidate = probe.replace(tzinfo=tz, fold=0).astimezone(UTC)
        if candidate.astimezone(tz).replace(tzinfo=None) == probe:
            return candidate
    raise ScheduleError(f"не вдалося знайти валідний локальний час для {naive} у {tz}")


def _daily_boundaries_for(trigger: DailySlots, day: date) -> list[datetime]:
    # Після shift_forward різні слоти можуть збігтися: лишаємо одну межу.
    return sorted({local_slot_to_utc(day, slot, trigger.timezone) for slot in trigger.times})


def next_boundary(trigger: Trigger, after: datetime) -> datetime:
    """Перша планова межа строго після `after`."""
    after_utc = to_utc(after)
    if isinstance(trigger, FixedInterval):
        step = timedelta(seconds=trigger.every_seconds)
        k = (after_utc - trigger.anchor_utc) // step + 1
        return trigger.anchor_utc + k * step
    start_day = after_utc.astimezone(trigger.timezone).date() - timedelta(days=1)
    for offset in range(_MAX_SEARCH_DAYS + 1):
        for boundary in _daily_boundaries_for(trigger, start_day + timedelta(days=offset)):
            if boundary > after_utc:
                return boundary
    raise ScheduleError("не знайдено наступної межі")  # pragma: no cover


def previous_boundary(trigger: Trigger, before: datetime) -> datetime:
    """Остання планова межа строго до `before`."""
    before_utc = to_utc(before)
    if isinstance(trigger, FixedInterval):
        step = timedelta(seconds=trigger.every_seconds)
        k = -((trigger.anchor_utc - before_utc) // step) - 1
        return trigger.anchor_utc + k * step
    start_day = before_utc.astimezone(trigger.timezone).date() + timedelta(days=1)
    for offset in range(_MAX_SEARCH_DAYS + 1):
        for boundary in reversed(
            _daily_boundaries_for(trigger, start_day - timedelta(days=offset))
        ):
            if boundary < before_utc:
                return boundary
    raise ScheduleError("не знайдено попередньої межі")  # pragma: no cover


def is_boundary(trigger: Trigger, moment: datetime) -> bool:
    moment_utc = to_utc(moment)
    return next_boundary(trigger, moment_utc - timedelta(microseconds=1)) == moment_utc


def upcoming_boundaries(trigger: Trigger, after: datetime, count: int) -> list[datetime]:
    result: list[datetime] = []
    cursor = to_utc(after)
    for _ in range(count):
        cursor = next_boundary(trigger, cursor)
        result.append(cursor)
    return result


def window_for_boundary(
    trigger: Trigger, boundary: datetime, scan_start_at: datetime
) -> Window | None:
    """Вікно випуску з межею `boundary`, обрізане знизу `scan_start_at` (D-013).

    Повертає `None`, якщо межа не пізніша за `scan_start_at`: такий випуск не створюється.
    """
    boundary_utc = to_utc(boundary)
    scan_start_utc = to_utc(scan_start_at)
    if not is_boundary(trigger, boundary_utc):
        raise ScheduleError(f"{boundary_utc.isoformat()} не є плановою межею")
    if boundary_utc <= scan_start_utc:
        return None
    start = max(previous_boundary(trigger, boundary_utc), scan_start_utc)
    return Window(start, boundary_utc)


def first_boundary_on_activation(
    trigger: Trigger, activated_at: datetime, scan_start_at: datetime
) -> datetime:
    """Найближча майбутня межа при першій активації; без backfill минулих вікон."""
    candidate = next_boundary(trigger, activated_at)
    scan_start_utc = to_utc(scan_start_at)
    while candidate <= scan_start_utc:
        candidate = next_boundary(trigger, candidate)
    return candidate


@dataclass(frozen=True, slots=True)
class DuePlan:
    """Належні після restart межі: які ще виконати, а які пропустити як застарілі."""

    run: tuple[datetime, ...]
    skipped: tuple[datetime, ...]


def due_boundaries(
    trigger: Trigger,
    last_materialized: datetime,
    now: datetime,
    grace_seconds: int,
) -> DuePlan:
    """Межі в `(last_materialized, now]`; старші за grace пропускаються (v4 §15).

    Розрахунок іде від останньої матеріалізованої межі, а не від останньої публікації.
    """
    if grace_seconds < 0:
        raise ScheduleError("grace_seconds не може бути від'ємним")
    now_utc = to_utc(now)
    grace = timedelta(seconds=grace_seconds)
    run: list[datetime] = []
    skipped: list[datetime] = []
    cursor = to_utc(last_materialized)
    while True:
        boundary = next_boundary(trigger, cursor)
        if boundary > now_utc:
            break
        (run if now_utc - boundary <= grace else skipped).append(boundary)
        cursor = boundary
    return DuePlan(tuple(run), tuple(skipped))


@dataclass(frozen=True, slots=True)
class Transition:
    """Перехід розкладу: старе вікно закінчується на C, далі перехідне `[C, N)`."""

    cutover_utc: datetime
    transition_window: Window


def plan_transition(old: Trigger, new: Trigger, now: datetime) -> Transition:
    """v4 §7.6: C — кінець поточного відкритого вікна за старим розкладом,
    N — перша межа нового розкладу строго після C."""
    cutover = next_boundary(old, now)
    return Transition(cutover, Window(cutover, next_boundary(new, cutover)))
