"""Customer-ready Antigravity caps: weekly periods anchored to the credit cycle, caps off until set, cached-token
prices, a scheduler watchdog, the monthly per-person usage report, and the install/teardown scripts that keep
suspended users from staying blocked (scheduler on by default, marker roles at install, restore at teardown)."""

import datetime as dt
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import support
from test_inference_log_users import OFFICIAL_SAMPLE, LoggingCtx
from test_scripts import ROOT, write_stub_bin

from controlplane import config, timeutil
from controlplane.httpkit import ApiError
from controlplane.services import governance as gov
from controlplane.services.pricing import PriceTable

UTC = dt.timezone.utc
SEOUL = timeutil.get_zone("Asia/Seoul")


class ResetAnchorTest(unittest.TestCase):
    def test_defaults_are_unchanged_monday_midnight(self):
        now = dt.datetime(2026, 11, 5, 3, 0, tzinfo=UTC)  # Thursday 12:00 KST
        start, _ = timeutil.governance_window("weekly", "calendar", now, tz="Asia/Seoul")
        self.assertEqual(start, dt.datetime(2026, 11, 2, 0, 0, tzinfo=SEOUL).astimezone(UTC))
        self.assertEqual(timeutil.governance_next_reset("weekly", "calendar", now, tz="Asia/Seoul"),
                         dt.datetime(2026, 11, 9, 0, 0, tzinfo=SEOUL).astimezone(UTC))

    def test_weekly_anchor_follows_the_credit_cycle(self):
        # Credit pool resets Wednesday 14:00 KST; it is Wednesday 13:00 KST, so the period began last Wednesday.
        now = dt.datetime(2026, 11, 4, 13, 0, tzinfo=SEOUL).astimezone(UTC)
        start, _ = timeutil.governance_window("weekly", "calendar", now, tz="Asia/Seoul", weekday=2, at="14:00")
        self.assertEqual(start, dt.datetime(2026, 10, 28, 14, 0, tzinfo=SEOUL).astimezone(UTC))
        self.assertEqual(timeutil.governance_next_reset("weekly", "calendar", now, tz="Asia/Seoul", weekday=2,
                                                        at="14:00"),
                         dt.datetime(2026, 11, 4, 14, 0, tzinfo=SEOUL).astimezone(UTC))
        after = dt.datetime(2026, 11, 4, 14, 1, tzinfo=SEOUL).astimezone(UTC)
        self.assertEqual(timeutil.governance_window("weekly", "calendar", after, tz="Asia/Seoul", weekday=2,
                                                    at="14:00")[0],
                         dt.datetime(2026, 11, 4, 14, 0, tzinfo=SEOUL).astimezone(UTC))

    def test_daily_and_monthly_reset_time(self):
        before = dt.datetime(2026, 11, 1, 8, 0, tzinfo=SEOUL).astimezone(UTC)
        self.assertEqual(timeutil.governance_window("daily", "calendar", before, tz="Asia/Seoul", at="09:00")[0],
                         dt.datetime(2026, 10, 31, 9, 0, tzinfo=SEOUL).astimezone(UTC))
        self.assertEqual(timeutil.governance_window("monthly", "calendar", before, tz="Asia/Seoul", at="09:00")[0],
                         dt.datetime(2026, 10, 1, 9, 0, tzinfo=SEOUL).astimezone(UTC))
        self.assertEqual(timeutil.governance_next_reset("monthly", "calendar", before, tz="Asia/Seoul", at="09:00"),
                         dt.datetime(2026, 11, 1, 9, 0, tzinfo=SEOUL).astimezone(UTC))

    def test_period_anchor_defaults_to_the_workspace_time_zone(self):
        settings = support.make_settings(governance={"reset_timezone": "", "reset_weekday": 2, "reset_time": "14:00"},
                                         workspace={"timezone": "Asia/Seoul"})
        self.assertEqual(gov.period_anchor(settings), {"tz": "Asia/Seoul", "weekday": 2, "at": "14:00"})

    def test_settings_validation(self):
        self.assertEqual(config.VALIDATORS["governance.antigravity.reset_weekday"](6), 6)
        self.assertEqual(config.VALIDATORS["governance.antigravity.reset_time"]("09:30"), "09:30")
        self.assertEqual(config.VALIDATORS["governance.antigravity.reset_timezone"](""), "")
        for bad in ("9:30", "24:00", "noon"):
            with self.assertRaises(config.Invalid):
                config.VALIDATORS["governance.antigravity.reset_time"](bad)
        with self.assertRaises(config.Invalid):
            config.VALIDATORS["governance.antigravity.reset_weekday"](7)


class ShippedDefaultsTest(unittest.TestCase):
    def test_caps_are_off_until_an_administrator_sets_one(self):
        ag = config.DEFAULTS["governance"]["antigravity"]
        self.assertEqual((ag["period"], ag["token_cap"], ag["request_cap"], ag["cost_cap"]), ("weekly", 0, 0, 0.0))
        self.assertEqual(ag["enforcement_mode"], "manual")
        self.assertEqual(gov.cap_check(10 ** 9, 10 ** 6, ag["token_cap"], ag["request_cap"], 1e6, ag["cost_cap"]),
                         (False, 0.0, []))


def sample_usage_rec():
    rec = {"requests": 1, "tokens": 85040, "promptTokens": 70825, "candidateTokens": 14215, "cachedTokens": 69079,
           "byModel": {}}
    gov.Usage.add_model(rec, "claude-opus-5-5", 1, 85040, 70825, 14215, 69079)
    return rec


class CachedTokenPriceTest(unittest.TestCase):
    def test_cached_prompt_tokens_use_the_cached_price(self):
        ag = support.make_settings(governance={"pricing_source": "manual", "model_prices": [
            {"model": "claude-opus-5-5", "input": 4.0, "output": 20.0, "cached": 0.2}]})["governance"]["antigravity"]
        cost, partial = gov.estimate_cost(sample_usage_rec(), gov.pricing_from(ag))
        # (70825 - 69079) x 4 + 69079 x 0.2 + 14215 x 20, per million tokens
        self.assertAlmostEqual(cost, (1746 * 4 + 69079 * 0.2 + 14215 * 20) / 1e6, places=6)
        self.assertFalse(partial)

    def test_without_a_cached_price_cached_tokens_count_as_input(self):
        ag = support.make_settings(governance={"pricing_source": "manual", "model_prices": [
            {"model": "claude-opus-5-5", "input": 4.0, "output": 20.0}]})["governance"]["antigravity"]
        cost, _ = gov.estimate_cost(sample_usage_rec(), gov.pricing_from(ag))
        self.assertAlmostEqual(cost, (70825 * 4 + 14215 * 20) / 1e6, places=6)

    def test_automatic_table_cached_price(self):
        # Price table keys are normalized model ids (claude-opus-5-5 -> claude-opus-5.5), as the price sync stores them.
        table = PriceTable("USD", {"claude-opus-5.5": {"input": 4.0, "output": 20.0, "cached": 0.2,
                                                       "source": "catalog"}})
        pricing = gov.pricing_from(support.make_settings()["governance"]["antigravity"], table)
        cost, _ = gov.estimate_cost(sample_usage_rec(), pricing)
        self.assertAlmostEqual(cost, (1746 * 4 + 69079 * 0.2 + 14215 * 20) / 1e6, places=6)
        self.assertEqual(pricing.lookup("claude-opus-5-5")["cached"], 0.2)

    def test_official_sample_is_aggregated_with_cached_tokens(self):
        ctx = LoggingCtx([OFFICIAL_SAMPLE])
        usage = gov._logging_usage(ctx, dt.datetime(2026, 10, 1, tzinfo=UTC), dt.datetime(2026, 11, 1, tzinfo=UTC))
        rec = usage.users["user@example.com"]
        self.assertEqual((rec["promptTokens"], rec["cachedTokens"], rec["candidateTokens"]), (70825, 69079, 14215))
        self.assertEqual(rec["byModel"]["claude-sonnet-5-5"]["cachedTokens"], 69079)

    def test_per_user_log_filter_is_case_insensitive(self):
        ctx = LoggingCtx([OFFICIAL_SAMPLE])
        gov._logging_usage(ctx, dt.datetime(2026, 10, 1, tzinfo=UTC), dt.datetime(2026, 11, 1, tzinfo=UTC),
                           email="user@example.com")
        flt = ctx.bodies[0]["filter"]
        self.assertIn('labels.user_id=~"(?i)^(user:)?user@example[.]com$"', flt)


class SchedulerWatchdogTest(unittest.TestCase):
    def ctx(self):
        settings = support.make_settings(governance={"scheduler_invokers": ["sched@demo.iam.gserviceaccount.com"]},
                                         security={"public_base_url": "https://cp.example.org"})
        return support.FakeCtx(settings)

    def test_stale_after_two_hours_without_a_scheduled_run(self):
        now = dt.datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
        ran = lambda minutes: {"lastScheduledRun": {"at": (now - dt.timedelta(minutes=minutes)).isoformat()}}
        self.assertTrue(gov.scheduler_status(self.ctx(), ran(180), now)["stale"])
        self.assertFalse(gov.scheduler_status(self.ctx(), ran(14), now)["stale"])
        self.assertFalse(gov.scheduler_status(self.ctx(), {}, now)["stale"])  # never ran yet (just deployed)
        unconfigured = support.FakeCtx(support.make_settings())
        self.assertFalse(gov.scheduler_status(unconfigured, ran(600), now)["stale"])


class BillingReportTest(unittest.TestCase):
    def report(self, users, month="2026-10", **governance):
        settings = support.make_settings(governance=dict({"pricing_source": "manual", "credit_per_user_month": 10.0,
                                                          "model_prices": [{"model": "claude-opus-5-5", "input": 4.0,
                                                                            "output": 20.0, "cached": 0.2}]},
                                                         **governance))
        ctx = support.FakeCtx(settings, params={"month": month} if month else {})
        usage = gov.Usage("bigquery")
        usage.ok = True
        for email, rec in users.items():
            usage.users[email] = rec
        with mock.patch.object(gov, "antigravity_usage", return_value=usage) as fake:
            out = gov.billing_report(ctx)
        return out, fake

    def test_per_person_cost_credit_and_amount_to_charge(self):
        heavy = {"requests": 0, "tokens": 0, "promptTokens": 0, "candidateTokens": 0, "cachedTokens": 0,
                 "clients": set(), "models": set(), "last": None, "byModel": {}}
        for _ in range(100):
            for key, val in (("requests", 1), ("tokens", 85040), ("promptTokens", 70825), ("candidateTokens", 14215),
                             ("cachedTokens", 69079)):
                heavy[key] += val
            gov.Usage.add_model(heavy, "claude-opus-5-5", 1, 85040, 70825, 14215, 69079)
        light = sample_usage_rec()
        out, fake = self.report({"heavy@example.com": heavy, "light@example.com": light})
        start, end = fake.call_args.args[1], fake.call_args.args[2]
        self.assertEqual((start, end), (dt.datetime(2026, 10, 1, tzinfo=UTC), dt.datetime(2026, 11, 1, tzinfo=UTC)))
        rows = {r["email"]: r for r in out["users"]}
        per_call = (1746 * 4 + 69079 * 0.2 + 14215 * 20) / 1e6
        self.assertAlmostEqual(rows["heavy@example.com"]["cost"], per_call * 100, places=4)
        self.assertAlmostEqual(rows["heavy@example.com"]["chargeable"], per_call * 100 - 10.0, places=4)
        self.assertEqual(rows["light@example.com"]["chargeable"], 0.0)  # inside the credit
        self.assertEqual(rows["heavy@example.com"]["cachedTokens"], 6907900)
        self.assertEqual(out["month"], "2026-10")
        self.assertTrue(out["complete"])
        self.assertAlmostEqual(out["totals"]["chargeable"], per_call * 100 - 10.0, places=4)

    def test_month_parameter_is_validated(self):
        for bad in ("2026-13", "26-10", "2026/10", "2099-01"):
            with self.assertRaises(ApiError):
                self.report({}, month=bad)


def run_bash(script, env_extra):
    env = dict(os.environ, **env_extra)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, check=False)


FAKE_POLICY_GCLOUD = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GCLOUD_LOG"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(args) + "\\n")
if args[:2] == ["projects", "get-iam-policy"]:
    print(json.dumps({"bindings": [
        {"role": "projects/demo/roles/CustomAntigravityCapBlocked",
         "members": ["user:capped@example.com", "serviceAccount:bot@demo.iam.gserviceaccount.com"]},
        {"role": "projects/demo/roles/CustomAntigravityCapBlocked", "members": ["user:conditional@example.com"],
         "condition": {"title": "t", "expression": "true"}},
        {"role": "projects/demo/roles/CustomAntigravityAdminBlocked", "members": ["user:blocked@example.com"]},
        {"role": "roles/discoveryengine.agentspaceUser", "members": ["user:active@example.com"]}]}))
    sys.exit(0)
if args[:3] == ["iam", "roles", "describe"] and args[3] == "roles/discoveryengine.agentspaceUser":
    print(json.dumps({"includedPermissions": ["discoveryengine.engines.get", "businessaicode.code.generate",
                                              "discoveryengine.sessions.create", "discoveryengine.unsupported.x"]}))
    sys.exit(0)
if args[:3] == ["iam", "roles", "describe"]:
    sys.exit(1)
if args[:3] == ["iam", "roles", "create"]:
    perms = args[args.index("--permissions") + 1]
    if "discoveryengine.unsupported.x" in perms:
        sys.stderr.write("ERROR: Permission discoveryengine.unsupported.x is not supported in custom roles.\\n")
        sys.exit(1)
    sys.exit(0)
sys.exit(0)
"""


class ScriptsKeepUsersOutOfPermanentBlocksTest(unittest.TestCase):
    def with_fake(self, body):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "calls.log")
            gcloud = os.path.join(tmp, "gcloud")
            with open(gcloud, "w", encoding="utf-8") as fh:
                fh.write(FAKE_POLICY_GCLOUD)
            os.chmod(gcloud, 0o755)
            res = run_bash(body, {"PATH": f"{tmp}:{os.environ.get('PATH', '')}", "FAKE_GCLOUD_LOG": log})
            calls = ""
            if os.path.exists(log):
                with open(log, encoding="utf-8") as fh:
                    calls = fh.read()
        return res, calls

    def lib(self):
        return 'source "%s"; ' % os.path.join(ROOT, "scripts/lib/common.sh")

    def test_teardown_restores_cap_suspended_users_first(self):
        res, calls = self.with_fake(self.lib() + "restore_capped_users demo")
        self.assertEqual(res.returncode, 0, msg=res.stderr)
        add = "projects add-iam-policy-binding demo --member user:capped@example.com --role " \
              "roles/discoveryengine.agentspaceUser --condition=None"
        remove = "projects remove-iam-policy-binding demo --member user:capped@example.com --role " \
                 "projects/demo/roles/CustomAntigravityCapBlocked --condition=None"
        self.assertIn(add, calls)
        self.assertIn(remove, calls)
        self.assertLess(calls.index(add), calls.index(remove))  # access first, so the GE app is never lost
        self.assertNotIn("conditional@example.com", calls)
        self.assertNotIn("serviceAccount:bot", calls.split("get-iam-policy", 1)[1])
        self.assertNotIn("user:blocked@example.com --role roles/discoveryengine.agentspaceUser", calls)
        self.assertIn("kept administrator block: user:blocked@example.com", res.stdout)

    def test_teardown_can_restore_administrator_blocks_too(self):
        _, calls = self.with_fake(self.lib() + "restore_capped_users demo roles/discoveryengine.agentspaceUser true")
        self.assertIn("projects add-iam-policy-binding demo --member user:blocked@example.com", calls)

    def test_teardown_dry_run_changes_nothing(self):
        res, calls = self.with_fake(self.lib() + "DRY_RUN=1 restore_capped_users demo")
        self.assertIn("[dry-run] restore user:capped@example.com", res.stdout)
        self.assertNotIn("add-iam-policy-binding", calls)

    def test_marker_roles_drop_businessaicode_and_unsupported_permissions(self):
        res, calls = self.with_fake(self.lib() + "ensure_antigravity_marker_roles demo")
        self.assertEqual(res.returncode, 0, msg=res.stderr)
        creates = [line for line in calls.splitlines() if line.startswith("iam roles create")]
        final = [line for line in creates if "discoveryengine.unsupported.x" not in line]
        self.assertEqual({line.split()[3] for line in final},
                         {"CustomAntigravityCapBlocked", "CustomAntigravityAdminBlocked"})
        for line in final:
            self.assertIn("discoveryengine.engines.get,discoveryengine.sessions.create", line)
            self.assertNotIn("businessaicode", line.split("--permissions", 1)[1])

    def test_marker_roles_dry_run(self):
        res, calls = self.with_fake(self.lib() + "DRY_RUN=1 ensure_antigravity_marker_roles demo")
        self.assertIn("[dry-run] Antigravity marker roles", res.stdout)
        self.assertEqual(calls, "")


class DeployDefaultsTest(unittest.TestCase):
    def deploy(self, *args, config_text=None, expect_ok=True):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "calls.log")
            env = dict(os.environ, PATH=f"{write_stub_bin(tmp, log)}:{os.environ.get('PATH', '')}")
            cmd = ["bash", os.path.join(ROOT, "scripts/deploy.sh")]
            if config_text is not None:
                path = os.path.join(tmp, "deploy.env")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write('PROJECT_ID="demo-hub-proj"\nADMIN_EMAILS="admin@example.org"\n' + config_text)
                cmd += ["--config", path]
            res = subprocess.run(cmd + list(args) + ["--dry-run"], capture_output=True, text=True, env=env,
                                 check=False)
        if expect_ok:
            self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        return res

    def test_enforcement_schedule_and_marker_roles_by_default(self):
        res = self.deploy("--project", "demo-hub-proj")
        # Every 5 minutes: a person over the limits is stopped within 5 minutes and restored soon after each reset.
        self.assertIn("Antigravity caps: enforcement runs */5 * * * *", res.stdout)
        self.assertIn("scheduler jobs create http ai-control-plane-enforce", res.stderr)
        self.assertIn("[dry-run] Antigravity marker roles CustomAntigravityCapBlocked and "
                      "CustomAntigravityAdminBlocked in demo-hub-proj", res.stdout)

    def test_schedule_can_be_turned_off_or_changed(self):
        res = self.deploy("--project", "demo-hub-proj", "--no-enforcement-schedule")
        self.assertIn("Antigravity caps: no enforcement schedule", res.stdout)
        self.assertNotIn("ai-control-plane-enforce", res.stderr)
        res = self.deploy("--project", "demo-hub-proj", "--enforcement-schedule", "*/15 * * * *")
        self.assertIn("enforcement runs */15 * * * *", res.stdout)
        res = self.deploy(config_text='ENFORCEMENT_SCHEDULE="off"\n')
        self.assertIn("no enforcement schedule", res.stdout)
        # Older deploy.env files carried ENFORCEMENT_SCHEDULE="": that keeps the default instead of turning it off.
        res = self.deploy(config_text='ENFORCEMENT_SCHEDULE=""\n')
        self.assertIn("enforcement runs */5 * * * *", res.stdout)

    def test_read_only_and_iap_installs_skip_the_default_schedule_without_failing(self):
        res = self.deploy("--project", "demo-hub-proj", "--read-only")
        self.assertNotIn("ai-control-plane-enforce", res.stderr)
        self.assertNotIn("Antigravity caps:", res.stdout)
        res = self.deploy("--project", "demo-hub-proj", "--access-mode", "iap")
        self.assertIn("IAP access mode: no Antigravity enforcement schedule", res.stderr)
        # An explicit schedule that cannot work still stops the deployment.
        res = self.deploy("--read-only", config_text='ENFORCEMENT_SCHEDULE="*/15 * * * *"\n', expect_ok=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("ENFORCEMENT_SCHEDULE needs ENABLE_GOVERNANCE_ACTIONS=true", res.stderr)

    def test_installer_flags_are_accepted(self):
        res = self.deploy("--project", "demo-hub-proj", "--dataset", "ai_logs", "--dataset-location", "US",
                          "--ge-locations", "global,us", "--no-log-prompts")
        self.assertIn("Audit dataset : demo-hub-proj:ai_logs (US", res.stdout)
        self.assertIn("GE Prompt Logging=false", res.stdout)


if __name__ == "__main__":
    unittest.main()
