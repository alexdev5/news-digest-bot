from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from newsdigest.scheduling import (
    DailySlots,
    Revision,
    ScheduleError,
    Window,
    apply_schedule_change,
    due_boundaries_rev,
    next_boundary_rev,
    window_for_boundary_rev,
)

KYIV = ZoneInfo("Europe/Kyiv")
EARLY = datetime(2020, 1, 1, tzinfo=UTC)


def kyiv(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=KYIV).astimezone(UTC)


def slots(*hhmm: str) -> DailySlots:
    return DailySlots(timezone=KYIV, times=tuple(time.fromisoformat(t) for t in hhmm))


OLD = slots("08:00", "21:00")
NEW = slots("09:00", "23:00")


def test_single_revision_matches_plain_logic() -> None:
    revs = (Revision(OLD),)
    assert next_boundary_rev(revs, kyiv(2026, 11, 10, 12)) == kyiv(2026, 11, 10, 21)
    assert window_for_boundary_rev(revs, kyiv(2026, 11, 10, 21), EARLY) == Window(
        kyiv(2026, 11, 10, 8), kyiv(2026, 11, 10, 21)
    )


def test_transition_window_t06() -> None:
    """Зміна вдень: старе вікно до 21:00, перехідне 21:00–23:00, далі 23:00–09:00."""
    revs = apply_schedule_change((Revision(OLD),), NEW, kyiv(2026, 11, 10, 12))
    assert revs[1].effective_from == kyiv(2026, 11, 10, 21)
    boundaries = []
    cursor = kyiv(2026, 11, 10, 12)
    for _ in range(3):
        cursor = next_boundary_rev(revs, cursor)
        boundaries.append(cursor)
    assert boundaries == [kyiv(2026, 11, 10, 21), kyiv(2026, 11, 10, 23), kyiv(2026, 11, 11, 9)]
    windows = [window_for_boundary_rev(revs, b, EARLY) for b in boundaries]
    assert windows == [
        Window(kyiv(2026, 11, 10, 8), kyiv(2026, 11, 10, 21)),
        Window(kyiv(2026, 11, 10, 21), kyiv(2026, 11, 10, 23)),
        Window(kyiv(2026, 11, 10, 23), kyiv(2026, 11, 11, 9)),
    ]


def test_second_change_before_cutover_replaces_pending_t09() -> None:
    first = apply_schedule_change((Revision(OLD),), NEW, kyiv(2026, 11, 10, 12))
    other = slots("10:00")
    second = apply_schedule_change(first, other, kyiv(2026, 11, 10, 13))
    assert len(second) == 2
    assert second[1].trigger == other
    assert second[1].effective_from == kyiv(2026, 11, 10, 21)


def test_reverting_before_cutover_drops_pending() -> None:
    first = apply_schedule_change((Revision(OLD),), NEW, kyiv(2026, 11, 10, 12))
    assert apply_schedule_change(first, OLD, kyiv(2026, 11, 10, 13)) == (Revision(OLD),)


def test_unchanged_schedule_keeps_revisions() -> None:
    revs = (Revision(OLD),)
    assert apply_schedule_change(revs, slots("21:00", "08:00"), kyiv(2026, 11, 10, 12)) == revs


def test_due_across_cutover() -> None:
    revs = apply_schedule_change((Revision(OLD),), NEW, kyiv(2026, 11, 10, 12))
    plan = due_boundaries_rev(revs, kyiv(2026, 11, 10, 12), kyiv(2026, 11, 10, 23, 5), 10_800)
    assert plan.run == (kyiv(2026, 11, 10, 21), kyiv(2026, 11, 10, 23))


def test_invalid_revision_order() -> None:
    with pytest.raises(ScheduleError):
        next_boundary_rev((Revision(OLD, kyiv(2026, 1, 1)),), kyiv(2026, 1, 2))
