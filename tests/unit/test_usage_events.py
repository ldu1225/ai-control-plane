"""Real-time automatic enforcement (Pub/Sub push + admin policy triggers) and customizable fixed-cap periods."""

from __future__ import annotations

import base64
import datetime as dt
import json
import unittest
from unittest import mock

import support

from controlplane import config, enforce_job, settings_store, timeutil
from controlplane.httpkit import ApiError
from controlplane.security import SCHEDULER_MODE, Identity
from controlplane.services import governance as gov
from controlplane.services import system as system_svc
from controlplane.services import usage_events as ue
from controlplane.services.pricing import PriceTable

UTC = dt.timezone.utc
SEOUL = timeutil.get_zone("Asia/Seoul")
NY = timeutil.get_zone("America/New_York")


def _push_body(entry, subscription="projects/demo-project/subscriptions/ai-control-plane-agy-usage-push"):
    raw = json.dumps(entry).encode("utf-8")
    return {
        "message": {"data": base64.b64encode(raw).decode("ascii"), "messageId": "m-1"},
        "subscription": subscription,
    }


def _inference_entry(
    person="alice@example.com",
    ts="2026-11-11T10:00:00Z",
    tokens=200000,
    prompt=150000,
    cand=50000,
    cached=0,
    model="gemini-2.5-pro",
    project="demo-project",
    insert_id="ins-1",
    log_id="businessaicode.googleapis.com%2Finference_response",
):
    return {
        "logName": "projects/%s/logs/%s" % (project, log_id),
        "timestamp": ts,
        "insertId": insert_id,
        "labels": {"user_id": person, "model_id": model},
        "jsonPayload": {
            "metadata": {
                "totalTokenCount": tokens,
                "promptTokenCount": prompt,
                "candidatesTokenCount": cand,
                "cachedContentTokenCount": cached,
            }
        },
    }


class CustomPeriodScheduleTest(unittest.TestCase):
    def test_monthly_reset_day_1_through_month_end_and_auto_restore(self):
        # User hits $100 cap on Nov 11 KST -> window is Nov 1 00:00 KST to Dec 1 00:00 KST.
        now = dt.datetime(2026, 11, 11, 15, 23, 45, tzinfo=SEOUL).astimezone(UTC)
        start, end = timeutil.governance_window("monthly", "calendar", now, tz="Asia/Seoul", day=1, at="00:00")
        nxt = timeutil.governance_next_reset("monthly", "calendar", now, tz="Asia/Seoul", day=1, at="00:00")
        self.assertEqual(start, dt.datetime(2026, 11, 1, 0, 0, tzinfo=SEOUL).astimezone(UTC))
        self.assertEqual(end, dt.datetime(2026, 11, 11, 15, 23, tzinfo=SEOUL).astimezone(UTC))
        self.assertEqual(nxt, dt.datetime(2026, 12, 1, 0, 0, tzinfo=SEOUL).astimezone(UTC))

    def test_monthly_reset_day_15_before_and_after_boundary(self):
        before = dt.datetime(2026, 5, 14, 23, 59, tzinfo=SEOUL).astimezone(UTC)
        start, _ = timeutil.governance_window("monthly", "calendar", before, tz="Asia/Seoul", day=15, at="00:00")
        nxt = timeutil.governance_next_reset("monthly", "calendar", before, tz="Asia/Seoul", day=15, at="00:00")
        self.assertEqual(start, dt.datetime(2026, 4, 15, 0, 0, tzinfo=SEOUL).astimezone(UTC))
        self.assertEqual(nxt, dt.datetime(2026, 5, 15, 0, 0, tzinfo=SEOUL).astimezone(UTC))

        after = dt.datetime(2026, 5, 15, 0, 0, tzinfo=SEOUL).astimezone(UTC)
        start2, _ = timeutil.governance_window("monthly", "calendar", after, tz="Asia/Seoul", day=15, at="00:00")
        nxt2 = timeutil.governance_next_reset("monthly", "calendar", after, tz="Asia/Seoul", day=15, at="00:00")
        self.assertEqual(start2, dt.datetime(2026, 5, 15, 0, 0, tzinfo=SEOUL).astimezone(UTC))
        self.assertEqual(nxt2, dt.datetime(2026, 6, 15, 0, 0, tzinfo=SEOUL).astimezone(UTC))

    def test_monthly_day_31_clamps_to_shorter_months_and_leap_year(self):
        # Non-leap Feb 2026 (28 days)
        now_feb = dt.datetime(2026, 2, 15, 12, 0, tzinfo=UTC)
        periods = timeutil.governance_periods("monthly", "calendar", now=now_feb, count=4, tz="UTC", day=31, at="00:00")
        expected = [
            (dt.datetime(2026, 1, 31, 0, 0, tzinfo=UTC), dt.datetime(2026, 2, 28, 0, 0, tzinfo=UTC)),
            (dt.datetime(2026, 2, 28, 0, 0, tzinfo=UTC), dt.datetime(2026, 3, 31, 0, 0, tzinfo=UTC)),
            (dt.datetime(2026, 3, 31, 0, 0, tzinfo=UTC), dt.datetime(2026, 4, 30, 0, 0, tzinfo=UTC)),
            (dt.datetime(2026, 4, 30, 0, 0, tzinfo=UTC), dt.datetime(2026, 5, 31, 0, 0, tzinfo=UTC)),
        ]
        self.assertEqual(periods, expected)

        # Leap Feb 2028 (29 days)
        now_leap = dt.datetime(2028, 2, 15, 12, 0, tzinfo=UTC)
        leap_periods = timeutil.governance_periods(
            "monthly", "calendar", now=now_leap, count=2, tz="UTC", day=31, at="00:00"
        )
        self.assertEqual(
            leap_periods,
            [
                (dt.datetime(2028, 1, 31, 0, 0, tzinfo=UTC), dt.datetime(2028, 2, 29, 0, 0, tzinfo=UTC)),
                (dt.datetime(2028, 2, 29, 0, 0, tzinfo=UTC), dt.datetime(2028, 3, 31, 0, 0, tzinfo=UTC)),
            ],
        )

    def test_custom_cycle_before_and_after_anchor_and_across_dst(self):
        # 14-day cycle anchored to 2026-03-01 at 09:00 America/New_York (crosses US spring-forward DST on Mar 8)
        now_after = dt.datetime(2026, 3, 10, 12, 0, tzinfo=NY).astimezone(UTC)
        periods = timeutil.governance_periods(
            "custom", "calendar", now=now_after, count=2, tz="America/New_York",
            at="09:00", cycle_days=14, cycle_start="2026-03-01"
        )
        self.assertEqual(periods[0][0], dt.datetime(2026, 3, 1, 9, 0, tzinfo=NY).astimezone(UTC))
        self.assertEqual(periods[0][1], dt.datetime(2026, 3, 15, 9, 0, tzinfo=NY).astimezone(UTC))
        self.assertEqual(periods[1][0], dt.datetime(2026, 3, 15, 9, 0, tzinfo=NY).astimezone(UTC))
        self.assertEqual(periods[1][1], dt.datetime(2026, 3, 29, 9, 0, tzinfo=NY).astimezone(UTC))

        # Before anchor date: steps backward in 14-day increments cleanly
        now_before = dt.datetime(2026, 2, 20, 10, 0, tzinfo=UTC)
        before_periods = timeutil.governance_periods(
            "custom", "calendar", now=now_before, count=2, tz="UTC",
            at="00:00", cycle_days=14, cycle_start="2026-03-01"
        )
        self.assertEqual(
            before_periods,
            [
                (dt.datetime(2026, 2, 15, 0, 0, tzinfo=UTC), dt.datetime(2026, 3, 1, 0, 0, tzinfo=UTC)),
                (dt.datetime(2026, 3, 1, 0, 0, tzinfo=UTC), dt.datetime(2026, 3, 15, 0, 0, tzinfo=UTC)),
            ],
        )

    def test_rolling_and_all_periods_preview(self):
        now = dt.datetime(2026, 11, 11, 10, 30, 20, tzinfo=UTC)
        rolling = timeutil.governance_periods("custom", "rolling", now=now, count=3, tz="UTC", cycle_days=10)
        self.assertEqual(rolling, [(dt.datetime(2026, 11, 1, 10, 30, 0, tzinfo=UTC), None)])
        all_p = timeutil.governance_periods("all", "calendar", now=now, count=3, tz="UTC")
        self.assertEqual(len(all_p), 1)
        self.assertIsNone(all_p[0][1])

    def test_config_validators_for_new_period_and_subscription_keys(self):
        self.assertEqual(config.VALIDATORS["governance.antigravity.period"]("custom"), "custom")
        self.assertEqual(config.VALIDATORS["governance.antigravity.reset_day"](31), 31)
        self.assertEqual(config.VALIDATORS["governance.antigravity.cycle_days"](92), 92)
        self.assertEqual(config.VALIDATORS["governance.antigravity.cycle_start"]("2026-04-01"), "2026-04-01")
        self.assertEqual(config.VALIDATORS["governance.antigravity.cycle_start"](""), "")
        sub = "projects/demo-project/subscriptions/ai-control-plane-agy-usage-push"
        self.assertEqual(config.VALIDATORS["governance.antigravity.usage_events_subscription"](sub), sub)

        for key, bad in (
            ("governance.antigravity.reset_day", 0),
            ("governance.antigravity.reset_day", 32),
            ("governance.antigravity.cycle_days", 0),
            ("governance.antigravity.cycle_days", 93),
            ("governance.antigravity.cycle_start", "2026-02-30"),
            ("governance.antigravity.cycle_start", "not-a-date"),
            ("governance.antigravity.usage_events_subscription", "short-sub-id"),
        ):
            with self.assertRaises(config.Invalid, msg=(key, bad)):
                config.VALIDATORS[key](bad)


class PeriodPreviewApiTest(unittest.TestCase):
    def test_preview_defaults_and_overrides(self):
        now = dt.datetime(2026, 11, 11, 3, 0, tzinfo=UTC)
        settings = support.make_settings(governance={"period": "monthly", "reset_day": 1, "reset_timezone": "Asia/Seoul"})
        ctx = support.FakeCtx(settings=settings, params={"period": "custom", "cycleDays": "15", "cycleStart": "2026-11-01"})
        with mock.patch.object(gov, "utcnow", return_value=now):
            res = gov.period_preview(ctx)
        self.assertEqual(res["period"], "custom")
        self.assertEqual(res["timezone"], "Asia/Seoul")
        self.assertFalse(res["rolling"])
        self.assertEqual(res["cycleStart"], "2026-11-01")
        self.assertEqual(len(res["periods"]), 3)
        self.assertEqual(res["periods"][0]["start"], "2026-10-31T15:00:00Z")
        self.assertEqual(res["periods"][0]["end"], "2026-11-15T15:00:00Z")

    def test_preview_rejects_invalid_params(self):
        for params in (
            {"period": "yearly"},
            {"window": "sliding"},
            {"tz": "Mars/Olympus"},
            {"at": "25:00"},
            {"day": "32"},
            {"cycleDays": "0"},
            {"cycleStart": "2026-02-31"},
        ):
            ctx = support.FakeCtx(params=params)
            with self.assertRaises(ApiError) as err:
                gov.period_preview(ctx)
            self.assertEqual(err.exception.code, "bad_request", params)


class FreshUsageWindowTest(unittest.TestCase):
    def test_fresh_period_usage_covers_current_minute(self):
        # Minute-truncated end is 10:05:00Z, actual now is 10:05:42Z -> fresh=True must query through 10:05:42Z.
        start = dt.datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
        end = dt.datetime(2026, 11, 11, 10, 5, 0, tzinfo=UTC)
        now = dt.datetime(2026, 11, 11, 10, 5, 42, tzinfo=UTC)
        ctx = support.FakeCtx()
        seen = []

        def fake_usage(_ctx, a, b, fresh=False):
            seen.append((a, b, fresh))
            return gov.Usage("logging")

        with mock.patch.object(gov, "antigravity_usage", side_effect=fake_usage):
            tasks_fresh = {}
            gov._usage_tasks(ctx, tasks_fresh, False, [], start, end, fresh=True, now=now)
            tasks_fresh["usage"]()

            tasks_cached = {}
            gov._usage_tasks(ctx, tasks_cached, False, [], start, end, fresh=False, now=now)
            tasks_cached["usage"]()

        self.assertEqual(seen[0], (start, now, True))
        self.assertEqual(seen[1], (start, end, False))


class UsageEventsPushTest(unittest.TestCase):
    def setUp(self):
        ue.reset()

    def tearDown(self):
        ue.reset()

    def _scheduler_ctx(self, body, gov_overrides=None):
        g = {"enforcement_mode": "auto", "cap_model": "period", "period": "monthly", "reset_day": 1,
             "token_cap": 100000, "request_cap": 0, "cost_cap": 0.0,
             "usage_events_subscription": "projects/demo-project/subscriptions/ai-control-plane-agy-usage-push"}
        if gov_overrides:
            g.update(gov_overrides)
        ctx = support.FakeCtx(settings=support.make_settings(governance=g), body=body)
        ctx.identity = Identity("sa@demo-project.iam.gserviceaccount.com", "admin", SCHEDULER_MODE, "sched:sa")
        return ctx

    def test_rejects_non_scheduler_identity(self):
        ctx = support.FakeCtx(body=_push_body(_inference_entry()))
        with self.assertRaises(ApiError) as err:
            ue.handle(ctx)
        self.assertEqual(err.exception.code, "forbidden")

    def test_acks_invalid_non_inference_and_manual_mode_messages(self):
        # Invalid base64 / JSON -> 200 ack (do not retry malformed messages)
        ctx_bad = self._scheduler_ctx({"message": {"data": "not-base64"}})
        self.assertEqual(ue.handle(ctx_bad), {"acknowledged": True, "action": "ignored", "reason": "invalid"})

        # Other project -> ignored
        ctx_proj = self._scheduler_ctx(_push_body(_inference_entry(project="other-project")))
        self.assertEqual(ue.handle(ctx_proj), {"acknowledged": True, "action": "ignored", "reason": "not_inference_entry"})

        # Service account -> ignored (no per-user cap)
        ctx_sa = self._scheduler_ctx(_push_body(_inference_entry(person="bot@demo-project.iam.gserviceaccount.com")))
        self.assertEqual(ue.handle(ctx_sa), {"acknowledged": True, "action": "ignored", "reason": "not_inference_entry"})

        # Manual mode -> records heartbeat and ignores
        ctx_man = self._scheduler_ctx(_push_body(_inference_entry()), gov_overrides={"enforcement_mode": "manual"})
        with mock.patch.object(gov, "mutate_state") as mut:
            res = ue.handle(ctx_man)
        self.assertEqual(res, {"acknowledged": True, "action": "ignored", "reason": "manual_mode"})
        self.assertTrue(mut.called)

        # notify_only -> ignored
        ue.reset()
        ctx_notify = self._scheduler_ctx(_push_body(_inference_entry()),
                                         gov_overrides={"enforcement_backend": "notify_only"})
        with mock.patch.object(gov, "mutate_state"):
            res = ue.handle(ctx_notify)
        self.assertEqual(res, {"acknowledged": True, "action": "ignored", "reason": "notify_only"})

    def test_period_mode_waits_for_read_lag_then_triggers_local_enforcement(self):
        t0 = dt.datetime(2026, 11, 11, 10, 0, 0, tzinfo=UTC)
        entry = _inference_entry(person="alice@example.com", ts="2026-11-11T10:00:00Z", tokens=60000)
        usage = gov.Usage("logging")
        usage.ok = True
        usage.users["alice@example.com"] = {"last": dt.datetime(2026, 11, 11, 9, 55, 0, tzinfo=UTC)}
        status_payload = {
            "policy": {"windowStart": "2026-11-01T00:00:00Z", "nextReset": "2026-12-01T00:00:00Z",
                       "tokenCap": 100000, "requestCap": 0, "costCap": 0.0},
            "users": [{
                "email": "alice@example.com", "state": gov.EA, "overCap": False,
                "tokens": 50000, "requests": 5, "estimatedCost": 0.0,
                "caps": {"tokenCap": 100000, "requestCap": 0, "costCap": 0.0},
            }],
            "exceptions": [],
        }
        internal = {"usage": usage, "pricing": gov.pricing_from(support.make_settings()["governance"]["antigravity"]),
                    "credit": False, "orgUsedUp": False}

        # At t0 + 2s (< READ_LAG 10s): projects 50k + 60k = 110k >= 100k, returns 503 so Pub/Sub redelivers after READ_LAG
        ctx = self._scheduler_ctx(_push_body(entry))
        with mock.patch.object(gov, "utcnow", return_value=t0 + dt.timedelta(seconds=2)), \
             mock.patch.object(gov, "default_status", return_value=(status_payload, internal)), \
             mock.patch.object(gov, "mutate_state"), \
             mock.patch.object(gov, "request_enforcement") as req:
            with self.assertRaises(ApiError) as err:
                ue.handle(ctx)
        self.assertEqual(err.exception.status, 503)
        self.assertEqual(err.exception.code, "unavailable")
        req.assert_not_called()

        # Redelivered at t0 + 12s (>= READ_LAG): triggers immediate enforcement with trigger="usage"
        saved_state = {}
        def fake_mutate(_ctx, fn):
            fn(saved_state)
        with mock.patch.object(gov, "utcnow", return_value=t0 + dt.timedelta(seconds=12)), \
             mock.patch.object(gov, "default_status", return_value=(status_payload, internal)), \
             mock.patch.object(gov, "mutate_state", side_effect=fake_mutate), \
             mock.patch.object(gov, "request_enforcement", return_value={"via": "run", "started": True}) as req:
            res = ue.handle(ctx)
        self.assertEqual(res, {"acknowledged": True, "action": "enforcement_started"})
        req.assert_called_once_with(ctx, trigger="usage")
        self.assertEqual(saved_state["usageEvents"]["triggers"], 1)
        self.assertEqual(saved_state["usageEvents"]["lastTriggerVia"], "run")

    def test_credit_mode_projection_triggers_when_free_credit_and_paid_budget_exhausted(self):
        t0 = dt.datetime(2026, 11, 11, 10, 0, 0, tzinfo=UTC)
        # 1M prompt tokens of gemini-2.5-pro at $5/1M = $5; free left $1, paid budget $2 -> $4 paid > $2 -> over!
        entry = _inference_entry(person="bob@example.com", ts="2026-11-11T10:00:00Z",
                                 tokens=1000000, prompt=1000000, cand=0, cached=0, model="gemini-2.5-pro")
        usage = gov.Usage("logging")
        usage.ok = True
        usage.users["bob@example.com"] = {"last": dt.datetime(2026, 11, 11, 9, 50, 0, tzinfo=UTC)}
        table = PriceTable("USD", {"gemini-2.5-pro": {"input": 5.0, "output": 15.0, "cached": 0.5, "source": "catalog"}})
        pricing = gov.pricing_from(support.make_settings()["governance"]["antigravity"], table)
        status_payload = {
            "policy": {"windowStart": "2026-11-09T00:00:00Z", "nextReset": "2026-11-16T00:00:00Z"},
            "users": [{
                "email": "bob@example.com", "state": gov.EA, "overCap": False,
                "tokens": 1000, "requests": 1, "caps": {"tokenCap": 0, "requestCap": 0},
                "credit": {"costKnown": True, "creditWeek": 2.5, "freeUsedWeek": 1.5, "freeLeftWeek": 1.0,
                           "paidWeek": 0.0, "paidMonth": 0.0, "budget": 2.0},
            }],
            "exceptions": [],
        }
        internal = {"usage": usage, "pricing": pricing, "credit": True, "orgUsedUp": False}
        ctx = self._scheduler_ctx(_push_body(entry), gov_overrides={"cap_model": "credit_budget"})
        with mock.patch.object(gov, "utcnow", return_value=t0 + dt.timedelta(seconds=15)), \
             mock.patch.object(gov, "default_status", return_value=(status_payload, internal)), \
             mock.patch.object(gov, "mutate_state"), \
             mock.patch.object(gov, "request_enforcement", return_value={"via": "run", "started": True}) as req:
            res = ue.handle(ctx)
        self.assertEqual(res, {"acknowledged": True, "action": "enforcement_started"})
        req.assert_called_once()

    def test_iam_deny_job_deduplication_and_refresh(self):
        t0 = dt.datetime(2026, 11, 11, 10, 0, 0, tzinfo=UTC)
        entry = _inference_entry(person="alice@example.com", ts="2026-11-11T10:00:00Z", tokens=200000)
        usage = gov.Usage("logging")
        usage.ok = True
        usage.users["alice@example.com"] = {"last": dt.datetime(2026, 11, 11, 9, 55, 0, tzinfo=UTC)}
        status_payload = {
            "policy": {"windowStart": "2026-11-01T00:00:00Z", "nextReset": "2026-12-01T00:00:00Z",
                       "tokenCap": 100000, "requestCap": 0, "costCap": 0.0},
            "users": [{
                "email": "alice@example.com", "state": gov.EA, "overCap": False,
                "tokens": 0, "requests": 0, "estimatedCost": 0.0,
                "caps": {"tokenCap": 100000, "requestCap": 0, "costCap": 0.0},
            }],
            "exceptions": [],
        }
        internal = {"usage": usage, "pricing": gov.pricing_from(support.make_settings()["governance"]["antigravity"]),
                    "credit": False, "orgUsedUp": False}
        job_name = "projects/demo-project/locations/us-central1/jobs/ai-control-plane-enforcer"
        ctx = self._scheduler_ctx(_push_body(entry), gov_overrides={
            "enforcement_backend": "iam_deny", "enforcement_job": job_name
        })

        # 1. A running job started BEFORE entry_ts (created + JOB_STARTUP < need) -> wait (503)
        old_running = {"latest": {"createTime": "2026-11-11T09:59:40Z", "state": "RUNNING", "completionTime": None}}
        with mock.patch.object(gov, "utcnow", return_value=t0 + dt.timedelta(seconds=2)), \
             mock.patch.object(gov, "default_status", return_value=(status_payload, internal)), \
             mock.patch.object(gov, "mutate_state"), \
             mock.patch.object(ue.jobs, "status", return_value=old_running), \
             mock.patch.object(gov, "request_enforcement") as req:
            with self.assertRaises(ApiError) as err:
                ue.handle(ctx)
        self.assertEqual(err.exception.status, 503)
        req.assert_not_called()

        # 2. No active job -> starts job immediately (container startup covers READ_LAG)
        ue.reset()
        with mock.patch.object(gov, "utcnow", return_value=t0 + dt.timedelta(seconds=2)), \
             mock.patch.object(gov, "default_status", return_value=(status_payload, internal)), \
             mock.patch.object(gov, "mutate_state"), \
             mock.patch.object(ue.jobs, "status", return_value={"latest": None}), \
             mock.patch.object(gov, "request_enforcement", return_value={"via": "job", "started": True}) as req:
            res = ue.handle(ctx)
        self.assertEqual(res, {"acknowledged": True, "action": "enforcement_started"})
        req.assert_called_once_with(ctx, trigger="usage")

        # 3. Second event right after -> covered by the pending execution, no duplicate job start
        entry2 = _inference_entry(person="alice@example.com", ts="2026-11-11T10:00:01Z", tokens=200000, insert_id="ins-2")
        ctx2 = self._scheduler_ctx(_push_body(entry2), gov_overrides={
            "enforcement_backend": "iam_deny", "enforcement_job": job_name
        })
        with mock.patch.object(gov, "utcnow", return_value=t0 + dt.timedelta(seconds=3)), \
             mock.patch.object(gov, "default_status", return_value=(status_payload, internal)), \
             mock.patch.object(gov, "mutate_state"), \
             mock.patch.object(gov, "request_enforcement") as req:
            res2 = ue.handle(ctx2)
        self.assertEqual(res2, {"acknowledged": True, "action": "covered"})
        req.assert_not_called()


class PolicyChangeAndRunRequestTest(unittest.TestCase):
    def test_after_policy_change_triggers_only_in_auto_mode_for_policy_keys(self):
        ctx_auto = support.FakeCtx(settings=support.make_settings(governance={"enforcement_mode": "auto"}))
        ctx_manual = support.FakeCtx(settings=support.make_settings(governance={"enforcement_mode": "manual"}))

        with mock.patch.object(gov, "request_enforcement", return_value={"via": "run", "started": True}) as req:
            self.assertEqual(gov.after_policy_change(ctx_auto, ["cost_cap"]), {"via": "run", "started": True})
            req.assert_called_once_with(ctx_auto, trigger="admin")

            req.reset_mock()
            # Non-policy key (webhook_url) does not trigger enforcement
            self.assertIsNone(gov.after_policy_change(ctx_auto, ["webhook_url"]))
            # Manual mode does not trigger enforcement
            self.assertIsNone(gov.after_policy_change(ctx_manual, ["cost_cap", "period"]))
            req.assert_not_called()

    def test_system_settings_post_triggers_enforcement_in_auto_mode(self):
        settings = support.make_settings(governance={"enforcement_mode": "auto", "period": "monthly", "reset_day": 1})
        updated = support.make_settings(governance={"enforcement_mode": "auto", "period": "monthly", "reset_day": 15})
        ctx = support.FakeCtx(
            settings=settings,
            body={"section": "governance", "values": {"reset_day": 15}},
        )
        mgr = mock.Mock()
        mgr.base = {}
        mgr.snapshot.return_value = (updated, 2)
        mgr.overrides.return_value = {}
        mgr.persistence.return_value = {"backend": "memory", "durable": False}
        ctx.app.settings = mgr
        with mock.patch.object(gov, "after_policy_change", return_value={"via": "job", "started": True}) as hook:
            res = system_svc.settings_post(ctx)
        hook.assert_called_once()
        self.assertEqual(res["enforcement"], {"via": "job", "started": True})

    def test_exception_save_and_delete_trigger_enforcement_in_auto_mode(self):
        settings = support.make_settings(governance={"enforcement_mode": "auto"})
        ctx = support.FakeCtx(
            settings=settings,
            body={"email": "alice@example.com", "type": "limit", "tokenCap": 50000, "scope": "permanent"},
        )
        state = {"exceptions": [], "suspensions": []}

        def fake_mutate(_ctx, fn, required=False):  # pylint: disable=unused-argument
            fn(state)
            return state

        with mock.patch.object(gov, "_change_user", return_value=("alice@example.com", gov.EA, gov.EA, None)), \
             mock.patch.object(gov, "governance_state", side_effect=lambda _c: dict(state)), \
             mock.patch.object(gov, "mutate_state", side_effect=fake_mutate), \
             mock.patch.object(gov, "write_audit", return_value=True), \
             mock.patch.object(gov, "request_enforcement", return_value={"via": "run", "started": True}) as req:
            saved = gov.exception_save(ctx)
            self.assertEqual(saved["run"], {"via": "run", "started": True})
            req.assert_called_once_with(ctx, trigger="admin")

            req.reset_mock()
            del_ctx = support.FakeCtx(settings=settings, body={"email": "alice@example.com"})
            deleted = gov.exception_delete(del_ctx)
            self.assertEqual(deleted["run"], {"via": "run", "started": True})
            req.assert_called_once_with(del_ctx, trigger="admin")

    def test_claim_run_request_and_enforce_job_trigger(self):
        now = dt.datetime(2026, 11, 11, 10, 2, 0, tzinfo=UTC)
        state = {"runRequest": {"trigger": "usage", "at": "2026-11-11T10:00:00Z"}}
        ctx = support.FakeCtx()

        def fake_mutate(_ctx, fn):
            fn(state)
            return state

        with mock.patch.object(gov, "governance_state", side_effect=lambda _c: dict(state)), \
             mock.patch.object(gov, "mutate_state", side_effect=fake_mutate):
            self.assertEqual(gov.claim_run_request(ctx, now=now), "usage")
            # Consumed on first claim -> second run defaults to scheduler
            self.assertEqual(gov.claim_run_request(ctx, now=now), "scheduler")

        # Expired (> RUN_REQUEST_TTL) falls back to scheduler
        expired_state = {"runRequest": {"trigger": "admin", "at": "2026-11-11T09:50:00Z"}}
        with mock.patch.object(gov, "governance_state", side_effect=lambda _c: dict(expired_state)), \
             mock.patch.object(gov, "mutate_state", side_effect=lambda _c, fn: fn(expired_state)):
            self.assertEqual(gov.claim_run_request(ctx, now=now), "scheduler")

        # enforce_job.run_cycle uses claim_run_request trigger when --trigger=scheduler
        fake_settings = support.make_settings(governance={"enforcement_backend": "iam_deny"})
        fake_app = support.FakeApp()
        fake_app.settings = mock.Mock()
        fake_app.settings.snapshot.return_value = (fake_settings, 1)
        args = enforce_job.parse_args([])
        with mock.patch.object(enforce_job, "App", return_value=fake_app), \
             mock.patch.object(enforce_job, "check_ready"), \
             mock.patch.object(gov, "claim_run_request", return_value="usage"), \
             mock.patch.object(gov, "enforce_cycle", return_value={"result": "success", "changes": []}) as cyc:
            out, _ = enforce_job.run_cycle(args, environ={})
        self.assertEqual(out["result"], "success")
        self.assertEqual(out["trigger"], "usage")
        self.assertEqual(cyc.call_args.kwargs["trigger"], "usage")

    def test_sanitize_governance_and_enforcement_status_realtime_field(self):
        clean = settings_store.sanitize_governance({
            "usageEvents": {
                "lastEventAt": "2026-11-11T10:00:00Z",
                "lastTriggerAt": "2026-11-11T10:00:12Z",
                "lastTriggerVia": "job",
                "triggers": 7,
            },
            "runRequest": {"trigger": "usage", "at": "2026-11-11T10:00:12Z"},
        })
        self.assertEqual(clean["usageEvents"]["triggers"], 7)
        self.assertEqual(clean["usageEvents"]["lastTriggerVia"], "job")
        self.assertEqual(clean["runRequest"], {"trigger": "usage", "at": "2026-11-11T10:00:12Z"})

        bad = settings_store.sanitize_governance({
            "usageEvents": {"lastTriggerVia": "invalid", "triggers": -5},
            "runRequest": {"trigger": "bogus", "at": ""},
        })
        self.assertNotIn("runRequest", bad)
        self.assertNotIn("usageEvents", bad)

        # enforcement_status exposes realtime
        sub = "projects/demo-project/subscriptions/ai-control-plane-agy-usage-push"
        ctx = support.FakeCtx(settings=support.make_settings(governance={"usage_events_subscription": sub}))
        with mock.patch.object(gov, "governance_state", return_value=clean), \
             mock.patch.object(gov.jobs, "status", return_value={"configured": False}):
            st = gov.enforcement_status(ctx)
        self.assertTrue(st["realtime"]["configured"])
        self.assertEqual(st["realtime"]["triggers"], 7)
        self.assertEqual(st["realtime"]["lastTriggerVia"], "job")


if __name__ == "__main__":
    unittest.main()
