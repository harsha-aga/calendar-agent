"""The scheduling engine: works out when you're busy and when you're free.

Plain, deterministic code with no AI involved. The agent asks this module; it
never does date math itself.

What it handles:
- Recurring events: expands the series (RRULE/EXDATE) into real occurrences,
  in the event's own time zone, so daylight-saving changes land correctly.
- Exceptions: a moved occurrence replaces the original slot; a cancelled
  occurrence removes it.
- Events that don't block time: "Show as: Free" (transparent), events you
  declined, and working-location markers.
- All-day events, placed on your local calendar day.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import config
from .db import Event

UTC = timezone.utc


def user_tz() -> ZoneInfo:
    return ZoneInfo(config.USER_TIMEZONE)


@dataclass
class Occurrence:
    event_id: str  # the Google event id (for a recurring series: the series id)
    summary: str
    start: datetime  # aware, UTC
    end: datetime  # aware, UTC
    all_day: bool
    busy: bool
    location: str | None
    attendee_count: int
    recurring: bool
    my_response: str | None  # accepted / declined / tentative / needsAction / None


@dataclass
class Slot:
    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


# ---------- helpers ----------

def _my_response(ev: Event) -> str | None:
    for a in ev.attendees or []:
        if a.get("self"):
            return a.get("response")
    return None


def _blocks_time(ev: Event) -> bool:
    raw = ev.raw or {}
    if raw.get("transparency") == "transparent":  # "Show as: Free"
        return False
    if raw.get("eventType") == "workingLocation":
        return False
    if _my_response(ev) == "declined":
        return False
    return True


def _all_day_bounds(start_utc: datetime, end_utc: datetime | None) -> tuple[datetime, datetime]:
    """All-day events are stored at midnight UTC of their date(s); put them on the
    user's local calendar day instead."""
    tz = user_tz()
    d0 = start_utc.astimezone(UTC).date()
    d1 = end_utc.astimezone(UTC).date() if end_utc else d0 + timedelta(days=1)
    return (
        datetime.combine(d0, time.min, tzinfo=tz).astimezone(UTC),
        datetime.combine(d1, time.min, tzinfo=tz).astimezone(UTC),
    )


def _occurrence(ev: Event, start: datetime, end: datetime, series_id: str | None = None) -> Occurrence:
    return Occurrence(
        event_id=series_id or ev.google_event_id,
        summary=ev.summary or "(no title)",
        start=start,
        end=end,
        all_day=ev.all_day,
        busy=_blocks_time(ev),
        location=ev.location,
        attendee_count=len(ev.attendees or []),
        recurring=bool(ev.recurrence or ev.recurring_event_id),
        my_response=_my_response(ev),
    )


def _expand_series(master: Event, window_start: datetime, window_end: datetime) -> list[tuple[datetime, datetime]]:
    """Every (start, end) in UTC of a recurring series that overlaps the window."""
    if not master.start_at or not master.recurrence:
        return []
    duration = (master.end_at - master.start_at) if master.end_at else timedelta(hours=1)
    rules = "\n".join(
        line for line in master.recurrence if line.split(":", 1)[0].split(";", 1)[0] in ("RRULE", "EXRULE", "RDATE", "EXDATE")
    )
    if not rules:
        return []

    if master.all_day:
        # All-day series repeat on calendar dates, so expand with plain dates.
        dtstart = datetime.combine(master.start_at.astimezone(UTC).date(), time.min)
        rule = rrulestr(rules, dtstart=dtstart, forceset=True, ignoretz=True)
        lo = (window_start.astimezone(user_tz()) - duration).replace(tzinfo=None) - timedelta(days=1)
        hi = window_end.astimezone(user_tz()).replace(tzinfo=None) + timedelta(days=1)
        out = []
        for occ in rule.between(lo, hi, inc=True):
            s, e = _all_day_bounds(occ.replace(tzinfo=UTC), (occ + duration).replace(tzinfo=UTC))
            out.append((s, e))
        return out

    # Timed series repeat on the wall clock of the event's own time zone:
    # a 9:00 AM weekly meeting stays at 9:00 AM across daylight-saving changes.
    tz = ZoneInfo(master.time_zone) if master.time_zone else user_tz()
    local_start = master.start_at.astimezone(tz)
    rules = _until_dates_to_utc(rules, tz)
    try:
        rule = rrulestr(rules, dtstart=local_start, forceset=True)
        occs = rule.between(window_start - duration, window_end, inc=True)
    except ValueError:
        # e.g. an UNTIL written as a plain date: expand on naive wall-clock time instead.
        rule = rrulestr(rules, dtstart=local_start.replace(tzinfo=None), forceset=True, ignoretz=True)
        lo = (window_start - duration).astimezone(tz).replace(tzinfo=None)
        hi = window_end.astimezone(tz).replace(tzinfo=None)
        occs = [o.replace(tzinfo=tz) for o in rule.between(lo, hi, inc=True)]

    out = []
    for occ in occs:
        # Re-attach the zone so each occurrence gets its own correct UTC offset.
        s = occ.replace(tzinfo=tz).astimezone(UTC)
        out.append((s, s + duration))
    return out


def _until_dates_to_utc(rules: str, tz: ZoneInfo) -> str:
    """A timed series may end with UNTIL=20261102 (a bare date). Google treats that
    day as included, so turn it into the end of that day, in UTC."""
    def fix(m: re.Match) -> str:
        d = datetime.strptime(m.group(1), "%Y%m%d")
        end_of_day = datetime.combine(d.date(), time(23, 59, 59), tzinfo=tz).astimezone(UTC)
        return "UNTIL=" + end_of_day.strftime("%Y%m%dT%H%M%SZ")
    return re.sub(r"UNTIL=(\d{8})(?=;|$|\n)", fix, rules)


def _overlaps(start: datetime, end: datetime, ws: datetime, we: datetime) -> bool:
    return start < we and end > ws


# ---------- public API ----------

def get_occurrences(session: Session, account_id: int, window_start: datetime, window_end: datetime) -> list[Occurrence]:
    """All events (with recurring series expanded) overlapping [window_start, window_end)."""
    window_start, window_end = window_start.astimezone(UTC), window_end.astimezone(UTC)
    # A little slack so all-day events in far-off time zones and long events aren't missed.
    slack = timedelta(days=2)

    base = select(Event).where(Event.account_id == account_id)
    # True when the event has repeat rules. (Empty values may be stored as a JSON
    # null rather than SQL NULL, so check the JSON type instead of IS NULL.)
    is_series = func.coalesce(func.jsonb_typeof(Event.recurrence), "null") == "array"

    singles = session.scalars(
        base.where(
            Event.status != "cancelled",
            ~is_series,
            Event.recurring_event_id.is_(None),
            Event.start_at < window_end + slack,
            or_(Event.end_at.is_(None), Event.end_at > window_start - slack),
        )
    ).all()
    masters = session.scalars(
        base.where(Event.status != "cancelled", is_series, Event.start_at < window_end + slack)
    ).all()
    exceptions = session.scalars(
        base.where(Event.recurring_event_id.is_not(None), Event.recurring_event_id.in_([m.google_event_id for m in masters]))
    ).all() if masters else []

    # Occurrences that were moved or cancelled, keyed by (series id, original start).
    replaced: set[tuple[str, datetime]] = set()
    for ex in exceptions:
        if ex.original_start_at:
            replaced.add((ex.recurring_event_id, ex.original_start_at.astimezone(UTC)))

    result: list[Occurrence] = []

    for ev in singles:
        if not ev.start_at:
            continue
        s, e = (ev.start_at, ev.end_at or ev.start_at)
        if ev.all_day:
            s, e = _all_day_bounds(ev.start_at, ev.end_at)
        if _overlaps(s, e, window_start, window_end):
            result.append(_occurrence(ev, s.astimezone(UTC), e.astimezone(UTC)))

    for m in masters:
        for s, e in _expand_series(m, window_start, window_end):
            original = s if not m.all_day else datetime.combine(
                s.astimezone(user_tz()).date(), time.min, tzinfo=UTC
            )
            if (m.google_event_id, original) in replaced or (m.google_event_id, s) in replaced:
                continue
            if _overlaps(s, e, window_start, window_end):
                result.append(_occurrence(m, s, e, series_id=m.google_event_id))

    for ex in exceptions:
        if ex.status == "cancelled" or not ex.start_at:
            continue
        s, e = ex.start_at, ex.end_at or ex.start_at
        if ex.all_day:
            s, e = _all_day_bounds(ex.start_at, ex.end_at)
        if _overlaps(s, e, window_start, window_end):
            result.append(_occurrence(ex, s.astimezone(UTC), e.astimezone(UTC), series_id=ex.recurring_event_id))

    result.sort(key=lambda o: (o.start, o.end))
    return result


def _merge(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[list[datetime]] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def _working_windows(window_start: datetime, window_end: datetime) -> list[tuple[datetime, datetime]]:
    """The parts of the window that fall inside working hours, day by day, in the user's zone."""
    tz = user_tz()
    ws, we = window_start.astimezone(tz), window_end.astimezone(tz)
    out = []
    day: date = ws.date()
    while day <= we.date():
        if day.isoweekday() in config.WORK_DAYS:
            s = datetime.combine(day, config.WORK_START, tzinfo=tz)
            e = datetime.combine(day, config.WORK_END, tzinfo=tz)
            s, e = max(s, ws), min(e, we)
            if s < e:
                out.append((s.astimezone(UTC), e.astimezone(UTC)))
        day += timedelta(days=1)
    return out


def find_free_slots(
    session: Session,
    account_id: int,
    window_start: datetime,
    window_end: datetime,
    min_minutes: int = 30,
    working_hours_only: bool = True,
) -> list[Slot]:
    busy = _merge(
        [(o.start, o.end) for o in get_occurrences(session, account_id, window_start, window_end) if o.busy]
    )
    windows = (
        _working_windows(window_start, window_end)
        if working_hours_only
        else [(window_start.astimezone(UTC), window_end.astimezone(UTC))]
    )
    now = datetime.now(UTC)
    free: list[Slot] = []
    for ws, we in windows:
        cursor = max(ws, now) if we > now else we  # don't offer time that's already passed
        for bs, be in busy:
            if be <= cursor or bs >= we:
                continue
            if bs > cursor:
                free.append(Slot(cursor, min(bs, we)))
            cursor = max(cursor, be)
            if cursor >= we:
                break
        if cursor < we:
            free.append(Slot(cursor, we))
    return [s for s in free if s.minutes >= min_minutes]


def conflicts(session: Session, account_id: int, start: datetime, end: datetime) -> list[Occurrence]:
    """Busy events that overlap [start, end)."""
    return [o for o in get_occurrences(session, account_id, start, end) if o.busy]
