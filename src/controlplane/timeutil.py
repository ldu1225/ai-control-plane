"""Date range parsing, timezone handling and timeline bucketing."""

from __future__ import annotations

import calendar as _calendar
import datetime as dt
import re
from dataclasses import dataclass

from .httpkit import ApiError, iso_z

try:  # zoneinfo needs system tz data (present in python:3.12-slim) or the tzdata package.
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception

UTC = dt.timezone.utc
TZ_RE = re.compile(r"^[A-Za-z_]+(?:/[A-Za-z0-9_+\-]+)*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_SPAN_DAYS = 400
GRANULARITIES = ("day", "week")


def valid_timezone(name):
    if not isinstance(name, str) or not TZ_RE.match(name) or len(name) > 64:
        return False
    if name == "UTC":
        return True
    if ZoneInfo is None:
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False


def get_zone(name):
    """Returns a tzinfo for an IANA name; falls back to UTC for invalid or unknown names."""
    if name == "UTC" or not valid_timezone(name):
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):  # pragma: no cover
        return UTC


def today_in(tz_name, now=None):
    now = now or dt.datetime.now(UTC)
    return now.astimezone(get_zone(tz_name)).date()


def parse_date(value, field):
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ApiError("bad_request", "Parameter '%s' must be a date in YYYY-MM-DD format." % field)
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ApiError("bad_request", "Parameter '%s' is not a valid calendar date." % field)


def local_midnight_utc(day, tz):
    return dt.datetime.combine(day, dt.time.min, tzinfo=tz).astimezone(UTC)


@dataclass(frozen=True)
class DateRange:
    start: dt.date
    end: dt.date
    granularity: str
    tz_name: str

    @property
    def tz(self):
        return get_zone(self.tz_name)

    @property
    def days(self):
        return (self.end - self.start).days + 1

    @property
    def bucket_days(self):
        return 7 if self.granularity == "week" else 1

    def previous(self):
        prev_end = self.start - dt.timedelta(days=1)
        prev_start = prev_end - dt.timedelta(days=self.days - 1)
        return DateRange(prev_start, prev_end, self.granularity, self.tz_name)

    def start_ts(self):
        """TIMESTAMP(start, tz) as an aware UTC datetime."""
        return local_midnight_utc(self.start, self.tz)

    def end_ts(self):
        """TIMESTAMP(end + 1 day, tz) as an aware UTC datetime (exclusive bound)."""
        return local_midnight_utc(self.end + dt.timedelta(days=1), self.tz)

    def buckets(self):
        step = dt.timedelta(days=self.bucket_days)
        out = []
        cur = self.start
        while cur <= self.end:
            out.append(cur)
            cur += step
        return out

    def bucket_labels(self):
        return [d.isoformat() for d in self.buckets()]

    def bucket_index(self, day):
        if day is None:
            return None
        if isinstance(day, str):
            try:
                day = dt.date.fromisoformat(day[:10])
            except ValueError:
                return None
        if isinstance(day, dt.datetime):
            day = day.astimezone(self.tz).date()
        if day < self.start or day > self.end:
            return None
        return (day - self.start).days // self.bucket_days

    def contains(self, day):
        return self.start <= day <= self.end

    def meta(self):
        return {"start": self.start.isoformat(), "end": self.end.isoformat(),
                "granularity": self.granularity, "timezone": self.tz_name}

    def suffixes(self, pad_days=1):
        """Date-shard suffix bounds (YYYYMMDD) padded by pad_days on each side."""
        lo = self.start - dt.timedelta(days=pad_days)
        hi = self.end + dt.timedelta(days=pad_days)
        return lo.strftime("%Y%m%d"), hi.strftime("%Y%m%d")


def parse_range(params, tz_name, default_days, now=None):
    """Parses start/end/granularity query parameters."""
    params = params or {}
    granularity = (params.get("granularity") or "day").strip().lower()
    if granularity not in GRANULARITIES:
        raise ApiError("bad_request", "Parameter 'granularity' must be 'day' or 'week'.")
    today = today_in(tz_name, now)
    start_raw = params.get("start") or ""
    end_raw = params.get("end") or ""
    if end_raw:
        end = parse_date(end_raw, "end")
    else:
        end = today
    if start_raw:
        start = parse_date(start_raw, "start")
    else:
        days = max(1, min(int(default_days or 14), MAX_SPAN_DAYS))
        start = end - dt.timedelta(days=days - 1)
    if start > end:
        raise ApiError("bad_request", "Parameter 'start' must not be after 'end'.")
    if (end - start).days + 1 > MAX_SPAN_DAYS:
        raise ApiError("bad_request", "The date range must not exceed %d days." % MAX_SPAN_DAYS)
    return DateRange(start, end, granularity, tz_name)


# ---------------------------------------------------------------------------
# Timeline shape
# ---------------------------------------------------------------------------

def series(key, name, values, **extra):
    item = {"key": key, "name": name, "values": list(values)}
    item.update(extra)
    return item


def timeline(rng, series_list):
    return {"granularity": rng.granularity, "buckets": rng.bucket_labels(), "series": list(series_list)}


def zeros(rng):
    return [0] * len(rng.buckets())


def fold(rng, pairs, cast=float):
    """Sums (day_or_bucket_date, value) pairs into the range's buckets."""
    values = [0] * len(rng.buckets())
    for day, value in pairs:
        idx = rng.bucket_index(day)
        if idx is None or value is None:
            continue
        values[idx] += value
    if cast is int:
        return [int(v) for v in values]
    return [round(v, 6) if isinstance(v, float) else v for v in values]


# ---------------------------------------------------------------------------
# Antigravity governance windows (agy-admin semantics)
# ---------------------------------------------------------------------------

# custom: periods of `cycle_days` days counted from `cycle_start` (for example every 14 days from a Monday).
PERIODS = ("daily", "weekly", "monthly", "custom", "all")
WINDOWS = ("calendar", "rolling")
RESET_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
CYCLE_DAYS_MAX = 92
CYCLE_DAYS_DEFAULT = 30
# Custom periods without a valid start date count from here, so their boundaries never move.
CYCLE_EPOCH = dt.date(1970, 1, 1)


def _check_period(period, window):
    if period not in PERIODS:
        raise ApiError("bad_request", "Parameter 'period' must be one of daily, weekly, monthly, custom, all.")
    if window not in WINDOWS:
        raise ApiError("bad_request", "Parameter 'window' must be 'calendar' or 'rolling'.")


def reset_hour_minute(at):
    """(hour, minute) of a 'HH:MM' reset time; empty or invalid values mean midnight."""
    m = RESET_TIME_RE.match(str(at or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _reset_weekday(weekday):
    try:
        value = int(weekday)
    except (TypeError, ValueError):
        return 0
    return value if 0 <= value <= 6 else 0


def reset_day_of_month(day):
    """Day of the month (1-31) on which monthly periods start; invalid values mean the 1st. A month without that day
    (29, 30 or 31) starts its period on its last day."""
    try:
        value = int(day)
    except (TypeError, ValueError):
        return 1
    return value if 1 <= value <= 31 else 1


def cycle_length(days):
    """Length of custom periods in days (1 to CYCLE_DAYS_MAX); invalid values mean CYCLE_DAYS_DEFAULT."""
    try:
        value = int(days)
    except (TypeError, ValueError):
        return CYCLE_DAYS_DEFAULT
    return value if 1 <= value <= CYCLE_DAYS_MAX else CYCLE_DAYS_DEFAULT


def cycle_start_date(value):
    """First day of custom periods (a date or YYYY-MM-DD); empty or invalid values mean CYCLE_EPOCH."""
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    text = str(value or "").strip()
    if DATE_RE.match(text):
        try:
            return dt.date.fromisoformat(text)
        except ValueError:
            pass
    return CYCLE_EPOCH


def _month_start(year, month, day, hour, minute, tzinfo):
    """`day` of the month at hour:minute (wall clock in tzinfo); the month's last day when it is shorter."""
    last = _calendar.monthrange(year, month)[1]
    return dt.datetime(year, month, min(day, last), hour, minute, tzinfo=tzinfo)


def _shift_month(year, month, delta):
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _calendar_start(period, local_now, weekday=0, at="00:00", day=1, cycle_days=CYCLE_DAYS_DEFAULT,
                    cycle_start=""):
    """Start of the calendar period containing local_now (wall clock in its own zone).

    Periods begin at the reset time `at` (HH:MM, default midnight): daily every day, weekly on `weekday` (0 = Monday
    ... 6 = Sunday, default Monday), monthly on day `day` of the month (default the 1st; the last day in months without
    that day) and custom every `cycle_days` days counted from `cycle_start` (YYYY-MM-DD, also before that date)."""
    hour, minute = reset_hour_minute(at)
    boundary = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if period == "daily":
        return boundary if local_now >= boundary else boundary - dt.timedelta(days=1)
    if period == "weekly":
        start = boundary - dt.timedelta(days=(local_now.weekday() - _reset_weekday(weekday)) % 7)
        return start if local_now >= start else start - dt.timedelta(days=7)
    if period == "custom":
        span = dt.timedelta(days=cycle_length(cycle_days))
        anchor = dt.datetime.combine(cycle_start_date(cycle_start), dt.time(hour, minute), tzinfo=local_now.tzinfo)
        # Whole cycles between the anchor and now on the wall clock (negative before the start date).
        steps = (local_now.replace(tzinfo=None) - anchor.replace(tzinfo=None)) // span
        return anchor + span * steps
    dom = reset_day_of_month(day)
    start = _month_start(local_now.year, local_now.month, dom, hour, minute, local_now.tzinfo)
    if local_now >= start:
        return start
    year, month = _shift_month(local_now.year, local_now.month, -1)
    return _month_start(year, month, dom, hour, minute, local_now.tzinfo)


def _next_start(period, start, day=1, cycle_days=CYCLE_DAYS_DEFAULT):
    """The period start after `start` (local wall clock). Aware-datetime arithmetic with zoneinfo is wall-clock based,
    so the next start keeps its local reset time across daylight-saving changes."""
    if period == "daily":
        return start + dt.timedelta(days=1)
    if period == "weekly":
        return start + dt.timedelta(days=7)
    if period == "custom":
        return start + dt.timedelta(days=cycle_length(cycle_days))
    year, month = _shift_month(start.year, start.month, 1)
    return _month_start(year, month, reset_day_of_month(day), start.hour, start.minute, start.tzinfo)


def _span_days(period, cycle_days=CYCLE_DAYS_DEFAULT):
    if period == "all":
        return 90
    if period == "custom":
        return cycle_length(cycle_days)
    return {"daily": 1, "weekly": 7, "monthly": 30}[period]


def governance_window(period, window, now=None, tz=None, weekday=0, at="00:00", day=1,
                      cycle_days=CYCLE_DAYS_DEFAULT, cycle_start=""):
    """Returns (start, end) aware UTC datetimes.

    calendar: period boundaries at the reset time `at` (default 00:00) in `tz` (default UTC; daily: today, weekly: the
    last `weekday` (default Monday), monthly: day `day` of the month (default the 1st), custom: every `cycle_days` days
    from `cycle_start`), all: now - 90 days. rolling: now - 24h / 7d / 30d / cycle_days / 90d."""
    now = (now or dt.datetime.now(UTC)).astimezone(UTC).replace(second=0, microsecond=0)
    _check_period(period, window)
    if period == "all" or window == "rolling":
        return now - dt.timedelta(days=_span_days(period, cycle_days)), now
    zone = get_zone(tz) if tz else UTC
    start = _calendar_start(period, now.astimezone(zone), weekday, at, day, cycle_days, cycle_start)
    return start.astimezone(UTC), now


def governance_next_reset(period, window, now=None, tz=None, weekday=0, at="00:00", day=1,
                          cycle_days=CYCLE_DAYS_DEFAULT, cycle_start=""):
    """Next calendar period boundary (aware UTC), when usage counters restart; None for rolling
    windows and 'all', which have no fixed reset."""
    _check_period(period, window)
    if period == "all" or window == "rolling":
        return None
    now = (now or dt.datetime.now(UTC)).astimezone(UTC)
    zone = get_zone(tz) if tz else UTC
    start = _calendar_start(period, now.astimezone(zone), weekday, at, day, cycle_days, cycle_start)
    return _next_start(period, start, day, cycle_days).astimezone(UTC)


def governance_period_end(period, window, now=None, tz=None, weekday=0, at="00:00", day=1,
                          cycle_days=CYCLE_DAYS_DEFAULT, cycle_start=""):
    """End of the current evaluation period: the next calendar reset, or for rolling windows the
    moment the current usage has fully left the window (now + window span)."""
    nxt = governance_next_reset(period, window, now, tz, weekday, at, day, cycle_days, cycle_start)
    if nxt is not None:
        return nxt
    now = (now or dt.datetime.now(UTC)).astimezone(UTC).replace(second=0, microsecond=0)
    return now + dt.timedelta(days=_span_days(period, cycle_days))


def governance_periods(period, window, now=None, count=3, tz=None, weekday=0, at="00:00", day=1,
                       cycle_days=CYCLE_DAYS_DEFAULT, cycle_start=""):
    """The current calendar period and the next ones: [(start, end)] aware UTC datetimes, `count` items (1 to 12).
    Rolling windows and 'all' have no fixed periods: one item, (now - span, None)."""
    _check_period(period, window)
    now = (now or dt.datetime.now(UTC)).astimezone(UTC)
    if period == "all" or window == "rolling":
        start, _ = governance_window(period, window, now, tz, weekday, at, day, cycle_days, cycle_start)
        return [(start, None)]
    zone = get_zone(tz) if tz else UTC
    start = _calendar_start(period, now.astimezone(zone), weekday, at, day, cycle_days, cycle_start)
    out = []
    for _ in range(max(1, min(int(count or 1), 12))):
        nxt = _next_start(period, start, day, cycle_days)
        out.append((start.astimezone(UTC), nxt.astimezone(UTC)))
        start = nxt
    return out


def rfc3339(value):
    return iso_z(value)
