"""Date range parsing, timezone handling and timeline bucketing."""

from __future__ import annotations

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

PERIODS = ("daily", "weekly", "monthly", "all")
WINDOWS = ("calendar", "rolling")


def _check_period(period, window):
    if period not in PERIODS:
        raise ApiError("bad_request", "Parameter 'period' must be one of daily, weekly, monthly, all.")
    if window not in WINDOWS:
        raise ApiError("bad_request", "Parameter 'window' must be 'calendar' or 'rolling'.")


def _calendar_start(period, local_now):
    """Start of the calendar period containing local_now (wall clock in its own zone)."""
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "daily":
        return midnight
    if period == "weekly":
        return midnight - dt.timedelta(days=local_now.weekday())
    return midnight.replace(day=1)


def governance_window(period, window, now=None, tz=None):
    """Returns (start, end) aware UTC datetimes.

    calendar: period boundaries at local midnight in `tz` (default UTC; daily: today 00:00,
    weekly: Monday 00:00, monthly: the 1st 00:00), all: now - 90 days.
    rolling: now - 24h / 7d / 30d / 90d."""
    now = (now or dt.datetime.now(UTC)).astimezone(UTC).replace(second=0, microsecond=0)
    _check_period(period, window)
    if period == "all":
        return now - dt.timedelta(days=90), now
    if window == "rolling":
        span = {"daily": 1, "weekly": 7, "monthly": 30}[period]
        return now - dt.timedelta(days=span), now
    zone = get_zone(tz) if tz else UTC
    start = _calendar_start(period, now.astimezone(zone))
    return start.astimezone(UTC), now


def governance_next_reset(period, window, now=None, tz=None):
    """Next calendar period boundary (aware UTC), when usage counters restart; None for rolling
    windows and 'all', which have no fixed reset."""
    _check_period(period, window)
    if period == "all" or window == "rolling":
        return None
    now = (now or dt.datetime.now(UTC)).astimezone(UTC)
    zone = get_zone(tz) if tz else UTC
    start = _calendar_start(period, now.astimezone(zone))
    if period == "daily":
        nxt = start + dt.timedelta(days=1)
    elif period == "weekly":
        nxt = start + dt.timedelta(days=7)
    else:
        nxt = (start + dt.timedelta(days=32)).replace(day=1)
    # Aware-datetime arithmetic with zoneinfo is wall-clock based: nxt is local midnight even
    # across daylight-saving changes.
    return nxt.astimezone(UTC)


def governance_period_end(period, window, now=None, tz=None):
    """End of the current evaluation period: the next calendar reset, or for rolling windows the
    moment the current usage has fully left the window (now + window span)."""
    nxt = governance_next_reset(period, window, now, tz)
    if nxt is not None:
        return nxt
    now = (now or dt.datetime.now(UTC)).astimezone(UTC).replace(second=0, microsecond=0)
    span = 90 if period == "all" else {"daily": 1, "weekly": 7, "monthly": 30}[period]
    return now + dt.timedelta(days=span)


def rfc3339(value):
    return iso_z(value)
