"""Per-person Antigravity spend ledger: the weekly free credit and the monthly paid budget, in USD.

What the ledger follows (Gemini Enterprise documentation, "Quotas and pay-as-you-go usage" and "AI developer tools
settings"):

- Licenses include Antigravity credit (Standard USD 10, Plus USD 15 per user and month). Gemini Enterprise enforces it
  per seven-day cycle, as monthly / 4 per seat, and unused credit does not roll over. The cycle starts when the project
  first sends a request to an AI developer tool and resets at the same time every seven days.
- Only Gemini models draw on that credit. Third-party models (Anthropic Claude) are always pay-as-you-go.
- One IAM permission (businessaicode.locations.generateContent) covers every model call, so the control plane can stop
  a person only from all of Antigravity, never from the paid part alone.

So each person gets a weekly free share (monthly credit / 4) for Gemini usage. Everything else (third-party models
and Gemini usage beyond the share) is paid and counts against a monthly paid budget. A person whose paid budget is
used up is stopped only while they cannot use anything for free: when this week's free share is used up, or when they
start paying again this week. The next weekly reset (a new free share) or the next month (a new budget) restores them,
so nobody loses access for longer than the credit rules require.

Usage is processed in time order in segments split at weekly resets and at the month start: the Gemini cost of a
segment is first covered by what is left of that week's share, and paid amounts count in the month they happened in.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .. import timeutil

UTC = dt.timezone.utc
EPS = 1e-9
# Models that draw on the included credit. Third-party models (claude-*, ...) are always pay-as-you-go.
CREDIT_MODEL_PREFIXES = ("gemini",)
_MODEL_PATH_PREFIXES = ("publishers/google/models/", "models/")


def credit_eligible(model, prefixes=CREDIT_MODEL_PREFIXES):
    """True for model ids that draw on the included Antigravity credit (Gemini models)."""
    key = str(model or "").strip().lower()
    for path in _MODEL_PATH_PREFIXES:
        if key.startswith(path):
            key = key[len(path):]
    return bool(key) and any(key.startswith(str(p).lower()) for p in prefixes if p)


@dataclass(frozen=True)
class Cycle:
    """Where weekly cycles and budget months start: weekly at `weekday` (0 = Monday) and `at` (HH:MM) in `tz`, months
    on the 1st at 00:00 in `tz`."""

    tz: str = "UTC"
    weekday: int = 0
    at: str = "00:00"

    def _zone(self):
        return timeutil.get_zone(self.tz)

    def week(self, t):
        """(start, end) aware UTC datetimes of the weekly cycle containing t."""
        zone = self._zone()
        start = timeutil._calendar_start("weekly", t.astimezone(zone), self.weekday, self.at)  # pylint: disable=protected-access
        # Aware arithmetic with zoneinfo is wall clock: the next reset keeps its local time across DST changes.
        return start.astimezone(UTC), (start + dt.timedelta(days=7)).astimezone(UTC)

    def month(self, t):
        """(start, end) aware UTC datetimes of the calendar month containing t."""
        zone = self._zone()
        start = timeutil._calendar_start("monthly", t.astimezone(zone), 0, "00:00")  # pylint: disable=protected-access
        end = (start + dt.timedelta(days=32)).replace(day=1)
        return start.astimezone(UTC), end.astimezone(UTC)


@dataclass(frozen=True)
class Segment:
    start: dt.datetime
    end: dt.datetime
    week_start: dt.datetime
    week_end: dt.datetime
    in_month: bool      # inside the evaluated month
    in_week: bool       # inside the evaluated (current) week


def segments(cycle, now, month_start=None):
    """Segments needed to evaluate one month: from the start of the weekly cycle containing the month start, split at
    every weekly reset and at the month start, up to the end of the month (reports) or of the current cycle (status).

    month_start None: the month containing `now`, segments end at the end of the current weekly cycle (or the month
    end if that comes first); `in_week` marks the cycle containing now. With month_start (a report), segments cover
    the whole month and in_week is False."""
    ms, me = cycle.month(month_start or now)
    if month_start is None:
        cur_ws, cur_we = cycle.week(now)
        stop = min(cur_we, me)
    else:
        cur_ws, cur_we, stop = None, None, me
    out = []
    ws, we = cycle.week(ms)
    while ws < stop:
        cuts = sorted({ws, we, ms} | ({stop} if ws < stop < we else set()))
        cuts = [c for c in cuts if ws <= c <= min(we, stop)]
        for a, b in zip(cuts, cuts[1:]):
            if b > a:
                out.append(Segment(a, b, ws, we, in_month=ms <= a < me, in_week=cur_ws is not None and ws == cur_ws))
        ws, we = we, cycle.week(we)[1]
    return out, (ms, me), (cur_ws, cur_we)


@dataclass
class Ledger:
    """One person's ledger for the evaluated month (amounts in USD)."""

    credit_week: float
    free_used_week: float = 0.0     # free share used in the current weekly cycle (both months of a split cycle)
    paid_week: float = 0.0          # paid in the current cycle, inside the evaluated month
    paid_month: float = 0.0
    free_month: float = 0.0         # credit applied inside the evaluated month
    gemini_month: float = 0.0
    other_month: float = 0.0
    weeks: list = field(default_factory=list)   # [{start, end, gemini, other, free, paid}] month parts per cycle

    @property
    def free_left_week(self):
        return max(0.0, self.credit_week - self.free_used_week)

    @property
    def cost_month(self):
        return self.gemini_month + self.other_month


def run(segs, costs, credit_week):
    """Ledger of one person. `costs` holds (gemini_cost, other_cost) in USD for each segment of `segs`, in order."""
    led = Ledger(credit_week=max(0.0, float(credit_week or 0)))
    left = {}
    weeks = {}
    for seg, (gemini, other) in zip(segs, costs):
        gemini, other = max(0.0, float(gemini or 0)), max(0.0, float(other or 0))
        avail = left.get(seg.week_start, led.credit_week)
        free = min(gemini, avail)
        left[seg.week_start] = avail - free
        paid = gemini - free + other
        if seg.in_week:
            led.free_used_week += free
        if not seg.in_month:
            continue
        led.paid_month += paid
        led.free_month += free
        led.gemini_month += gemini
        led.other_month += other
        if seg.in_week:
            led.paid_week += paid
        w = weeks.setdefault(seg.week_start, {"start": seg.start, "end": seg.end, "gemini": 0.0, "other": 0.0,
                                              "free": 0.0, "paid": 0.0})
        w["start"], w["end"] = min(w["start"], seg.start), max(w["end"], seg.end)
        w["gemini"] += gemini
        w["other"] += other
        w["free"] += free
        w["paid"] += paid
    led.weeks = [weeks[k] for k in sorted(weeks)]
    return led


def budget_used_up(led, budget, org_used_up=False):
    """True when the paid budget is spent: the person's own budget (None = no limit; 0 = credits only) or the
    organization's monthly paid budget."""
    return bool(org_used_up) or (budget is not None and led.paid_month >= float(budget) - EPS)


def decide(led, budget, org_used_up=False):
    """(over, reasons) for one person now. Over only when the paid budget is spent and nothing is free right now:
    "credit" (this week's free share is used up) or "paid" (paid usage again this week)."""
    if not budget_used_up(led, budget, org_used_up):
        return False, []
    reasons = []
    if led.free_left_week <= EPS:
        reasons.append("credit")
    if led.paid_week > EPS:
        reasons.append("paid")
    if not reasons:
        return False, []
    return True, ["budget"] + reasons


def restore_time(led, budget, now, week_end, month_end, org_used_up=False):
    """When a person who is over regains access without further usage: the weekly reset (a new free share, no paid
    usage yet) or the month start (a new paid budget), whichever comes first and lifts the block. None when neither
    does (for example a credits-only policy without any credit)."""
    for t in sorted({week_end, month_end}):
        if t is None or t <= now:
            continue
        new_week, new_month = t >= week_end, t >= month_end
        probe = Ledger(credit_week=led.credit_week,
                       free_used_week=0.0 if new_week else led.free_used_week,
                       paid_week=0.0,
                       paid_month=0.0 if new_month else led.paid_month)
        if not decide(probe, budget, org_used_up and not new_month)[0]:
            return t
    return None
