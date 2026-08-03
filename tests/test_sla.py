# tests/test_sla.py
"""
Tests for the business-hours SLA engine.

Calendars mirror the real `sla_calendar` rows so a passing suite means the
engine handles the actual data, not a simplified version of it:

    KL-HQ      09:00-18:00 Mon-Fri  Asia/Kuala_Lumpur  2026-08-31;2026-09-16;2026-12-25
    Penang     09:00-17:30 Mon-Fri  Asia/Kuala_Lumpur  2026-08-31;2026-07-07;2026-12-25
    Remote     24x7 follow-the-sun  UTC                2026-12-25;2026-01-01
"""

from datetime import date, datetime, time

import pytest

from app.services.sla import (
    AT_RISK,
    BREACHED,
    WITHIN_SLA,
    add_business_minutes,
    build_calendar,
    business_minutes_between,
    classify,
    evaluate,
    parse_business_hours,
    parse_created,
    parse_holidays,
)

KL = build_calendar(
    {
        "region": "KL-HQ",
        "business_hours": "09:00-18:00 Mon-Fri",
        "timezone": "Asia/Kuala_Lumpur",
        "holiday_dates": "2026-08-31;2026-09-16;2026-12-25",
    }
)
PENANG = build_calendar(
    {
        "region": "Penang",
        "business_hours": "09:00-17:30 Mon-Fri",
        "timezone": "Asia/Kuala_Lumpur",
        "holiday_dates": "2026-08-31;2026-07-07;2026-12-25",
    }
)
REMOTE = build_calendar(
    {
        "region": "Remote",
        "business_hours": "24x7 follow-the-sun",
        "timezone": "UTC",
        "holiday_dates": "2026-12-25;2026-01-01",
    }
)


# =============================================================================
# PARSING — three real formats in one text column
# =============================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-07-08 00:00:00", datetime(2026, 7, 8, 0, 0)),
        ("2026-07-08", datetime(2026, 7, 8, 0, 0)),
        ("2026-07-08T14:30:00", datetime(2026, 7, 8, 14, 30)),
        ("03/07/2026", datetime(2026, 7, 3, 0, 0)),  # DD/MM — 3 July, not 7 March
        ("26/07/2026", datetime(2026, 7, 26, 0, 0)),  # day 26 proves day-first
        ("Jul 20 2026", datetime(2026, 7, 20, 0, 0)),
        ("Jul 20 2026 14:30", datetime(2026, 7, 20, 14, 30)),
    ],
)
def test_parse_created_handles_all_three_formats(raw, expected):
    assert parse_created(raw) == expected


def test_parse_created_is_day_first_not_month_first():
    """03/07/2026 must be 3 July. Month-first would silently shift 4 months."""
    assert parse_created("03/07/2026").month == 7


def test_parse_created_rejects_garbage_rather_than_guessing():
    with pytest.raises(ValueError):
        parse_created("not a date")


def test_parse_created_empty_and_none():
    assert parse_created(None) is None
    assert parse_created("  ") is None


def test_parse_business_hours_windowed():
    bh = parse_business_hours("09:00-18:00 Mon-Fri")
    assert bh.always_open is False
    assert bh.start == time(9, 0) and bh.end == time(18, 0)
    assert bh.weekdays == frozenset({0, 1, 2, 3, 4})
    assert bh.minutes_per_day == 540


def test_parse_business_hours_penang_closes_earlier():
    assert parse_business_hours("09:00-17:30 Mon-Fri").minutes_per_day == 510


def test_parse_business_hours_always_open():
    bh = parse_business_hours("24x7 follow-the-sun")
    assert bh.always_open is True
    assert bh.minutes_per_day == 1440
    assert bh.weekdays == frozenset(range(7))


def test_parse_holidays():
    assert parse_holidays("2026-08-31;2026-09-16") == frozenset(
        {date(2026, 8, 31), date(2026, 9, 16)}
    )
    assert parse_holidays(None) == frozenset()
    assert parse_holidays("") == frozenset()


# =============================================================================
# BUSINESS-MINUTE ARITHMETIC
# =============================================================================


def test_within_a_single_working_day():
    # Wed 5 Aug 2026, 10:00 -> 12:00
    assert business_minutes_between(
        datetime(2026, 8, 5, 10, 0), datetime(2026, 8, 5, 12, 0), KL
    ) == 120


def test_after_hours_contributes_nothing():
    """18:00 Wed -> 08:00 Thu is 14 clock hours but zero business minutes."""
    assert business_minutes_between(
        datetime(2026, 8, 5, 18, 0), datetime(2026, 8, 6, 8, 0), KL
    ) == 0


def test_overnight_spans_only_count_open_time():
    # Wed 17:00 -> Thu 10:00 = 60 min Wed + 60 min Thu
    assert business_minutes_between(
        datetime(2026, 8, 5, 17, 0), datetime(2026, 8, 6, 10, 0), KL
    ) == 120


def test_weekend_is_skipped():
    """Fri 17:00 -> Mon 10:00 crosses 65 clock hours but only 2 business hours."""
    assert business_minutes_between(
        datetime(2026, 8, 7, 17, 0), datetime(2026, 8, 10, 10, 0), KL
    ) == 120


def test_holiday_is_skipped():
    """31 Aug 2026 is a KL-HQ holiday (Monday). Fri 17:00 -> Tue 10:00."""
    assert business_minutes_between(
        datetime(2026, 8, 28, 17, 0), datetime(2026, 9, 1, 10, 0), KL
    ) == 120


def test_same_holiday_is_not_a_holiday_elsewhere():
    """7 July is a Penang holiday but a normal working day in KL-HQ."""
    start, end = datetime(2026, 7, 7, 9, 0), datetime(2026, 7, 7, 17, 0)
    assert business_minutes_between(start, end, PENANG) == 0
    assert business_minutes_between(start, end, KL) == 480


def test_24x7_counts_everything_including_weekends():
    """Remote is follow-the-sun: Fri 17:00 -> Mon 10:00 is all countable."""
    assert business_minutes_between(
        datetime(2026, 8, 7, 17, 0), datetime(2026, 8, 10, 10, 0), REMOTE
    ) == 65 * 60


def test_24x7_still_honours_its_own_holidays():
    assert business_minutes_between(
        datetime(2026, 12, 25, 0, 0), datetime(2026, 12, 26, 0, 0), REMOTE
    ) == 0


def test_date_only_created_clamps_to_opening_time():
    """
    A date-only Created (00:00) must not credit the hours before opening.
    Midnight -> 12:00 on a KL working day is 3 business hours, not 12.
    """
    assert business_minutes_between(
        datetime(2026, 8, 5, 0, 0), datetime(2026, 8, 5, 12, 0), KL
    ) == 180


def test_end_before_start_is_zero():
    assert business_minutes_between(
        datetime(2026, 8, 5, 12, 0), datetime(2026, 8, 5, 10, 0), KL
    ) == 0


def test_full_working_day():
    assert business_minutes_between(
        datetime(2026, 8, 5, 9, 0), datetime(2026, 8, 5, 18, 0), KL
    ) == 540


# =============================================================================
# add_business_minutes / breach_at
# =============================================================================


def test_add_minutes_within_one_day():
    assert add_business_minutes(datetime(2026, 8, 5, 10, 0), 120, KL) == datetime(
        2026, 8, 5, 12, 0
    )


def test_add_minutes_rolls_to_next_working_day():
    """Wed 17:00 + 120 business min = 60 min Wed + 60 min Thu -> Thu 10:00."""
    assert add_business_minutes(datetime(2026, 8, 5, 17, 0), 120, KL) == datetime(
        2026, 8, 6, 10, 0
    )


def test_add_minutes_skips_weekend():
    """Fri 17:00 + 120 = 60 Fri + 60 Mon -> Mon 10:00."""
    assert add_business_minutes(datetime(2026, 8, 7, 17, 0), 120, KL) == datetime(
        2026, 8, 10, 10, 0
    )


def test_add_minutes_round_trips_with_elapsed():
    start = datetime(2026, 8, 5, 14, 0)
    for minutes in (30, 240, 540, 1200):
        landed = add_business_minutes(start, minutes, KL)
        assert business_minutes_between(start, landed, KL) == minutes


# =============================================================================
# CLASSIFICATION
# =============================================================================


def test_classify_boundaries():
    assert classify(240, 240, 120) == BREACHED  # exactly at target
    assert classify(241, 240, 120) == BREACHED
    assert classify(120, 240, 120) == AT_RISK  # exactly at the risk window
    assert classify(119, 240, 120) == WITHIN_SLA
    assert classify(0, 240, 120) == WITHIN_SLA


# =============================================================================
# END TO END — the seeded VIP after-hours trap
# =============================================================================


def test_vip_after_hours_is_not_breached_by_wall_clock():
    """
    The trap: a VIP ticket raised Friday 17:30 with a 4h target, checked
    Monday 09:30. Raw elapsed is ~64 hours and looks catastrophically
    breached. In business time only 30 min of Friday plus 30 min of Monday
    have passed, so it is Within SLA with 3 hours left.
    """
    result = evaluate(
        created="2026-08-07 17:30:00",  # Friday
        now=datetime(2026, 8, 10, 9, 30),  # Monday
        cal=KL,
        target_minutes=240,
        at_risk_window_minutes=120,
    )
    assert result.business_minutes_elapsed == 60
    assert result.state == WITHIN_SLA
    assert result.minutes_to_breach == 180


def test_same_ticket_in_remote_region_does_breach():
    """Identical ticket under 24x7 cover has burned its target many times over."""
    result = evaluate(
        created="2026-08-07 17:30:00",
        now=datetime(2026, 8, 10, 9, 30),
        cal=REMOTE,
        target_minutes=240,
        at_risk_window_minutes=120,
    )
    assert result.business_minutes_elapsed == 3840
    assert result.state == BREACHED


def test_evaluate_reports_breach_instant():
    """Raised Wed 16:00 with a 4h target; breach lands Thu 11:00, not Wed 20:00."""
    result = evaluate(
        created="2026-08-05 16:00:00",
        now=datetime(2026, 8, 5, 17, 0),
        cal=KL,
        target_minutes=240,
        at_risk_window_minutes=120,
    )
    # 16:00 + 4 business hours = 2h Wed (16:00-18:00), then 2h from Thu 09:00
    assert result.breach_at == datetime(2026, 8, 6, 11, 0)
    assert result.business_minutes_elapsed == 60
    assert result.minutes_to_breach == 180
    assert result.state == WITHIN_SLA  # 180 remaining is outside the 120 window


def test_evaluate_crosses_into_at_risk_overnight():
    """Same ticket next morning: 2h Wed + 1h Thu = 180 elapsed, 60 left."""
    result = evaluate(
        created="2026-08-05 16:00:00",
        now=datetime(2026, 8, 6, 10, 0),
        cal=KL,
        target_minutes=240,
        at_risk_window_minutes=120,
    )
    assert result.business_minutes_elapsed == 180
    assert result.minutes_to_breach == 60
    assert result.state == AT_RISK
    assert result.breach_at == datetime(2026, 8, 6, 11, 0)


def test_evaluate_accepts_each_created_format():
    for raw in ("2026-08-05 10:00:00", "05/08/2026", "Aug 05 2026"):
        result = evaluate(
            created=raw,
            now=datetime(2026, 8, 5, 17, 0),
            cal=KL,
            target_minutes=480,
            at_risk_window_minutes=60,
        )
        assert result.business_minutes_elapsed > 0
