"""Credit caps drill (controlplane/caps_drill.py): simulated weeks of scheduled enforcement against the real engine.

Each test runs services/governance.py exactly as Cloud Scheduler drives it in production (every five minutes) against
a simulated project, checks every run against an independent call-by-call reference ledger (rules R1-R6 in
caps_drill.py) and then pins the behaviour administrators rely on:

- the weekly reset gives the free credit back to everybody who was stopped, the 1st of the month gives the paid budget
  back, and a credits-only person waits for the weekly reset (a new month brings no free credit);
- with the paid budget spent, a person still uses this week's free credit and is stopped when it is used up or at
  their first paid call;
- a wave of people above the change budget is spread over the next runs, never dropped;
- resets follow the project's own 7-day cycle across a daylight-saving change.

The mutation tests break the engine on purpose (free credit never renewed, never stopping, Claude counted against the
credit, the old all-or-nothing change budget) and require the drill to fail: a drill that cannot fail proves nothing.
"""

import datetime as dt
import unittest
from unittest import mock

import support  # noqa: F401  (puts src/ on the import path)

from controlplane import caps_drill, timeutil
from controlplane.services import credit_ledger
from controlplane.services import governance as gov

SEOUL = timeutil.get_zone("Asia/Seoul")
LA = timeutil.get_zone("America/Los_Angeles")


def local(t, zone=SEOUL):
    return t.astimezone(zone).strftime("%m-%d %a %H:%M")


def events(res, email, zone=SEOUL):
    return [(local(e.at, zone), e.action, tuple(e.reasons), local(e.until, zone) if e.until else None)
            for e in res.events if e.email == email]


def week(res, email, start_local):
    return next(w for w in res.weeks[email] if w["start"].astimezone(SEOUL).strftime("%m-%d") == start_local)


def rules(res):
    return sorted({v[0] for v in res.violations})


class CreditCapsDrillTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Mon 2026-11-23 .. Thu 12-03 (Asia/Seoul, weekly reset Monday 00:00): a weekly reset on Mon 11-30 and the
        # month start on Tue 12-01 inside the same weekly cycle. USD 20 paid budget, Standard credit (USD 2.50 a week).
        cls.res = caps_drill.Drill(dt.datetime(2026, 11, 23, tzinfo=SEOUL), 10, budget=20.0, skip_quiet=True).run()

    def test_every_rule_holds_on_every_run(self):
        self.assertEqual(self.res.violations, [])
        self.assertTrue(self.res.passed)
        self.assertGreater(self.res.runs, 500)
        self.assertEqual(self.res.final_capped, [])

    def test_weekly_reset_and_month_start_restore_a_paying_person(self):
        ev = events(self.res, "claude-only@cymbal.example.com")
        self.assertEqual(ev[0][1:], ("block", ("budget", "paid"), "11-30 Mon 00:00"))
        # Weekly reset: back at 00:00, stopped again by the run after the first paid call (budget still spent).
        self.assertIn(("11-30 Mon 00:00", "restore", (), None), ev)
        self.assertIn(("11-30 Mon 09:05", "block", ("budget", "paid"), "12-01 Tue 00:00"), ev)
        # Month start: a new paid budget, so back at 00:00 on the 1st, before the next weekly reset.
        self.assertIn(("12-01 Tue 00:00", "restore", (), None), ev)

    def test_spent_budget_still_leaves_this_weeks_free_credit(self):
        ev = events(self.res, "gemini-heavy@cymbal.example.com")
        self.assertIn(("11-30 Mon 00:00", "restore", (), None), ev)
        stop = next(e for e in ev if e[0].startswith("11-30") and e[1] == "block")
        self.assertIn("credit", stop[2])
        # Stopped only after using the whole weekly share (USD 2.50) for free, about 85 minutes of work.
        self.assertEqual(round(week(self.res, "gemini-heavy@cymbal.example.com", "11-30")["free"], 2), 2.50)
        self.assertGreaterEqual(stop[0], "11-30 Mon 10:20")

    def test_credits_only_waits_for_the_weekly_reset_not_the_month(self):
        ev = events(self.res, "credits-only@cymbal.example.com")
        self.assertEqual(ev[0], ("11-23 Mon 17:15", "block", ("budget", "credit"), "11-30 Mon 00:00"))
        self.assertIn(("11-30 Mon 00:00", "restore", (), None), ev)
        self.assertIn(("11-30 Mon 17:15", "block", ("budget", "credit"), "12-07 Mon 00:00"), ev)
        self.assertNotIn(("12-01 Tue 00:00", "restore", (), None), ev)

    def test_people_inside_the_free_credit_or_with_an_allow_rule_are_never_stopped(self):
        for email in ("light@cymbal.example.com", "vip@cymbal.example.com", "contractor@cymbal.example.com"):
            self.assertEqual(events(self.res, email), [], email)

    def test_monthly_report_splits_free_credit_and_amount_to_charge(self):
        rows = {u["email"]: u for u in self.res.reports["2026-11"]["users"]}
        light = rows["light@cymbal.example.com"]
        self.assertEqual((light["chargeable"], light["other"]), (0, 0))
        self.assertGreater(light["credit"], 0)
        claude = rows["claude-only@cymbal.example.com"]
        self.assertEqual(claude["credit"], 0)      # Claude never draws on the credit
        self.assertAlmostEqual(claude["chargeable"], claude["other"], places=6)
        self.assertEqual(self.res.reports["2026-11"]["capModel"], "credit_budget")


class CreditCapsDrillScenarioTest(unittest.TestCase):
    def test_credits_only_for_everyone(self):
        res = caps_drill.Drill(dt.datetime(2026, 11, 30, tzinfo=SEOUL), 8, budget=0.0, skip_quiet=True).run()
        self.assertEqual(res.violations, [])
        # Claude is never free: with no paid budget it is stopped by the run after its first paid call. It still has
        # this week's free credit, so it is back on the 1st (paid usage counts per month) and stopped again by the
        # run after its next paid call.
        ev = events(res, "claude-only@cymbal.example.com")
        self.assertEqual(ev[:3], [("11-30 Mon 09:05", "block", ("budget", "paid"), "12-01 Tue 00:00"),
                                  ("12-01 Tue 00:00", "restore", (), None),
                                  ("12-01 Tue 09:05", "block", ("budget", "paid"), "12-07 Mon 00:00")])
        # Gemini only: the week's free credit is used up, so only the weekly reset brings it back.
        ev = events(res, "gemini-heavy@cymbal.example.com")
        self.assertEqual(ev[:2], [("11-30 Mon 10:25", "block", ("budget", "credit", "paid"), "12-07 Mon 00:00"),
                                  ("12-07 Mon 00:00", "restore", (), None)])
        for (email, _ms), (over_by, allowance) in res.overshoot.items():
            self.assertLessEqual(over_by, allowance, email)

    def test_organization_budget_wave_is_spread_over_runs(self):
        res = caps_drill.Drill(dt.datetime(2026, 11, 2, tzinfo=SEOUL), 7, budget=100.0, org_budget=150.0, crowd=24,
                               skip_quiet=True).run()
        self.assertEqual(res.violations, [])
        self.assertEqual(res.change_budget, 10)
        self.assertGreater(res.deferred_max, 0)     # more people over at once than one run may stop
        crowd = [p.email for p in res.persons if p.email.startswith(caps_drill.CROWD_PREFIX)]
        stops = sorted({e.at for e in res.events if e.email in crowd and e.action == "block"})
        stopped = {e.email for e in res.events if e.email in crowd and e.action == "block"}
        self.assertEqual(stopped, set(crowd))
        # Everybody in the wave is stopped within ceil(32 / 10) consecutive five-minute runs.
        self.assertLessEqual(len(stops), 4)
        self.assertLessEqual(stops[-1] - stops[0], dt.timedelta(minutes=15))
        over_by, allowance, _unstoppable = next(iter(res.org_overshoot.values()))
        self.assertLessEqual(over_by, allowance)

    def test_project_cycle_across_daylight_saving(self):
        # The project's 7-day cycle: Wednesday 14:00 in Los Angeles; DST ends on Sunday 2026-11-01.
        res = caps_drill.Drill(dt.datetime(2026, 10, 26, tzinfo=LA), 12, budget=20.0, tz="America/Los_Angeles",
                               weekday=2, at="14:00", skip_quiet=True).run()
        self.assertEqual(res.violations, [])
        restores = [e[0] for e in events(res, "claude-only@cymbal.example.com", LA) if e[1] == "restore"]
        self.assertIn("10-28 Wed 14:00", restores)   # PDT
        self.assertIn("11-01 Sun 00:00", restores)   # month start
        self.assertIn("11-04 Wed 14:00", restores)   # PST: same wall-clock time after the change


class DrillCatchesBrokenEnginesTest(unittest.TestCase):
    """Each mutation reintroduces a failure the caps must never have; the drill has to report it."""

    def drill(self, **kwargs):
        args = dict(budget=20.0, skip_quiet=True)
        args.update(kwargs)
        return caps_drill.Drill(dt.datetime(2026, 11, 23, tzinfo=SEOUL), 9, **args).run()

    def test_free_credit_not_renewed_after_a_stop(self):
        # "Removing IAM also takes the free credit away": the weekly share never comes back.
        with mock.patch.object(credit_ledger.Ledger, "free_left_week", property(lambda self: 0.0)):
            res = self.drill()
        self.assertIn("R1", rules(res))

    def test_never_stopping(self):
        with mock.patch.object(credit_ledger, "decide", lambda led, budget, org_used_up=False: (False, [])):
            res = self.drill()
        self.assertIn("R2", rules(res))
        self.assertIn("R4", rules(res))

    def test_claude_counted_against_the_free_credit(self):
        with mock.patch.object(gov, "credit_prefixes", lambda ag: ("gemini", "claude")):
            res = self.drill()
        self.assertIn("R6", rules(res))

    def test_all_or_nothing_change_budget(self):
        # The old circuit breaker: above the change budget nobody was stopped, so a wave kept spending.
        real = gov.evaluate

        def withhold_all(*args, **kwargs):
            gates, plan, budget = real(*args, **kwargs)
            if plan["deferredBlocks"]:
                plan["deferredBlocks"] = plan["deferredBlocks"] + plan["block"] + plan["disallow"]
                plan["block"], plan["disallow"] = [], []
            return gates, plan, budget
        with mock.patch.object(gov, "evaluate", withhold_all):
            res = caps_drill.Drill(dt.datetime(2026, 11, 2, tzinfo=SEOUL), 3, budget=100.0, org_budget=150.0,
                                   crowd=24, skip_quiet=True).run()
        self.assertIn("R2", rules(res))


if __name__ == "__main__":
    unittest.main()
