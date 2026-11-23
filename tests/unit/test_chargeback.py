"""Chargeback: integer-cent facts per (person, project), organization rollups and the month close."""

import datetime as dt
import random
import unittest
from unittest import mock

import support

from controlplane.httpkit import ApiError
from controlplane.services import chargeback as cb
from controlplane.services import governance as gov
from controlplane.services import org_model

NOW = dt.datetime(2026, 11, 19, 3, 0, tzinfo=dt.timezone.utc)
WF = "principal://iam.googleapis.com/locations/global/workforcepools/corp-pool/subject/e123"


class StubOrg:
    """The OrgModel surface chargeback uses: Company/Sales, Company/R&D; members and a project mapping."""

    def __init__(self, members=None, projects=None, units=None):
        self.version = 3
        self.updated_at = "2026-11-01T00:00:00Z"
        self.updated_by = "admin@example.com"
        self.source = {}
        self.units = units if units is not None else {
            "u_company": {"id": "u_company", "name": "Company", "parent": None, "path": "Company",
                          "costCenter": "", "depth": 0},
            "u_sales": {"id": "u_sales", "name": "Sales", "parent": "u_company", "path": "Company/Sales",
                        "costCenter": "CC-1", "depth": 1},
            "u_rnd": {"id": "u_rnd", "name": "R&D", "parent": "u_company", "path": "Company/R&D",
                      "costCenter": "CC-2", "depth": 1},
        }
        self.members = members if members is not None else {"alice@example.com": "u_sales"}
        self.projects = projects if projects is not None else {"proj-rnd": "u_rnd"}
        self.empty = not self.units

    def unit(self, unit_id):
        if unit_id in self.units:
            return self.units[unit_id]
        return {"id": org_model.UNASSIGNED, "name": "Unassigned", "parent": None, "path": "Unassigned",
                "costCenter": "", "depth": 0}

    def ancestors(self, unit_id):
        if unit_id not in self.units:
            return [org_model.UNASSIGNED]
        out = []
        while unit_id:
            out.append(unit_id)
            unit_id = self.units[unit_id]["parent"]
        return out

    def dfs_order(self):
        return [u for u in ("u_company", "u_rnd", "u_sales") if u in self.units]

    def unit_for(self, person, project, labels=None, label_key=""):
        if person in self.members:
            return self.members[person], "member"
        if project in self.projects:
            return self.projects[project], "project"
        if label_key and (labels or {}).get(label_key) == "cc-1":
            return "u_sales", "label"
        return org_model.UNASSIGNED, "unassigned"


def row(email, cost, chargeable, parts, credit=None, tier=None):
    return {"email": email, "requests": sum(p[1] for p in parts), "tokens": sum(p[2] for p in parts),
            "cost": cost, "chargeable": chargeable, "credit": credit, "tier": tier, "creditSource": "fixed",
            "byProject": [{"project": p[0], "requests": p[1], "tokens": p[2], "cost": p[3], "credit": p[4],
                           "chargeable": p[5]} for p in parts]}


REPORT = {
    "month": "2026-10", "currency": "USD", "complete": True, "usageSource": "bigquery",
    "users": [
        # alice: member of Sales; used two projects. cost 12.345 = credit 2.5 + chargeable 9.845
        row("alice@example.com", 12.345, 9.845, [("proj-a", 3, 300, 10.0, 2.5, 7.5), ("proj-rnd", 1, 100, 2.345, 0.0,
                                                                                         2.345)], credit=2.5),
        # bob: no member mapping; proj-rnd maps to R&D, proj-b is unassigned
        row("bob@example.com", 3.0, 1.0, [("proj-rnd", 2, 50, 1.5, 1.0, 0.5), ("proj-b", 2, 50, 1.5, 1.0, 0.5)],
            credit=2.0),
        # a workforce identity with unknown cost (a model without price)
        row(WF, None, None, [("proj-b", 5, 500, None, None, None)]),
        # a service account never becomes a person
        row("robot@demo-project.iam.gserviceaccount.com", 1.0, 1.0, [("proj-a", 1, 1, 1.0, 0.0, 1.0)]),
    ],
}


def make_ctx(body=None, params=None, role="admin", **chargeback):
    settings = support.make_settings(chargeback=chargeback) if chargeback else support.make_settings()
    return support.FakeCtx(settings=settings, body=body, params=params, role=role)


class Patched:
    """Patches the report, the org model, the clock and the audit writer."""

    def __init__(self, report=None, org=None):
        self.report = report if report is not None else REPORT
        self.org = org if org is not None else StubOrg()
        self.patchers = []

    def __enter__(self):
        for target, value in ((gov, {"billing_report": mock.MagicMock(side_effect=lambda c: self.report),
                                     "utcnow": mock.MagicMock(return_value=NOW),
                                     "write_audit": mock.MagicMock(return_value=True)}),
                              (org_model, {"load": mock.MagicMock(side_effect=lambda c: self.org)})):
            for name, fake in value.items():
                patcher = mock.patch.object(target, name, fake)
                patcher.start()
                self.patchers.append(patcher)
                setattr(self, name, fake)
        return self

    def __exit__(self, *exc):
        for patcher in self.patchers:
            patcher.stop()
        return False


class MoneyTest(unittest.TestCase):
    def test_to_cents_rounds_half_up(self):
        self.assertEqual(cb.to_cents(12.345), 1235)
        self.assertEqual(cb.to_cents(0.004), 0)
        self.assertEqual(cb.to_cents(0.005), 1)
        self.assertEqual(cb.to_cents("2.50"), 250)
        self.assertIsNone(cb.to_cents(None))
        self.assertIsNone(cb.to_cents("x"))
        self.assertIsNone(cb.to_cents(float("nan")))

    def test_allocate_is_exact_and_non_negative(self):
        rng = random.Random(7)
        for _ in range(500):
            n = rng.randint(1, 7)
            weights = [rng.choice([0, 0.001, rng.random() * 50]) for _ in range(n)]
            total = rng.randint(0, 100000)
            parts = cb.allocate(total, weights, fallback=[1] * n)
            self.assertEqual(sum(parts), total)
            self.assertTrue(all(p >= 0 for p in parts))

    def test_allocate_without_weights_uses_the_fallback_then_the_first_part(self):
        self.assertEqual(cb.allocate(7, [0, 0], fallback=[0, 1]), [0, 7])
        self.assertEqual(cb.allocate(7, [0, 0]), [7, 0])
        self.assertEqual(cb.allocate(0, [1, 2]), [0, 0])
        self.assertEqual(cb.allocate(5, []), [])


class FactsTest(unittest.TestCase):
    def test_credit_share_facts_match_the_person_to_the_cent(self):
        facts = cb.usage_facts(REPORT["users"][0], "credit_share")
        self.assertEqual(sum(f["cost"] for f in facts), 1235)
        self.assertEqual(sum(f["chargeable"] for f in facts), 985)
        self.assertEqual(sum(f["credit"] for f in facts), 250)
        for f in facts:
            self.assertEqual(f["cost"], f["credit"] + f["chargeable"])

    def test_list_mode_charges_the_whole_cost(self):
        facts = cb.usage_facts(REPORT["users"][0], "list")
        self.assertEqual(sum(f["chargeable"] for f in facts), 1235)
        self.assertEqual(sum(f["credit"] for f in facts), 0)

    def test_unknown_cost_keeps_usage_but_no_money(self):
        facts = cb.usage_facts(REPORT["users"][2], "credit_share")
        self.assertEqual((facts[0]["requests"], facts[0]["cost"], facts[0]["chargeable"]), (5, None, None))

    def test_rows_without_projects_get_one_unknown_project(self):
        facts = cb.usage_facts({"email": "x@example.com", "requests": 1, "tokens": 2, "cost": 1.0,
                                "chargeable": 0.25}, "credit_share")
        self.assertEqual([(f["project"], f["cost"], f["chargeable"], f["credit"]) for f in facts],
                         [("unknown", 100, 25, 75)])


class BuildTest(unittest.TestCase):
    def test_rollups_agree_to_the_cent(self):
        with Patched():
            model = cb.build(make_ctx(), "2026-10")
        s = model["summary"]
        totals = s["totals"]
        self.assertEqual(totals["users"], 3)  # the service account is not a person
        self.assertEqual(totals["unknownUsers"], 1)
        self.assertEqual(totals["cost"], 15.35)       # 12.35 + 3.00
        self.assertEqual(totals["chargeable"], 10.85)  # 9.85 + 1.00
        self.assertEqual(totals["credit"], 4.50)
        self.assertEqual(totals["total"], 10.85)
        users = {u["person"]: u for u in model["users"]}
        self.assertEqual(round(sum(u["total"] or 0 for u in users.values()), 2), totals["total"])
        roots = [u for u in s["units"] if u["parent"] is None]
        self.assertEqual(round(sum(u["rollup"]["total"] for u in roots), 2), totals["total"])
        self.assertEqual(round(sum(p["total"] for p in s["projects"]), 2), totals["total"])
        # alice is a member of Sales everywhere; bob splits between R&D (project) and unassigned
        self.assertEqual((users["alice@example.com"]["unitId"], users["alice@example.com"]["via"]),
                         ("u_sales", "member"))
        self.assertEqual(users["bob@example.com"]["via"], "mixed")
        self.assertEqual(users[WF]["kind"], "workforce")
        self.assertIsNone(users[WF]["total"])
        unassigned = next(u for u in s["units"] if u["id"] == org_model.UNASSIGNED)
        self.assertEqual(unassigned["rollup"]["total"], 0.5)
        self.assertEqual(totals["unassignedUsers"], 2)  # bob (proj-b) and the workforce user
        self.assertEqual(s["units"][-1]["id"], org_model.UNASSIGNED)
        company = next(u for u in s["units"] if u["id"] == "u_company")
        self.assertEqual(company["rollup"]["total"], 10.35)  # Sales 9.85 + R&D 0.50
        self.assertEqual(company["users"], 2)

    def test_random_reports_keep_the_invariant(self):
        rng = random.Random(11)
        projects = ["proj-a", "proj-b", "proj-rnd", "proj-c"]
        for _ in range(40):
            users = []
            for i in range(rng.randint(1, 25)):
                parts = []
                for p in rng.sample(projects, rng.randint(1, 3)):
                    cost = round(rng.random() * 40, 6)
                    credit = round(cost * rng.random(), 6)
                    parts.append((p, rng.randint(1, 9), rng.randint(1, 999), cost, credit, round(cost - credit, 6)))
                cost = round(sum(p[3] for p in parts), 6)
                charge = round(sum(p[5] for p in parts), 6)
                users.append(row("u%d@example.com" % i, cost, charge, parts))
            report = dict(REPORT, users=users)
            members = {"u%d@example.com" % i: rng.choice(["u_sales", "u_rnd"]) for i in range(0, 25, 3)}
            with Patched(report=report, org=StubOrg(members=members)):
                model = cb.build(make_ctx(), "2026-10")
            s = model["summary"]
            cents = cb.to_cents
            total = cents(s["totals"]["total"])
            self.assertEqual(sum(cents(u["total"]) for u in model["users"]), total)
            self.assertEqual(sum(cents(u["own"]["total"]) for u in s["units"]), total)
            self.assertEqual(sum(cents(p["total"]) for p in s["projects"]), total)
            self.assertEqual(sum(cents(u["chargeable"]) + cents(u["credit"]) for u in model["users"]),
                             cents(s["totals"]["cost"]))

    def test_project_labels_attribute_projects(self):
        with Patched(), mock.patch.object(cb, "_project_labels", return_value={"proj-b": {"cost-center": "cc-1"}}):
            model = cb.build(make_ctx(project_unit_label="cost-center"), "2026-10")
        bob = next(u for u in model["users"] if u["person"] == "bob@example.com")
        self.assertEqual(bob["via"], "mixed")
        project_b = next(p for p in model["summary"]["projects"] if p["project"] == "proj-b")
        self.assertEqual(project_b["unitId"], "u_sales")
        self.assertEqual(model["summary"]["orgModel"]["labelKey"], "cost-center")

    def test_seat_costs_follow_license_assignments(self):
        index = mock.MagicMock(ok=True, people={
            "alice@example.com": [{"tier": "SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT", "project": "proj-a"}],
            "carol@example.com": [{"tier": "SUBSCRIPTION_TIER_ENTERPRISE", "project": "proj-rnd"}],
            "dave@example.com": [{"tier": "SUBSCRIPTION_TIER_NOTEBOOK_LM", "project": "proj-a"}]})
        index.tiers = lambda person: sorted({a["tier"] for a in index.people.get(person, [])})
        with Patched(), mock.patch.object(cb, "_license_index", return_value=index):
            model = cb.build(make_ctx(seat_prices=["SUBSCRIPTION_TIER_ENTERPRISE=21",
                                                   "SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT=30"]), "2026-10")
        s = model["summary"]
        self.assertTrue(s["seatPricing"])
        self.assertEqual(s["totals"]["seat"], 51.0)
        self.assertEqual(s["totals"]["total"], 61.85)
        users = {u["person"]: u for u in model["users"]}
        self.assertEqual((users["carol@example.com"]["seat"], users["carol@example.com"]["unitId"]), (21.0, "u_rnd"))
        self.assertEqual(users["carol@example.com"]["tier"], "SUBSCRIPTION_TIER_ENTERPRISE")
        self.assertNotIn("dave@example.com", users)  # no seat price for that tier

    def test_seat_prices_need_a_usd_report(self):
        report = dict(REPORT, currency="KRW")
        ctx = make_ctx(seat_prices=["SUBSCRIPTION_TIER_ENTERPRISE=21"])
        with Patched(report=report):
            model = cb.build(ctx, "2026-10")
        self.assertFalse(model["summary"]["seatPricing"])
        self.assertIn("currency_mismatch", [w["code"] for w in ctx.warnings])


class MonthTest(unittest.TestCase):
    def test_months(self):
        with Patched():
            ctx = make_ctx()
            self.assertEqual(cb.current_month(ctx), "2026-11")
            months = cb.recent_months(ctx)
            self.assertEqual((months[0], months[1], months[-1], len(months)), ("2026-11", "2026-10", "2025-11", 13))
            self.assertEqual(cb.parse_month("", ctx), "2026-11")
            for bad in ("2026-13", "26-01", "2026/10", "2019-12"):
                with self.assertRaises(ApiError):
                    cb.parse_month(bad, ctx)
            with self.assertRaises(ApiError):
                cb.parse_month("2026-12", ctx)  # future

    def test_months_take_ascii_digits_only(self):
        # \d also matches the digits of other scripts, which int() reads: "2025-<Arabic-Indic 12>" sorts before the
        # running month, so it used to pass as a second spelling of 2025-12 (its own close record and documents).
        with Patched():
            ctx = make_ctx()
            for bad in ("2025-\u0661\u0662", "2025-1\u0662", "\uff12\uff10\uff12\uff15-\uff11\uff10", "2026-10x",
                        "2026-1"):
                with self.assertRaises(ApiError, msg=repr(bad)):
                    cb.parse_month(bad, ctx)
            self.assertEqual(cb.parse_month(" 2025-12 ", ctx), "2025-12")


class CloseTest(unittest.TestCase):
    def test_only_past_months_close_and_a_closed_month_is_frozen(self):
        with Patched() as p:
            ctx = make_ctx(body={"month": "2026-11"})
            with self.assertRaises(ApiError) as err:
                cb.close(ctx)
            self.assertEqual(err.exception.code, "bad_request")
            ctx = make_ctx(body={"month": "2026-10"})
            out = cb.close(ctx)
            self.assertEqual((out["closeId"], out["status"], out["rows"]), ("c_202610_1", "closed", 5))
            self.assertEqual(p.write_audit.call_args.args[1], "chargeback_close")
            self.assertNotIn("alice@example.com", str(p.write_audit.call_args.args[2]))
            # The live figures change after the close; the closed month keeps its snapshot.
            p.report = dict(REPORT, users=[])
            view = make_ctx(params={"month": "2026-10"})
            view.app = ctx.app
            summary = cb.summary(view)
            self.assertEqual((summary["status"], summary["totals"]["total"]), ("closed", 10.85))
            self.assertEqual(summary["close"]["closeId"], "c_202610_1")
            self.assertEqual(summary["closes"][0]["month"], "2026-10")
            report = make_ctx(params={"month": "2026-10", "level": "user"})
            report.app = ctx.app
            self.assertEqual(len(cb.report(report)["rows"]), 3)
            # Closing again needs an explicit re-close and creates a new close.
            again = make_ctx(body={"month": "2026-10"})
            again.app = ctx.app
            with self.assertRaises(ApiError) as err:
                cb.close(again)
            self.assertEqual(err.exception.code, "conflict")
            again.body = {"month": "2026-10", "reclose": True}
            out = cb.close(again)
            self.assertEqual(out["closeId"], "c_202610_2")
            summary = cb.summary(view)
            self.assertEqual((summary["close"]["closeId"], summary["totals"]["total"]), ("c_202610_2", 0.0))
            self.assertEqual(len(summary["closes"]), 2)

    def test_open_month_summary_and_report_levels(self):
        with Patched():
            ctx = make_ctx(params={"month": "2026-10"})
            summary = cb.summary(ctx)
            self.assertEqual((summary["status"], summary["current"], summary["close"]), ("open", False, None))
            self.assertEqual(summary["months"][0], "2026-11")
            for level, count in (("user", 3), ("project", 3)):
                ctx = make_ctx(params={"month": "2026-10", "level": level})
                self.assertEqual(len(cb.report(ctx)["rows"]), count)
            with self.assertRaises(ApiError):
                cb.report(make_ctx(params={"month": "2026-10", "level": "nope"}))

    def test_bigquery_copy(self):
        calls = []

        class RestCtx(support.FakeCtx):
            def rest(self, method, url, **kwargs):
                calls.append((method, url, kwargs.get("json_body")))
                if method == "GET":
                    from controlplane.gcp import GcpError
                    raise GcpError("not_found", status=404)
                return {}

        settings = support.make_settings(chargeback={"close_dataset": "finance"})
        ctx = RestCtx(settings=settings, body={"month": "2026-10"})
        with Patched():
            out = cb.close(ctx)
        self.assertEqual(out["bigquery"], {"table": "demo-project.finance.chargeback_close", "rows": 5})
        methods = [(m, u.rsplit("/", 1)[-1]) for m, u, _ in calls]
        self.assertEqual(methods, [("GET", "chargeback_close"), ("POST", "tables"), ("POST", "insertAll")])
        inserted = calls[-1][2]["rows"]
        self.assertEqual(inserted[0]["insertId"], "c_202610_1-0")
        self.assertTrue(all(r["json"]["close_id"] == "c_202610_1" for r in inserted))
        money = [r["json"]["total"] for r in inserted if r["json"]["total"] is not None]
        self.assertEqual(sum(int(v.replace(".", "")) for v in money), 1085)


class OrgModelIntegrationTest(unittest.TestCase):
    def test_an_imported_csv_drives_the_rollup(self):
        csv_text = ("principal,unit_path,cost_center\n"
                    "alice@example.com,Company/Sales,CC-1\n"
                    "project:proj-rnd,Company/R&D,CC-2\n"
                    "%s,Company/R&D,\n" % WF)
        ctx = make_ctx(body={"csv": csv_text, "dryRun": False})
        with mock.patch.object(gov, "write_audit", return_value=True), \
                mock.patch.object(gov, "billing_report", side_effect=lambda c: REPORT), \
                mock.patch.object(gov, "utcnow", return_value=NOW):
            applied = org_model.import_csv(ctx)
            self.assertTrue(applied["applied"])
            view = make_ctx(params={"month": "2026-10"})
            view.app = ctx.app
            summary = cb.summary(view)
            people = make_ctx(params={"month": "2026-10", "level": "user"})
            people.app = ctx.app
            users = {u["person"]: u for u in cb.report(people)["rows"]}
        units = {u["path"]: u for u in summary["units"]}
        self.assertEqual(units["Company/Sales"]["rollup"]["total"], 9.85)
        self.assertEqual(units["Company/Sales"]["costCenter"], "CC-1")
        self.assertEqual(units["Company/R&D"]["rollup"]["total"], 0.5)
        self.assertEqual(units["Company"]["rollup"]["total"], 10.35)
        self.assertEqual(units["Unassigned"]["rollup"]["total"], 0.5)
        self.assertEqual((users[WF]["unitPath"], users[WF]["via"]), ("Company/R&D", "member"))
        self.assertEqual(summary["orgModel"]["version"], 1)
        self.assertFalse(summary["orgModel"]["empty"])


if __name__ == "__main__":
    unittest.main()
