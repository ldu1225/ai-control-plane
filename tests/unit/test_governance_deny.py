"""IAM deny policy enforcement and notify only (governance.antigravity.enforcement_backend), organization unit
budgets in the enforcement decision, the enforcement job hand-off and the scope picker.

The IAM v2 API is the in-memory fake of test_enforcers behind ctx.rest; usage, audit and webhooks are patched; the
governance state is a real SettingsManager over a MemoryStore. No test reaches Google Cloud.
"""

import copy
import datetime as dt
import unittest
from unittest import mock

import support
from test_enforcers import AP_DEMO, FakeIam
from test_governance_caps import store

from controlplane.gcp import GcpError
from controlplane.httpkit import ApiError
from controlplane.security import uid_for
from controlplane.services import budgets, enforcers, scope
from controlplane.services import governance as gov
from controlplane.settings_store import aux_docs, sanitize_governance

UTC = dt.timezone.utc
AP_B = "cloudresourcemanager.googleapis.com/projects/proj-b"
AP_C = "cloudresourcemanager.googleapis.com/projects/proj-c"
PID = enforcers.DEFAULT_POLICY_ID
JOB = "projects/%s/locations/asia-northeast3/jobs/ai-control-plane-enforcer" % support.PROJECT
RUN_URL = "https://run.googleapis.com/v2/%s:run" % JOB
WF_KEY = "principal://iam.googleapis.com/locations/global/workforcepools/corp-pool/subject/dana.kim"
WF_DENY = "principal://iam.googleapis.com/locations/global/workforcePools/corp-pool/subject/dana.kim"
# The same person as logged: Google documents the subject as case-sensitive, the person key above is lower-cased.
WF_LOGGED = "principal://iam.googleapis.com/locations/global/workforcePools/corp-pool/subject/Dana.Kim"


def deny(email):
    return "principal://goog/subject/%s" % email


def usage_of(entries, ok=True):
    """Usage with (person, tokens, requests) rows in the window (token cap 500000, request cap 1000)."""
    usage = gov.Usage("logging")
    usage.ok = ok
    for email, tokens, requests in entries:
        rec = usage.user(email)
        rec["tokens"], rec["requests"] = tokens, requests
        usage.records += requests
    return usage


USAGE = [("heavy@example.com", 600000, 40), ("light@example.com", 1000, 3), ("owner@example.com", 900000, 50),
         ("capped@example.com", 10, 1), (WF_KEY, 700000, 12)]


class Rest:
    """ctx.rest: Cloud Run job runs go to `runs`, everything else to the IAM v2 fake."""

    def __init__(self, iam, run_error=None):
        self.iam = iam
        self.runs = []
        self.run_error = run_error

    def __call__(self, method, url, **kwargs):
        if url == RUN_URL:
            self.runs.append(method)
            if self.run_error is not None:
                raise self.run_error
            return {"name": JOB.rsplit("/jobs/", 1)[0] + "/operations/op-1",
                    "metadata": {"name": JOB + "/executions/ai-control-plane-enforcer-abc12"}}
        return self.iam.rest(method, url, **kwargs)


class Deny:
    """Patches usage, prices, licenses, audit and webhook, and fails on any allow-policy access."""

    def __init__(self, usage=None):
        self.usage = usage if usage is not None else usage_of(USAGE)
        self.patchers = []

    def __enter__(self):
        fail = mock.MagicMock(side_effect=AssertionError("the allow policy must not be used"))
        targets = {"antigravity_usage": mock.MagicMock(side_effect=lambda *a, **k: self.usage),
                   "get_policy": fail, "set_policy": fail, "marker_status": fail, "permission_status": fail,
                   "write_audit": mock.MagicMock(return_value=True),
                   "send_webhook": mock.MagicMock(return_value=True),
                   "price_table": mock.MagicMock(return_value=None),
                   "license_credits": mock.MagicMock(return_value={})}
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

    def webhook_texts(self):
        return [call.args[1] for call in self.send_webhook.call_args_list]


def make_ctx(iam=None, body=None, job=False, run_error=None, workspace=None, **governance):
    values = {"enforcement_backend": "iam_deny", "enforcement_mode": "auto"}
    values.update(governance)
    if job:
        values["enforcement_job"] = JOB
    ws = {"projects": ["proj-b"]}
    ws.update(workspace or {})
    ctx = support.FakeCtx(support.make_settings(governance=values, workspace=ws), body=body or {})
    ctx.app.settings = store()
    ctx.iam = iam if iam is not None else FakeIam()
    ctx.rest = Rest(ctx.iam, run_error=run_error)
    return ctx


def status(ctx, mode=None):
    ag = ctx.settings["governance"]["antigravity"]
    return gov.compute_status(ctx, ag["period"], ag["window"], ag["token_cap"], ag["request_cap"], mode=mode)


def row(payload, email):
    return next(u for u in payload["users"] if u["email"] == email)


def gate(payload_or_gates, gid):
    gates = payload_or_gates["gates"] if isinstance(payload_or_gates, dict) else payload_or_gates
    return next(g for g in gates if g["id"] == gid)


def policy_body(*principals):
    return {"displayName": enforcers.DISPLAY_NAME,
            "rules": [{"denyRule": {"deniedPrincipals": list(principals),
                                    "deniedPermissions": [enforcers.ANTIGRAVITY_PERMISSION]}}]}


class DenyStatusTest(unittest.TestCase):
    def test_state_comes_from_the_deny_policy_and_the_allow_policy_is_never_read(self):
        ctx = make_ctx()
        for ap in (AP_DEMO, AP_B):
            ctx.iam.put(ap, policy_body(deny("capped@example.com")))
        with Deny():
            payload, internal = status(ctx)
        self.assertEqual(row(payload, "capped@example.com")["state"], gov.CAPPED)
        # A deny policy also stops owners, group grants and workforce identities: everyone is enforceable.
        for email in ("heavy@example.com", "owner@example.com", WF_KEY, "light@example.com"):
            self.assertEqual(row(payload, email)["state"], gov.EA, email)
        self.assertEqual(sorted(payload["plan"]["block"]),
                         sorted(uid_for(e) for e in ("heavy@example.com", "owner@example.com", WF_KEY)))
        self.assertEqual(payload["plan"]["restore"], [uid_for("capped@example.com")])
        self.assertEqual(gate(payload, "G1_POLICY_READABLE"),
                         {"id": "G1_POLICY_READABLE", "status": "pass",
                          "detail": "Deny policies read at 2 attachment point(s)."})
        self.assertEqual(gate(payload, "G5_NON_USER_GRANT_ABSENT")["status"], "pass")
        self.assertEqual(gate(payload, "G6_DENY_CAPACITY")["status"], "pass")
        self.assertEqual(gate(payload, "G7_SURGE_GUARD")["status"], "pass")
        self.assertEqual((payload["enforcement"]["backend"], payload["policy"]["enforcementBackend"]),
                         ("iam_deny", "iam_deny"))
        self.assertEqual(payload["prerequisites"]["markerRoles"], [])
        self.assertEqual(payload["enforcement"]["deny"]["people"], 1)
        self.assertEqual(internal["denyPeople"], {"capped@example.com": deny("capped@example.com")})

    def test_a_block_rule_makes_a_listed_person_blocked(self):
        ctx = make_ctx()
        ctx.app.settings.mutate_governance(
            lambda s: s["exceptions"].append({"email": "capped@example.com", "type": "block"}), "t")
        for ap in (AP_DEMO, AP_B):
            ctx.iam.put(ap, policy_body(deny("capped@example.com")))
        with Deny():
            payload, _ = status(ctx)
        self.assertEqual(row(payload, "capped@example.com")["state"], gov.BLOCKED)
        self.assertEqual(payload["plan"]["restore"], [])

    def test_an_unreadable_attachment_point_aborts_and_nobody_is_classified(self):
        ctx = make_ctx()
        ctx.iam.fail("GET", AP_B, GcpError("denied", 403, "Permission denied.", "PERMISSION_DENIED"))
        with Deny():
            payload, _ = status(ctx)
        g1 = gate(payload, "G1_POLICY_READABLE")
        self.assertEqual((g1["status"], g1["detail"]),
                         ("fail", "The deny policy could not be read at 1 of 2 attachment points."))
        self.assertTrue(payload["plan"]["aborted"])
        self.assertEqual({u["state"] for u in payload["users"]}, {gov.UNKNOWN})
        self.assertEqual(row(payload, "heavy@example.com")["reason"], "DENY_POLICY_UNREADABLE")
        self.assertTrue(any(w["source"] == enforcers.SOURCE for w in ctx.warnings))

    def test_a_policy_changed_outside_the_dashboard_blocks_changes(self):
        ctx = make_ctx()
        ctx.iam.put(AP_DEMO, policy_body("principalSet://goog/group/team@example.com"))
        with Deny():
            payload, _ = status(ctx)
        g1 = gate(payload, "G1_POLICY_READABLE")
        self.assertEqual(g1["status"], "fail")
        self.assertIn("changed outside the dashboard", g1["detail"])


class DenyEnforceTest(unittest.TestCase):
    def test_apply_writes_one_policy_per_attachment_point_with_people_only(self):
        ctx = make_ctx(body={})
        for ap in (AP_DEMO, AP_B):
            ctx.iam.put(ap, policy_body(deny("capped@example.com")))
        with Deny() as d:
            out = gov.enforce(ctx)
        self.assertTrue(out["executed"])
        want = sorted([deny("heavy@example.com"), deny("owner@example.com"), WF_DENY])
        for ap in (AP_DEMO, AP_B):
            self.assertEqual(sorted(ctx.iam.principals(ap)), want, ap)
            stored = ctx.iam.policies[(ap, PID)]
            self.assertEqual(stored["rules"][0]["denyRule"]["deniedPermissions"], [enforcers.ANTIGRAVITY_PERMISSION])
        self.assertEqual({(c["email"], c["action"], c["status"]) for c in out["changes"]},
                         {("heavy@example.com", "block", "applied"), ("owner@example.com", "block", "applied"),
                          (WF_KEY, "block", "applied"), ("capped@example.com", "restore", "applied")})
        state = ctx.app.settings.governance_state()
        self.assertEqual(sorted(s["email"] for s in state["suspensions"]),
                         sorted(["heavy@example.com", "owner@example.com", WF_KEY]))
        self.assertEqual(state["deny"]["attachments"], [AP_DEMO, AP_B])
        self.assertEqual(state["deny"]["policyId"], PID)
        self.assertEqual(d.write_audit.call_args.args[2]["backend"], "iam_deny")
        self.assertEqual(out["backend"], "iam_deny")

    def test_dry_run_and_manual_mode_write_nothing(self):
        ctx = make_ctx(body={"dryRun": True})
        with Deny():
            out = gov.enforce(ctx)
        self.assertFalse(out["executed"])
        self.assertEqual(ctx.iam.writes(), [])
        ctx = make_ctx(body={}, enforcement_mode="manual")
        with Deny():
            out = gov.enforce(ctx)
        self.assertEqual(ctx.iam.writes(), [])
        self.assertEqual(len(out["plan"]["review"]), 3)

    def test_nobody_left_deletes_the_policy(self):
        ctx = make_ctx(body={}, enforcement_mode="manual")
        for ap in (AP_DEMO, AP_B):
            ctx.iam.put(ap, policy_body(deny("capped@example.com")))
        with Deny():
            gov.enforce(ctx)
        self.assertIsNone(ctx.iam.principals(AP_DEMO))
        self.assertIsNone(ctx.iam.principals(AP_B))

    def test_a_failed_attachment_point_fails_only_its_people(self):
        ctx = make_ctx(body={}, enforcement_mode="manual")
        ctx.app.settings.mutate_governance(
            lambda s: s["exceptions"].append({"email": "light@example.com", "type": "block"}), "t")
        ctx.iam.fail("POST", AP_B, GcpError("denied", 403, "Permission denied.", "PERMISSION_DENIED"))
        with Deny():
            out = gov.enforce(ctx)
        change = next(c for c in out["changes"] if c["email"] == "light@example.com")
        self.assertEqual((change["action"], change["status"]), ("disallow", "failed"))
        self.assertIn("projects/proj-b", change["message"])
        self.assertEqual(ctx.iam.principals(AP_DEMO), [deny("light@example.com")])

    def test_surge_guard_holds_a_wave_of_new_suspensions_for_review(self):
        ctx = make_ctx(body={}, surge_guard_min=2, surge_guard_ratio=0.5)
        with Deny():
            out = gov.enforce(ctx)
        g7 = gate(out["gates"], "G7_SURGE_GUARD")
        self.assertEqual(g7["status"], "warn")
        self.assertEqual(g7["detail"], "3 new suspensions of 5 people reach the surge guard (2 people and 50%), so "
                                       "nobody is suspended in this run.")
        self.assertEqual(out["plan"]["block"], [])
        self.assertEqual(len(out["plan"]["review"]), 3)
        self.assertEqual(ctx.iam.writes(), [])

    def test_attachment_points_no_longer_configured_lose_the_policy(self):
        ctx = make_ctx(body={}, enforcement_mode="manual")
        ctx.iam.put(AP_C, policy_body(deny("capped@example.com")))
        ctx.app.settings.mutate_governance(lambda s: s.update(deny={"attachments": [AP_DEMO, AP_B, AP_C],
                                                                    "policyId": PID, "at": "2026-11-01T00:00:00Z"}),
                                           "t")
        with Deny():
            out = gov.enforce(ctx)
        self.assertIsNone(ctx.iam.principals(AP_C))
        self.assertTrue(out["executed"])
        self.assertEqual(ctx.app.settings.governance_state()["deny"]["attachments"], [AP_DEMO, AP_B])

    def test_cap_with_a_job_is_refused_but_its_dry_run_works(self):
        ctx = make_ctx(body={"mode": "apply"}, job=True)
        with Deny():
            with self.assertRaises(ApiError):
                gov.cap(ctx)
            ctx.body = {"mode": "dry_run"}
            out = gov.cap(ctx)
        self.assertEqual(out["mode"], "dry_run")


class WorkforceCaseTest(unittest.TestCase):
    """Google documents google.subject, and so the workforce principal identifier, as case-sensitive. People stay keyed
    in lower case for matching and storage; the deny policy gets the principal as it was logged."""

    def usage(self, entries=USAGE):
        usage = usage_of(entries)
        usage.principals[WF_KEY] = WF_LOGGED
        return usage

    def test_a_workforce_subject_is_denied_as_logged(self):
        ctx = make_ctx(body={})
        with Deny(self.usage()):
            out = gov.enforce(ctx)
        self.assertIn((WF_KEY, "block", "applied"), {(c["email"], c["action"], c["status"]) for c in out["changes"]})
        for ap in (AP_DEMO, AP_B):
            self.assertIn(WF_LOGGED, ctx.iam.principals(ap), ap)
            self.assertNotIn(WF_DENY, ctx.iam.principals(ap), ap)
        self.assertIn(WF_KEY, [s["email"] for s in ctx.app.settings.governance_state()["suspensions"]])

    def test_an_entry_written_lower_cased_before_is_rewritten_as_logged(self):
        ctx = make_ctx(body={})
        for ap in (AP_DEMO, AP_B):
            ctx.iam.put(ap, policy_body(WF_DENY))
        with Deny(self.usage()):
            gov.enforce(ctx)
        for ap in (AP_DEMO, AP_B):
            self.assertIn(WF_LOGGED, ctx.iam.principals(ap), ap)
            self.assertNotIn(WF_DENY, ctx.iam.principals(ap), ap)

    def test_a_restore_removes_the_entry_as_the_policy_lists_it(self):
        ctx = make_ctx(body={})
        for ap in (AP_DEMO, AP_B):
            ctx.iam.put(ap, policy_body(WF_LOGGED))
        with Deny(usage_of([(WF_KEY, 10, 1)])):   # under the caps, and no logged form in this window
            out = gov.enforce(ctx)
        self.assertIn((WF_KEY, "restore", "applied"), {(c["email"], c["action"], c["status"]) for c in out["changes"]})
        self.assertIsNone(ctx.iam.principals(AP_DEMO))

    def test_principals_as_logged_are_kept_only_for_their_own_workforce_key(self):
        usage = gov.Usage("logging")
        gov._note_principal(usage, WF_KEY, WF_LOGGED)                                    # pylint: disable=protected-access
        gov._note_principal(usage, WF_KEY, WF_LOGGED.replace("Dana", "DANA"))           # first one wins
        gov._note_principal(usage, "alice@example.com", "user:Alice@Example.com")       # e-mail: case-insensitive
        other = WF_KEY.replace("dana.kim", "lee")
        gov._note_principal(usage, other, WF_LOGGED)                                     # another person's label
        self.assertEqual(usage.principals, {WF_KEY: WF_LOGGED})
        merged = gov.Usage("logging")
        merged.merge(usage)
        self.assertEqual(merged.principals, {WF_KEY: WF_LOGGED})
        listed = {WF_KEY: WF_DENY}
        self.assertEqual(gov._deny_principal_for(WF_KEY, listed, usage.principals), WF_LOGGED)  # pylint: disable=protected-access
        self.assertEqual(gov._deny_principal_for(WF_KEY, listed, {}), WF_DENY)                 # pylint: disable=protected-access
        self.assertEqual(gov._deny_principal_for(WF_KEY, {}, {}), WF_DENY)                     # pylint: disable=protected-access
        self.assertEqual(gov._deny_principal_for("alice@example.com", {}, {}),                 # pylint: disable=protected-access
                         deny("alice@example.com"))


class CapacityGateTest(unittest.TestCase):
    CFG = gov.gov_config(support.make_settings())

    def users(self, count, state, over, prefix):
        return [{"uid": "%s%04d" % (prefix, i), "state": state, "overCap": over, "capUsage": i}
                for i in range(count)]

    def test_new_suspensions_beyond_the_limit_wait_and_restores_go_through(self):
        users = self.users(3, gov.EA, True, "n") + self.users(2, gov.CAPPED, False, "c")
        info = {"attachments": [AP_DEMO], "current": enforcers.DENY_PRINCIPAL_LIMIT - 1 + 2, "surgeMin": 100,
                "surgeRatio": 1.0}
        gates, plan, _ = gov.evaluate(users, True, True, 10, [], self.CFG, "r", mode="auto", backend="iam_deny",
                                      deny=info)
        g6 = gate(gates, "G6_DENY_CAPACITY")
        # 2,501 listed now - 2 restores + 3 new = 2,502: two new suspensions wait (the least severe first).
        self.assertEqual(g6, {"id": "G6_DENY_CAPACITY", "status": "warn", "detail": "2 new suspensions wait because "
                              "projects/demo-project would go over the limit of 2500 people."})
        self.assertEqual(plan["block"], ["n0002"])
        self.assertEqual(plan["capacityHeld"], ["n0000", "n0001"])
        self.assertEqual(sorted(plan["restore"]), ["c0000", "c0001"])

    def test_a_warning_starts_at_2000_people(self):
        info = {"attachments": [AP_DEMO], "current": 2000, "surgeMin": 100, "surgeRatio": 1.0}
        gates, _, _ = gov.evaluate([], True, True, 1, [], self.CFG, "r", backend="iam_deny", deny=info)
        self.assertEqual(gate(gates, "G6_DENY_CAPACITY")["detail"],
                         "2000 people at projects/demo-project; a warning starts at 2000 and the limit is 2500.")

    def test_allow_swap_gates_are_unchanged(self):
        gates, plan, _ = gov.evaluate(self.users(1, gov.EA, True, "n"), True, True, 5, [], self.CFG, "r")
        self.assertEqual([g["id"] for g in gates], ["G1_POLICY_READABLE", "G2_ANY_DETERMINATE",
                                                    "G3_TELEMETRY_HEALTHY", "G4_CHANGE_BUDGET",
                                                    "G5_NON_USER_GRANT_ABSENT"])
        self.assertEqual(plan["capacityHeld"], [])

    def test_approved_and_review_forced_users(self):
        users = [{"uid": "a", "state": gov.EA, "overCap": True, "approved": True},
                 {"uid": "b", "state": gov.EA, "overCap": True},
                 {"uid": "c", "state": gov.EA, "overCap": True, "forceReview": True},
                 {"uid": "d", "state": gov.CAPPED, "overCap": False, "hold": True}]
        _, manual, _ = gov.evaluate(users, True, True, 5, [], self.CFG, "r", mode="manual")
        self.assertEqual((manual["block"], sorted(manual["review"]), manual["restore"]), (["a"], ["b", "c"], []))
        _, auto, _ = gov.evaluate(users, True, True, 5, [], self.CFG, "r", mode="auto")
        self.assertEqual((sorted(auto["block"]), auto["review"]), (["a", "b"], ["c"]))


class JobHandOffTest(unittest.TestCase):
    def test_run_now_starts_the_job_and_writes_nothing_itself(self):
        ctx = make_ctx(body={}, job=True)
        with Deny() as d:
            out = gov.enforce(ctx)
        self.assertEqual(out["triggered"], True)
        self.assertEqual(out["execution"], JOB + "/executions/ai-control-plane-enforcer-abc12")
        self.assertEqual(ctx.rest.runs, ["POST"])
        self.assertEqual(ctx.iam.writes(), [])
        self.assertEqual(d.write_audit.call_args.args[1], "enforce_job_run")

    def test_a_dry_run_stays_in_the_dashboard(self):
        ctx = make_ctx(body={"dryRun": True}, job=True)
        with Deny():
            out = gov.enforce(ctx)
        self.assertEqual(out["mode"], "dry_run")
        self.assertEqual(ctx.rest.runs, [])

    def test_a_refused_job_run_is_an_upstream_error(self):
        ctx = make_ctx(body={}, job=True, run_error=GcpError("denied", 403, "Permission denied."))
        with Deny():
            with self.assertRaises(ApiError) as caught:
                gov.enforce(ctx)
        self.assertEqual(caught.exception.code, "upstream_error")
        self.assertIn("roles/run.jobsExecutor", caught.exception.message)

    def test_enforce_cycle_is_the_jobs_entry_point_and_never_starts_a_job(self):
        ctx = make_ctx(job=True)
        with Deny():
            out = gov.enforce_cycle(ctx, trigger="scheduler", dry_run=False)
        self.assertTrue(out["executed"])
        self.assertEqual(ctx.rest.runs, [])
        self.assertEqual(ctx.app.settings.governance_state()["lastScheduledRun"]["result"], "success")


class DenyDecisionsTest(unittest.TestCase):
    def test_review_suspend_is_recorded_and_applied_by_a_run(self):
        ctx = make_ctx(enforcement_mode="manual",
                       body={"decision": "suspend", "uids": [uid_for("heavy@example.com")]})
        with Deny():
            out = gov.review(ctx)
        self.assertEqual(out["results"][0]["status"], "applied")
        self.assertEqual(ctx.iam.principals(AP_DEMO), [deny("heavy@example.com")])
        record = next(s for s in ctx.app.settings.governance_state()["suspensions"]
                      if s["email"] == "heavy@example.com")
        self.assertEqual(record["trigger"], "auto")   # applied: an ordinary suspension record now
        self.assertNotIn("approved", record)

    def test_review_suspend_with_a_job_is_queued_and_the_job_applies_it_in_manual_mode(self):
        ctx = make_ctx(enforcement_mode="manual", job=True,
                       body={"decision": "suspend", "uids": [uid_for("heavy@example.com")]})
        with Deny():
            out = gov.review(ctx)
            self.assertEqual(out["results"][0]["status"], "queued")
            self.assertEqual(out["via"], "job")
            self.assertEqual(ctx.iam.writes(), [])
            record = next(s for s in ctx.app.settings.governance_state()["suspensions"]
                          if s["email"] == "heavy@example.com")
            self.assertTrue(record["approved"])
            gov.enforce_cycle(ctx, trigger="scheduler")   # what the job runs
        self.assertEqual(ctx.iam.principals(AP_DEMO), [deny("heavy@example.com")])
        self.assertEqual(ctx.iam.principals(AP_B), [deny("heavy@example.com")])

    def test_review_allow_of_a_suspended_person_restores_now(self):
        ctx = make_ctx(enforcement_mode="manual",
                       body={"decision": "allow", "uids": [uid_for("capped@example.com")]})
        for ap in (AP_DEMO, AP_B):
            ctx.iam.put(ap, policy_body(deny("capped@example.com"), deny("light@example.com")))
        ctx.app.settings.mutate_governance(lambda s: s["suspensions"].append(
            {"email": "capped@example.com", "until": "2099-01-01T00:00:00Z", "trigger": "auto"}), "t")
        with Deny(usage_of([("capped@example.com", 900000, 5), ("light@example.com", 900000, 5)], ok=False)):
            out = gov.review(ctx)
        self.assertEqual(out["results"][0]["status"], "applied")
        self.assertEqual(ctx.iam.principals(AP_DEMO), [deny("light@example.com")])

    def test_block_rule_and_its_removal(self):
        ctx = make_ctx(enforcement_mode="manual", body={"email": "light@example.com", "type": "block"})
        with Deny():
            saved = gov.exception_save(ctx)
            self.assertEqual(saved["change"]["action"], "disallow")
            self.assertEqual(saved["enforcement"]["status"], "applied")
            self.assertEqual(ctx.iam.principals(AP_DEMO), [deny("light@example.com")])
            ctx.body = {"email": "light@example.com"}
            removed = gov.exception_delete(ctx)
        self.assertEqual(removed["change"]["action"], "allow")
        self.assertEqual(removed["enforcement"]["status"], "applied")
        self.assertIsNone(ctx.iam.principals(AP_DEMO))

    def test_removing_a_block_rule_without_restoring_keeps_the_person_until_the_period_ends(self):
        ctx = make_ctx(enforcement_mode="manual", body={"email": "light@example.com", "type": "block"})
        with Deny():
            gov.exception_save(ctx)
            ctx.body = {"email": "light@example.com", "restoreAccess": False}
            gov.exception_delete(ctx)
            record = next(s for s in ctx.app.settings.governance_state()["suspensions"]
                          if s["email"] == "light@example.com")
            self.assertTrue(record["hold"])
            ctx.body = {}
            gov.enforce(ctx)   # healthy telemetry and under the cap: still not restored before the end
        self.assertEqual(ctx.iam.principals(AP_DEMO), [deny("light@example.com")])

    def test_drawer_actions(self):
        ctx = make_ctx(enforcement_mode="manual", body={"uid": uid_for("light@example.com"), "action": "disallow"})
        with Deny():
            out = gov.user_action(ctx)
            self.assertEqual((out["newState"], out["enforcement"]["status"]), (gov.BLOCKED, "applied"))
            self.assertEqual(ctx.iam.principals(AP_B), [deny("light@example.com")])
            ctx.body = {"uid": uid_for("light@example.com"), "action": "allow"}
            out = gov.user_action(ctx)
        self.assertEqual(out["previousState"], gov.BLOCKED)
        self.assertIsNone(ctx.iam.principals(AP_B))

    def test_an_account_iam_does_not_find_does_not_stop_the_others(self):
        # Seen live: IAM rejects a deny policy that lists an account it does not find.
        ctx = make_ctx(body={}, enforcement_mode="manual")
        ctx.iam.unknown.add(deny("ghost@example.com"))
        ctx.app.settings.mutate_governance(lambda s: s["exceptions"].extend([
            {"email": "ghost@example.com", "type": "block"}, {"email": "light@example.com", "type": "block"}]), "t")
        with Deny():
            out = gov.enforce(ctx)
        by = {c["email"]: c for c in out["changes"]}
        self.assertEqual((by["ghost@example.com"]["status"], by["ghost@example.com"]["message"]),
                         ("failed", gov.MISSING_ACCOUNT_MESSAGE))
        self.assertEqual(by["light@example.com"]["status"], "applied")
        self.assertEqual(ctx.iam.principals(AP_DEMO), [deny("light@example.com")])
        self.assertEqual(ctx.iam.principals(AP_B), [deny("light@example.com")])


class NotifyOnlyTest(unittest.TestCase):
    def ctx(self, body=None):
        return make_ctx(body=body, enforcement_backend="notify_only")

    def test_over_cap_people_are_reviewed_and_announced_and_nothing_is_written(self):
        ctx = self.ctx(body={})
        ctx.rest = mock.MagicMock(side_effect=AssertionError("notify only calls no Google API"))
        with Deny() as d:
            payload, _ = status(ctx)
            out = gov.enforce(ctx)
        self.assertEqual(payload["policy"]["enforcementMode"], "auto")
        self.assertEqual(payload["plan"]["block"], [])
        self.assertEqual(len(payload["plan"]["review"]), 3)
        self.assertEqual(gate(payload, "G1_POLICY_READABLE")["detail"], "Notify only: no IAM policy is read or changed.")
        self.assertFalse(out["executed"])
        self.assertIn("await review", d.webhook_texts()[-1])
        self.assertIn("notify only", d.webhook_texts()[-1])

    def test_suspensions_and_drawer_actions_are_refused(self):
        ctx = self.ctx(body={"decision": "suspend", "uids": [uid_for("heavy@example.com")]})
        with Deny():
            out = gov.review(ctx)
            self.assertEqual(out["results"][0]["message"], gov.NOTIFY_ONLY_REFUSED)
            ctx.body = {"uid": uid_for("heavy@example.com"), "action": "disallow"}
            with self.assertRaises(ApiError):
                gov.user_action(ctx)


class LeftoverDenyTest(unittest.TestCase):
    def test_switching_away_from_deny_removes_the_recorded_policies_on_an_administrator_run(self):
        ctx = make_ctx(body={}, enforcement_backend="notify_only")
        for ap in (AP_DEMO, AP_B):
            ctx.iam.put(ap, policy_body(deny("capped@example.com")))
        ctx.app.settings.mutate_governance(lambda s: s.update(deny={"attachments": [AP_DEMO, AP_B], "policyId": PID,
                                                                    "at": "2026-11-01T00:00:00Z"}), "t")
        with Deny():
            info = gov.enforcement_status(ctx)
            self.assertEqual(info["leftover"]["attachments"], [AP_DEMO, AP_B])
            gov.enforce(ctx)
        self.assertIsNone(ctx.iam.principals(AP_DEMO))
        self.assertIsNone(ctx.iam.principals(AP_B))
        self.assertEqual(ctx.app.settings.governance_state()["deny"]["attachments"], [])


class MarkerHandoverTest(unittest.TestCase):
    """Marker roles left by iam_allow_swap after a switch to iam_deny or notify_only (gov.release_markers)."""

    @staticmethod
    def allow_policy(ctx, caps=(), blocks=()):
        cfg = gov.gov_config(ctx.settings)
        bindings = [{"role": cfg.access_role, "members": ["user:light@example.com"]}]
        if caps:
            bindings.append({"role": cfg.cap_marker, "members": ["user:%s" % e for e in caps]})
        if blocks:
            bindings.append({"role": cfg.admin_marker, "members": ["user:%s" % e for e in blocks]})
        return {"etag": "BwX1", "version": 3, "bindings": bindings}, cfg

    @staticmethod
    def members(policy, role):
        return sorted(m for b in policy["bindings"] if b["role"] == role for m in b["members"])

    @staticmethod
    def release(ctx, policy, usage=None):
        written = []
        with Deny(usage) as d, mock.patch.object(gov, "get_policy", return_value=policy), \
                mock.patch.object(gov, "set_policy", side_effect=lambda _ctx, new: written.append(new) or new):
            out = gov.release_markers(ctx)
        return out, written, d

    def test_deny_releases_people_the_deny_policy_lists_or_nothing_stops(self):
        ctx = make_ctx()
        for ap in (AP_DEMO, AP_B):
            ctx.iam.put(ap, policy_body(deny("heavy@example.com")))
        ctx.app.settings.mutate_governance(lambda s: s["exceptions"].append(
            {"email": "blocked@example.com", "type": "block"}), "t")
        policy, cfg = self.allow_policy(ctx, caps=("heavy@example.com", "owner@example.com", "idle@example.com"),
                                        blocks=("blocked@example.com",))
        out, written, d = self.release(ctx, policy)
        self.assertEqual(len(written), 1)
        new = written[0]
        self.assertEqual(self.members(new, cfg.cap_marker), ["user:owner@example.com"])      # over the cap, not listed
        self.assertEqual(self.members(new, cfg.admin_marker), ["user:blocked@example.com"])  # block rule, not listed
        access = self.members(new, cfg.access_role)
        self.assertIn("user:heavy@example.com", access)   # listed: the deny policy keeps them out
        self.assertIn("user:idle@example.com", access)    # no usage and no record: nothing stops them
        self.assertEqual((out["holders"], out["released"], out["waiting"], out["manual"], out["error"]),
                         (4, 2, 2, 0, None))
        self.assertEqual(ctx.app.settings.governance_state()["markers"]["waiting"], 2)
        audit = [c for c in d.write_audit.call_args_list if c.args[1] == "marker_release"]
        self.assertEqual(audit[0].args[2]["users"], ["heavy@example.com", "idle@example.com"])

    def test_notify_only_waits_for_running_suspensions_and_block_rules(self):
        ctx = make_ctx(enforcement_backend="notify_only")

        def seed(state):
            state["suspensions"].append({"email": "a@example.com", "until": "2099-01-01T00:00:00Z", "trigger": "auto"})
            state["exceptions"].append({"email": "c@example.com", "type": "block"})
        ctx.app.settings.mutate_governance(seed, "t")
        policy, cfg = self.allow_policy(ctx, caps=("a@example.com", "b@example.com"),
                                        blocks=("c@example.com", "d@example.com"))
        out, written, _ = self.release(ctx, policy)
        new = written[0]
        self.assertEqual(self.members(new, cfg.cap_marker), ["user:a@example.com"])
        self.assertEqual(self.members(new, cfg.admin_marker), ["user:c@example.com"])
        self.assertTrue({"user:b@example.com", "user:d@example.com"} <= set(self.members(new, cfg.access_role)))
        self.assertEqual((out["released"], out["waiting"]), (2, 2))

    def test_without_the_project_iam_role_nothing_changes_and_the_status_says_why(self):
        ctx = make_ctx()
        with Deny(), mock.patch.object(gov, "get_policy", side_effect=GcpError("denied", 403, "Permission denied.")):
            out = gov.release_markers(ctx)
            info = gov.enforcement_status(ctx)
        self.assertEqual(out["error"], "denied")
        self.assertEqual(info["markers"]["error"], "denied")
        self.assertTrue(any(w["source"] == gov.MARKER_SOURCE and w["code"] == "api_error" for w in ctx.warnings))

    def test_a_concurrent_change_is_read_again_and_retried_once(self):
        ctx = make_ctx(enforcement_backend="notify_only")
        policy, _ = self.allow_policy(ctx, caps=("b@example.com",))
        calls = []

        def set_policy(_ctx, new):
            calls.append(new)
            if len(calls) == 1:
                raise GcpError("conflict", 409, "etag mismatch")
            return new
        with Deny(), mock.patch.object(gov, "get_policy", return_value=policy) as get, \
                mock.patch.object(gov, "set_policy", side_effect=set_policy):
            out = gov.release_markers(ctx)
        self.assertEqual((len(calls), get.call_count, out["released"], out["error"]), (2, 2, 1, None))

    def test_allow_swap_needs_no_hand_over(self):
        ctx = make_ctx(enforcement_backend="iam_allow_swap")
        with Deny():   # get_policy fails the test if it is called
            self.assertIsNone(gov.release_markers(ctx))

    def test_maintenance_runs_it_off_allow_swap_on_its_cadence(self):
        from controlplane.services import maintenance
        claims = []

        def claim(_app, task, due):
            claims.append((task, due))
            return True
        result = {"holders": 2, "released": 1, "waiting": 1, "manual": 0, "error": None}
        out = {}
        with mock.patch.object(maintenance, "_claim", side_effect=claim), \
                mock.patch.object(maintenance, "_finish") as finish, \
                mock.patch.object(maintenance, "_system_ctx", return_value="ctx"), \
                mock.patch.object(gov, "release_markers", return_value=result) as release:
            swap = support.make_settings(governance={"enforcement_backend": "iam_allow_swap"})
            maintenance._release_markers(None, swap, "system:maintenance", "scheduled", out)  # pylint: disable=protected-access
            self.assertEqual(claims, [])
            denying = support.make_settings(governance={"enforcement_backend": "iam_deny"})
            maintenance._release_markers(None, denying, "system:maintenance", "scheduled", out)  # pylint: disable=protected-access
        task, due = claims[0]
        self.assertEqual(task, "antigravity_markers")
        release.assert_called_once_with("ctx", trigger="scheduled")
        self.assertEqual(out["antigravity_markers"]["waiting"], 1)
        self.assertTrue(finish.call_args.args[2])
        now = dt.datetime.now(UTC)

        def ago(minutes):
            return (now - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertTrue(due({}))
        self.assertFalse(due({"finishedAt": ago(30), "status": "ok", "summary": {"waiting": 0}}))
        self.assertTrue(due({"finishedAt": ago(30), "status": "ok", "summary": {"waiting": 1}}))
        self.assertFalse(due({"finishedAt": ago(5), "status": "error"}))
        self.assertTrue(due({"finishedAt": ago(61), "status": "ok", "summary": {"waiting": 0}}))

    def test_the_summary_is_sanitized(self):
        clean = sanitize_governance({"markers": {"at": "2026-11-19T00:00:00Z", "holders": "4", "released": 2,
                                                 "waiting": -1, "error": "Not A Kind"}})
        self.assertEqual(clean["markers"], {"at": "2026-11-19T00:00:00Z", "holders": 0, "released": 2,
                                            "waiting": 0, "manual": 0, "error": None})
        self.assertNotIn("markers", sanitize_governance({"markers": {"holders": 3}}))


class UnitBudgetTest(unittest.TestCase):
    UNIT = "u_0123456789"

    def ctx(self, action, members=None):
        ctx = make_ctx(body={}, enforcement_mode="auto", cap_model="credit_budget", credit_per_user_month=0.0,
                       token_cap=0, request_cap=0, pricing_source="manual", input_price_per_million=1.0,
                       output_price_per_million=1.0)
        aux = aux_docs(ctx.app)
        aux.mutate("org_model", lambda doc: doc.update(
            {"version": 1, "units": {self.UNIT: {"name": "Sales", "parent": None}},
             "members": {"heavy@example.com": self.UNIT} if members is None else members}))
        from controlplane.services import org_model
        nodes = budgets.validate_nodes([{"scope": "unit", "unitId": self.UNIT, "monthlyPaidUsd": 1.0,
                                         "action": action, "thresholds": [50, 100]}], org_model.load(ctx))
        aux.mutate(budgets.DOC, lambda doc: doc.update({"version": 1, "nodes": nodes}))
        return ctx

    def usage(self):
        usage = gov.Usage("logging")
        usage.ok = True
        for email in ("heavy@example.com", "light@example.com"):
            rec = usage.user(email)
            rec["requests"], rec["tokens"], rec["promptTokens"], rec["candidateTokens"] = 5, 2000000, 1000000, 1000000
            gov.Usage.add_model(rec, "claude-x", 5, 2000000, 1000000, 1000000)
            gov.Usage.add_project(rec, "proj-b", "claude-x", 5, 2000000, 1000000, 1000000)
            usage.records += 5
        return usage

    def test_a_used_up_unit_budget_suspends_its_people_within_one_run(self):
        ctx = self.ctx("suspend")
        with Deny(self.usage()) as d:
            payload, _ = status(ctx)
            heavy, light = row(payload, "heavy@example.com"), row(payload, "light@example.com")
            self.assertTrue(heavy["overCap"])
            self.assertIn("unit_budget", heavy["overCapReasons"])
            self.assertEqual(heavy["unitBudget"], {"suspend": [self.UNIT], "review": []})
            self.assertFalse(light["overCap"])   # Unassigned: no budget
            self.assertEqual(payload["plan"]["block"], [uid_for("heavy@example.com")])
            self.assertTrue(payload["budgets"]["units"][0]["usedUp"])
            out = gov.enforce(ctx)
            again = gov.enforce(ctx)
        self.assertEqual(ctx.iam.principals(AP_DEMO), [deny("heavy@example.com")])
        self.assertEqual(out["budgetAlerts"], 2)       # 50% and 100%, once per month
        self.assertEqual(again["budgetAlerts"], 0)
        texts = [t for t in d.webhook_texts() if t.startswith("AI Control Plane budgets")]
        self.assertEqual(len(texts), 1)
        self.assertIn("Unit Sales reached 100%", texts[0])

    def test_a_review_budget_queues_its_people_even_in_automatic_mode(self):
        ctx = self.ctx("review")
        with Deny(self.usage()):
            payload, _ = status(ctx)
        heavy = row(payload, "heavy@example.com")
        self.assertTrue(heavy["forceReview"])
        self.assertEqual(payload["plan"]["block"], [])
        self.assertEqual(payload["plan"]["review"], [uid_for("heavy@example.com")])

    def test_spend_attributed_through_a_project_label_never_suspends_or_reviews(self):
        # Project labels are set by project owners, not administrators: a label must not be able to spend a unit's
        # budget away and get its members suspended. The spend still counts for the status and its alerts.
        from controlplane.services import chargeback
        for action in ("suspend", "review"):
            ctx = self.ctx(action, members={})
            ctx.settings["chargeback"]["project_unit_label"] = "team"
            with Deny(self.usage()), \
                    mock.patch.object(chargeback, "_project_labels", return_value={"proj-b": {"team": self.UNIT}}):
                payload, _ = status(ctx)
                out = gov.enforce(ctx)
            for email in ("heavy@example.com", "light@example.com"):
                person = row(payload, email)
                self.assertFalse(person["overCap"] or person.get("forceReview"), (action, email))
                self.assertNotIn("unit_budget", person.get("overCapReasons") or [], (action, email))
            self.assertEqual((payload["plan"]["block"], payload["plan"]["review"]), ([], []), action)
            self.assertTrue(payload["budgets"]["units"][0]["usedUp"], action)
            self.assertEqual(out["budgetAlerts"], 2, action)
            self.assertIsNone(ctx.iam.principals(AP_DEMO), action)


class EnforcementStatusTest(unittest.TestCase):
    def test_reports_attachment_points_capacity_and_job(self):
        ctx = make_ctx()
        ctx.iam.put(AP_DEMO, policy_body(deny("a@example.com"), deny("b@example.com")))
        with Deny():
            out = gov.enforcement_status(ctx)
        self.assertEqual(out["backend"], "iam_deny")
        self.assertEqual(out["permissions"], [enforcers.ANTIGRAVITY_PERMISSION])
        self.assertEqual(out["job"], {"configured": False, "job": "", "latest": None, "error": None})
        self.assertEqual([(a["attachment"], a["exists"], a["count"]) for a in out["deny"]["attachments"]],
                         [(AP_DEMO, True, 2), (AP_B, False, 0)])
        self.assertEqual(out["deny"]["attachments"][0]["ratio"], 0.0008)
        self.assertFalse(out["spendCaps"]["enabled"])

    def test_scheduler_counts_the_job_as_configured(self):
        ctx = make_ctx(job=True)
        self.assertEqual(gov.scheduler_status(ctx, {})["configured"], True)
        self.assertEqual(gov.scheduler_status(ctx, {})["job"], JOB)

    def test_too_many_attachment_points_for_the_policy_api_quota_are_flagged(self):
        # Live: the IAM policy API allows 10 policy reads and 10 writes per minute per project by default.
        few = make_ctx()
        many = make_ctx(workspace={"projects": ["proj-b", "proj-c", "proj-d", "proj-e", "proj-f"]})
        with Deny():
            gov.enforcement_status(few)
            gov.enforcement_status(many)
        self.assertFalse([w for w in few.warnings if w["code"] == "quota"])
        quota = [w for w in many.warnings if w["code"] == "quota"]
        self.assertEqual(len(quota), 1)
        self.assertTrue(quota[0]["message"].startswith("6 deny policy attachment points"))


class StateSanitizerTest(unittest.TestCase):
    def test_workforce_people_flags_and_deny_record(self):
        clean = sanitize_governance({
            "exceptions": [{"email": WF_KEY, "type": "block"}],
            "suspensions": [{"email": WF_KEY, "trigger": "unblock", "approved": True, "hold": "yes",
                             "reasons": ["unit_budget", "credit"]}],
            "notified": {WF_KEY: "2026-11-01T00:00:00Z"},
            "deny": {"attachments": [AP_DEMO, "folders/1", AP_DEMO + "\n", AP_B], "policyId": "other-id",
                     "at": "2026-11-01T00:00:00Z"}})
        self.assertEqual(clean["exceptions"][0]["email"], WF_KEY)
        suspension = clean["suspensions"][0]
        self.assertEqual((suspension["trigger"], suspension.get("approved"), suspension.get("hold")),
                         ("unblock", True, None))
        self.assertEqual(suspension["reasons"], ["unit_budget", "credit"])
        self.assertEqual(clean["notified"], {WF_KEY: "2026-11-01T00:00:00Z"})
        self.assertEqual(clean["deny"]["attachments"], [AP_DEMO, AP_B])
        self.assertIsNone(clean["deny"]["policyId"])
        self.assertNotIn("deny", sanitize_governance({}))


class Gcp:
    """Named like the real client, so scope._live accepts it; serves Resource Manager search pages."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def paginate(self, url, *, quota_project, items_key, params=None, page_size=None, max_pages=10, **_kwargs):
        self.calls.append((url, dict(params or {})))
        return copy.deepcopy(self.pages.get(items_key, [])), False


class ScopeCandidatesTest(unittest.TestCase):
    def ctx(self, params, pages):
        ctx = support.FakeCtx(support.make_settings(workspace={"project_id": "hub-project"}), params=params)
        ctx.app.gcp = Gcp(pages)
        ctx.gcp = ctx.app.gcp
        return ctx

    def test_projects_are_searched_by_name_or_id_and_filtered_by_every_word(self):
        pages = {"projects": [
            {"projectId": "sales-ai", "displayName": "Sales AI", "parent": "folders/1", "state": "ACTIVE"},
            {"projectId": "sales-old", "displayName": "Sales legacy", "state": "DELETE_REQUESTED"},
            {"projectId": "ai-control-plane-auth-x1", "displayName": "auth", "state": "ACTIVE"},
            {"projectId": "sales-hr", "displayName": "HR tools", "state": "ACTIVE"}]}
        ctx = self.ctx({"kind": "project", "query": "sales ai"}, pages)
        out = scope.candidates(ctx)
        self.assertEqual(ctx.gcp.calls[0], (scope.CRM + "/projects:search", {"query": "name:sales* id:sales*"}))
        self.assertEqual(out, {"kind": "project", "truncated": False,
                               "items": [{"id": "sales-ai", "name": "Sales AI", "parent": "folders/1"}]})

    def test_folders_and_organizations_use_numeric_ids(self):
        ctx = self.ctx({"kind": "folder", "query": "fin"}, {"folders": [
            {"name": "folders/123", "displayName": "Finance", "parent": "organizations/9", "state": "ACTIVE"},
            {"name": "bogus", "displayName": "Finance 2"}]})
        self.assertEqual(scope.candidates(ctx)["items"], [{"id": "123", "name": "Finance",
                                                           "parent": "organizations/9"}])
        ctx = self.ctx({"kind": "organization"}, {"organizations": [
            {"name": "organizations/9", "displayName": "example.com", "state": "ACTIVE"}]})
        self.assertEqual(scope.candidates(ctx)["items"], [{"id": "9", "name": "example.com", "parent": None}])
        self.assertNotIn("query", ctx.gcp.calls[0][1] or {})

    def test_query_text_never_reaches_the_search_expression_unfiltered(self):
        ctx = self.ctx({"kind": "project", "query": "x* labels.a:*"}, {"projects": []})
        scope.candidates(ctx)
        self.assertEqual(ctx.gcp.calls[0][1], {"query": "name:x* id:x*"})

    def test_invalid_parameters_and_unconfigured_workspaces(self):
        with self.assertRaises(ApiError):
            scope.candidates(support.FakeCtx(params={"kind": "billing"}))
        with self.assertRaises(ApiError):
            scope.candidates(support.FakeCtx(params={"query": "a\nb"}))
        # The demo workspace of the unit tests is a placeholder: nothing is called.
        self.assertEqual(scope.candidates(support.FakeCtx(params={"kind": "project"})),
                         {"kind": "project", "items": [], "truncated": False})


if __name__ == "__main__":
    unittest.main()
