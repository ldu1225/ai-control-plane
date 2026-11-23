"""Organization and project budgets: validation, the stored document, spend rollups over the unit tree, used-up
lookups, threshold alerts and the GET / POST handlers."""

import hashlib
import types
import unittest
from unittest import mock

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane.gcp import GcpError
from controlplane.httpkit import ApiError
from controlplane.services import budgets, chargeback, governance, org_model
from controlplane.settings_store import StoreConflict, aux_docs

MONTH = "2026-11"
SALES = org_model.unit_id("Sales")
KOREA = org_model.unit_id("Sales/Korea")
JAPAN = org_model.unit_id("Sales/Japan")
ENG = org_model.unit_id("Engineering")
ORG = org_model.OrgModel({"version": 2, "units": {
    SALES: {"name": "Sales", "parent": None}, KOREA: {"name": "Korea", "parent": SALES},
    JAPAN: {"name": "Japan", "parent": SALES}, ENG: {"name": "Engineering", "parent": None}}})
SAME = object()


def fact(person, project, unit, chargeable, cost=SAME):
    return {"person": person, "project": project, "unit": unit, "via": "member", "requests": 1, "tokens": 10,
            "cost": chargeable if cost is SAME else cost, "credit": 0, "chargeable": chargeable, "seat": 0}


# Korea 10.00 and Japan 25.00 roll up to Sales (35.00); Engineering's cost is unknown; one fact is unattributed.
FACTS = [fact("a@example.com", "proj-alpha", KOREA, 1000, cost=1500),
         fact("b@example.com", "proj-beta", JAPAN, 2500, cost=3000),
         fact("c@example.com", "proj-alpha", ENG, None, cost=None),
         fact("d@example.com", "proj-alpha", org_model.UNASSIGNED, 700)]


def unit(uid, usd, action, thresholds=None):
    node = {"scope": "unit", "unitId": uid, "monthlyPaidUsd": usd, "action": action}
    if thresholds is not None:
        node["thresholds"] = thresholds
    return node


def project(pid, usd, action, thresholds=None):
    node = {"scope": "project", "project": pid, "monthlyPaidUsd": usd, "action": action}
    if thresholds is not None:
        node["thresholds"] = thresholds
    return node


NODES = [unit(SALES, 30, "suspend"), unit(KOREA, 20, "review"), unit(ENG, 0, "notify"),
         project("proj-alpha", 10, "hard_cap"), project("proj-beta", 100, "notify")]


def tree(*raw):
    return budgets.BudgetTree({"version": 1, "nodes": budgets.validate_nodes(list(raw), ORG)})


def raises(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except ApiError as err:
        return err.code, err.message
    raise AssertionError("ApiError not raised")


def expected_id(text):
    return "b_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


class ValidationTest(unittest.TestCase):
    def test_clean_nodes_and_defaults(self):
        nodes = budgets.validate_nodes([unit(" %s " % KOREA, 20, "review"),
                                        project(" Proj-Alpha ", 10.25, "hard_cap", [100, 50, 50, 80.0]),
                                        project("proj-beta", 0, "notify", [])], ORG)
        self.assertEqual(nodes, [
            {"id": expected_id("unit:" + KOREA), "scope": "unit", "unitId": KOREA, "monthlyPaidUsd": 20.0,
             "action": "review", "thresholds": [50, 80, 100]},
            {"id": expected_id("project:proj-alpha"), "scope": "project", "project": "proj-alpha",
             "monthlyPaidUsd": 10.25, "action": "hard_cap", "thresholds": [50, 80, 100]},
            {"id": expected_id("project:proj-beta"), "scope": "project", "project": "proj-beta",
             "monthlyPaidUsd": 0.0, "action": "notify", "thresholds": []}])

    def test_ids_are_deterministic_and_client_ids_are_ignored(self):
        first = budgets.validate_nodes([dict(unit(KOREA, 1, "notify"), id="b_0000000000")])[0]
        second = budgets.validate_nodes([unit(KOREA, 5, "suspend")])[0]
        self.assertEqual(first["id"], expected_id("unit:" + KOREA))
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(budgets.node_id("project", "proj-alpha"), expected_id("project:proj-alpha"))
        self.assertNotEqual(budgets.node_id("unit", "proj-alpha"), budgets.node_id("project", "proj-alpha"))

    def test_bad_action(self):
        self.assertEqual(raises(budgets.validate_nodes, [unit(KOREA, 1, "hard_cap")], ORG),
                         ("bad_request", "Budget 1 (unit Sales/Korea): action must be suspend, review or notify."))
        nodes = [unit(KOREA, 1, "notify"), project("proj-alpha", 1, "suspend")]
        self.assertEqual(raises(budgets.validate_nodes, nodes),
                         ("bad_request", "Budget 2 (project proj-alpha): action must be notify or hard_cap."))
        self.assertEqual(raises(budgets.validate_nodes, [project("proj-alpha", 1, None)])[0], "bad_request")

    def test_bad_thresholds(self):
        message = "Budget 1 (project proj-alpha): thresholds must be a list of whole percentages from 1 to 100."
        for value in ([0], [101], [50.5], ["50"], [True], "50", [float("nan")], {"50": 1}, list(range(1, 102))):
            self.assertEqual(raises(budgets.validate_nodes, [project("proj-alpha", 1, "notify", value)]),
                             ("bad_request", message), value)
        self.assertEqual(budgets.validate_nodes([project("proj-alpha", 1, "notify", [1, 100.0])])[0]["thresholds"],
                         [1, 100])

    def test_bad_amounts(self):
        message = ("Budget 1 (unit Sales): monthlyPaidUsd must be a number from 0 to 1000000000 (USD per month).")
        for value in (-1, -0.01, 1e9 + 1, True, "10", None, float("inf"), float("nan"), 10 ** 400, [10]):
            self.assertEqual(raises(budgets.validate_nodes, [unit(SALES, value, "notify")], ORG),
                             ("bad_request", message), value)
        for value, stored in ((0, 0.0), (1e9, 1e9), (12.345, 12.35), (7, 7.0)):
            self.assertEqual(budgets.validate_nodes([unit(SALES, value, "notify")])[0]["monthlyPaidUsd"], stored)

    def test_duplicate_targets(self):
        nodes = [unit(KOREA, 1, "notify"), project("proj-alpha", 1, "notify"), unit(KOREA, 2, "review")]
        self.assertEqual(raises(budgets.validate_nodes, nodes, ORG),
                         ("bad_request", "Budget 3: the unit Sales/Korea already has a budget (budget 1). Each unit "
                                         "and project can have one budget."))
        self.assertEqual(raises(budgets.validate_nodes,
                                [project("proj-alpha", 1, "notify"), project("PROJ-ALPHA", 2, "hard_cap")]),
                         ("bad_request", "Budget 2: the project proj-alpha already has a budget (budget 1). Each unit "
                                         "and project can have one budget."))
        # The same id is allowed once per scope.
        self.assertEqual(len(budgets.validate_nodes([unit(KOREA, 1, "notify"), project("proj-alpha", 1, "notify")])),
                         2)

    def test_unknown_and_malformed_units(self):
        self.assertEqual(raises(budgets.validate_nodes, [unit("u_0123456789", 1, "notify")], ORG),
                         ("bad_request", "Budget 1: unit u_0123456789 is not in the organization model. Reload the "
                                         "organization tree and choose a unit from it."))
        self.assertEqual(raises(budgets.validate_nodes, [unit(KOREA, 1, "notify")], org_model.OrgModel()),
                         ("bad_request", "Budget 1: unit %s is not in the organization model. Reload the organization "
                                         "tree and choose a unit from it." % KOREA))
        self.assertEqual(len(budgets.validate_nodes([unit("u_0123456789", 1, "notify")])), 1)  # no model: format only
        for value in ("Sales", "u_012345678", "u_0123456789a", "U_0123456789", None, 42):
            self.assertEqual(raises(budgets.validate_nodes, [unit(value, 1, "notify")]),
                             ("bad_request", "Budget 1: unitId must be the id of a unit in the organization tree "
                                             "(u_ and 10 hexadecimal digits)."), value)
        # The virtual Unassigned unit collects usage that no rule attributes.
        node = budgets.validate_nodes([unit(org_model.UNASSIGNED, 1, "review")], ORG)[0]
        self.assertEqual(node["unitId"], org_model.UNASSIGNED)

    def test_bad_projects_scopes_and_shapes(self):
        for value in ("Bad_Project!", "", None, "ab", "x" * 101, 7):
            self.assertEqual(raises(budgets.validate_nodes, [project(value, 1, "notify")]),
                             ("bad_request", "Budget 1: project must be a Google Cloud project id."), value)
        self.assertEqual(raises(budgets.validate_nodes, [{"scope": "org", "monthlyPaidUsd": 1, "action": "notify"}]),
                         ("bad_request", "Budget 1: scope must be unit or project."))
        self.assertEqual(raises(budgets.validate_nodes, [unit(KOREA, 1, "notify"), "junk"]),
                         ("bad_request", "Budget 2 must be an object."))
        for value in (None, {"nodes": []}, "[]", 5):
            self.assertEqual(raises(budgets.validate_nodes, value),
                             ("bad_request", "Field 'nodes' must be a list of budgets."))
        self.assertEqual(budgets.validate_nodes([]), [])

    def test_too_many(self):
        nodes = [project("proj-%05d" % i, 1, "notify") for i in range(budgets.MAX_NODES)]
        self.assertEqual(len(budgets.validate_nodes(nodes)), 5000)
        self.assertEqual(raises(budgets.validate_nodes, nodes + [project("proj-extra", 1, "notify")]),
                         ("bad_request", "At most 5000 budgets are allowed; the request has 5001."))
        with mock.patch.object(budgets, "MAX_NODES", 2):
            self.assertEqual(raises(budgets.validate_nodes, nodes[:3]),
                             ("bad_request", "At most 2 budgets are allowed; the request has 3."))

    def test_hard_caps_only_for_workspace_projects(self):
        # A hard cap becomes a Google spend cap that pauses Agent Platform in the project: only for this workspace.
        workspace = {"proj-alpha"}
        self.assertEqual(raises(budgets.validate_nodes, [project("proj-alpha", 1, "hard_cap"),
                                                         project("other-team", 1, "hard_cap")], workspace=workspace),
                         ("bad_request", "Budget 2 (project other-team): a hard cap is allowed only for projects of "
                                         "this workspace. Choose notify or a workspace project."))
        nodes = budgets.validate_nodes([project(" Proj-Alpha ", 1, "hard_cap"), project("other-team", 1, "notify")],
                                       workspace=workspace)
        self.assertEqual([(n["project"], n["action"]) for n in nodes],
                         [("proj-alpha", "hard_cap"), ("other-team", "notify")])
        self.assertEqual(raises(budgets.validate_nodes, [project("proj-alpha", 1, "hard_cap")], workspace=set())[0],
                         "bad_request")   # an empty workspace (the project list failed) allows no hard cap
        # Without a workspace (stored documents are read that way) only the format is checked.
        self.assertEqual(len(budgets.validate_nodes([project("other-team", 1, "hard_cap")])), 1)


class StoredTreeTest(unittest.TestCase):
    def test_missing_document(self):
        loaded = budgets.load(types.SimpleNamespace(app=types.SimpleNamespace()))
        self.assertEqual((loaded.version, loaded.updated_at, loaded.updated_by, loaded.nodes, loaded.empty),
                         (0, None, None, [], True))
        self.assertEqual((loaded.units, loaded.projects), ({}, {}))

    def test_damaged_documents_are_read_defensively(self):
        app = types.SimpleNamespace()
        good = budgets.validate_nodes([unit(KOREA, 20, "review"), project("proj-alpha", 10, "hard_cap")])
        raw = [good[0], dict(good[0], action="explode"), dict(good[0], monthlyPaidUsd=99), "junk", None, good[1],
               dict(good[1], scope="project", project="Bad_Project!")]
        aux_docs(app).mutate(budgets.DOC, lambda doc: doc.update(
            {"version": 3, "updatedAt": "2026-11-19T03:00:00Z", "updatedBy": "admin@example.com", "nodes": raw}))
        with self.assertLogs("controlplane.budgets", level="WARNING"):
            loaded = budgets.load(types.SimpleNamespace(app=app))
        self.assertEqual(loaded.version, 3)
        self.assertEqual(loaded.updated_by, "admin@example.com")
        self.assertEqual(loaded.nodes, good)
        self.assertEqual(loaded.units, {KOREA: good[0]})
        self.assertEqual(loaded.projects, {"proj-alpha": good[1]})
        self.assertFalse(loaded.empty)


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        self.tree = tree(*NODES)
        self.status = budgets.evaluate(self.tree, ORG, FACTS)

    def test_unit_spend_rolls_up_to_parents(self):
        sales = self.status.units[SALES]
        self.assertEqual((sales["budgetCents"], sales["spentCents"], sales["ratio"], sales["usedUp"], sales["crossed"]),
                         (3000, 3500, 1.1667, True, [50, 80, 100]))
        korea = self.status.units[KOREA]
        self.assertEqual((korea["spentCents"], korea["ratio"], korea["usedUp"], korea["crossed"]),
                         (1000, 0.5, False, [50]))
        self.assertIs(korea["node"], self.tree.units[KOREA])
        self.assertEqual(set(self.status.units), {SALES, KOREA, ENG})

    def test_unknown_amounts_count_zero_and_a_zero_budget_is_used_up(self):
        eng = self.status.units[ENG]
        self.assertEqual((eng["budgetCents"], eng["spentCents"], eng["ratio"], eng["usedUp"], eng["crossed"]),
                         (0, 0, None, True, []))
        japan = budgets.evaluate(tree(unit(JAPAN, 0, "notify")), ORG, FACTS).units[JAPAN]
        self.assertEqual((japan["spentCents"], japan["ratio"], japan["usedUp"], japan["crossed"]),
                         (2500, None, True, [50, 80, 100]))  # any paid usage exceeds a budget of 0

    def test_project_spend_and_cost(self):
        alpha, beta = self.status.projects["proj-alpha"], self.status.projects["proj-beta"]
        self.assertEqual((alpha["spentCents"], alpha["costCents"], alpha["usedUp"], alpha["crossed"]),
                         (1700, 2200, True, [50, 80, 100]))
        self.assertEqual((beta["spentCents"], beta["costCents"], beta["ratio"], beta["usedUp"], beta["crossed"]),
                         (2500, 3000, 0.25, False, []))
        empty = budgets.evaluate(tree(project("proj-gamma", 5, "notify")), ORG, FACTS).projects["proj-gamma"]
        self.assertEqual((empty["spentCents"], empty["costCents"], empty["usedUp"]), (0, 0, False))

    def test_thresholds_are_reached_exactly(self):
        status = budgets.evaluate(tree(unit(KOREA, 12.5, "notify", [79, 80, 81])), ORG, FACTS)
        self.assertEqual(status.units[KOREA]["crossed"], [79, 80])  # 10.00 of 12.50 is exactly 80%
        self.assertFalse(status.units[KOREA]["usedUp"])
        exact = budgets.evaluate(tree(unit(KOREA, 10, "suspend")), ORG, FACTS).units[KOREA]
        self.assertTrue(exact["usedUp"])  # spend equal to the budget uses it up

    def test_suspend_and_review_lookups_walk_the_ancestor_chain(self):
        sales = self.tree.units[SALES]
        self.assertEqual(self.status.suspend_nodes_for([KOREA]), [sales])
        self.assertEqual(self.status.suspend_nodes_for(KOREA), [sales])
        self.assertEqual(self.status.suspend_nodes_for([KOREA, JAPAN, SALES]), [sales])  # no duplicates
        self.assertEqual(self.status.suspend_nodes_for([ENG, org_model.UNASSIGNED]), [])
        self.assertEqual(self.status.suspend_nodes_for([]), [])
        self.assertEqual(self.status.review_nodes_for([KOREA]), [])  # Korea is not used up
        status = budgets.evaluate(tree(unit(SALES, 30, "review"), unit(KOREA, 5, "review")), ORG, FACTS)
        korea, sales_review = status.units[KOREA]["node"], status.units[SALES]["node"]
        self.assertEqual(status.review_nodes_for([KOREA, JAPAN]), [korea, sales_review])  # nearest first
        self.assertEqual(status.suspend_nodes_for([KOREA]), [])

    def test_unknown_units_count_for_the_unassigned_budget(self):
        status = budgets.evaluate(tree(unit(org_model.UNASSIGNED, 1, "suspend")), ORG,
                                  FACTS + [fact("e@example.com", "proj-beta", "u_9999999999", 50)])
        self.assertEqual(status.units[org_model.UNASSIGNED]["spentCents"], 750)
        self.assertEqual([n["unitId"] for n in status.suspend_nodes_for(["u_9999999999"])], [org_model.UNASSIGNED])

    def test_public_status(self):
        stale = "u_0123456789"
        extra = [unit(stale, 5, "notify"), unit(org_model.UNASSIGNED, 5, "review")]
        nodes = budgets.validate_nodes(NODES + extra)  # the stale unit was saved before it left the model
        status = budgets.evaluate(budgets.BudgetTree({"nodes": nodes}), ORG, FACTS).public()
        self.assertEqual([u["unitId"] for u in status["units"]], [ENG, SALES, KOREA, org_model.UNASSIGNED, stale])
        self.assertEqual(status["units"][1], {
            "id": budgets.node_id("unit", SALES), "unitId": SALES, "path": "Sales", "depth": 0, "known": True,
            "budget": 30.0, "spent": 35.0, "ratio": 1.1667, "usedUp": True, "action": "suspend",
            "thresholds": [50, 80, 100], "crossed": [50, 80, 100]})
        self.assertEqual((status["units"][2]["path"], status["units"][2]["depth"]), ("Sales/Korea", 1))
        self.assertEqual((status["units"][3]["path"], status["units"][3]["known"], status["units"][3]["spent"]),
                         ("Unassigned", True, 7.0))
        self.assertEqual((status["units"][4]["path"], status["units"][4]["known"]), (None, False))
        self.assertEqual(status["projects"][0], {
            "id": budgets.node_id("project", "proj-alpha"), "project": "proj-alpha", "budget": 10.0, "spent": 17.0,
            "cost": 22.0, "ratio": 1.7, "usedUp": True, "action": "hard_cap", "thresholds": [50, 80, 100],
            "crossed": [50, 80, 100]})
        self.assertEqual([p["project"] for p in status["projects"]], ["proj-alpha", "proj-beta"])


class AlertsTest(unittest.TestCase):
    def setUp(self):
        self.tree = tree(*NODES)
        self.status = budgets.evaluate(self.tree, ORG, FACTS)
        self.sales, self.korea = self.tree.units[SALES], self.tree.units[KOREA]
        self.alpha = self.tree.projects["proj-alpha"]
        self.all = [(self.sales, 50), (self.sales, 80), (self.sales, 100), (self.korea, 50),
                    (self.alpha, 50), (self.alpha, 80), (self.alpha, 100)]

    def test_new_alerts_skip_announced_thresholds(self):
        self.assertEqual(budgets.new_alerts(self.status, MONTH, {}), self.all)
        notified = {MONTH: {self.sales["id"]: [50], self.alpha["id"]: [50, 80, 100]}, "2026-10": {}}
        self.assertEqual(budgets.new_alerts(self.status, MONTH, notified),
                         [(self.sales, 80), (self.sales, 100), (self.korea, 50)])
        self.assertEqual(budgets.new_alerts(self.status, "2026-12", notified), self.all)
        # The alerts document only counts for its own month.
        doc = {"month": MONTH, "notified": {self.sales["id"]: [50, 80, 100]}}
        self.assertEqual(budgets.new_alerts(self.status, MONTH, doc), self.all[3:])
        self.assertEqual(budgets.new_alerts(self.status, "2026-12", doc), self.all)
        self.assertEqual(budgets.new_alerts(self.status, MONTH, None), self.all)

    def test_record_alerts_returns_fresh_pairs_and_starts_over_each_month(self):
        ctx = types.SimpleNamespace(app=types.SimpleNamespace())
        self.assertEqual(budgets.load_alerts(ctx), {"month": None, "notified": {}})
        self.assertEqual(budgets.record_alerts(ctx, MONTH, self.all[:2]), self.all[:2])
        self.assertEqual(budgets.record_alerts(ctx, MONTH, self.all), self.all[2:])  # only the pairs not stored yet
        self.assertEqual(budgets.record_alerts(ctx, MONTH, self.all), [])
        stored = budgets.load_alerts(ctx)
        self.assertEqual(stored, {"month": MONTH, "notified": {self.sales["id"]: [50, 80, 100],
                                                               self.korea["id"]: [50],
                                                               self.alpha["id"]: [50, 80, 100]}})
        self.assertEqual(budgets.new_alerts(self.status, MONTH, stored), [])
        self.assertEqual(budgets.record_alerts(ctx, "2026-12", [(self.korea, 50)]), [(self.korea, 50)])
        self.assertEqual(budgets.load_alerts(ctx), {"month": "2026-12", "notified": {self.korea["id"]: [50]}})
        # A late run for a month that is over announces nothing and keeps the new month.
        self.assertEqual(budgets.record_alerts(ctx, MONTH, self.all), [])
        self.assertEqual(budgets.load_alerts(ctx)["month"], "2026-12")
        self.assertEqual(budgets.record_alerts(ctx, "2026-12", []), [])

    def test_record_alerts_checks_its_input(self):
        ctx = types.SimpleNamespace(app=types.SimpleNamespace())
        for month in ("2026-13", "202611", None, "2026-1"):
            with self.assertRaises(ValueError):
                budgets.record_alerts(ctx, month, self.all)
        junk = [({"id": "not-an-id"}, 50), (self.sales, 0), (self.sales, True), ("node", 50)]
        self.assertEqual(budgets.record_alerts(ctx, MONTH, junk), [])

    def test_record_alerts_conflict(self):
        ctx = types.SimpleNamespace(app=types.SimpleNamespace())
        with mock.patch.object(aux_docs(ctx.app), "mutate", side_effect=StoreConflict()):
            self.assertEqual(raises(budgets.record_alerts, ctx, MONTH, self.all)[0], "conflict")

    def test_alert_text(self):
        self.assertEqual(budgets.alert_text(self.korea, 50, self.status, ORG),
                         "Unit Sales/Korea reached 50% of its monthly paid budget: USD 10.00 of USD 20.00 spent this "
                         "month (action: review).")
        self.assertEqual(budgets.alert_text(self.alpha, 100, self.status, ORG),
                         "Project proj-alpha reached 100% of its monthly paid budget: USD 17.00 of USD 10.00 spent "
                         "this month (action: hard cap).")
        big = budgets.evaluate(tree(unit(SALES, 2.5, "suspend")), ORG, FACTS * 100)
        self.assertIn("USD 3,500.00 of USD 2.50", budgets.alert_text(big.units[SALES]["node"], 100, big, ORG))
        japan = tree(unit(JAPAN, 0, "notify"))
        zero = budgets.evaluate(japan, ORG, FACTS)
        self.assertEqual(budgets.alert_text(japan.units[JAPAN], 50, zero, ORG),
                         "Unit Sales/Japan has paid usage of USD 25.00 this month, but its monthly paid budget is "
                         "USD 0 (action: notify).")

    def test_alert_text_never_contains_email_addresses(self):
        odd = org_model.unit_id("ops@example.com")
        org = org_model.OrgModel({"units": {odd: {"name": "ops@example.com", "parent": None}}})
        node = budgets.validate_nodes([unit(odd, 1, "notify")], org)[0]
        status = budgets.evaluate(budgets.BudgetTree({"nodes": [node]}), org,
                                  [fact("ops@example.com", "proj-alpha", odd, 100)])
        text = budgets.alert_text(node, 100, status, org)
        self.assertNotIn("@", text)
        self.assertIn("<email>", text)


class HandlerTest(unittest.TestCase):
    def setUp(self):
        # proj-alpha has a hard cap in NODES: hard caps are allowed only for workspace projects (ctx.all_projects).
        self.ctx = support.FakeCtx(settings=support.make_settings(workspace={"projects": ["proj-alpha", "proj-beta"]}),
                                   body={"nodes": NODES})
        patches = [mock.patch.object(chargeback, "current_month", return_value=MONTH),
                   mock.patch.object(org_model, "load", return_value=ORG),
                   mock.patch.object(governance, "write_audit", return_value=True)]
        self.current_month, self.org_load, self.audit = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)

    def build(self, **kwargs):
        model = {"summary": {"currency": "USD"}, "users": [], "facts": FACTS, "orgVersion": 2}
        return mock.patch.object(chargeback, "build", **(kwargs or {"return_value": model}))

    def test_save_then_get(self):
        saved = budgets.save_budgets(self.ctx)
        self.assertEqual((saved["version"], saved["updatedBy"], saved["auditLogged"]), (1, "admin@example.com", True))
        self.assertEqual(saved["nodes"], budgets.validate_nodes(NODES, ORG))
        self.audit.assert_called_once_with(self.ctx, "budgets_save", {
            "nodes": 5, "units": 3, "projects": 2, "suspend": 1, "review": 1, "notify": 2, "hardCap": 1, "version": 1})
        self.assertNotIn("@", repr(self.audit.call_args[0][2]))
        self.assertEqual(self.ctx.app.invalidations, 1)
        self.assertEqual(budgets.save_budgets(self.ctx)["version"], 2)
        with self.build() as build:
            out = budgets.get_budgets(self.ctx)
        build.assert_called_once_with(self.ctx, MONTH)
        self.assertEqual((out["version"], out["updatedBy"], out["month"], out["currency"]),
                         (2, "admin@example.com", MONTH, "USD"))
        self.assertEqual(out["nodes"], saved["nodes"])
        self.assertEqual([u["unitId"] for u in out["status"]["units"]], [ENG, SALES, KOREA])
        self.assertEqual(out["status"]["projects"][0]["spent"], 17.0)
        self.assertEqual(out["actions"], {"unit": ["suspend", "review", "notify"], "project": ["notify", "hard_cap"]})
        self.assertEqual((out["defaultThresholds"], out["maxNodes"], out["spendCaps"]), ([50, 80, 100], 5000, False))
        self.assertEqual(self.ctx.warnings, [])

    def test_get_without_budgets_skips_the_chargeback_figures(self):
        with self.build(side_effect=AssertionError("not needed")):
            out = budgets.get_budgets(self.ctx)
        self.assertEqual((out["version"], out["nodes"], out["status"]), (0, [], {"units": [], "projects": []}))

    def test_status_failures_become_warnings(self):
        budgets.save_budgets(self.ctx)
        cases = [({"side_effect": GcpError("denied", 403)}, "denied", False),
                 ({"side_effect": ApiError("upstream_error", "The organization model could not be read.")},
                  "status_unavailable", True),
                 ({"return_value": {"summary": {"currency": "KRW"}, "facts": FACTS}}, "currency_mismatch", False),
                 ({"side_effect": ValueError("bug")}, "internal_error", True)]   # logged with its traceback
        for kwargs, code, logs in cases:
            self.ctx.warnings = []
            with self.build(**kwargs):
                if logs:
                    with self.assertLogs("controlplane.budgets", level="WARNING"):
                        out = budgets.get_budgets(self.ctx)
                else:
                    out = budgets.get_budgets(self.ctx)
            self.assertIsNone(out["status"], code)
            self.assertEqual(len(out["nodes"]), 5)
            self.assertEqual([(w["source"], w["code"]) for w in self.ctx.warnings], [("budgets:status", code)])

    def test_save_requires_an_administrator(self):
        ctx = support.FakeCtx(body={"nodes": NODES}, role="viewer")
        self.assertEqual(raises(budgets.save_budgets, ctx), ("forbidden", "Administrator privileges are required."))
        self.assertEqual(budgets.load(ctx).version, 0)

    def test_save_validates_against_the_organization_model(self):
        self.ctx.body = {"nodes": NODES + [unit("u_0123456789", 1, "notify")]}
        self.assertEqual(raises(budgets.save_budgets, self.ctx)[0], "bad_request")
        self.ctx.body = {}
        self.assertEqual(raises(budgets.save_budgets, self.ctx), ("bad_request", "Field 'nodes' is required."))
        self.assertEqual(budgets.load(self.ctx).version, 0)
        self.audit.assert_not_called()
        self.ctx.body = {"nodes": []}
        self.assertEqual(budgets.save_budgets(self.ctx)["nodes"], [])  # removing every budget is a valid save

    def test_store_failures(self):
        store = aux_docs(self.ctx.app)
        with mock.patch.object(store, "mutate", side_effect=StoreConflict()):
            self.assertEqual(raises(budgets.save_budgets, self.ctx)[0], "conflict")
        with mock.patch.object(store, "mutate", side_effect=OSError("disk")), \
                self.assertLogs("controlplane.budgets", level="ERROR"):
            self.assertEqual(raises(budgets.save_budgets, self.ctx)[0], "upstream_error")
        with mock.patch.object(store, "load", side_effect=OSError("disk")), \
                self.assertLogs("controlplane.budgets", level="WARNING"):
            self.assertEqual(raises(budgets.load, self.ctx)[0], "upstream_error")

    def test_a_failed_audit_entry_does_not_undo_the_save(self):
        self.audit.side_effect = RuntimeError("logging down")
        with self.assertLogs("controlplane.budgets", level="ERROR"):
            saved = budgets.save_budgets(self.ctx)
        self.assertFalse(saved["auditLogged"])
        self.assertEqual(budgets.load(self.ctx).version, 1)

    def test_save_refuses_hard_caps_outside_the_workspace(self):
        self.ctx.body = {"nodes": NODES + [project("other-team", 5, "hard_cap")]}
        self.assertEqual(raises(budgets.save_budgets, self.ctx),
                         ("bad_request", "Budget 6 (project other-team): a hard cap is allowed only for projects of "
                                         "this workspace. Choose notify or a workspace project."))
        self.assertEqual(budgets.load(self.ctx).version, 0)
        self.audit.assert_not_called()
        self.ctx.body = {"nodes": NODES + [project("other-team", 5, "notify")]}   # alerts only: any project
        self.assertEqual(len(budgets.save_budgets(self.ctx)["nodes"]), 6)

    def test_only_hard_caps_need_the_project_list_and_it_fails_closed(self):
        class NoProjects(support.FakeCtx):
            @property
            def all_projects(self):
                raise GcpError("unavailable", 503)
        ctx = NoProjects(body={"nodes": [project("proj-beta", 100, "notify")]})
        self.assertEqual(len(budgets.save_budgets(ctx)["nodes"]), 1)
        ctx.body = {"nodes": NODES}
        with self.assertLogs("controlplane.budgets", level="WARNING"):
            self.assertEqual(raises(budgets.save_budgets, ctx)[0], "bad_request")
        self.assertEqual(budgets.load(ctx).version, 1)


if __name__ == "__main__":
    unittest.main()
