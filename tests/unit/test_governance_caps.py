"""Per-user caps over a settlement period: estimated cost, exception rules, suspensions that end at
the period reset, manual review and the scheduled enforcement run.

Upstream calls are replaced with the fakes of test_governance_actions; the persistent governance
state uses a real SettingsManager over a MemoryStore.
"""

import copy
import datetime as dt
import unittest
from unittest import mock

import support
from support import ACCESS_ROLE, ADMIN_MARKER, CAP_MARKER
from test_governance_actions import POLICY, USAGE, Harness, make_usage, members

from controlplane import config, timeutil
from controlplane.httpkit import ApiError
from controlplane.security import Identity, scheduler_identity, uid_for
from controlplane.services import governance as gov
from controlplane.services.pricing import PriceTable
from controlplane.settings_store import MemoryStore, SettingsManager, sanitize_governance

UTC = dt.timezone.utc


def store():
    return SettingsManager(copy.deepcopy(config.DEFAULTS), MemoryStore())


def ctx_with(body=None, settings_store=None, identity=None, **governance):
    ctx = support.FakeCtx(support.make_settings(governance=governance), body=body or {})
    ctx.app.settings = settings_store or store()
    if identity is not None:
        ctx.identity = identity
    return ctx


def user_row(ctx, email, **governance):
    payload, _ = gov.compute_status(ctx, *(lambda ag: (ag["period"], ag["window"], ag["token_cap"],
                                                        ag["request_cap"]))(ctx.settings["governance"]["antigravity"]))
    return next(u for u in payload["users"] if u["email"] == email), payload


class PricingTest(unittest.TestCase):
    def test_cost_needs_a_configured_price(self):
        pricing = gov.pricing_from(support.make_settings()["governance"]["antigravity"])
        self.assertFalse(pricing.configured)
        self.assertEqual(gov.estimate_cost({"tokens": 10 ** 6, "promptTokens": 0, "candidateTokens": 0}, pricing),
                         (None, False))

    def test_model_prefix_prices_and_default(self):
        ag = support.make_settings(governance={
            "pricing_source": "manual", "input_price_per_million": 1.0, "output_price_per_million": 4.0,
            "model_prices": [{"model": "gemini-3.8-pro", "input": 2.0, "output": 12.0}]})["governance"]["antigravity"]
        pricing = gov.pricing_from(ag)
        rec = {"tokens": 0, "promptTokens": 0, "candidateTokens": 0, "byModel": {
            "gemini-3.8-pro-preview": {"tokens": 3 * 10 ** 6, "promptTokens": 10 ** 6, "candidateTokens": 10 ** 6},
            "gemini-3.8-flash": {"tokens": 2 * 10 ** 6, "promptTokens": 10 ** 6, "candidateTokens": 10 ** 6}}}
        cost, partial = gov.estimate_cost(rec, pricing)
        # pro: 1M in x 2 + 2M out (candidates + thinking) x 12 = 26; flash (default): 1 x 1 + 1 x 4 = 5
        self.assertEqual((cost, partial), (31.0, False))

    def test_unpriced_model_marks_the_estimate_partial(self):
        ag = support.make_settings(governance={"model_prices": [{"model": "gemini-3.8-pro", "input": 1, "output": 1}]})
        pricing = gov.pricing_from(ag["governance"]["antigravity"])
        rec = {"byModel": {"other-model": {"tokens": 100, "promptTokens": 50, "candidateTokens": 50},
                           "gemini-3.8-pro": {"tokens": 10 ** 6, "promptTokens": 0, "candidateTokens": 0}}}
        self.assertEqual(gov.estimate_cost(rec, pricing), (1.0, True))
        # No token of the user can be priced: the cost is unknown (null), not zero.
        only_unpriced = {"byModel": {"other-model": rec["byModel"]["other-model"]}}
        self.assertEqual(gov.estimate_cost(only_unpriced, pricing), (None, True))

    def test_cap_check_reports_reasons(self):
        self.assertEqual(gov.cap_check(10, 1, 100, 10, cost=5.0, cost_cap=4.0), (True, 1.25, ["cost"]))
        self.assertEqual(gov.cap_check(100, 10, 100, 10), (True, 1.0, ["tokens", "requests"]))
        self.assertEqual(gov.cap_check(10, 1, 100, 10, cost=None, cost_cap=4.0), (False, 0.1, []))
        # Only a cost cap and no price: usage against the cap is unknown, not 0%.
        self.assertEqual(gov.cap_check(10, 1, 0, 0, cost=None, cost_cap=4.0), (False, None, []))
        self.assertEqual(gov.cap_check(10, 1, 0, 0), (False, 0.0, []))

    def test_price_validation(self):
        check = config.VALIDATORS["governance.antigravity.model_prices"]
        self.assertEqual(check([{"model": "Gemini-3.8-Pro", "input": 1, "output": 2}]),
                         [{"model": "gemini-3.8-pro", "input": 1.0, "output": 2.0}])
        for bad in ([{"model": "x", "input": -1, "output": 0}], [{"model": "a b", "input": 1, "output": 1}],
                    [{"model": "m", "input": 1, "output": 1}, {"model": "m", "input": 2, "output": 2}],
                    [{"model": "m", "input": 1, "output": 1, "extra": 1}]):
            with self.assertRaises(config.Invalid):
                check(bad)
        with self.assertRaises(config.Invalid):
            config.VALIDATORS["governance.antigravity.currency"]("usd")
        self.assertIn("governance.antigravity.scheduler_invokers", config.UI_LOCKED_KEYS)


class SettlementWindowTest(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 31, 16, 30, tzinfo=UTC)  # 2026-11-01 01:30 in Asia/Seoul

    def test_monthly_calendar_in_a_time_zone(self):
        start, _ = timeutil.governance_window("monthly", "calendar", self.NOW, tz="Asia/Seoul")
        self.assertEqual(start, dt.datetime(2026, 10, 31, 15, 0, tzinfo=UTC))  # 2026-11-01 00:00 KST
        self.assertEqual(timeutil.governance_next_reset("monthly", "calendar", self.NOW, tz="Asia/Seoul"),
                         dt.datetime(2026, 11, 30, 15, 0, tzinfo=UTC))  # 2026-12-01 00:00 KST
        # UTC remains the default.
        self.assertEqual(timeutil.governance_window("monthly", "calendar", self.NOW)[0],
                         dt.datetime(2026, 10, 1, tzinfo=UTC))
        self.assertEqual(timeutil.governance_next_reset("monthly", "calendar", self.NOW),
                         dt.datetime(2026, 11, 1, tzinfo=UTC))

    def test_daily_weekly_and_rolling(self):
        self.assertEqual(timeutil.governance_next_reset("daily", "calendar", self.NOW),
                         dt.datetime(2026, 11, 1, tzinfo=UTC))
        self.assertEqual(timeutil.governance_next_reset("weekly", "calendar", self.NOW),
                         dt.datetime(2026, 11, 2, tzinfo=UTC))  # next Monday
        self.assertIsNone(timeutil.governance_next_reset("monthly", "rolling", self.NOW))
        self.assertEqual(timeutil.governance_period_end("weekly", "rolling", self.NOW),
                         self.NOW.replace(second=0) + dt.timedelta(days=7))


class StateStoreTest(unittest.TestCase):
    def test_sanitize_bounds_and_types(self):
        clean = sanitize_governance({
            "exceptions": [{"email": "A@Example.com", "type": "limit", "tokenCap": 5, "costCap": "x"},
                           {"email": "a@example.com", "type": "allow"}, {"email": "bad", "type": "allow"},
                           {"email": "b@example.com", "type": "nope"}],
            "suspensions": [{"email": "c@example.com", "trigger": "weird", "reasons": ["cost", "x"]}],
            "lastRun": {"at": "2026-10-20T00:00:00Z", "blocked": 2, "result": "success"}})
        self.assertEqual([(e["email"], e["type"], e["tokenCap"], e["costCap"]) for e in clean["exceptions"]],
                         [("a@example.com", "limit", 5, None)])
        self.assertEqual(clean["suspensions"][0]["trigger"], "admin")
        self.assertEqual(clean["suspensions"][0]["reasons"], ["cost"])
        self.assertEqual(clean["lastRun"]["blocked"], 2)

    def test_mutate_does_not_bump_the_settings_version(self):
        mgr = store()
        version = mgr.snapshot()[1]
        mgr.mutate_governance(lambda s: s["exceptions"].append({"email": "x@example.com", "type": "allow"}), "t")
        self.assertEqual(mgr.snapshot()[1], version)
        self.assertEqual(mgr.governance_state()["exceptions"][0]["email"], "x@example.com")
        self.assertNotIn("governance", mgr.overrides())


def u(uid, state, over, exception=None, suspension=None):
    return {"uid": uid, "state": state, "overCap": over, "exception": exception, "suspension": suspension}


class PlanTest(unittest.TestCase):
    CFG = gov.gov_config(support.make_settings())
    NOW = dt.datetime(2026, 11, 1, 0, 5, tzinfo=UTC)

    def evaluate(self, users, mode="auto", telemetry_ok=True, records=10):
        return gov.evaluate(users, True, telemetry_ok, records, [], self.CFG, ACCESS_ROLE, mode=mode, now=self.NOW)[1]

    def test_manual_mode_queues_over_cap_users_for_review(self):
        plan = self.evaluate([u("a", "ENFORCEABLE_ALLOWED", True), u("b", "ENFORCEABLE_ALLOWED", False)], "manual")
        self.assertEqual((plan["block"], plan["review"]), ([], ["a"]))
        plan = self.evaluate([u("a", "ENFORCEABLE_ALLOWED", True)], "auto")
        self.assertEqual((plan["block"], plan["review"]), (["a"], []))

    def test_exception_rules(self):
        plan = self.evaluate([
            u("allow_over", "ENFORCEABLE_ALLOWED", True, {"type": "allow"}),
            u("allow_capped", "CAPPED_BY_TOOL", True, {"type": "allow"}),
            u("block_ea", "ENFORCEABLE_ALLOWED", False, {"type": "block"}),
            u("block_capped", "CAPPED_BY_TOOL", True, {"type": "block"}),
            u("block_done", "BLOCKED_BY_ADMIN", False, {"type": "block"}),
            u("block_ne", "NOT_ENFORCEABLE", False, {"type": "block"})], telemetry_ok=False, records=0)
        self.assertEqual(plan["block"], [])
        self.assertEqual(plan["restore"], ["allow_capped"])  # explicit decision, not gated by G3
        self.assertEqual(plan["disallow"], ["block_ea", "block_capped"])
        self.assertEqual(plan["manual"], ["block_ne"])

    def test_period_end_restores_without_telemetry(self):
        ended = {"until": "2026-11-01T00:00:00Z"}
        running = {"until": "2026-12-01T00:00:00Z"}
        plan = self.evaluate([u("ended", "CAPPED_BY_TOOL", None, suspension=ended),
                              u("running", "CAPPED_BY_TOOL", None, suspension=running),
                              u("legacy", "CAPPED_BY_TOOL", False)], telemetry_ok=False, records=0)
        self.assertEqual(plan["restore"], ["ended"])
        self.assertTrue(plan["withheldRestores"])
        # Known to be over the cap again in the new period: stays suspended.
        plan = self.evaluate([u("ended", "CAPPED_BY_TOOL", True, suspension=ended)])
        self.assertEqual(plan["restore"], [])


def usage_with_cost():
    usage = make_usage(USAGE)
    for email, rec in usage.users.items():
        rec["byModel"] = {"gemini-3.8-pro": {"tokens": rec["tokens"], "promptTokens": 0, "candidateTokens": 0}}
    return usage


class StatusTest(unittest.TestCase):
    def test_cost_cap_individual_limit_and_review_queue(self):
        mgr = store()
        mgr.mutate_governance(lambda s: s["exceptions"].append(
            {"email": "light@example.com", "type": "limit", "tokenCap": 500}), "t")
        ctx = ctx_with(settings_store=mgr, cost_cap=5.0, pricing_source="manual", output_price_per_million=10.0,
                       token_cap=0, request_cap=0)
        with Harness(usage=usage_with_cost()):
            heavy, payload = user_row(ctx, "heavy@example.com")
            light = next(x for x in payload["users"] if x["email"] == "light@example.com")
        self.assertEqual((heavy["estimatedCost"], heavy["overCapReasons"]), (6.0, ["cost"]))
        self.assertEqual(heavy["plannedAction"], "review")  # manual mode is the default
        self.assertEqual([r["email"] for r in payload["reviewQueue"]], ["light@example.com", "heavy@example.com"])
        self.assertEqual(light["caps"]["tokenCap"], 500)
        self.assertEqual((light["overCap"], light["overCapReasons"]), (True, ["tokens"]))
        self.assertEqual(payload["exceptions"][0]["type"], "limit")
        self.assertEqual(payload["policy"]["enforcementMode"], "manual")
        self.assertEqual(payload["totals"]["estimatedCost"], round(sum(t for _, t, _ in USAGE) * 10 / 1e6, 6))

    def test_cost_cap_without_prices_warns_and_is_not_applied(self):
        ctx = ctx_with(cost_cap=5.0, token_cap=0, request_cap=0)
        with Harness(usage=usage_with_cost()):
            heavy, payload = user_row(ctx, "heavy@example.com")
        self.assertIsNone(heavy["estimatedCost"])
        self.assertFalse(heavy["overCap"])
        self.assertIsNone(payload["totals"]["estimatedCost"])
        self.assertIn("pricing_not_configured", [w["code"] for w in ctx.warnings])

    def test_automatic_prices_estimate_in_the_cap_currency(self):
        table = PriceTable("KRW", {"gemini-3.8-pro": {"input": 1000.0, "output": 10000.0, "source": "billing"}},
                           {"from": "USD", "to": "KRW", "rate": 1358.75, "asOf": "2026-10-20T00:00:00Z"},
                           {"billing": 1, "catalog": 0})
        ctx = ctx_with(cost_cap=5000.0, currency="KRW", token_cap=0, request_cap=0)
        with Harness(usage=usage_with_cost()) as h:
            h.price_table.return_value = table
            heavy, payload = user_row(ctx, "heavy@example.com")
        h.price_table.assert_called_with(ctx, "KRW")
        policy = payload["policy"]
        self.assertEqual((policy["currency"], policy["currencySetting"], policy["costCap"]), ("KRW", "KRW", 5000.0))
        self.assertEqual((policy["pricing"]["source"], policy["pricing"]["counts"]), ("auto", {"billing": 1, "catalog": 0}))
        self.assertEqual(policy["pricing"]["fx"]["rate"], 1358.75)
        self.assertEqual(policy["pricing"]["used"], [{"model": "gemini-3.8-pro", "price": {
            "input": 1000.0, "output": 10000.0, "source": "billing", "complete": True}}])
        # 600,000 output tokens at 10,000 KRW per 1M tokens = 6,000 KRW, above the 5,000 KRW cap.
        self.assertEqual((heavy["estimatedCost"], heavy["overCapReasons"]), (6000.0, ["cost"]))

    def test_price_loading_failure_is_only_a_warning(self):
        ctx = ctx_with(cost_cap=5000.0, token_cap=0, request_cap=0)
        with Harness(usage=usage_with_cost()) as h:
            h.price_table.side_effect = ApiError("bad_request", "The query is too expensive.")
            heavy, payload = user_row(ctx, "heavy@example.com")
        self.assertIsNone(heavy["estimatedCost"])
        self.assertFalse(heavy["overCap"])
        self.assertEqual(payload["policy"]["currencySetting"], "")
        codes = [w["code"] for w in ctx.warnings]
        self.assertIn("pricing_unavailable", codes)
        self.assertIn("pricing_not_configured", codes)

    def test_suspended_user_shows_restore_time(self):
        mgr = store()
        mgr.mutate_governance(lambda s: s["suspensions"].append(
            {"email": "capped@example.com", "since": "2026-10-20T00:00:00Z", "until": "2999-01-01T00:00:00Z",
             "trigger": "auto", "reasons": ["tokens"]}), "t")
        ctx = ctx_with(settings_store=mgr)
        with Harness():
            capped, _ = user_row(ctx, "capped@example.com")
        self.assertEqual(capped["suspension"]["trigger"], "auto")
        self.assertEqual(capped["restoreAt"], "2999-01-01T00:00:00Z")
        self.assertEqual(capped["plannedAction"], "restore")  # under the cap with healthy telemetry


class EnforceTest(unittest.TestCase):
    def test_manual_mode_never_blocks_and_announces_review_once(self):
        ctx = ctx_with(body={})
        with Harness() as h:
            out = gov.enforce(ctx)
            again = gov.enforce(ctx)
        written = h.written_policies()
        self.assertEqual(len(written), 2)  # the restore of capped@ (first run) ... and nothing to block
        self.assertIn("user:heavy@example.com", members(written[0], ACCESS_ROLE))
        self.assertEqual(out["enforcementMode"], "manual")
        self.assertEqual(out["newReview"], ["heavy@example.com"])
        self.assertEqual(again["newReview"], [])
        state = ctx.app.settings.governance_state()
        self.assertEqual(state["lastRun"]["mode"], "manual")
        self.assertEqual(state["lastRun"]["review"], 1)
        self.assertEqual(h.write_audit.call_args.args[1], "enforce")

    def test_auto_mode_suspends_until_the_period_reset(self):
        ctx = ctx_with(body={}, enforcement_mode="auto", period="monthly", reset_timezone="Asia/Seoul")
        ctx.identity = Identity("sched@demo-project.iam.gserviceaccount.com", "admin", "scheduler", "scheduler:x")
        with Harness() as h:
            out = gov.enforce(ctx)
        written = h.written_policies()[0]
        self.assertIn("user:heavy@example.com", members(written, CAP_MARKER))
        self.assertEqual(out["trigger"], "scheduler")
        state = ctx.app.settings.governance_state()
        record = next(s for s in state["suspensions"] if s["email"] == "heavy@example.com")
        expected = timeutil.governance_next_reset("monthly", "calendar", tz="Asia/Seoul")
        self.assertEqual(record["until"], gov.iso(expected))
        self.assertEqual((record["trigger"], record["reasons"]), ("auto", ["tokens"]))
        self.assertEqual(state["lastScheduledRun"]["blocked"], 1)

    def test_block_exception_is_enforced_in_every_cycle(self):
        mgr = store()
        mgr.mutate_governance(lambda s: s["exceptions"].append({"email": "light@example.com", "type": "block"}), "t")
        ctx = ctx_with(body={}, settings_store=mgr)
        with Harness() as h:
            out = gov.enforce(ctx)
        written = h.written_policies()[0]
        self.assertIn("user:light@example.com", members(written, ADMIN_MARKER))
        self.assertNotIn("user:light@example.com", members(written, ACCESS_ROLE))
        self.assertIn(("light@example.com", "disallow", "applied"),
                      {(c["email"], c["action"], c["status"]) for c in out["changes"]})

    def test_dry_run_records_nothing(self):
        ctx = ctx_with(body={"dryRun": True}, enforcement_mode="auto")
        with Harness() as h:
            out = gov.enforce(ctx)
        h.set_policy.assert_not_called()
        self.assertEqual(out["mode"], "dry_run")
        self.assertIsNone(ctx.app.settings.governance_state()["lastRun"])
        with self.assertRaises(ApiError):
            gov.enforce(ctx_with(body={"dryRun": "yes"}))


class ReviewTest(unittest.TestCase):
    def test_suspend_from_review_queue(self):
        ctx = ctx_with(body={"decision": "suspend", "uids": [uid_for("heavy@example.com"), uid_for("light@example.com")]})
        with Harness() as h:
            out = gov.review(ctx)
        statuses = {r["email"]: r["status"] for r in out["results"]}
        self.assertEqual(statuses, {"heavy@example.com": "applied", "light@example.com": "skipped"})
        self.assertIn("user:heavy@example.com", members(h.written_policies()[0], CAP_MARKER))
        record = ctx.app.settings.governance_state()["suspensions"][0]
        self.assertEqual((record["email"], record["trigger"]), ("heavy@example.com", "review"))

    def test_allow_for_the_rest_of_the_period(self):
        ctx = ctx_with(body={"decision": "allow", "uids": [uid_for("heavy@example.com"), uid_for("capped@example.com")],
                             "note": "release week"})
        with Harness() as h:
            out = gov.review(ctx)
        self.assertEqual({r["email"]: r["status"] for r in out["results"]},
                         {"heavy@example.com": "applied", "capped@example.com": "applied"})
        self.assertIn("user:capped@example.com", members(h.written_policies()[0], ACCESS_ROLE))
        rules = {e["email"]: e for e in ctx.app.settings.governance_state()["exceptions"]}
        self.assertEqual(set(rules), {"heavy@example.com", "capped@example.com"})
        self.assertTrue(all(r["type"] == "allow" and r["until"] and r["note"] == "release week"
                            for r in rules.values()))

    def test_validation(self):
        for body in ({"decision": "ban", "uids": [uid_for("a@example.com")]}, {"decision": "allow", "uids": []},
                     {"decision": "allow", "uids": ["zz"]}):
            with self.assertRaises(ApiError):
                with Harness():
                    gov.review(ctx_with(body=body))


class ExceptionTest(unittest.TestCase):
    def test_block_rule_disallows_now_and_delete_restores(self):
        mgr = store()
        ctx = ctx_with(body={"email": "light@example.com", "type": "block", "note": "left the project"},
                       settings_store=mgr)
        with Harness() as h:
            out = gov.exception_save(ctx)
        self.assertEqual(out["change"], {"action": "disallow", "previousState": "ENFORCEABLE_ALLOWED",
                                         "newState": "BLOCKED_BY_ADMIN"})
        self.assertIn("user:light@example.com", members(h.written_policies()[0], ADMIN_MARKER))
        blocked = copy.deepcopy(POLICY)
        blocked["bindings"][0]["members"].remove("user:light@example.com")
        blocked["bindings"].append({"role": ADMIN_MARKER, "members": ["user:light@example.com"]})
        ctx = ctx_with(body={"email": "light@example.com"}, settings_store=mgr)
        with Harness(policies=[blocked]) as h:
            out = gov.exception_delete(ctx)
        self.assertEqual(out["change"]["action"], "allow")
        self.assertIn("user:light@example.com", members(h.written_policies()[0], ACCESS_ROLE))
        self.assertEqual(mgr.governance_state()["exceptions"], [])

    def test_allow_rule_restores_a_suspended_user(self):
        ctx = ctx_with(body={"email": "capped@example.com", "type": "allow", "scope": "period"})
        with Harness() as h:
            out = gov.exception_save(ctx)
        self.assertEqual(out["change"]["action"], "restore")
        self.assertIn("user:capped@example.com", members(h.written_policies()[0], ACCESS_ROLE))
        self.assertEqual(out["exception"]["scope"], "period")

    def test_limit_rule_changes_no_binding(self):
        ctx = ctx_with(body={"email": "light@example.com", "type": "limit", "tokenCap": 2000000, "costCap": 50})
        with Harness() as h:
            out = gov.exception_save(ctx)
        h.set_policy.assert_not_called()
        self.assertIsNone(out["change"])
        self.assertEqual((out["exception"]["tokenCap"], out["exception"]["costCap"]), (2000000, 50.0))

    def test_not_enforceable_user_cannot_be_blocked(self):
        with self.assertRaises(ApiError) as caught:
            with Harness():
                gov.exception_save(ctx_with(body={"email": "owner@example.com", "type": "block"}))
        self.assertEqual(caught.exception.code, "conflict")

    def test_validation(self):
        for body in ({"email": "light@example.com", "type": "maybe"},
                     {"email": "light@example.com", "type": "limit"},
                     {"email": "svc@demo-project.iam.gserviceaccount.com", "type": "allow"},
                     {"email": "light@example.com", "type": "limit", "tokenCap": -1},
                     {"type": "allow"}):
            with self.assertRaises(ApiError):
                with Harness():
                    gov.exception_save(ctx_with(body=body))
        with self.assertRaises(ApiError) as caught:
            with Harness():
                gov.exception_delete(ctx_with(body={"email": "nobody@example.com"}))
        self.assertEqual(caught.exception.code, "not_found")


class UserActionSyncTest(unittest.TestCase):
    def test_disallow_records_a_block_rule(self):
        ctx = ctx_with(body={"uid": uid_for("light@example.com"), "action": "disallow"})
        with Harness():
            out = gov.user_action(ctx)
        self.assertEqual(out["exception"]["type"], "block")
        self.assertEqual(ctx.app.settings.governance_state()["exceptions"][0]["email"], "light@example.com")

    def test_early_restore_allows_for_the_rest_of_the_period(self):
        ctx = ctx_with(body={"uid": uid_for("capped@example.com"), "action": "allow"})
        with Harness():
            out = gov.user_action(ctx)
        self.assertEqual((out["exception"]["type"], out["exception"]["scope"]), ("allow", "period"))


class SchedulerAuthTest(unittest.TestCase):
    PATH = "/api/v1/governance/antigravity/enforce"
    SA = "enforcer@demo-project.iam.gserviceaccount.com"

    def settings(self, invokers=(SA,), base="https://cp.example.com"):
        settings = support.make_settings(governance={"scheduler_invokers": list(invokers)})
        settings["security"]["public_base_url"] = base
        return settings

    def test_accepts_an_allowed_service_account(self):
        verifier = mock.MagicMock()
        verifier.verify_google_oidc.return_value = self.SA
        identity = scheduler_identity({"Authorization": "Bearer a.b.c"}, self.settings(), verifier, self.PATH)
        self.assertEqual((identity.email, identity.role, identity.auth_mode), (self.SA, "admin", "scheduler"))
        audiences = verifier.verify_google_oidc.call_args.args[1]
        self.assertIn("https://cp.example.com" + self.PATH, audiences)

    def test_rejections(self):
        verifier = mock.MagicMock()
        verifier.verify_google_oidc.return_value = "other@demo-project.iam.gserviceaccount.com"
        with self.assertLogs("controlplane.security", level="WARNING"):
            with self.assertRaises(ApiError) as caught:
                scheduler_identity({"Authorization": "Bearer a.b.c"}, self.settings(), verifier, self.PATH)
        self.assertEqual(caught.exception.code, "forbidden")
        verifier.verify_google_oidc.side_effect = ValueError("bad signature")
        with self.assertLogs("controlplane.security", level="WARNING"):
            with self.assertRaises(ApiError) as caught:
                scheduler_identity({"Authorization": "Bearer a.b.c"}, self.settings(), verifier, self.PATH)
        self.assertEqual(caught.exception.code, "unauthenticated")
        for settings in (self.settings(invokers=()), self.settings(base="")):
            with self.assertRaises(ApiError) as caught:
                scheduler_identity({"Authorization": "Bearer a.b.c"}, settings, verifier, self.PATH)
            self.assertEqual(caught.exception.code, "unauthenticated")

    def test_real_verifier_rejects_unsigned_or_wrong_algorithm(self):
        from controlplane.security import IapVerifier
        verifier = IapVerifier()
        for token in ("", "a.b", "eyJhbGciOiJub25lIn0.e30.", "eyJhbGciOiJFUzI1NiJ9.e30.sig"):
            with self.assertRaises(ValueError):
                verifier.verify_google_oidc(token, ["https://cp.example.com"])
        with self.assertRaises(ValueError):
            verifier.verify_google_oidc("eyJhbGciOiJSUzI1NiJ9.e30.sig", [])


class SchedulerRouteTest(unittest.TestCase):
    """HTTP level: only the enforce route accepts a scheduler bearer token, and only that path skips
    the cookie-based CSRF check."""
    SA = "enforcer@demo-project.iam.gserviceaccount.com"

    @classmethod
    def setUpClass(cls):
        import os
        import tempfile
        import threading
        from controlplane import app as app_module
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = os.path.join(cls.tmp.name, "config.yaml")
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write("workspace:\n  project_id: demo-project\nstorage:\n  backend: memory\nsecurity:\n"
                     "  auth_mode: local\n  public_base_url: https://cp.example.com\ngovernance:\n  antigravity:\n"
                     "    scheduler_invokers: [%s]\n" % cls.SA)
        with mock.patch.dict(os.environ, {"CONFIG_PATH": cfg}):
            cls.app = app_module.App(environ={"CONFIG_PATH": cfg})
        cls.app.verifier.verify_google_oidc = mock.MagicMock(return_value=cls.SA)
        cls.server = app_module.make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def post(self, path, headers):
        import http.client
        import json
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request("POST", path, body=b"{}", headers=dict({"Content-Type": "application/json"}, **headers))
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read() or b"{}")
        finally:
            conn.close()

    def test_bearer_only_on_the_enforce_route(self):
        with Harness():
            status, body = self.post("/api/v1/governance/antigravity/enforce", {"Authorization": "Bearer a.b.c"})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["data"]["trigger"], body["data"]["enforcementMode"]), ("scheduler", "manual"))
        self.assertEqual(self.app.settings.governance_state()["lastScheduledRun"]["trigger"], "scheduler")
        audiences = self.app.verifier.verify_google_oidc.call_args.args[1]
        self.assertIn("https://cp.example.com/api/v1/governance/antigravity/enforce", audiences)
        # The same bearer token is not accepted on other write routes (cookie session + CSRF required).
        status, body = self.post("/api/v1/governance/antigravity/review", {"Authorization": "Bearer a.b.c"})
        self.assertEqual((status, body["error"]["code"]), (403, "csrf_failed"))
        # Without a bearer token the enforce route keeps the session + CSRF path.
        status, body = self.post("/api/v1/governance/antigravity/enforce", {})
        self.assertEqual((status, body["error"]["code"]), (403, "csrf_failed"))


if __name__ == "__main__":
    unittest.main()
