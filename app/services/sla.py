# app/services/sla.py
"""
Business-hours SLA engine.

Round 2 added `sla_calendar` specifically so SLA is computed from working
hours, regional timezones and public holidays — not from raw elapsed time.
Subtracting two timestamps is the single most likely silent failure in this
build: it produces plausible numbers that are wrong, and the seeded
"VIP after-hours near breach" trap exists to expose exactly that.

Everything here is pure and takes its calendar as data, so it can be unit
tested without a database and reused by Operator 5.

-----------------------------------------------------------------------------
FACTS ABOUT THE REAL DATA (verified against Supabase, 3 Aug 2026)
-----------------------------------------------------------------------------
`issues."Created"` is a TEXT column carrying THREE different formats:
    2026-07-08 00:00:00     ISO, with or without time   (332 rows)
    03/07/2026              DD/MM/YYYY, day first       (53 rows)
    Jul 20 2026             abbreviated month name      (75 rows)
`Updated` and `Due date` are proper timestamptz and need no parsing.

Region does NOT come from the assignment group. `customfield_10101` maps to
three different regions for the same group (App Support spans KL-HQ, Remote
and Singapore), so that join is ambiguous. The reporter's location is exact:

    issues."Reporter"  ->  users_directory.display_name
                       ->  users_directory.location
                       ->  sla_calendar.region          (1:1, all 97 matched)

`display_name` is not unique in users_directory, so resolution must handle
more than one match rather than assuming one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

__all__ = [
    "BusinessCalendar",
    "SlaResult",
    "parse_created",
    "parse_business_hours",
    "parse_holidays",
    "build_calendar",
    "business_minutes_between",
    "add_business_minutes",
    "classify",
    "evaluate",
]

# Mon=0 .. Sun=6
_WEEKDAY = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

_ALWAYS_OPEN_MARKERS = ("24x7", "24/7", "follow-the-sun")


# =============================================================================
# PARSING
# =============================================================================


def parse_created(raw: str | datetime | None) -> Optional[datetime]:
    """
    Parse `issues."Created"`, which mixes three formats in one text column.

    Returns a naive datetime (wall-clock in the ticket's region). Date-only
    values carry no time component, so they resolve to 00:00 — see
    `business_minutes_between`, which clamps a start before opening time to
    the start of the business day rather than crediting overnight minutes.

    Raises ValueError on an unrecognised format rather than guessing: a wrong
    date silently corrupts every SLA number downstream.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None) if raw.tzinfo else raw

    s = str(raw).strip()
    if not s:
        return None

    # ISO first — it is both the most common and unambiguous.
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s[: len(fmt) + 4], fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass

    # DD/MM/YYYY — day first. Confirmed: the leading component reaches 26.
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?$", s)
    if m:
        d, mo, y, hh, mm, ss = m.groups()
        if int(d) > 31 or int(mo) > 12:
            raise ValueError(f"Unparseable Created value: {raw!r}")
        return datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0))

    # "Jul 20 2026" / "Jul 20 2026 14:30"
    for fmt in ("%b %d %Y %H:%M:%S", "%b %d %Y %H:%M", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue

    raise ValueError(f"Unparseable Created value: {raw!r}")


@dataclass(frozen=True)
class BusinessHours:
    """Parsed `sla_calendar.business_hours`."""

    always_open: bool
    start: Optional[time] = None
    end: Optional[time] = None
    weekdays: frozenset[int] = frozenset()

    @property
    def minutes_per_day(self) -> int:
        if self.always_open:
            return 24 * 60
        assert self.start and self.end
        return (self.end.hour * 60 + self.end.minute) - (
            self.start.hour * 60 + self.start.minute
        )


def parse_business_hours(raw: str) -> BusinessHours:
    """
    Parse the two shapes present in the data:

        "09:00-18:00 Mon-Fri"      windowed
        "09:00-17:30 Mon-Fri"      windowed (Penang closes 30 min earlier)
        "24x7 follow-the-sun"      always open
    """
    s = (raw or "").strip()
    low = s.lower()

    if any(marker in low for marker in _ALWAYS_OPEN_MARKERS):
        return BusinessHours(always_open=True, weekdays=frozenset(range(7)))

    m = re.search(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", s)
    if not m:
        raise ValueError(f"Unparseable business_hours: {raw!r}")
    sh, sm, eh, em = (int(g) for g in m.groups())
    start, end = time(sh, sm), time(eh, em)
    if end <= start:
        raise ValueError(f"business_hours end not after start: {raw!r}")

    days = _parse_weekdays(s)
    return BusinessHours(always_open=False, start=start, end=end, weekdays=days)


def _parse_weekdays(s: str) -> frozenset[int]:
    low = s.lower()
    rng = re.search(r"(mon|tue|wed|thu|fri|sat|sun)\s*-\s*(mon|tue|wed|thu|fri|sat|sun)", low)
    if rng:
        a, b = _WEEKDAY[rng.group(1)], _WEEKDAY[rng.group(2)]
        return frozenset(range(a, b + 1)) if a <= b else frozenset(
            list(range(a, 7)) + list(range(0, b + 1))
        )
    named = {_WEEKDAY[d] for d in _WEEKDAY if d in low}
    return frozenset(named) if named else frozenset(range(0, 5))  # default Mon-Fri


def parse_holidays(raw: str | None) -> frozenset[date]:
    """Parse `sla_calendar.holiday_dates`: 'YYYY-MM-DD;YYYY-MM-DD;...'."""
    if not raw:
        return frozenset()
    out = set()
    for part in re.split(r"[;,]", str(raw)):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(datetime.strptime(part, "%Y-%m-%d").date())
        except ValueError as exc:
            raise ValueError(f"Unparseable holiday date {part!r}") from exc
    return frozenset(out)


@dataclass(frozen=True)
class BusinessCalendar:
    region: str
    timezone: str
    hours: BusinessHours
    holidays: frozenset[date]

    def tz(self):
        if ZoneInfo is None:  # pragma: no cover
            raise RuntimeError("zoneinfo unavailable; install tzdata")
        return ZoneInfo(self.timezone)

    def is_working_day(self, d: date) -> bool:
        if d in self.holidays:
            return False
        return d.weekday() in self.hours.weekdays


def build_calendar(row: dict) -> BusinessCalendar:
    """Build a calendar from one `sla_calendar` row."""
    return BusinessCalendar(
        region=row["region"],
        timezone=row["timezone"],
        hours=parse_business_hours(row["business_hours"]),
        holidays=parse_holidays(row.get("holiday_dates")),
    )


# =============================================================================
# BUSINESS-TIME ARITHMETIC
# =============================================================================


def _day_window(cal: BusinessCalendar, d: date) -> Optional[tuple[datetime, datetime]]:
    """The open interval on date `d`, or None when closed."""
    if not cal.is_working_day(d):
        return None
    if cal.hours.always_open:
        return datetime.combine(d, time.min), datetime.combine(d, time.min) + timedelta(days=1)
    return datetime.combine(d, cal.hours.start), datetime.combine(d, cal.hours.end)


def business_minutes_between(
    start: datetime, end: datetime, cal: BusinessCalendar, *, max_days: int = 3650
) -> int:
    """
    Whole business minutes between two naive wall-clock datetimes.

    Closed days contribute nothing. A `start` before opening is clamped to the
    open time — which is what makes date-only `Created` values behave sanely
    instead of crediting a full night of elapsed time.
    """
    if end <= start:
        return 0

    total = 0
    d = start.date()
    guard = 0
    while d <= end.date():
        guard += 1
        if guard > max_days:  # pragma: no cover
            raise RuntimeError("business_minutes_between exceeded max_days")
        window = _day_window(cal, d)
        if window:
            open_at, close_at = window
            lo = max(start, open_at)
            hi = min(end, close_at)
            if hi > lo:
                total += int((hi - lo).total_seconds() // 60)
        d += timedelta(days=1)
    return total


def add_business_minutes(
    start: datetime, minutes: int, cal: BusinessCalendar, *, max_days: int = 3650
) -> datetime:
    """
    The wall-clock instant reached after `minutes` of business time.

    Used for `breach_at`: when this ticket will breach if nobody touches it.
    """
    if minutes <= 0:
        return start

    remaining = minutes
    d = start.date()
    cursor = start
    guard = 0
    while remaining > 0:
        guard += 1
        if guard > max_days:  # pragma: no cover
            raise RuntimeError("add_business_minutes exceeded max_days")
        window = _day_window(cal, d)
        if window:
            open_at, close_at = window
            lo = max(cursor, open_at)
            if close_at > lo:
                available = int((close_at - lo).total_seconds() // 60)
                if available >= remaining:
                    return lo + timedelta(minutes=remaining)
                remaining -= available
        d += timedelta(days=1)
        cursor = datetime.combine(d, time.min)
    return cursor  # pragma: no cover


# =============================================================================
# CLASSIFICATION
# =============================================================================

BREACHED = "Breached"
AT_RISK = "At risk"
WITHIN_SLA = "Within SLA"


def classify(elapsed_minutes: int, target_minutes: int, at_risk_window_minutes: int) -> str:
    """Breached / At risk / Within SLA, matching the values used in the data."""
    if elapsed_minutes >= target_minutes:
        return BREACHED
    if (target_minutes - elapsed_minutes) <= at_risk_window_minutes:
        return AT_RISK
    return WITHIN_SLA


@dataclass(frozen=True)
class SlaResult:
    region: str
    timezone: str
    business_minutes_elapsed: int
    target_minutes: int
    minutes_to_breach: int
    breach_at: datetime
    state: str


def evaluate(
    created: str | datetime,
    now: datetime,
    cal: BusinessCalendar,
    target_minutes: int,
    at_risk_window_minutes: int,
) -> SlaResult:
    """Full evaluation for one ticket. `created` and `now` are region wall-clock."""
    start = parse_created(created)
    if start is None:
        raise ValueError("created is required")

    elapsed = business_minutes_between(start, now, cal)
    state = classify(elapsed, target_minutes, at_risk_window_minutes)
    return SlaResult(
        region=cal.region,
        timezone=cal.timezone,
        business_minutes_elapsed=elapsed,
        target_minutes=target_minutes,
        minutes_to_breach=target_minutes - elapsed,
        breach_at=add_business_minutes(start, target_minutes, cal),
        state=state,
    )
