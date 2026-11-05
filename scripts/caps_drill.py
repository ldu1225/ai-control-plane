#!/usr/bin/env python3
"""Credit caps drill: watch a simulated month of automatic Antigravity enforcement, with checks.

The real control plane engine runs every five minutes (as Cloud Scheduler calls it) against a simulated project:
IAM policy, Antigravity inference logs (official schema) and the clock. Eight simulated people use Gemini and Claude;
the output is each person's week-by-week spend, every automatic stop and restore, and PASS/FAIL for the rules
(nobody loses free credit, everybody over the budget is stopped within one run, every stop ends at the weekly reset
or the month start, the monthly report matches an independent calculation). No Google Cloud access is needed.

  python3 scripts/caps_drill.py                       # USD 100 paid budget, Standard credit (USD 10 a month)
  python3 scripts/caps_drill.py --budget 0            # credits only
  python3 scripts/caps_drill.py --org-budget 300      # plus a USD 300 budget for everyone together
  python3 scripts/caps_drill.py --org-budget 300 --crowd 30   # 30 more people: a wave above the change budget
  python3 scripts/caps_drill.py --weekday 2 --at 14:00 --tz America/Los_Angeles   # the project's 7-day cycle
  python3 scripts/caps_drill.py --lang en

Exit status 0 = PASS, 1 = FAIL.
"""

import argparse
import datetime as dt
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from controlplane import caps_drill, timeutil  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--budget", type=float, default=100.0, help="paid budget per person and month in USD "
                                                                 "(0 = credits only, -1 = no limit; default 100)")
    ap.add_argument("--credit", type=float, default=10.0, help="credit per person and month in USD (Standard 10, "
                                                                "Plus 15; default 10)")
    ap.add_argument("--org-budget", type=float, default=None, help="paid budget for everyone together, USD per month")
    ap.add_argument("--tz", default="Asia/Seoul", help="time zone of the weekly reset and the month (default Asia/Seoul)")
    ap.add_argument("--weekday", type=int, default=0, help="weekly reset day, 0 = Monday (default 0)")
    ap.add_argument("--at", default="00:00", help="weekly reset time HH:MM (default 00:00)")
    ap.add_argument("--start", default="", help="first day YYYY-MM-DD (default: the 1st of last month)")
    ap.add_argument("--days", type=int, default=38, help="days to simulate (default 38: a month and the next week)")
    ap.add_argument("--crowd", type=int, default=0, help="add this many people who use Claude every 10 minutes "
                                                          "(with --org-budget: many people over at the same moment)")
    ap.add_argument("--fast", action="store_true", help="skip scheduler runs in which nothing can change")
    ap.add_argument("--lang", choices=("ko", "en"), default="ko")
    args = ap.parse_args()
    zone = timeutil.get_zone(args.tz)
    if args.start:
        first = dt.datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=zone)
    else:
        today = dt.datetime.now(zone)
        first = (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1, hour=0, minute=0, second=0,
                                                                      microsecond=0)
    started = time.monotonic()
    drill = caps_drill.Drill(first, args.days, budget=None if args.budget < 0 else args.budget,
                             credit_month=args.credit, org_budget=args.org_budget, tz=args.tz,
                             weekday=args.weekday, at=args.at, skip_quiet=args.fast, crowd=max(0, args.crowd))
    result = drill.run()
    print(caps_drill.format_report(result, args.lang))
    print("(%.1fs)" % (time.monotonic() - started))
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
