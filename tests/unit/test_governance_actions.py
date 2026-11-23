"""Governance write paths: cap enforcement and per-user actions.

Upstream calls are replaced with fakes so the tests verify the safety properties without a
network: a single atomic etag-guarded setIamPolicy per cycle, one retry with a fresh plan on
409/ABORTED, aborts on failing gates, and NOT_ENFORCEABLE/UNKNOWN users are never modified.
"""

import copy
import os
import re
import unittest
from unittest import mock

import support
from support import ACCESS_ROLE, ADMIN_MARKER, CAP_MARKER

from controlplane.gcp import GcpError
from controlplane.httpkit import ApiError
from controlplane.security import Identity, uid_for
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


IAM_ROLES = "https://iam.googleapis.com/v1/projects/%s/roles" % support.PROJECT
# The access role: Gemini Enterprise permissions plus businessaicode.* (Antigravity), which marker roles leave out.
BASE_PERMISSIONS = ["discoveryengine.assistants.assist", "discoveryengine.sessions.get", "discoveryengine.widget.a",
                    "discoveryengine.widget.b", "discoveryengine.widget.c", "businessaicode.completions.generate"]


class FakeIam:
    """The IAM custom role API behind ctx.rest: read a role, create a role, undelete a role.

    roles maps a role id to "live", "deleted" (deleted in the last 7 days: readable with deleted=true, restorable)
    or "purging" (deleted earlier: not readable, but the id cannot be used for a new role). unsupported lists
    permissions that custom roles reject, reported one per create call. fail maps an operation ("base", "get",
    "create", "undelete"), or (operation, role id), to the GcpError it raises."""

    def __init__(self, roles=None, unsupported=(), fail=None):
        self.roles = dict(roles or {})
        self.unsupported = tuple(unsupported)
        self.fail = dict(fail or {})
        self.calls = []
        self.created = {}
        self.undeleted = {}

    def _op(self, op, role_id=None):
        self.calls.append((op, role_id))
        err = self.fail.get((op, role_id)) or self.fail.get(op)
        if err is not None:
            raise err

    def __call__(self, method, url, **kwargs):
        body = kwargs.get("json_body") or {}
        if method == "GET" and url == "https://iam.googleapis.com/v1/" + ACCESS_ROLE:
            self._op("base")
            return {"name": ACCESS_ROLE, "includedPermissions": list(BASE_PERMISSIONS)}
        if method == "GET" and url.startswith(IAM_ROLES + "/"):
            role_id = url[len(IAM_ROLES) + 1:]
            self._op("get", role_id)
            if self.roles.get(role_id) not in ("live", "deleted"):
                raise GcpError("not_found", status=404)
            role = {"name": "projects/%s/roles/%s" % (support.PROJECT, role_id), "etag": "etag-" + role_id}
            if self.roles[role_id] == "deleted":
                role["deleted"] = True
            return role
        if method == "POST" and url.startswith(IAM_ROLES + "/") and url.endswith(":undelete"):
            role_id = url[len(IAM_ROLES) + 1:-len(":undelete")]
            self._op("undelete", role_id)
            self.roles[role_id] = "live"
            self.undeleted[role_id] = body
            return {}
        if method == "POST" and url == IAM_ROLES:
            role_id = body["roleId"]
            self._op("create", role_id)
            if role_id in self.roles:
                raise GcpError("conflict", status=409, message="A role named %s already exists." % role_id)
            rejected = [p for p in body["role"]["includedPermissions"] if p in self.unsupported]
            if rejected:
                raise GcpError("invalid", status=400, message="Permission %s is not valid." % rejected[0])
            self.roles[role_id] = "live"
            self.created[role_id] = list(body["role"]["includedPermissions"])
            return {}
        raise AssertionError("unexpected REST call %s %s" % (method, url))

    def ops(self, op):
        return [role_id for name, role_id in self.calls if name == op]


class Harness:
    def __init__(self, policies=None, usage=None, markers=None, set_effect=None, ancestor=False, iam=None):
        self.policies = list(policies or [POLICY])
        self.usage = usage if usage is not None else make_usage(USAGE)
        self.markers = markers or MARKERS
        self.set_effect = set_effect
        self.ancestor = ancestor
        self.iam = iam  # a FakeIam: marker roles are then read from it (the real marker_status) instead of `markers`
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
            # Neither are license tiers (Gemini Enterprise license API): everyone gets credit_per_user_month.
            "license_credits": mock.MagicMock(return_value={}),
        }
        if self.iam is not None:
            del targets["marker_status"]
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

    def audit_actions(self):
        return [call.args[1] for call in self.write_audit.call_args_list]


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

    def test_change_budget_gate_defers_suspensions_but_restores(self):
        with Harness() as h:
            out = gov.cap(self.ctx(change_budget_min=0, change_budget_ratio=0.0))
        self.assertFalse(out["plan"]["aborted"])
        self.assertTrue(out["plan"]["deferredBlocks"])
        g4 = next(g for g in out["gates"] if g["id"] == "G4_CHANGE_BUDGET")
        self.assertEqual(g4["status"], "warn")
        deferred = [c for c in out["changes"] if c["status"] == "skipped"]
        self.assertTrue(deferred)
        self.assertTrue(all(c["action"] == "block" and c["message"].startswith("Deferred") for c in deferred))
        # The suspended user below the caps is still restored in the same run.
        applied = [(c["email"], c["action"]) for c in out["changes"] if c["status"] == "applied"]
        self.assertEqual(applied, [("capped@example.com", "restore")])
        written = h.written_policies()[0]
        self.assertIn("user:capped@example.com", members(written, ACCESS_ROLE))
        self.assertNotIn("user:capped@example.com", members(written, CAP_MARKER))
        for c in deferred:   # deferred suspensions leave access untouched in this run
            self.assertNotIn("user:%s" % c["email"], members(written, CAP_MARKER))
        self.assertEqual(h.write_audit.call_args.args[2]["result"], "partial")

    def test_change_budget_suspends_the_furthest_over_first_and_the_rest_next_run(self):
        pol = {"version": 3, "etag": "BwX1", "bindings": [
            {"role": ACCESS_ROLE, "members": ["user:a@example.com", "user:b@example.com", "user:c@example.com"]}]}
        usage = make_usage([("c@example.com", 600000, 5), ("a@example.com", 900000, 5), ("b@example.com", 700000, 5)])
        with Harness(policies=[pol], usage=usage) as h:
            out = gov.cap(self.ctx(change_budget_min=2, change_budget_ratio=0.0))
        g4 = next(g for g in out["gates"] if g["id"] == "G4_CHANGE_BUDGET")
        self.assertEqual(g4["status"], "warn")
        self.assertFalse(out["plan"]["aborted"])
        self.assertEqual(sorted(c["email"] for c in out["changes"] if c["status"] == "applied"),
                         ["a@example.com", "b@example.com"])   # token cap 500000: a 1.8x, b 1.4x, c 1.2x
        self.assertEqual([c["email"] for c in out["changes"] if c["status"] == "skipped"], ["c@example.com"])
        written = h.written_policies()[0]
        self.assertEqual(members(written, CAP_MARKER), {"user:a@example.com", "user:b@example.com"})
        # The next run (the scheduler calls every few minutes) suspends the one that waited.
        with Harness(policies=[written], usage=usage) as h2:
            out2 = gov.cap(self.ctx(change_budget_min=2, change_budget_ratio=0.0))
        self.assertEqual([(c["email"], c["action"], c["status"]) for c in out2["changes"]],
                         [("c@example.com", "block", "applied")])
        self.assertEqual(next(g for g in out2["gates"] if g["id"] == "G4_CHANGE_BUDGET")["status"], "pass")
        self.assertEqual(members(h2.written_policies()[0], CAP_MARKER),
                         {"user:a@example.com", "user:b@example.com", "user:c@example.com"})

    def test_unreadable_policy_aborts(self):
        with Harness(policies=[GcpError("denied", status=403)]) as h:
            out = gov.cap(self.ctx())
        h.set_policy.assert_not_called()
        self.assertTrue(out["plan"]["aborted"])
        g1 = next(g for g in out["gates"] if g["id"] == "G1_POLICY_READABLE")
        self.assertEqual(g1["status"], "fail")

    def test_missing_cap_marker_is_created_when_an_administrator_runs(self):
        iam = FakeIam(roles={"CustomAntigravityAdminBlocked": "live"})
        ctx = self.ctx()
        ctx.rest = iam
        with Harness(iam=iam) as h:
            out = gov.cap(ctx)
        self.assertEqual(iam.roles["CustomAntigravityCapBlocked"], "live")
        self.assertEqual(iam.ops("create"), ["CustomAntigravityCapBlocked"])
        written = h.written_policies()[0]
        self.assertIn("user:heavy@example.com", members(written, CAP_MARKER))
        self.assertNotIn("user:heavy@example.com", members(written, ACCESS_ROLE))
        statuses = {c["email"]: c["status"] for c in out["changes"]}
        self.assertEqual(statuses, {"heavy@example.com": "applied", "capped@example.com": "applied"})
        # Creating the role is recorded in the audit log before the run itself.
        self.assertEqual(h.audit_actions(), ["setup", "cap_apply"])
        self.assertEqual(h.write_audit.call_args_list[0].args[2]["trigger"], "on_demand")

    def test_cap_marker_that_cannot_be_created_fails_blocks_but_applies_restores(self):
        iam = FakeIam(roles={"CustomAntigravityAdminBlocked": "live"},
                      fail={"create": GcpError("denied", status=403)})
        ctx = self.ctx()
        ctx.rest = iam
        with Harness(iam=iam) as h:
            out = gov.cap(ctx)
        written = h.written_policies()[0]
        self.assertIn("user:heavy@example.com", members(written, ACCESS_ROLE))
        self.assertEqual(members(written, CAP_MARKER), set())
        self.assertIn("user:capped@example.com", members(written, ACCESS_ROLE))
        changes = {c["email"]: c for c in out["changes"]}
        self.assertEqual({e: c["status"] for e, c in changes.items()},
                         {"heavy@example.com": "failed", "capped@example.com": "applied"})
        self.assertIn("roles/iam.roleAdmin", changes["heavy@example.com"]["message"])

    def test_dry_run_creates_no_role(self):
        iam = FakeIam()
        ctx = self.ctx("dry_run")
        ctx.rest = iam
        with Harness(iam=iam) as h:
            gov.cap(ctx)
        h.set_policy.assert_not_called()
        self.assertEqual(iam.ops("create") + iam.ops("undelete"), [])
        self.assertEqual(iam.roles, {})

    def test_scheduled_run_creates_no_role(self):
        iam = FakeIam(roles={"CustomAntigravityAdminBlocked": "live"})
        ctx = support.FakeCtx(support.make_settings(governance={"enforcement_mode": "auto"}))
        ctx.identity = Identity("sched@demo-project.iam.gserviceaccount.com", "admin", "scheduler", "scheduler:x")
        ctx.rest = iam
        with Harness(iam=iam) as h:
            out = gov.enforce(ctx)
        self.assertEqual(out["trigger"], "scheduler")
        self.assertEqual(iam.ops("create") + iam.ops("undelete"), [])
        self.assertNotIn("CustomAntigravityCapBlocked", iam.roles)
        changes = {c["email"]: c for c in out["changes"]}
        self.assertEqual({e: c["status"] for e, c in changes.items()},
                         {"heavy@example.com": "failed", "capped@example.com": "applied"})
        self.assertEqual(changes["heavy@example.com"]["message"],
                         "Marker role CustomAntigravityCapBlocked does not exist. Run setup first.")
        self.assertNotIn("setup", h.audit_actions())

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

    def block(self, iam, email="light@example.com"):
        """An administrator selects Block for `email` on a project whose custom roles are `iam`."""
        ctx = support.FakeCtx(body={"uid": uid_for(email), "action": "disallow"})
        ctx.rest = iam
        h = Harness(iam=iam)
        with h:
            return gov.user_action(ctx), ctx, h

    def assertBlockRefused(self, iam):
        ctx = support.FakeCtx(body={"uid": uid_for("light@example.com"), "action": "disallow"})
        ctx.rest = iam
        h = Harness(iam=iam)
        with self.assertRaises(ApiError) as caught:
            with h:
                gov.user_action(ctx)
        self.assertEqual((caught.exception.code, caught.exception.status), ("conflict", 409))
        h.set_policy.assert_not_called()  # the person keeps access: nothing is half done
        return caught.exception.message

    def test_block_creates_the_missing_marker_roles(self):
        """The reported case: no marker role in the project, an administrator selects Block."""
        iam = FakeIam()
        out, ctx, h = self.block(iam)
        self.assertEqual((out["previousState"], out["newState"]), ("ENFORCEABLE_ALLOWED", "BLOCKED_BY_ADMIN"))
        written = h.written_policies()[0]
        self.assertIn("user:light@example.com", members(written, ADMIN_MARKER))
        self.assertNotIn("user:light@example.com", members(written, ACCESS_ROLE))
        # Both roles are created like the setup button does, with the access role's permissions minus Antigravity.
        self.assertEqual(iam.roles, {"CustomAntigravityCapBlocked": "live", "CustomAntigravityAdminBlocked": "live"})
        kept = [p for p in BASE_PERMISSIONS if not p.startswith("businessaicode.")]
        self.assertEqual(iam.created["CustomAntigravityAdminBlocked"], kept)
        self.assertEqual(iam.ops("base"), [None])  # the access role is read once for both roles
        self.assertEqual(h.audit_actions(), ["setup", "disallow"])
        setup_entry = h.write_audit.call_args_list[0].args[2]
        self.assertEqual((setup_entry["trigger"], setup_entry["result"]), ("on_demand", "success"))
        self.assertEqual([r["status"] for r in setup_entry["results"]], ["created", "created"])

    def test_block_restores_a_deleted_marker_role(self):
        iam = FakeIam(roles={"CustomAntigravityCapBlocked": "live", "CustomAntigravityAdminBlocked": "deleted"})
        out, _, h = self.block(iam)
        self.assertEqual(out["newState"], "BLOCKED_BY_ADMIN")
        self.assertEqual(iam.undeleted, {"CustomAntigravityAdminBlocked": {"etag": "etag-CustomAntigravityAdminBlocked"}})
        self.assertEqual(iam.ops("create") + iam.ops("base"), [])
        self.assertIn("user:light@example.com", members(h.written_policies()[0], ADMIN_MARKER))

    def test_block_needs_only_the_admin_marker(self):
        """The cap marker cannot be made usable; blocking uses the admin marker and still works."""
        iam = FakeIam(roles={"CustomAntigravityCapBlocked": "purging"})
        out, _, h = self.block(iam)
        self.assertEqual(out["newState"], "BLOCKED_BY_ADMIN")
        self.assertEqual(iam.roles["CustomAntigravityAdminBlocked"], "live")
        setup_entry = h.write_audit.call_args_list[0].args[2]
        self.assertEqual(setup_entry["result"], "partial")
        self.assertEqual({r["role"]: r["status"] for r in setup_entry["results"]},
                         {"CustomAntigravityCapBlocked": "failed", "CustomAntigravityAdminBlocked": "created"})

    def test_block_refused_when_the_service_account_may_not_manage_roles(self):
        expected = ("Role CustomAntigravityAdminBlocked is needed to stop Antigravity for a person, but the dashboard "
                    "service account is not allowed to read or create custom roles in this project. Grant it "
                    "roles/iam.roleAdmin and try again.")
        denied = GcpError("denied", status=403)
        # Not allowed to read roles, or allowed to read but not to create or to restore: the same remedy.
        for iam in (FakeIam(fail={"get": denied}), FakeIam(fail={"create": denied}),
                    FakeIam(roles={"CustomAntigravityAdminBlocked": "deleted"}, fail={"undelete": denied})):
            self.assertEqual(self.assertBlockRefused(iam), expected)

    def test_block_refused_when_the_role_id_is_reserved_by_a_deleted_role(self):
        iam = FakeIam(roles={"CustomAntigravityAdminBlocked": "purging"})
        message = self.assertBlockRefused(iam)
        self.assertEqual(message,
                         "Role CustomAntigravityAdminBlocked is needed to stop Antigravity for a person, but Google "
                         "Cloud does not accept this role ID right now, which happens for up to 44 days after a role "
                         "with the same ID was deleted. Enter another role ID in Settings > Governance policy.")
        # Read, create (ID in use), read again, create again: then it is the ID of a deleted role, not a race.
        admin_calls = [op for op, role_id in iam.calls if role_id == "CustomAntigravityAdminBlocked"]
        self.assertEqual(admin_calls, ["get", "get", "create", "get", "create"])  # the first read is marker_status

    def test_block_refused_with_the_reason_for_other_failures(self):
        lead = "Role CustomAntigravityAdminBlocked is needed to stop Antigravity for a person, but "
        cases = (
            (FakeIam(roles={"CustomAntigravityAdminBlocked": "deleted"},
                     fail={"undelete": GcpError("unavailable", status=503)}),
             "it is deleted and could not be restored (unavailable). Restore it in IAM > Roles, or enter another "
             "role ID in Settings > Governance policy."),
            (FakeIam(fail={"create": GcpError("unavailable", status=503)}),
             "it could not be prepared (unavailable). Try again in a moment."),
            (FakeIam(fail={"get": GcpError("timeout")}), "it could not be prepared (timeout). Try again in a moment."),
        )
        for iam, tail in cases:
            self.assertEqual(self.assertBlockRefused(iam), lead + tail)
        # The access role cannot be read, so the new role cannot be given its permissions (logged for the operator).
        with self.assertLogs("controlplane.governance", level="WARNING"):
            message = self.assertBlockRefused(FakeIam(fail={"base": GcpError("denied", status=403)}))
        self.assertEqual(message, lead + "it could not be created because the permissions of %s could not be read."
                         % ACCESS_ROLE)

    def test_role_created_by_someone_else_meanwhile_is_used(self):
        """Create answers that the ID is in use because another administrator created the role a moment ago."""
        iam = FakeIam()
        original = iam._op  # pylint: disable=protected-access

        def racing(op, role_id=None):
            original(op, role_id)
            if op == "create" and role_id not in iam.roles:
                iam.roles[role_id] = "live"
                raise GcpError("conflict", status=409)
        iam._op = racing  # pylint: disable=protected-access
        out, _, h = self.block(iam)
        self.assertEqual(out["newState"], "BLOCKED_BY_ADMIN")
        statuses = [r["status"] for r in h.write_audit.call_args_list[0].args[2]["results"]]
        self.assertEqual(statuses, ["exists", "exists"])

    def test_existing_marker_roles_are_not_touched(self):
        iam = FakeIam(roles={"CustomAntigravityCapBlocked": "live", "CustomAntigravityAdminBlocked": "live"})
        _, ctx, h = self.block(iam)
        self.assertEqual(iam.ops("create") + iam.ops("undelete") + iam.ops("base"), [])
        self.assertEqual(h.audit_actions(), ["disallow"])
        self.assertEqual(ctx.app.invalidations, 1)

    def test_review_suspension_creates_the_missing_cap_marker(self):
        iam = FakeIam(roles={"CustomAntigravityAdminBlocked": "live"})
        ctx = support.FakeCtx(body={"decision": "suspend", "uids": [uid_for("heavy@example.com")]})
        ctx.rest = iam
        with Harness(iam=iam) as h:
            out = gov.review(ctx)
        self.assertEqual([(r["email"], r["status"]) for r in out["results"]], [("heavy@example.com", "applied")])
        self.assertEqual(iam.ops("create"), ["CustomAntigravityCapBlocked"])
        self.assertIn("user:heavy@example.com", members(h.written_policies()[0], CAP_MARKER))

    def test_review_suspension_fails_with_the_reason_when_the_cap_marker_cannot_be_created(self):
        iam = FakeIam(roles={"CustomAntigravityAdminBlocked": "live"}, fail={"create": GcpError("denied", status=403)})
        ctx = support.FakeCtx(body={"decision": "suspend", "uids": [uid_for("heavy@example.com")]})
        ctx.rest = iam
        with Harness(iam=iam) as h:
            out = gov.review(ctx)
        h.set_policy.assert_not_called()
        self.assertEqual(out["results"][0]["status"], "failed")
        self.assertIn("roles/iam.roleAdmin", out["results"][0]["message"])
        self.assertEqual(iam.ops("create"), ["CustomAntigravityCapBlocked"])  # tried once, not once per person

    def test_setup_restores_deleted_roles_and_creates_missing_ones(self):
        iam = FakeIam(roles={"CustomAntigravityCapBlocked": "deleted"})
        ctx = support.FakeCtx()
        ctx.rest = iam
        with Harness(iam=iam) as h:
            out = gov.setup(ctx)
        self.assertEqual([(r["role"], r["status"]) for r in out["results"]],
                         [("CustomAntigravityCapBlocked", "restored"), ("CustomAntigravityAdminBlocked", "created")])
        self.assertEqual(iam.roles, {"CustomAntigravityCapBlocked": "live", "CustomAntigravityAdminBlocked": "live"})
        self.assertEqual(h.write_audit.call_args.args[2]["trigger"], "admin")
        self.assertTrue(out["auditLogged"])

    def test_setup_drops_every_permission_custom_roles_reject(self):
        """Three permissions are rejected, reported one per answer: each attempt drops the named one and tries again."""
        rejected = ("discoveryengine.widget.a", "discoveryengine.widget.b", "discoveryengine.widget.c")
        iam = FakeIam(roles={"CustomAntigravityCapBlocked": "live"}, unsupported=rejected)
        ctx = support.FakeCtx()
        ctx.rest = iam
        with Harness(iam=iam):
            out = gov.setup(ctx)
        self.assertEqual(out["results"][1], {"role": "CustomAntigravityAdminBlocked", "status": "created",
                                             "message": "Created with 2 permissions (businessaicode.* excluded)."})
        self.assertEqual(iam.created["CustomAntigravityAdminBlocked"],
                         ["discoveryengine.assistants.assist", "discoveryengine.sessions.get"])
        self.assertEqual(len(iam.ops("create")), 4)

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


class MarkerMessageTest(unittest.TestCase):
    """What the server says about an unavailable marker role reaches a Korean administrator in Korean: each sentence
    matches one template of core/messages.js, and that template has a Korean translation."""

    JS = os.path.join(support.SRC, "web", "assets", "js")

    def read(self, *parts):
        with open(os.path.join(self.JS, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_every_sentence_has_one_template_and_a_translation(self):
        templates = []
        for pattern, key in re.findall(r"^\s*\[/(\^.*\$)/, '(.*)'\],\s*$", self.read("core", "messages.js"),
                                       re.MULTILINE):
            try:
                templates.append((re.compile(pattern), key))
            except re.error:  # a JavaScript-only construct in an unrelated template
                continue
        korean = self.read("i18n", "ko.js")
        cfg = gov.gov_config(support.make_settings())
        role = cfg.admin_marker_id
        failures = [gov._marker_failed(role, "", "denied"),  # pylint: disable=protected-access
                    gov._marker_failed(role, "", "unavailable", reason="deleted"),  # pylint: disable=protected-access
                    gov._marker_failed(role, "", "conflict", reason="reserved"),  # pylint: disable=protected-access
                    gov._marker_failed(role, "", "unreadable", reason="base_role"),  # pylint: disable=protected-access
                    gov._marker_failed(role, "", "timeout")]  # pylint: disable=protected-access
        sentences = [gov.marker_unavailable(f, cfg) for f in failures]
        self.assertEqual(len(set(sentences)), len(failures))
        sentences.append("The deleted role could not be restored (unavailable).")
        for sentence in sentences:
            keys = [key for pattern, key in templates if pattern.match(sentence)]
            self.assertEqual(len(keys), 1, sentence)
            self.assertIn('\n  "%s": "' % keys[0], korean)
        for fixed in ("The deleted role was restored.", "The role ID is still reserved by a deleted role."):
            self.assertIn('\n  "%s": "' % fixed, korean)


class FullAiBlockAndConflictRepairTest(unittest.TestCase):
    """Tests administrator repair of leftover roles/discoveryengine.user conflicts and full AI service + dashboard
    revocation (POST /api/v1/users/access-action)."""

    def test_admin_disallow_repairs_unconditional_discoveryengine_user_conflict(self):
        pol = {"version": 3, "etag": "e1", "bindings": [
            {"role": ADMIN_MARKER, "members": ["user:dual@example.com"]},
            {"role": "roles/discoveryengine.user", "members": ["user:dual@example.com"]},
            {"role": "roles/aiplatform.user", "members": ["user:dual@example.com"]},
        ]}
        h = Harness(policies=[pol])
        with h:
            ctx = support.FakeCtx(body={"uid": uid_for("dual@example.com"), "action": "disallow"})
            out = gov.user_action(ctx)
        self.assertEqual(out["newState"], "BLOCKED_BY_ADMIN")
        written = h.written_policies()[0]
        self.assertIn("user:dual@example.com", members(written, ADMIN_MARKER))
        self.assertNotIn("user:dual@example.com", members(written, "roles/discoveryengine.user"))
        self.assertIn("user:dual@example.com", members(written, "roles/aiplatform.user"))

    def test_block_all_removes_every_ai_role_blocks_dashboard_and_restores_cleanly(self):
        from controlplane.auth import GoogleSignIn
        from controlplane.settings_store import MemoryStore, SettingsManager
        from controlplane.services import users as users_svc

        base = support.make_settings(security={"auth_mode": "google", "admins": ["admin@example.com"],
                                               "allowed_users": ["*"]})
        mgr = SettingsManager(base, MemoryStore())
        signin = GoogleSignIn(base, {"SESSION_SECRET": "x" * 40}, settings_manager=mgr)
        session_token = signin.sessions.sign({"email": "target@example.com", "name": "Target", "picture": "",
                                              "sub": "123", "ims": 1}, 3600)
        headers = {"Cookie": "cp_session=" + session_token}
        self.assertIsNotNone(signin.user_from_headers(headers, base))

        pol_initial = {"version": 3, "etag": "e1", "bindings": [
            {"role": ADMIN_MARKER, "members": ["user:target@example.com"]},
            {"role": "roles/discoveryengine.user", "members": ["user:target@example.com"]},
            {"role": "roles/aiplatform.user", "members": ["user:target@example.com"]},
            {"role": "roles/viewer", "members": ["user:target@example.com"]},
        ]}
        h = Harness(policies=[pol_initial])
        with h:
            ctx = support.FakeCtx(settings=base, body={"uid": uid_for("target@example.com"), "action": "block_all"})
            ctx.app.settings = mgr
            blocked = users_svc.access_action(ctx)

        self.assertTrue(blocked["aiBlocked"])
        self.assertEqual(sorted(blocked["removedRoles"]),
                         sorted([ADMIN_MARKER, "roles/discoveryengine.user", "roles/aiplatform.user"]))
        written = h.written_policies()[0]
        self.assertEqual(members(written, ADMIN_MARKER), set())
        self.assertEqual(members(written, "roles/discoveryengine.user"), set())
        self.assertEqual(members(written, "roles/aiplatform.user"), set())
        self.assertEqual(members(written, "roles/viewer"), {"user:target@example.com"})
        # Active dashboard session is immediately invalidated even when ALLOWED_USERS="*".
        self.assertIsNone(signin.user_from_headers(headers, base))
        self.assertTrue(mgr.is_blocked("target@example.com"))

        # Restore AI access puts back normalized roles (ACCESS_ROLE + roles/aiplatform.user) and unblocks sign-in.
        h2 = Harness(policies=[written])
        with h2:
            ctx2 = support.FakeCtx(settings=base, body={"uid": uid_for("target@example.com"), "action": "unblock_all"})
            ctx2.app.settings = mgr
            restored = users_svc.access_action(ctx2)

        self.assertFalse(restored["aiBlocked"])
        self.assertEqual(sorted(restored["restoredRoles"]), sorted([ACCESS_ROLE, "roles/aiplatform.user"]))
        written2 = h2.written_policies()[0]
        self.assertIn("user:target@example.com", members(written2, ACCESS_ROLE))
        self.assertIn("user:target@example.com", members(written2, "roles/aiplatform.user"))
        self.assertFalse(mgr.is_blocked("target@example.com"))

    def test_block_all_refuses_self_admin_and_project_owner(self):
        from controlplane.settings_store import MemoryStore, SettingsManager
        from controlplane.services import users as users_svc

        base = support.make_settings(security={"admins": ["admin@example.com", "coadmin@example.com"]})
        mgr = SettingsManager(base, MemoryStore())
        pol = {"version": 3, "etag": "e1", "bindings": [
            {"role": "roles/owner", "members": ["user:projowner@example.com"]},
            {"role": ACCESS_ROLE, "members": ["user:coadmin@example.com", "user:admin@example.com"]},
        ]}
        for target in ("admin@example.com", "coadmin@example.com", "projowner@example.com"):
            with self.assertRaises(ApiError) as caught:
                with Harness(policies=[pol]):
                    ctx = support.FakeCtx(settings=base, body={"email": target, "action": "block_all"})
                    ctx.app.settings = mgr
                    users_svc.access_action(ctx)
            self.assertEqual(caught.exception.code, "conflict")


if __name__ == "__main__":
    unittest.main()
