"""Ledger arithmetic of credit-aware Antigravity caps (services/credit_ledger.py).

The rules under test, from the Gemini Enterprise documentation:
- credit per user and month (Standard USD 10, Plus USD 15) is enforced per seven-day cycle as monthly / 4;
- only Gemini models draw on it, Claude is always pay-as-you-go;
- a person can only be stopped from all of Antigravity, so a person whose paid budget is spent is stopped only
  while nothing is free: this week's share is used up, or they pay again this week.
"""

import datetime as dt
import unittest

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane.services import credit_ledger as cl

UTC = dt.timezone.utc
SEOUL = cl.Cycle(tz="Asia/Seoul", weekday=0, at="00:00")


def kst(y, m, d, hh=0, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=dt.timezone(dt.timedelta(hours=9))).astimezone(UTC)


class EligibilityTest(unittest.TestCase):
    def test_only_gemini_models_draw_on_the_credit(self):
        for model in ("gemini-3-pro", "Gemini-3.1-Flash", "models/gemini-3-pro-image",
                      "publishers/google/models/gemini-3-flash"):
            self.assertTrue(cl.credit_eligible(model), model)
        for model in ("claude-opus-5-5", "claude-sonnet-5-5", "unknown", "", None):
            self.assertFalse(cl.credit_eligible(model), model)


class CycleTest(unittest.TestCase):
    def test_weekly_cycle_and_month_in_the_reset_zone(self):
        now = kst(2026, 11, 5, 18, 30)          # Thursday
        self.assertEqual(SEOUL.week(now), (kst(2026, 11, 2), kst(2026, 11, 9)))
        self.assertEqual(SEOUL.month(now), (kst(2026, 11, 1), kst(2026, 12, 1)))
        thursday = cl.Cycle(tz="Asia/Seoul", weekday=3, at="17:45")
        self.assertEqual(thursday.week(now), (kst(2026, 11, 5, 17, 45), kst(2026, 11, 12, 17, 45)))
        self.assertEqual(thursday.week(kst(2026, 11, 5, 17, 44)), (kst(2026, 10, 29, 17, 45), kst(2026, 11, 5, 17, 45)))

    def test_weekly_reset_keeps_its_local_time_across_daylight_saving(self):
        pt = cl.Cycle(tz="America/Los_Angeles", weekday=2, at="14:00")   # docs example: Wednesday 2 PM PT
        start, end = pt.week(dt.datetime(2026, 10, 30, 12, tzinfo=UTC))  # DST ends Sunday 2026-11-01
        zone = pt._zone()                                                # pylint: disable=protected-access
        self.assertEqual((start.astimezone(zone).hour, end.astimezone(zone).hour), (14, 14))
        self.assertEqual(end - start, dt.timedelta(days=7, hours=1))


class SegmentsTest(unittest.TestCase):
    def test_status_segments_split_the_first_cycle_at_the_month_start(self):
        segs, month, week = cl.segments(SEOUL, kst(2026, 11, 5, 18, 30))
        self.assertEqual(month, (kst(2026, 11, 1), kst(2026, 12, 1)))
        self.assertEqual(week, (kst(2026, 11, 2), kst(2026, 11, 9)))
        self.assertEqual([(s.start, s.end, s.in_month, s.in_week) for s in segs], [
            (kst(2026, 10, 26), kst(2026, 11, 1), False, False),   # previous month, same cycle as Nov 1
            (kst(2026, 11, 1), kst(2026, 11, 2), True, False),
            (kst(2026, 11, 2), kst(2026, 11, 9), True, True),
        ])

    def test_report_segments_cover_the_whole_month(self):
        segs, month, _ = cl.segments(SEOUL, kst(2026, 11, 20), month_start=kst(2026, 10, 1))
        self.assertEqual(month, (kst(2026, 10, 1), kst(2026, 11, 1)))
        self.assertEqual(segs[0].start, kst(2026, 9, 28))
        self.assertFalse(segs[0].in_month)
        self.assertEqual(segs[-1].end, kst(2026, 11, 1))
        self.assertTrue(all(s.in_month for s in segs[1:]))
        self.assertFalse(any(s.in_week for s in segs))
        covered = sum((s.end - s.start for s in segs if s.in_month), dt.timedelta())
        self.assertEqual(covered, dt.timedelta(days=31))


def ledger(costs, credit_week=2.5, now=kst(2026, 11, 5, 12)):
    segs, _, _ = cl.segments(SEOUL, now)
    padded = list(costs) + [(0, 0)] * (len(segs) - len(costs))
    return cl.run(segs, padded, credit_week)


class RunTest(unittest.TestCase):
    def test_gemini_within_the_weekly_share_is_free(self):
        led = ledger([(0, 0), (0, 0), (2.0, 0)])
        self.assertEqual((led.free_month, led.paid_month, led.free_left_week), (2.0, 0.0, 0.5))

    def test_gemini_beyond_the_share_and_claude_are_paid(self):
        led = ledger([(0, 0), (0, 0), (4.0, 10.0)])
        self.assertAlmostEqual(led.free_month, 2.5)
        self.assertAlmostEqual(led.paid_month, 1.5 + 10.0)
        self.assertAlmostEqual(led.paid_week, 11.5)
        self.assertEqual(led.free_left_week, 0.0)

    def test_claude_never_uses_the_credit(self):
        led = ledger([(0, 0), (0, 0), (0, 3.0)])
        self.assertEqual((led.free_month, led.paid_month, led.free_left_week), (0.0, 3.0, 2.5))

    def test_a_cycle_split_by_the_month_start_shares_one_credit(self):
        # Oct 26-31 (previous month) used 2.0 of the cycle's 2.5; Nov 1 used 1.0 more: 0.5 free, 0.5 paid in November.
        segs, _, _ = cl.segments(SEOUL, kst(2026, 11, 1, 12))
        led = cl.run(segs, [(2.0, 0), (1.0, 0)], 2.5)
        self.assertAlmostEqual(led.free_month, 0.5)
        self.assertAlmostEqual(led.paid_month, 0.5)
        self.assertAlmostEqual(led.free_used_week, 2.5)
        self.assertEqual(len(led.weeks), 1)

    def test_unused_credit_does_not_roll_over(self):
        led = ledger([(0, 0), (0, 0), (5.0, 0)], now=kst(2026, 11, 5, 12))
        self.assertAlmostEqual(led.paid_month, 2.5)   # last week's unused share does not cover this week


class DecideTest(unittest.TestCase):
    def test_no_budget_never_blocks(self):
        self.assertEqual(cl.decide(ledger([(0, 0), (0, 0), (50, 500)]), None), (False, []))

    def test_budget_left_never_blocks(self):
        self.assertEqual(cl.decide(ledger([(0, 0), (0, 0), (2.5, 99)]), 100), (False, []))

    def test_spent_budget_blocks_only_while_nothing_is_free(self):
        paid_again = ledger([(0, 0), (0, 0), (0, 100)])
        self.assertEqual(cl.decide(paid_again, 100), (True, ["budget", "paid"]))
        share_used = cl.Ledger(credit_week=2.5, free_used_week=2.5, paid_month=100)
        self.assertEqual(cl.decide(share_used, 100), (True, ["budget", "credit"]))
        fresh_week = cl.Ledger(credit_week=2.5, free_used_week=1.0, paid_month=100)
        self.assertEqual(cl.decide(fresh_week, 100), (False, []))   # free credit left, nothing paid this week

    def test_credits_only(self):
        self.assertEqual(cl.decide(ledger([(0, 0), (0, 0), (2.0, 0)]), 0), (False, []))
        self.assertEqual(cl.decide(ledger([(0, 0), (0, 0), (2.0, 0.01)]), 0), (True, ["budget", "paid"]))
        self.assertEqual(cl.decide(ledger([(0, 0), (0, 0), (2.5, 0)]), 0)[0], True)

    def test_organization_budget_makes_everyone_credits_only(self):
        led = ledger([(0, 0), (0, 0), (1.0, 0.5)])
        self.assertEqual(cl.decide(led, None), (False, []))
        self.assertEqual(cl.decide(led, None, org_used_up=True), (True, ["budget", "paid"]))


class RestoreTimeTest(unittest.TestCase):
    NOW = kst(2026, 11, 18, 10)
    WEEK_END = kst(2026, 11, 23)
    MONTH_END = kst(2026, 12, 1)

    def test_weekly_reset_restores_a_spent_budget(self):
        led = cl.Ledger(credit_week=2.5, free_used_week=2.5, paid_week=3, paid_month=101)
        self.assertEqual(cl.restore_time(led, 100, self.NOW, self.WEEK_END, self.MONTH_END), self.WEEK_END)

    def test_month_start_restores_first_when_it_comes_first(self):
        led = cl.Ledger(credit_week=2.5, free_used_week=2.5, paid_week=3, paid_month=101)
        self.assertEqual(cl.restore_time(led, 100, kst(2026, 11, 28), kst(2026, 11, 30, 23) + dt.timedelta(days=1),
                                         self.MONTH_END), self.MONTH_END)

    def test_credits_only_waits_for_the_new_share(self):
        share_used = cl.Ledger(credit_week=2.5, free_used_week=2.5, paid_week=0.2, paid_month=0.2)
        later_week_end = kst(2026, 12, 7)
        self.assertEqual(cl.restore_time(share_used, 0, kst(2026, 11, 28), later_week_end, self.MONTH_END),
                         later_week_end)
        paid_with_share_left = cl.Ledger(credit_week=2.5, free_used_week=1.0, paid_week=0.2, paid_month=0.2)
        self.assertEqual(cl.restore_time(paid_with_share_left, 0, kst(2026, 11, 28), later_week_end, self.MONTH_END),
                         self.MONTH_END)

    def test_no_credit_and_no_budget_has_no_restore_time(self):
        led = cl.Ledger(credit_week=0, paid_week=0.1, paid_month=0.1)
        self.assertIsNone(cl.restore_time(led, 0, self.NOW, self.WEEK_END, self.MONTH_END))


class CustomerExampleTest(unittest.TestCase):
    """USD 100 monthly paid budget, Standard credit (USD 2.50 a week): week 1 uses the credit and USD 100 more; after
    that the person gets only the free credit each week, until next month."""

    def test_week_by_week(self):
        cycle = SEOUL
        budget, share = 100, 2.5
        calls = []   # (time, gemini_cost, claude_cost)

        def state(now):
            segs, (ms, me), (ws, we) = cl.segments(cycle, now)
            costs = []
            for s in segs:
                g = sum(c[1] for c in calls if s.start <= c[0] < s.end and c[0] < now)
                o = sum(c[2] for c in calls if s.start <= c[0] < s.end and c[0] < now)
                costs.append((g, o))
            led = cl.run(segs, costs, share)
            over, _ = cl.decide(led, budget)
            return led, over, cl.restore_time(led, budget, now, we, me) if over else None

        calls += [(kst(2026, 11, 2, 10), 2.5, 0), (kst(2026, 11, 3, 10), 0, 100.0)]
        led, over, back = state(kst(2026, 11, 3, 10, 5))
        self.assertTrue(over)
        self.assertAlmostEqual(led.paid_month, 100.0)
        self.assertEqual(back, kst(2026, 11, 9))                    # restored at the next weekly reset
        led, over, _ = state(kst(2026, 11, 9, 0, 5))
        self.assertFalse(over)                                      # week 2: free credit usable again
        calls.append((kst(2026, 11, 9, 9), 2.0, 0))
        self.assertFalse(state(kst(2026, 11, 9, 9, 5))[1])          # USD 0.50 of the share left
        calls.append((kst(2026, 11, 9, 11), 0.6, 0))
        led, over, back = state(kst(2026, 11, 9, 11, 5))
        self.assertTrue(over)                                       # share used up: stopped again
        self.assertAlmostEqual(led.paid_month, 100.1)
        self.assertEqual(back, kst(2026, 11, 16))
        calls.append((kst(2026, 11, 16, 9), 0, 0.3))                # week 3 starts with Claude: paid again
        self.assertTrue(state(kst(2026, 11, 16, 9, 5))[1])
        self.assertFalse(state(kst(2026, 12, 1, 0, 5))[1])          # new month: new budget


if __name__ == "__main__":
    unittest.main()
