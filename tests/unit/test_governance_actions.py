"""Governance write paths: cap enforcement and per-user actions.

Upstream calls are replaced with fakes so the tests verify the safety properties without a
network: a single atomic etag-guarded setIamPolicy per cycle, one retry with a fresh plan on
409/ABORTED, aborts on failing gates, and NOT_ENFORCEABLE/UNKNOWN users are never modified.
"""

import copy
import unittest
from unittest import mock

import support
from support import ACCESS_ROLE, ADMIN_MARKER, CAP_MARKER

from controlplane.gcp import GcpError
from controlplane.httpkit import ApiError
from controlplane.security import uid_for
from controlplane.services import governance as gov

COND = {"title": "t", "expression": "true"}
POLICY = {"version": 3, "etag": "BwX1", "bindings": [
    {"role": ACCESS_ROLE, "members": ["user:heavy@example.com", "user:light@example.com", "user:owner@example.com",
                                      "group:team@example.com"]},
    {"role": "roles/owner", "members": ["user:owner@example.com"]},
    {"role": CAP_MARKER, "members": ["user:capped@example.com"]},
    {"role": ACCESS_ROLE, "members": ["user:cond@example.com"], "condition": COND},
]}
# (email, tokens, requests) in the governance window. Token cap 500000 / request cap 1000 (defaults).
USAGE = [("heavy@example.com", 600000, 40), ("light@example.com", 1000, 3), ("owner@example.com", 900000, 50),
         ("capped@example.com", 10, 1), ("cond@example.com", 700000, 10), ("grp@example.com", 800000, 20)]
MARKERS = [{"role": "CustomAntigravityCapBlocked", "exists": True},
           {"role": "CustomAntigravityAdminBlocked", "exists": True}]
NOT_ENFORCEABLE = {"owner@example.com", "cond@example.com", "grp@example.com"}


def make_usage(entries, ok=True):
    usage = gov.Usage("logging")
    usage.ok = ok
    for email, tokens, requests in entries:
        rec = usage.user(email)
        rec["tokens"], rec["requests"] = tokens, requests
        usage.records += requests
    return usage


def members(policy, role, conditional=False):
    out = set()
    for binding in policy["bindings"]:
        if binding["role"] == role and bool(binding.get("condition")) == conditional:
            out |= set(binding["members"])
    return out


class Harness:
    def __init__(self, policies=None, usage=None, markers=None, set_effect=None, ancestor=False):
        self.policies = list(policies or [POLICY])
        self.usage = usage if usage is not None else make_usage(USAGE)
        self.markers = markers or MARKERS
        self.set_effect = set_effect
        self.ancestor = ancestor
        self.patchers = []

    def _get_policy(self, ctx, project=None):  # pylint: disable=unused-argument
        item = self.policies.pop(0) if len(self.policies) > 1 else self.policies[0]
        if isinstance(item, Exception):
            raise item
        return copy.deepcopy(item)

    def __enter__(self):
        targets = {
            "antigravity_usage": mock.MagicMock(side_effect=lambda *a, **k: self.usage),
            "get_policy": mock.MagicMock(side_effect=self._get_policy),
            "set_policy": mock.MagicMock(side_effect=self.set_effect, return_value={}),
            "marker_status": mock.MagicMock(return_value=self.markers),
            "permission_status": mock.MagicMock(return_value={p: True for p in gov.REQUIRED_PERMISSIONS}),
            "make_ancestor_check": mock.MagicMock(return_value=lambda email: self.ancestor),
            "write_audit": mock.MagicMock(return_value=True),
            "send_webhook": mock.MagicMock(return_value=False),
            # Automatic token prices (billing export / price list) are not part of these tests.
            "price_table": mock.MagicMock(return_value=None),
        }
        for name, fake in targets.items():
            patcher = mock.patch.object(gov, name, fake)
            patcher.start()
            self.patchers.append(patcher)
            setattr(self, name, fake)
        return self

    def __exit__(self, *exc):
        for patcher in self.patchers:
            patcher.stop()
        return False

    def written_policies(self):
        return [call.args[1] for call in self.set_policy.call_args_list]


class CapTest(unittest.TestCase):
    def ctx(self, mode="apply", **governance):
        return support.FakeCtx(support.make_settings(governance=governance), body={"mode": mode})

    def test_dry_run_changes_nothing(self):
        with Harness() as h:
            out = gov.cap(self.ctx("dry_run"))
        h.set_policy.assert_not_called()
        self.assertFalse(out["executed"])
        self.assertEqual({(c["email"], c["action"], c["status"]) for c in out["changes"]},
                         {("heavy@example.com", "block", "skipped"), ("capped@example.com", "restore", "skipped")})
        self.assertEqual(sorted(out["plan"]["manual"]), sorted(uid_for(e) for e in NOT_ENFORCEABLE))
        self.assertEqual(h.write_audit.call_args.args[1], "cap_dry_run")
        self.assertEqual(h.write_audit.call_args.args[2]["result"], "dry_run")

    def test_apply_is_one_atomic_etag_guarded_write(self):
        ctx = self.ctx()
        with Harness() as h:
            out = gov.cap(ctx)
        self.assertEqual(h.set_policy.call_count, 1)
        written = h.written_policies()[0]
        self.assertEqual((written["etag"], written["version"]), ("BwX1", 3))
        self.assertNotIn("user:heavy@example.com", members(written, ACCESS_ROLE))
        self.assertIn("user:heavy@example.com", members(written, CAP_MARKER))
        self.assertIn("user:capped@example.com", members(written, ACCESS_ROLE))
        self.assertNotIn("user:capped@example.com", members(written, CAP_MARKER))
        # Not enforceable users and non-user grants are untouched.
        self.assertIn("user:owner@example.com", members(written, ACCESS_ROLE))
        self.assertIn("user:owner@example.com", members(written, "roles/owner"))
        self.assertEqual(members(written, ACCESS_ROLE, conditional=True), {"user:cond@example.com"})
        self.assertIn("group:team@example.com", members(written, ACCESS_ROLE))
        self.assertTrue(out["executed"])
        self.assertEqual({(c["email"], c["status"]) for c in out["changes"]},
                         {("heavy@example.com", "applied"), ("capped@example.com", "applied")})
        self.assertFalse(NOT_ENFORCEABLE & {c["email"] for c in out["changes"]})
        self.assertEqual(ctx.app.invalidations, 1)
        self.assertEqual(h.write_audit.call_args.args[2]["result"], "success")
        g5 = next(g for g in out["gates"] if g["id"] == "G5_NON_USER_GRANT_ABSENT")
        self.assertEqual(g5["status"], "warn")

    def test_conflict_retries_once_with_fresh_policy(self):
        fresh = dict(copy.deepcopy(POLICY), etag="BwX2")
        with Harness(policies=[POLICY, fresh], set_effect=[GcpError("conflict", status=409), {}]) as h:
            out = gov.cap(self.ctx())
        self.assertEqual(h.get_policy.call_count, 2)
        self.assertEqual([p["etag"] for p in h.written_policies()], ["BwX1", "BwX2"])
        self.assertTrue(out["executed"])

    def test_second_conflict_fails_without_further_writes(self):
        conflict = GcpError("conflict", status=409)
        with Harness(set_effect=[conflict, conflict]) as h:
            with self.assertLogs("controlplane.governance", level="INFO"):
                out = gov.cap(self.ctx())
        self.assertEqual(h.set_policy.call_count, 2)
        self.assertFalse(out["executed"])
        self.assertTrue(all(c["status"] == "failed" for c in out["changes"]))
        self.assertEqual(h.write_audit.call_args.args[2]["result"], "failed")

    def test_change_budget_gate_aborts(self):
        with Harness() as h:
            out = gov.cap(self.ctx(change_budget_min=0, change_budget_ratio=0.1))
        h.set_policy.assert_not_called()
        self.assertTrue(out["plan"]["aborted"])
        self.assertIn("G4_CHANGE_BUDGET", out["plan"]["abortReason"])
        self.assertTrue(all(c["status"] == "skipped" for c in out["changes"]))
        self.assertEqual(h.write_audit.call_args.args[2]["result"], "aborted")

    def test_unreadable_policy_aborts(self):
        with Harness(policies=[GcpError("denied", status=403)]) as h:
            out = gov.cap(self.ctx())
        h.set_policy.assert_not_called()
        self.assertTrue(out["plan"]["aborted"])
        g1 = next(g for g in out["gates"] if g["id"] == "G1_POLICY_READABLE")
        self.assertEqual(g1["status"], "fail")

    def test_missing_cap_marker_fails_blocks_but_applies_restores(self):
        markers = [{"role": "CustomAntigravityCapBlocked", "exists": False},
                   {"role": "CustomAntigravityAdminBlocked", "exists": True}]
        with Harness(markers=markers) as h:
            out = gov.cap(self.ctx())
        written = h.written_policies()[0]
        self.assertIn("user:heavy@example.com", members(written, ACCESS_ROLE))
        self.assertIn("user:capped@example.com", members(written, ACCESS_ROLE))
        statuses = {c["email"]: c["status"] for c in out["changes"]}
        self.assertEqual(statuses, {"heavy@example.com": "failed", "capped@example.com": "applied"})

    def test_unavailable_usage_never_writes(self):
        with Harness(usage=make_usage([], ok=False)) as h:
            ctx = self.ctx()
            out = gov.cap(ctx)
            status, _ = gov.compute_status(ctx, "daily", "calendar", 500000, 1000)
        h.set_policy.assert_not_called()
        self.assertEqual((out["plan"]["block"], out["plan"]["restore"]), ([], []))
        self.assertTrue(out["plan"]["withheldRestores"])
        capped = next(u for u in status["users"] if u["email"] == "capped@example.com")
        self.assertIsNone(capped["tokens"])
        self.assertIsNone(capped["overCap"])
        self.assertIsNone(status["manualActions"])

    def test_population_unknown_when_usage_and_policy_fail(self):
        with Harness(policies=[GcpError("denied", status=403)], usage=make_usage([], ok=False)):
            status, _ = gov.compute_status(self.ctx(), "daily", "calendar", 500000, 1000)
        self.assertIsNone(status["users"])
        self.assertEqual(status["population"], {"total": None, "byState": None, "enforceableCoverage": None})
        self.assertEqual(status["usageSource"], "unavailable")

    def test_invalid_mode(self):
        with self.assertRaises(ApiError):
            gov.cap(self.ctx("execute"))


class UserActionTest(unittest.TestCase):
    def act(self, harness, email, action):
        ctx = support.FakeCtx(body={"uid": uid_for(email), "action": action})
        with harness:
            return gov.user_action(ctx), ctx

    def assertConflict(self, harness, email, action):
        with self.assertRaises(ApiError) as caught:
            self.act(harness, email, action)
        self.assertEqual((caught.exception.code, caught.exception.status), ("conflict", 409))
        harness.set_policy.assert_not_called()
        return caught.exception

    def test_not_enforceable_users_are_refused(self):
        err = self.assertConflict(Harness(), "owner@example.com", "disallow")
        self.assertEqual(err.message, gov.REMEDIATION["PRIVILEGED_ROLE"])
        self.assertConflict(Harness(), "cond@example.com", "allow")
        self.assertConflict(Harness(), "grp@example.com", "allow")

    def test_unknown_users_are_refused(self):
        solo = {"version": 3, "etag": "e", "bindings": []}
        err = self.assertConflict(Harness(policies=[solo], usage=make_usage([("solo@example.com", 5, 1)]),
                                          ancestor=None), "solo@example.com", "allow")
        self.assertEqual(err.message, gov.REMEDIATION["ANCESTOR_UNCHECKED"])

    def test_disallow_enforceable_user(self):
        h = Harness()
        out, ctx = self.act(h, "light@example.com", "disallow")
        written = h.written_policies()[0]
        self.assertNotIn("user:light@example.com", members(written, ACCESS_ROLE))
        self.assertIn("user:light@example.com", members(written, ADMIN_MARKER))
        self.assertEqual(written["etag"], "BwX1")
        self.assertEqual((out["previousState"], out["newState"]), ("ENFORCEABLE_ALLOWED", "BLOCKED_BY_ADMIN"))
        self.assertTrue(out["auditLogged"])
        self.assertEqual(ctx.app.invalidations, 1)

    def test_disallow_requires_admin_marker(self):
        markers = [{"role": "CustomAntigravityCapBlocked", "exists": True},
                   {"role": "CustomAntigravityAdminBlocked", "exists": False}]
        self.assertConflict(Harness(markers=markers), "light@example.com", "disallow")

    def test_allow_capped_user(self):
        h = Harness()
        out, _ = self.act(h, "capped@example.com", "allow")
        written = h.written_policies()[0]
        self.assertIn("user:capped@example.com", members(written, ACCESS_ROLE))
        self.assertNotIn("user:capped@example.com", members(written, CAP_MARKER))
        self.assertEqual(out["newState"], "ENFORCEABLE_ALLOWED")

    def test_allow_no_access_user_with_checked_ancestors(self):
        h = Harness(usage=make_usage(USAGE + [("new@example.com", 5, 1)]),
                    policies=[{"version": 3, "etag": "e1", "bindings": []}])
        out, _ = self.act(h, "new@example.com", "allow")
        self.assertEqual(out["previousState"], "NO_ACCESS")
        self.assertIn("user:new@example.com", members(h.written_policies()[0], ACCESS_ROLE))

    def test_invalid_transition(self):
        self.assertConflict(Harness(), "light@example.com", "allow")

    def test_conflict_retry_rereads_policy(self):
        fresh = dict(copy.deepcopy(POLICY), etag="BwX9")
        h = Harness(policies=[POLICY, fresh], set_effect=[GcpError("conflict", status=409), {}])
        self.act(h, "light@example.com", "disallow")
        self.assertEqual(h.get_policy.call_count, 2)
        self.assertEqual([p["etag"] for p in h.written_policies()], ["BwX1", "BwX9"])

    def test_validation_and_not_found(self):
        for body in ({"uid": "zz", "action": "allow"}, {"uid": uid_for("light@example.com"), "action": "delete"}):
            with self.assertRaises(ApiError) as caught:
                with Harness():
                    gov.user_action(support.FakeCtx(body=body))
            self.assertEqual(caught.exception.code, "bad_request")
        with self.assertRaises(ApiError) as caught:
            self.act(Harness(), "nobody@example.com", "allow")
        self.assertEqual(caught.exception.code, "not_found")


if __name__ == "__main__":
    unittest.main()
