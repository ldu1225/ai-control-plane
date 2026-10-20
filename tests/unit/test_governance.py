"""Antigravity IAM 6-state classification, safety gates G1-G5, plan/change budget and policy mutation
(agy-admin semantics)."""

import copy
import unittest

import support
from support import ACCESS_ROLE, ADMIN_MARKER, CAP_MARKER, LEGACY_MARKER

from controlplane.services import governance as gov

CFG = gov.gov_config(support.make_settings(governance={"extra_access_roles": ["roles/discoveryengine.editor"]}))


def policy(*bindings, etag="BwYfakeEtag="):
    return {"version": 3, "etag": etag, "bindings": [dict(b) for b in bindings]}


def b(role, *members, condition=None):
    out = {"role": role, "members": list(members)}
    if condition:
        out["condition"] = condition
    return out


COND = {"title": "business-hours", "expression": "request.time.getHours('UTC') < 18"}


def classify(pol, email, has_usage=False, ancestor=lambda e: False, readable=True):
    return gov.classify(email, has_usage, gov.PolicyIndex(pol, readable=readable), CFG, ancestor)


class GovConfigTest(unittest.TestCase):
    def test_marker_names_are_project_scoped(self):
        self.assertEqual(CFG.cap_marker, CAP_MARKER)
        self.assertEqual(CFG.admin_marker, ADMIN_MARKER)
        self.assertEqual(CFG.legacy_markers, (LEGACY_MARKER,))
        self.assertEqual(CFG.access_role, ACCESS_ROLE)


class ClassifierTest(unittest.TestCase):
    def test_policy_unreadable(self):
        self.assertEqual(classify(None, "a@example.com", readable=False), ("UNKNOWN", "POLICY_UNREADABLE"))

    def test_enforceable_allowed(self):
        pol = policy(b(ACCESS_ROLE, "user:a@example.com", "group:team@example.com"))
        # Group grants coexist (G5 warns) but the direct unconditional binding is enforceable.
        self.assertEqual(classify(pol, "A@Example.com", has_usage=True), ("ENFORCEABLE_ALLOWED", None))

    def test_capped_and_blocked(self):
        pol = policy(b(CAP_MARKER, "user:c@example.com"), b(ADMIN_MARKER, "user:d@example.com"),
                     b(LEGACY_MARKER, "user:e@example.com"))
        self.assertEqual(classify(pol, "c@example.com"), ("CAPPED_BY_TOOL", None))
        self.assertEqual(classify(pol, "d@example.com"), ("BLOCKED_BY_ADMIN", None))
        self.assertEqual(classify(pol, "e@example.com"), ("BLOCKED_BY_ADMIN", None))

    def test_not_enforceable_reasons(self):
        pol = policy(b(ACCESS_ROLE, "user:owner@example.com", "user:both@example.com"),
                     b("roles/owner", "user:owner@example.com"),
                     b("roles/discoveryengine.editor", "user:other@example.com"),
                     b(CAP_MARKER, "user:both@example.com"),
                     b(ACCESS_ROLE, "user:cond@example.com", condition=COND))
        self.assertEqual(classify(pol, "owner@example.com"), ("NOT_ENFORCEABLE", "PRIVILEGED_ROLE"))
        self.assertEqual(classify(pol, "other@example.com"), ("NOT_ENFORCEABLE", "OTHER_ACCESS_ROLE"))
        self.assertEqual(classify(pol, "both@example.com"), ("NOT_ENFORCEABLE", "CONFLICTING_BINDINGS"))
        self.assertEqual(classify(pol, "cond@example.com"), ("NOT_ENFORCEABLE", "CONFLICTING_BINDINGS"))

    def test_group_and_domain_grants(self):
        pol = policy(b(ACCESS_ROLE, "group:team@example.com"))
        self.assertEqual(classify(pol, "g@example.com", has_usage=True), ("NOT_ENFORCEABLE", "GROUP_GRANT_PRESENT"))
        dom = policy(b(ACCESS_ROLE, "domain:example.com"))
        self.assertEqual(classify(dom, "g@example.com", has_usage=True), ("NOT_ENFORCEABLE", "GROUP_GRANT_PRESENT"))
        other_domain = policy(b(ACCESS_ROLE, "domain:other.com"))
        self.assertEqual(classify(other_domain, "g@example.com", has_usage=True), ("NO_ACCESS", None))

    def test_inherited_and_unchecked_ancestors(self):
        pol = policy()
        self.assertEqual(classify(pol, "i@example.com", True, ancestor=lambda e: True),
                         ("NOT_ENFORCEABLE", "INHERITED_BINDING"))
        self.assertEqual(classify(pol, "i@example.com", True, ancestor=lambda e: None),
                         ("UNKNOWN", "ANCESTOR_UNCHECKED"))
        self.assertEqual(classify(pol, "i@example.com", True, ancestor=lambda e: False), ("NO_ACCESS", None))
        self.assertEqual(classify(pol, "i@example.com", True, ancestor=None), ("UNKNOWN", "ANCESTOR_UNCHECKED"))

    def test_no_access_without_usage(self):
        self.assertEqual(classify(policy(b("roles/viewer", "user:n@example.com")), "n@example.com",
                                  ancestor=lambda e: None), ("NO_ACCESS", None))

    def test_over_cap(self):
        self.assertEqual(gov.over_cap(500000, 10, 500000, 1000), (True, 1.0))
        self.assertEqual(gov.over_cap(100, 1000, 500000, 1000), (True, 1.0))
        self.assertEqual(gov.over_cap(250000, 10, 500000, 1000), (False, 0.5))
        self.assertEqual(gov.over_cap(10 ** 9, 10 ** 9, 0, 0), (False, 0.0))


def user(uid, state, over):
    return {"uid": uid, "state": state, "overCap": over}


class GatesAndPlanTest(unittest.TestCase):
    def evaluate(self, users, readable=True, telemetry_ok=True, records=10, non_user=(), cfg=CFG):
        return gov.evaluate(users, readable, telemetry_ok, records, list(non_user), cfg, ACCESS_ROLE)

    def gate(self, gates, gate_id):
        return next(g["status"] for g in gates if g["id"] == gate_id)

    def test_all_gates_pass_and_plan(self):
        users = [user("ea_over", "ENFORCEABLE_ALLOWED", True), user("ea_ok", "ENFORCEABLE_ALLOWED", False),
                 user("cap_ok", "CAPPED_BY_TOOL", False), user("cap_over", "CAPPED_BY_TOOL", True),
                 user("ne_over", "NOT_ENFORCEABLE", True), user("unk_over", "UNKNOWN", True),
                 user("blocked", "BLOCKED_BY_ADMIN", True), user("none", "NO_ACCESS", False)]
        gates, plan, budget = self.evaluate(users)
        self.assertEqual([g["id"] for g in gates], ["G1_POLICY_READABLE", "G2_ANY_DETERMINATE",
                                                     "G3_TELEMETRY_HEALTHY", "G4_CHANGE_BUDGET",
                                                     "G5_NON_USER_GRANT_ABSENT"])
        self.assertTrue(all(g["status"] == "pass" for g in gates))
        self.assertEqual(plan["block"], ["ea_over"])
        self.assertEqual(plan["restore"], ["cap_ok"])
        self.assertEqual(plan["manual"], ["ne_over", "unk_over"])
        self.assertFalse(plan["aborted"])
        self.assertIsNone(plan["abortReason"])
        self.assertEqual(budget, 10)

    def test_g1_policy_unreadable_aborts(self):
        gates, plan, _ = self.evaluate([user("u", "UNKNOWN", True)], readable=False)
        self.assertEqual(self.gate(gates, "G1_POLICY_READABLE"), "fail")
        self.assertTrue(plan["aborted"])
        self.assertIn("G1_POLICY_READABLE", plan["abortReason"])

    def test_g2_all_unknown_aborts(self):
        gates, plan, _ = self.evaluate([user("u1", "UNKNOWN", False), user("u2", "UNKNOWN", True)])
        self.assertEqual(self.gate(gates, "G2_ANY_DETERMINATE"), "fail")
        self.assertTrue(plan["aborted"])
        gates, _, _ = self.evaluate([])
        self.assertEqual(self.gate(gates, "G2_ANY_DETERMINATE"), "pass")

    def test_g3_unhealthy_telemetry_withholds_restores(self):
        users = [user("cap_ok", "CAPPED_BY_TOOL", False), user("ea_over", "ENFORCEABLE_ALLOWED", True)]
        for telemetry_ok, records in ((False, 0), (True, 0)):
            gates, plan, _ = self.evaluate(users, telemetry_ok=telemetry_ok, records=records)
            self.assertEqual(self.gate(gates, "G3_TELEMETRY_HEALTHY"), "warn")
            self.assertEqual(plan["restore"], [])
            self.assertTrue(plan["withheldRestores"])
            self.assertFalse(plan["aborted"])

    def test_unknown_usage_never_restores_or_blocks(self):
        users = [user("cap", "CAPPED_BY_TOOL", None), user("ea", "ENFORCEABLE_ALLOWED", None)]
        _, plan, _ = self.evaluate(users, telemetry_ok=True, records=5)
        self.assertEqual((plan["block"], plan["restore"]), ([], []))

    def test_g4_change_budget(self):
        tight = gov.GovConfig(**dict(CFG.__dict__, change_budget_min=1, change_budget_ratio=0.1))
        users = [user("a", "ENFORCEABLE_ALLOWED", True), user("b", "ENFORCEABLE_ALLOWED", True),
                 user("c", "NO_ACCESS", False)]
        gates, plan, budget = self.evaluate(users, cfg=tight)
        self.assertEqual(budget, 1)  # max(1, ceil(0.1 * 3))
        self.assertEqual(self.gate(gates, "G4_CHANGE_BUDGET"), "fail")
        self.assertTrue(plan["aborted"])
        ratio_cfg = gov.GovConfig(**dict(CFG.__dict__, change_budget_min=0, change_budget_ratio=0.5))
        _, plan, budget = self.evaluate(users, cfg=ratio_cfg)
        self.assertEqual(budget, 2)  # ceil(0.5 * 3)
        self.assertFalse(plan["aborted"])

    def test_g5_group_grants_warn(self):
        gates, plan, _ = self.evaluate([user("a", "ENFORCEABLE_ALLOWED", False)], non_user=["group:t@example.com"])
        self.assertEqual(self.gate(gates, "G5_NON_USER_GRANT_ABSENT"), "warn")
        self.assertFalse(plan["aborted"])


class MutationTest(unittest.TestCase):
    def base(self):
        return policy(b(ACCESS_ROLE, "user:a@example.com", "user:b@example.com", "group:team@example.com"),
                      b(CAP_MARKER, "user:c@example.com"),
                      b(ADMIN_MARKER, "user:d@example.com"),
                      b(LEGACY_MARKER, "user:d@example.com"),
                      b(ACCESS_ROLE, "user:a@example.com", condition=COND),
                      b("roles/viewer", "user:a@example.com"))

    def roles_of(self, pol, email, conditional=False):
        member = "user:%s" % email
        return sorted(x["role"] for x in pol["bindings"]
                      if member in x["members"] and bool(x.get("condition")) == conditional)

    def test_block_moves_access_to_cap_marker(self):
        original = self.base()
        new = gov.apply_changes(original, [("A@Example.com", "block")], CFG)
        self.assertEqual(self.roles_of(new, "a@example.com"), sorted([CAP_MARKER, "roles/viewer"]))
        self.assertEqual(self.roles_of(new, "a@example.com", conditional=True), [ACCESS_ROLE])  # untouched
        self.assertIn("group:team@example.com", new["bindings"][0]["members"])
        self.assertIn("user:b@example.com", new["bindings"][0]["members"])
        self.assertEqual((new["etag"], new["version"]), ("BwYfakeEtag=", 3))
        self.assertEqual(original, self.base())  # input is not mutated

    def test_restore_moves_cap_marker_to_access(self):
        new = gov.apply_changes(self.base(), [("c@example.com", "restore")], CFG)
        self.assertEqual(self.roles_of(new, "c@example.com"), [ACCESS_ROLE])
        self.assertNotIn(CAP_MARKER, [x["role"] for x in new["bindings"]])  # empty binding removed

    def test_disallow_and_allow(self):
        new = gov.apply_changes(self.base(), [("b@example.com", "disallow")], CFG)
        self.assertEqual(self.roles_of(new, "b@example.com"), [ADMIN_MARKER])
        back = gov.apply_changes(new, [("b@example.com", "allow")], CFG)
        self.assertEqual(self.roles_of(back, "b@example.com"), [ACCESS_ROLE])
        allowed = gov.apply_changes(self.base(), [("d@example.com", "allow")], CFG)
        self.assertEqual(self.roles_of(allowed, "d@example.com"), [ACCESS_ROLE])  # admin + legacy removed
        new_user = gov.apply_changes(policy(), [("n@example.com", "allow")], CFG)
        self.assertEqual(new_user["bindings"], [{"role": ACCESS_ROLE, "members": ["user:n@example.com"]}])

    def test_batch_is_one_policy(self):
        new = gov.apply_changes(self.base(), [("a@example.com", "block"), ("c@example.com", "restore")], CFG)
        self.assertIn(CAP_MARKER, self.roles_of(new, "a@example.com"))
        self.assertEqual(self.roles_of(new, "c@example.com"), [ACCESS_ROLE])
        with self.assertRaises(ValueError):
            gov.apply_changes(self.base(), [("a@example.com", "delete")], CFG)

    def test_transitions_never_touch_not_enforceable_or_unknown(self):
        for action in ("block", "restore", "allow", "disallow"):
            for state in ("NOT_ENFORCEABLE", "UNKNOWN"):
                with self.assertRaises(ValueError):
                    gov.check_transition(state, action)
        gov.check_transition("ENFORCEABLE_ALLOWED", "block")
        gov.check_transition("CAPPED_BY_TOOL", "restore")
        gov.check_transition("CAPPED_BY_TOOL", "disallow")
        gov.check_transition("NO_ACCESS", "allow")
        with self.assertRaises(ValueError):
            gov.check_transition("BLOCKED_BY_ADMIN", "restore")
        with self.assertRaises(ValueError):
            gov.check_transition("ENFORCEABLE_ALLOWED", "allow")
        self.assertEqual(copy.deepcopy(gov.TARGET_STATE), {"block": "CAPPED_BY_TOOL", "restore": "ENFORCEABLE_ALLOWED",
                                                           "disallow": "BLOCKED_BY_ADMIN",
                                                           "allow": "ENFORCEABLE_ALLOWED"})


if __name__ == "__main__":
    unittest.main()
