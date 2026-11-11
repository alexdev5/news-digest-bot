from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from newsdigest.scheduling import (
    DailySlots,
    FixedInterval,
    ScheduleError,
    Window,
    due_boundaries,
    first_boundary_on_activation,
    is_boundary,
    local_slot_to_utc,
    next_boundary,
    plan_transition,
    previous_boundary,
    upcoming_boundaries,
    window_for_boundary,
)

KYIV = ZoneInfo("Europe/Kyiv")
EARLY = datetime(2020, 1, 1, tzinfo=UTC)


def kyiv(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=KYIV).astimezone(UTC)


def slots(*hhmm: str, tz: ZoneInfo = KYIV) -> DailySlots:
    return DailySlots(timezone=tz, times=tuple(time.fromisoformat(t) for t in hhmm))


UKRAINE = slots("08:00", "21:00")
CRYPTO = slots("09:00", "15:00", "23:00")


class TestWindowBounds:
    """T01: межі [start, end) для 08:00/21:00."""

    def test_morning_and_evening_windows(self) -> None:
        morning = window_for_boundary(UKRAINE, kyiv(2026, 11, 10, 8, 0), EARLY)
        evening = window_for_boundary(UKRAINE, kyiv(2026, 11, 10, 21, 0), EARLY)
        assert morning == Window(kyiv(2026, 11, 9, 21, 0), kyiv(2026, 11, 10, 8, 0))
        assert evening == Window(kyiv(2026, 11, 10, 8, 0), kyiv(2026, 11, 10, 21, 0))

    @pytest.mark.parametrize(
        ("moment", "in_morning", "in_evening"),
        [
            ((7, 59, 59), True, False),
            ((8, 0, 0), False, True),
            ((20, 59, 59), False, True),
            ((21, 0, 0), False, False),
        ],
    )
    def test_edges(self, moment: tuple[int, int, int], in_morning: bool, in_evening: bool) -> None:
        morning = window_for_boundary(UKRAINE, kyiv(2026, 11, 10, 8, 0), EARLY)
        evening = window_for_boundary(UKRAINE, kyiv(2026, 11, 10, 21, 0), EARLY)
        assert morning is not None and evening is not None
        ts = kyiv(2026, 11, 10, *moment)
        assert morning.contains(ts) is in_morning
        assert evening.contains(ts) is in_evening

    def test_naive_moment_rejected(self) -> None:
        window = Window(kyiv(2026, 11, 10, 8, 0), kyiv(2026, 11, 10, 21, 0))
        with pytest.raises(ScheduleError):
            window.contains(datetime(2026, 11, 10, 12, 0))


class TestIndependentSchedules:
    """T02: різні розклади цільових каналів дають різні вікна."""

    def test_crypto_three_slots(self) -> None:
        w = window_for_boundary(CRYPTO, kyiv(2026, 11, 10, 9, 0), EARLY)
        assert w == Window(kyiv(2026, 11, 9, 23, 0), kyiv(2026, 11, 10, 9, 0))
        assert next_boundary(CRYPTO, kyiv(2026, 11, 10, 9, 0)) == kyiv(2026, 11, 10, 15, 0)

    def test_single_daily_slot(self) -> None:
        once = slots("20:00")
        w = window_for_boundary(once, kyiv(2026, 11, 10, 20, 0), EARLY)
        assert w == Window(kyiv(2026, 11, 9, 20, 0), kyiv(2026, 11, 10, 20, 0))

    def test_slot_order_irrelevant(self) -> None:
        assert slots("21:00", "08:00") == UKRAINE

    def test_duplicate_slots_rejected(self) -> None:
        with pytest.raises(ScheduleError):
            slots("08:00", "08:00")


class TestFixedInterval:
    """T03: fixed_interval від anchor, незалежно від restart."""

    trigger = FixedInterval(every_seconds=21_600, anchor_utc=kyiv(2026, 11, 10, 0, 0))

    def test_grid_both_directions(self) -> None:
        assert next_boundary(self.trigger, kyiv(2026, 11, 10, 1, 0)) == kyiv(2026, 11, 10, 6, 0)
        assert next_boundary(self.trigger, kyiv(2026, 11, 9, 23, 0)) == kyiv(2026, 11, 10, 0, 0)
        assert previous_boundary(self.trigger, kyiv(2026, 11, 10, 6, 0)) == kyiv(2026, 11, 10, 0, 0)
        assert previous_boundary(self.trigger, kyiv(2026, 11, 10, 0, 0)) == kyiv(2026, 11, 9, 18, 0)

    def test_exact_boundary_is_not_next(self) -> None:
        b = kyiv(2026, 11, 10, 12, 0)
        assert next_boundary(self.trigger, b) == b + timedelta(hours=6)
        assert is_boundary(self.trigger, b)
        assert not is_boundary(self.trigger, b + timedelta(seconds=1))

    def test_too_short_interval(self) -> None:
        with pytest.raises(ScheduleError):
            FixedInterval(every_seconds=59, anchor_utc=EARLY)


class TestDst:
    """T04: неоднозначний/відсутній час, колізії перенесених слотів."""

    def test_nonexistent_shifts_forward(self) -> None:
        # 2026-03-29: у Києві 03:00 → 04:00.
        result = local_slot_to_utc(datetime(2026, 3, 29).date(), time(3, 30), KYIV)
        assert result.astimezone(KYIV).replace(tzinfo=None) == datetime(2026, 3, 29, 4, 0)

    def test_ambiguous_uses_first_occurrence(self) -> None:
        # 2026-10-25: 04:00 → 03:00, 03:30 буває двічі; перша поява — UTC+3.
        result = local_slot_to_utc(datetime(2026, 10, 25).date(), time(3, 30), KYIV)
        assert result == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)

    def test_collapsed_slots_produce_one_boundary(self) -> None:
        trigger = slots("03:15", "03:45", "04:00")
        day_start = kyiv(2026, 3, 29, 0, 0)
        boundaries = upcoming_boundaries(trigger, day_start, 2)
        assert boundaries[0] == kyiv(2026, 3, 29, 4, 0)
        assert boundaries[1] == kyiv(2026, 3, 30, 3, 15)

    def test_short_night_window_on_spring_day(self) -> None:
        w = window_for_boundary(UKRAINE, kyiv(2026, 3, 29, 8, 0), EARLY)
        assert w is not None
        assert w.end_utc - w.start_utc == timedelta(hours=10)


class TestTimezonesAndCalendar:
    """T05: різні timezone, переходи місяця/року."""

    def test_utc_profile_across_new_year(self) -> None:
        utc_slots = slots("00:00", tz=ZoneInfo("UTC"))
        b = datetime(2027, 1, 1, tzinfo=UTC)
        w = window_for_boundary(utc_slots, b, EARLY)
        assert w == Window(datetime(2026, 12, 31, tzinfo=UTC), b)

    def test_kyiv_across_month(self) -> None:
        assert next_boundary(UKRAINE, kyiv(2026, 11, 30, 21, 0)) == kyiv(2026, 12, 1, 8, 0)


class TestScanStartAt:
    """M26 / D-013: вікно обрізається знизу, ранніші межі не створюють випусків."""

    scan_start = kyiv(2026, 11, 12, 0, 0)

    def test_first_window_clipped(self) -> None:
        w = window_for_boundary(UKRAINE, kyiv(2026, 11, 12, 8, 0), self.scan_start)
        assert w == Window(self.scan_start, kyiv(2026, 11, 12, 8, 0))

    def test_boundary_before_start_has_no_window(self) -> None:
        assert window_for_boundary(UKRAINE, kyiv(2026, 11, 11, 21, 0), self.scan_start) is None

    def test_later_windows_unchanged(self) -> None:
        w = window_for_boundary(UKRAINE, kyiv(2026, 11, 12, 21, 0), self.scan_start)
        assert w == Window(kyiv(2026, 11, 12, 8, 0), kyiv(2026, 11, 12, 21, 0))

    def test_non_boundary_rejected(self) -> None:
        with pytest.raises(ScheduleError):
            window_for_boundary(UKRAINE, kyiv(2026, 11, 12, 9, 0), self.scan_start)

    def test_first_activation_no_backfill(self) -> None:
        activated = kyiv(2026, 11, 13, 12, 0)
        assert first_boundary_on_activation(UKRAINE, activated, self.scan_start) == kyiv(
            2026, 11, 13, 21, 0
        )

    def test_activation_before_scan_start(self) -> None:
        activated = kyiv(2026, 11, 10, 12, 0)
        assert first_boundary_on_activation(UKRAINE, activated, self.scan_start) == kyiv(
            2026, 11, 12, 8, 0
        )


class TestMisfires:
    """T08, T14: catch-up від матеріалізованої межі, grace без розширення вікна."""

    def test_restart_within_grace(self) -> None:
        plan = due_boundaries(
            UKRAINE, kyiv(2026, 11, 9, 21, 0), kyiv(2026, 11, 10, 8, 12), grace_seconds=10_800
        )
        assert plan.run == (kyiv(2026, 11, 10, 8, 0),)
        assert plan.skipped == ()

    def test_old_misfires_skipped_windows_not_extended(self) -> None:
        plan = due_boundaries(
            UKRAINE, kyiv(2026, 11, 9, 21, 0), kyiv(2026, 11, 11, 9, 0), grace_seconds=10_800
        )
        assert plan.skipped == (
            kyiv(2026, 11, 10, 8, 0),
            kyiv(2026, 11, 10, 21, 0),
        )
        assert plan.run == (kyiv(2026, 11, 11, 8, 0),)
        w = window_for_boundary(UKRAINE, plan.run[0], EARLY)
        assert w == Window(kyiv(2026, 11, 10, 21, 0), kyiv(2026, 11, 11, 8, 0))

    def test_nothing_due(self) -> None:
        plan = due_boundaries(UKRAINE, kyiv(2026, 11, 10, 8, 0), kyiv(2026, 11, 10, 9, 0), 10_800)
        assert plan.run == () and plan.skipped == ()


class TestTransition:
    """T06: перехід 08:00/21:00 → 09:00/23:00 під час денного вікна."""

    def test_transition_window(self) -> None:
        new = slots("09:00", "23:00")
        t = plan_transition(UKRAINE, new, kyiv(2026, 11, 10, 12, 0))
        assert t.cutover_utc == kyiv(2026, 11, 10, 21, 0)
        assert t.transition_window == Window(kyiv(2026, 11, 10, 21, 0), kyiv(2026, 11, 10, 23, 0))
        assert next_boundary(new, t.transition_window.end_utc) == kyiv(2026, 11, 11, 9, 0)

    def test_change_on_exact_boundary(self) -> None:
        new = slots("09:00", "23:00")
        t = plan_transition(UKRAINE, new, kyiv(2026, 11, 10, 8, 0))
        assert t.cutover_utc == kyiv(2026, 11, 10, 21, 0)
