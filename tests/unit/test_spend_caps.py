"""Google Cloud spend caps for hard-capped project budgets: the opt-in gate, the catalog lookup, the create body,
amount updates, orphans, other budgets, lifts and enforced caps, against an in-memory fake of the Cloud Billing
Catalog, Cloud Billing account, Budget API and Resource Manager endpoints (no real Google call is ever made)."""

import copy
import unittest
from unittest import mock

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane.gcp import GcpError
from controlplane.httpkit import ApiError
from controlplane.services import budgets, governance, spend_caps
from controlplane.settings_store import aux_docs

ACCOUNT = "000000-111111-222222"
BUDGETS_URL = "%s/billingAccounts/%s/budgets" % (spend_caps.BUDGETS_V1, ACCOUNT)
ACCOUNT_URL = "%s/billingAccounts/%s" % (spend_caps.BILLING_V1, ACCOUNT)
SERVICES_URL = "%s/services" % spend_caps.BILLING_V1
SERVICE = "services/C7E2-9256-1C43"
COMPUTE = {"name": "services/6F81-5844-456A", "serviceId": "6F81-5844-456A", "displayName": "Compute Engine"}
NUMBERS = {"proj-alpha": "1234567", "proj-beta": "2345678"}


def spend_cap_problems(body):
    """The documented restrictions of a spend cap budget that a create body breaks (empty when it is valid)."""
    out = []
    flt = body.get("budgetFilter") or {}
    if len(flt.get("projects") or []) != 1:
        out.append("exactly one project")
    if len(flt.get("services") or []) != 1:
        out.append("exactly one service")
    if flt.get("calendarPeriod", "MONTH") != "MONTH" or "customPeriod" in flt:
        out.append("monthly period")
    if flt.get("creditTypesTreatment") != "EXCLUDE_ALL_CREDITS":
        out.append("credits excluded")
    for key in ("resourceAncestors", "creditTypes", "subaccounts", "labels"):
        if key in flt:
            out.append("no " + key)
    spec = (body.get("amount") or {}).get("specifiedAmount")
    if not isinstance(spec, dict) or int(spec.get("units") or 0) < 0 or int(spec.get("nanos") or 0) < 0 or \
            "lastPeriodAmount" in (body.get("amount") or {}):
        out.append("specifiedAmount >= 0")
    rules = body.get("thresholdRules") or []
    if sorted(r.get("thresholdPercent") for r in rules) != [0.5, 0.8, 1.0] or \
            any(r.get("spendBasis", "CURRENT_SPEND") != "CURRENT_SPEND" for r in rules):
        out.append("three CURRENT_SPEND thresholds")
    rule = body.get("notificationsRule") or {}
    if rule != {"enableProjectLevelRecipients": True}:
        out.append("project-level recipients only")
    if body.get("ownershipScope", "ALL_USERS") not in ("ALL_USERS", "OWNERSHIP_SCOPE_UNSPECIFIED"):
        out.append("ownership scope")
    if body.get("spendCap") != {"inputState": "CONFIGURED"}:
        out.append("inputState CONFIGURED")
    if len(body.get("displayName") or "") > 60:
        out.append("display name of at most 60 characters")
    return out


class FakeGoogle:
    """In-memory Google endpoints used by spend_caps. Every call is recorded in `calls`."""

    def __init__(self, services=None, currency="USD", page=2):
        self.calls = []
        self.services = services if services is not None else [
            COMPUTE, {"name": SERVICE, "serviceId": "C7E2-9256-1C43", "displayName": "Agent Platform"}]
        self.currency = currency
        self.budgets = []
        self.page = page
        self.fail = {}      # (method, url prefix) -> GcpError
        self._next = 1

    def add_budget(self, display, units, state=None, currency="USD", nanos=0, projects=("projects/1234567",),
                   services=(SERVICE,)):
        budget = {"name": "billingAccounts/%s/budgets/b-%04d" % (ACCOUNT, self._next), "displayName": display,
                  "budgetFilter": {"projects": list(projects), "services": list(services),
                                   "creditTypesTreatment": "EXCLUDE_ALL_CREDITS", "calendarPeriod": "MONTH"},
                  "amount": {"specifiedAmount": {"currencyCode": currency, "units": str(units), "nanos": nanos}},
                  "etag": "etag-%d" % self._next}
        self._next += 1
        if state:
            budget["spendCap"] = {"outputState": state}
        self.budgets.append(budget)
        return budget

    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]

    def rest(self, method, url, params=None, json_body=None, **_kwargs):
        params = dict(params or {})
        self.calls.append((method, url, params, copy.deepcopy(json_body)))
        for (fail_method, prefix), err in self.fail.items():
            if fail_method == method and url.startswith(prefix):
                raise err
        if method == "GET" and url == SERVICES_URL:
            return self._page(self.services, "services", params)
        if method == "GET" and url == ACCOUNT_URL:
            return {"name": "billingAccounts/" + ACCOUNT, "open": True, "currencyCode": self.currency}
        if method == "GET" and url.startswith(spend_caps.CRM_V3 + "/projects/"):
            pid = url.rsplit("/", 1)[1]
            if pid not in NUMBERS:
                raise GcpError("not_found", 404, "not found", "NOT_FOUND")
            return {"name": "projects/" + NUMBERS[pid], "projectId": pid, "state": "ACTIVE"}
        if url == BUDGETS_URL and method == "GET":
            return self._page(self.budgets, "budgets", params)
        if url == BUDGETS_URL and method == "POST":
            return self._create(json_body)
        if method == "PATCH" and url.startswith(BUDGETS_URL + "/"):
            return self._patch(url[len(spend_caps.BUDGETS_V1) + 1:], params.get("updateMask", ""), json_body)
        raise AssertionError("unexpected call %s %s" % (method, url))

    def _page(self, items, key, params):
        start = int(params.get("pageToken") or 0)
        size = min(self.page, int(params.get("pageSize") or self.page))
        out = {key: copy.deepcopy(items[start:start + size])}
        if start + size < len(items):
            out["nextPageToken"] = str(start + size)
        return out

    def _create(self, body):
        problems = spend_cap_problems(body)
        if problems or body["amount"]["specifiedAmount"].get("currencyCode", self.currency) != self.currency:
            raise GcpError("invalid", 400, "; ".join(problems), "INVALID_ARGUMENT")
        budget = copy.deepcopy(body)
        budget["name"] = "billingAccounts/%s/budgets/b-%04d" % (ACCOUNT, self._next)
        budget["etag"] = "etag-%d" % self._next
        self._next += 1
        budget["spendCap"] = {"outputState": "CONFIGURED"}
        for rule in budget["thresholdRules"]:
            rule.setdefault("spendBasis", "CURRENT_SPEND")
        self.budgets.append(budget)
        return copy.deepcopy(budget)

    def _patch(self, name, mask, body):
        budget = next((b for b in self.budgets if b["name"] == name), None)
        if budget is None:
            raise GcpError("not_found", 404, "", "NOT_FOUND")
        fields = [f for f in mask.split(",") if f]
        if (budget.get("spendCap") or {}).get("outputState") == "ENFORCED" and fields != ["spendCap.inputState"]:
            raise GcpError("invalid", 400, "the spend cap is enforced", "FAILED_PRECONDITION")
        for field in fields:
            if field == "amount.specifiedAmount":
                budget["amount"]["specifiedAmount"] = dict(body["amount"]["specifiedAmount"])
            elif field == "spendCap.inputState":
                budget.setdefault("spendCap", {})["outputState"] = body["spendCap"]["inputState"]
            else:
                raise AssertionError("unexpected update mask field %s" % field)
        budget["etag"] += "+"
        return copy.deepcopy(budget)


LONG_ID = "p" + "a" * 37 + "b"   # 39 characters: the display name would have 61
# The workspace projects of CapsCtx (ctx.all_projects): spend caps are created or changed only for these.
WORKSPACE = ("proj-alpha", "proj-beta", "proj-gamma", "proj-delta", "proj-unknown", LONG_ID)


class CapsCtx(support.FakeCtx):
    """FakeCtx whose REST calls go to a FakeGoogle."""

    def __init__(self, google, on=True, account=ACCOUNT, role="admin", body=None, workspace=WORKSPACE):
        settings = support.make_settings()
        settings["governance"]["antigravity"]["spend_caps"] = on
        settings["workspace"]["billing"]["account_id"] = account
        settings["workspace"]["projects"] = list(workspace)
        super().__init__(settings=settings, body=body, role=role)
        self.google = google

    def rest(self, method, url, **kwargs):
        return self.google.rest(method, url, **kwargs)


def store_budgets(ctx, nodes):
    clean = budgets.validate_nodes(nodes)
    aux_docs(ctx.app).mutate(budgets.DOC, lambda doc: doc.update({"version": 1, "nodes": clean}))


def hard_cap(project, usd):
    return {"scope": "project", "project": project, "monthlyPaidUsd": usd, "action": "hard_cap"}


def by_project(results):
    return {item["project"]: item for item in results}


class GateTest(unittest.TestCase):
    def test_enabled_and_account_id(self):
        settings = support.make_settings()
        self.assertFalse(spend_caps.enabled(settings))  # opt-in: off by default
        settings["governance"]["antigravity"]["spend_caps"] = True
        self.assertTrue(spend_caps.enabled(settings))
        settings["governance"]["antigravity"]["spend_caps"] = "true"
        self.assertFalse(spend_caps.enabled(settings))
        self.assertFalse(spend_caps.enabled({}))
        for raw, value in (("", ""), (" 0a0b0c-111111-2222ff ", "0A0B0C-111111-2222FF"), ("123", ""), (None, "")):
            settings["workspace"]["billing"]["account_id"] = raw
            self.assertEqual(spend_caps.account_id(settings), value)
        self.assertEqual(spend_caps.account_id({}), "")

    def test_off_or_without_account_no_call_is_made(self):
        create = {"project": "proj-alpha", "op": "create", "amount": 10.0, "blocked": None}
        for on, account in ((False, ACCOUNT), (True, ""), (True, "not-an-account")):
            google = FakeGoogle()
            google.add_budget("AI Control Plane cap: proj-alpha", 10, state="ENFORCED")
            ctx = CapsCtx(google, on=on, account=account, body={"dryRun": False, "project": "proj-alpha"})
            read = spend_caps.read_caps(ctx)
            self.assertEqual((read["ok"], read["items"]), (False, {}))
            self.assertTrue(read["error"])
            self.assertEqual([r["status"] for r in spend_caps.apply_caps(ctx, [create])], ["skipped"])
            self.assertEqual(spend_caps.lift(ctx, "proj-alpha")["status"], "skipped")
            status = spend_caps.get_spend_caps(ctx)
            self.assertEqual((status["enabled"], status["plan"], status["caps"]), (on, [], {}))
            self.assertTrue(status["error"])
            self.assertEqual(self.raises(spend_caps.apply_spend_caps, ctx)[0], "not_configured")
            self.assertEqual(self.raises(spend_caps.lift_spend_cap, ctx)[0], "not_configured")
            self.assertEqual(google.calls, [], (on, account))

    def raises(self, fn, *args):
        try:
            fn(*args)
        except ApiError as err:
            return err.code, err.message
        raise AssertionError("ApiError not raised")

    def test_desired_caps_are_the_hard_capped_projects(self):
        tree = budgets.BudgetTree({"nodes": budgets.validate_nodes([
            hard_cap("proj-alpha", 150.5), {"scope": "project", "project": "proj-beta", "monthlyPaidUsd": 9,
                                            "action": "notify"},
            {"scope": "unit", "unitId": "u_0123456789", "monthlyPaidUsd": 5, "action": "suspend"},
            hard_cap("proj-gamma", 0)])})
        self.assertEqual(spend_caps.desired_caps(tree), {"proj-alpha": 150.5, "proj-gamma": 0.0})
        self.assertEqual(spend_caps.desired_caps(budgets.BudgetTree()), {})


class ServiceLookupTest(unittest.TestCase):
    def lookup(self, services, page=2):
        google = FakeGoogle(services=services, page=page)
        ctx = CapsCtx(google)
        return spend_caps.service_name(ctx), google, ctx

    def test_agent_platform_is_found_by_its_new_or_old_billing_name(self):
        for display in ("Agent Platform", "Vertex AI", "Gemini Enterprise Agent Platform", " agent  PLATFORM "):
            found, _, _ = self.lookup([COMPUTE, {"name": "services/AAAA-BBBB-CCCC", "displayName": display}])
            self.assertEqual(found, "services/AAAA-BBBB-CCCC", display)

    def test_pages_are_followed_and_the_result_is_cached(self):
        services = [dict(COMPUTE, name="services/0000-0000-000%d" % i) for i in range(4)]
        services.append({"name": "services/AAAA-BBBB-CCCC", "displayName": "Vertex AI"})
        found, google, ctx = self.lookup(services, page=2)
        self.assertEqual(found, "services/AAAA-BBBB-CCCC")
        self.assertEqual([c[2].get("pageToken") for c in google.calls], [None, "2", "4"])
        self.assertEqual(spend_caps.service_name(ctx), found)
        self.assertEqual(len(google.calls), 3)  # cached

    def test_the_documented_id_and_the_new_name_win(self):
        found, _, _ = self.lookup([{"name": "services/AAAA-BBBB-CCCC", "displayName": "Vertex AI"},
                                   {"name": SERVICE, "displayName": "Something renamed"}])
        self.assertEqual(found, SERVICE)
        found, _, _ = self.lookup([{"name": "services/AAAA-BBBB-CCCC", "displayName": "Vertex AI"},
                                   {"name": "services/DDDD-EEEE-FFFF", "displayName": "Agent Platform"}])
        self.assertEqual(found, "services/DDDD-EEEE-FFFF")

    def test_no_match_is_not_cached(self):
        found, google, ctx = self.lookup([COMPUTE,
                                          {"name": "services/AAAA-BBBB-CCCC", "displayName": "Vertex AI Search"},
                                          {"name": "bad", "displayName": "Agent Platform"}])
        self.assertIsNone(found)
        spend_caps.service_name(ctx)
        self.assertEqual(len(google.calls), 4)  # two pages, twice


class CreateTest(unittest.TestCase):
    def test_create_body_follows_the_spend_cap_rules(self):
        google = FakeGoogle()
        ctx = CapsCtx(google)
        plan = spend_caps.plan_caps(spend_caps.read_caps(ctx), {"proj-alpha": 150.25})
        self.assertEqual([(p["project"], p["op"], p["amount"], p["blocked"]) for p in plan],
                         [("proj-alpha", "create", 150.25, None)])
        results = spend_caps.apply_caps(ctx, plan)
        self.assertEqual([(r["status"], r["budget"]) for r in results],
                         [("created", "billingAccounts/%s/budgets/b-0001" % ACCOUNT)])
        posts = [call for call in google.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 1)
        body = posts[0][3]
        self.assertEqual(body, {
            "displayName": "AI Control Plane cap: proj-alpha",
            "budgetFilter": {"projects": ["projects/1234567"], "services": [SERVICE],
                             "creditTypesTreatment": "EXCLUDE_ALL_CREDITS", "calendarPeriod": "MONTH"},
            "amount": {"specifiedAmount": {"currencyCode": "USD", "units": "150", "nanos": 250000000}},
            "thresholdRules": [{"thresholdPercent": 0.5, "spendBasis": "CURRENT_SPEND"},
                               {"thresholdPercent": 0.8, "spendBasis": "CURRENT_SPEND"},
                               {"thresholdPercent": 1.0, "spendBasis": "CURRENT_SPEND"}],
            "notificationsRule": {"enableProjectLevelRecipients": True},
            "spendCap": {"inputState": "CONFIGURED"}})
        self.assertEqual(spend_cap_problems(body), [])
        # The account currency comes from billingAccounts.get when the account has no budget yet.
        self.assertIn(("GET", ACCOUNT_URL, {}, None), google.calls)
        read = spend_caps.read_caps(ctx)
        self.assertEqual(read["items"]["proj-alpha"]["amount"], 150.25)
        self.assertEqual((read["items"]["proj-alpha"]["outputState"], read["currency"]), ("CONFIGURED", "USD"))
        again = spend_caps.plan_caps(read, {"proj-alpha": 150.25})
        self.assertEqual([p["op"] for p in again], ["none"])
        self.assertEqual([r["status"] for r in spend_caps.apply_caps(ctx, again)], ["unchanged"])
        self.assertEqual(len([c for c in google.calls if c[0] == "POST"]), 1)

    def test_a_stale_create_does_not_create_a_second_cap(self):
        google = FakeGoogle()
        ctx = CapsCtx(google)
        plan = spend_caps.plan_caps(spend_caps.read_caps(ctx), {"proj-alpha": 20})
        google.add_budget("AI Control Plane cap: proj-alpha", 20, state="CONFIGURED")  # created meanwhile
        self.assertEqual([r["status"] for r in spend_caps.apply_caps(ctx, plan)], ["unchanged"])
        self.assertEqual(google.writes(), [])

    def test_creates_are_refused_on_a_billing_account_in_another_currency(self):
        google = FakeGoogle(currency="EUR")
        ctx = CapsCtx(google)
        plan = spend_caps.plan_caps(spend_caps.read_caps(ctx), {"proj-alpha": 10})
        self.assertEqual(plan[0]["blocked"], None)  # no budget tells the currency yet
        result = spend_caps.apply_caps(ctx, plan)[0]
        self.assertEqual(result["status"], "currency_mismatch")
        self.assertIn("billed in EUR", result["message"])
        self.assertEqual(google.writes(), [])
        google.add_budget("Team budget", 500, currency="EUR")  # an alerts-only budget tells the currency
        plan = spend_caps.plan_caps(spend_caps.read_caps(ctx), {"proj-alpha": 10})
        self.assertEqual((plan[0]["op"], plan[0]["blocked"], plan[0]["currency"]),
                         ("create", "currency_mismatch", "EUR"))
        self.assertEqual(spend_caps.apply_caps(ctx, plan)[0]["status"], "currency_mismatch")
        self.assertEqual(google.writes(), [])

    def test_project_ids_that_do_not_fit_the_display_name(self):
        long_id = LONG_ID   # a workspace project of CapsCtx
        ctx = CapsCtx(FakeGoogle())
        plan = spend_caps.plan_caps(spend_caps.read_caps(ctx), {long_id: 10, "proj-alpha": 5})
        self.assertEqual({p["project"]: p["blocked"] for p in plan}, {long_id: "name_too_long", "proj-alpha": None})
        results = by_project(spend_caps.apply_caps(ctx, plan))
        self.assertEqual((results[long_id]["status"], results["proj-alpha"]["status"]), ("name_too_long", "created"))

    def test_failures_are_reported_per_project(self):
        google = FakeGoogle()
        ctx = CapsCtx(google)
        plan = spend_caps.plan_caps(spend_caps.read_caps(ctx), {"proj-alpha": 5, "proj-unknown": 5})
        google.fail[("POST", BUDGETS_URL)] = GcpError("denied", 403, "", "PERMISSION_DENIED")
        with self.assertLogs("controlplane.spend_caps", level="WARNING"):
            results = by_project(spend_caps.apply_caps(ctx, plan))
        self.assertEqual(results["proj-alpha"]["status"], "failed")
        self.assertIn("billing.budgets.configureSpendCap", results["proj-alpha"]["message"])
        self.assertEqual(results["proj-unknown"]["status"], "failed")  # Resource Manager does not know it
        google = FakeGoogle(services=[COMPUTE])
        ctx = CapsCtx(google)
        result = spend_caps.apply_caps(ctx, spend_caps.plan_caps(spend_caps.read_caps(ctx), {"proj-alpha": 5}))[0]
        self.assertEqual(result["status"], "failed")
        self.assertIn("Cloud Billing Catalog", result["message"])
        self.assertEqual(google.writes(), [])

    def test_a_dry_run_makes_no_call(self):
        google = FakeGoogle()
        google.add_budget("AI Control Plane cap: proj-beta", 40, state="CONFIGURED")
        google.add_budget("AI Control Plane cap: proj-gamma", 7, state="CONFIGURED")
        google.add_budget("AI Control Plane cap: proj-delta", 9, state="ENFORCED")
        ctx = CapsCtx(google)
        plan = spend_caps.plan_caps(spend_caps.read_caps(ctx), {"proj-alpha": 10, "proj-beta": 50, "proj-delta": 12,
                                                                "proj-gamma": 7})
        calls = len(google.calls)
        results = by_project(spend_caps.apply_caps(ctx, plan, dry_run=True))
        self.assertEqual({p: r["status"] for p, r in results.items()},
                         {"proj-alpha": "dry_run", "proj-beta": "dry_run", "proj-delta": "enforced",
                          "proj-gamma": "unchanged"})
        self.assertEqual(len(google.calls), calls)


class UpdateTest(unittest.TestCase):
    def patches(self, google):
        return [(call[2], call[3]) for call in google.calls if call[0] == "PATCH"]

    def test_a_higher_amount_also_configures_the_cap(self):
        google = FakeGoogle()
        budget = google.add_budget("AI Control Plane cap: proj-alpha", 100, state="CONFIGURED")
        ctx = CapsCtx(google)
        plan = spend_caps.plan_caps(spend_caps.read_caps(ctx), {"proj-alpha": 150})
        self.assertEqual([(p["op"], p["current"], p["budget"]) for p in plan], [("update", 100.0, budget["name"])])
        self.assertEqual([r["status"] for r in spend_caps.apply_caps(ctx, plan)], ["updated"])
        self.assertEqual(self.patches(google), [
            ({"updateMask": "amount.specifiedAmount,spendCap.inputState"},
             {"amount": {"specifiedAmount": {"currencyCode": "USD", "units": "150", "nanos": 0}},
              "spendCap": {"inputState": "CONFIGURED"}})])
        self.assertEqual(spend_caps.read_caps(ctx)["items"]["proj-alpha"]["amount"], 150.0)

    def test_a_lower_amount_leaves_a_lifted_cap_lifted(self):
        google = FakeGoogle()
        google.add_budget("AI Control Plane cap: proj-alpha", 100, state="AWAITING_NEXT_PERIOD")
        ctx = CapsCtx(google)
        plan = spend_caps.plan_caps(spend_caps.read_caps(ctx), {"proj-alpha": 80.5})
        self.assertEqual([r["status"] for r in spend_caps.apply_caps(ctx, plan)], ["updated"])
        self.assertEqual(self.patches(google), [
            ({"updateMask": "amount.specifiedAmount"},
             {"amount": {"specifiedAmount": {"currencyCode": "USD", "units": "80", "nanos": 500000000}}})])
        item = spend_caps.read_caps(ctx)["items"]["proj-alpha"]
        self.assertEqual((item["amount"], item["outputState"]), (80.5, "AWAITING_NEXT_PERIOD"))

    def test_an_enforced_cap_is_only_ever_patched_by_a_lift(self):
        google = FakeGoogle()
        google.add_budget("AI Control Plane cap: proj-alpha", 100, state="ENFORCED")
        ctx = CapsCtx(google)
        plan = spend_caps.plan_caps(spend_caps.read_caps(ctx), {"proj-alpha": 150})
        self.assertEqual([(p["op"], p["blocked"], p["state"]) for p in plan], [("update", "enforced", "ENFORCED")])
        result = spend_caps.apply_caps(ctx, plan)[0]
        self.assertEqual(result["status"], "enforced")
        self.assertIn("only a lift", result["message"])
        self.assertEqual(self.patches(google), [])
        self.assertEqual(spend_caps.lift(ctx, "proj-alpha")["status"], "lifted")
        self.assertEqual(self.patches(google), [({"updateMask": "spendCap.inputState"},
                                                 {"spendCap": {"inputState": "AWAITING_NEXT_PERIOD"}})])

    def test_a_stale_plan_never_patches_a_cap_that_became_enforced(self):
        google = FakeGoogle()
        budget = google.add_budget("AI Control Plane cap: proj-alpha", 100, state="CONFIGURED")
        ctx = CapsCtx(google)
        plan = spend_caps.plan_caps(spend_caps.read_caps(ctx), {"proj-alpha": 150})
        self.assertEqual(plan[0]["blocked"], None)
        budget["spendCap"]["outputState"] = "ENFORCED"  # Google enforced it after the plan was made
        self.assertEqual(spend_caps.apply_caps(ctx, plan)[0]["status"], "enforced")
        self.assertEqual(self.patches(google), [])

    def test_google_refusing_an_update_is_a_failure(self):
        google = FakeGoogle()
        google.add_budget("AI Control Plane cap: proj-alpha", 100, state="CONFIGURED")
        ctx = CapsCtx(google)
        current = spend_caps.read_caps(ctx)
        plan = spend_caps.plan_caps(current, {"proj-alpha": 90})
        google.budgets[0]["spendCap"]["outputState"] = "ENFORCED"  # enforced between the read and the patch
        with self.assertLogs("controlplane.spend_caps", level="WARNING"):
            result = spend_caps.apply_caps(ctx, plan, current=current)[0]
        self.assertEqual(result["status"], "failed")
        self.assertIn("FAILED_PRECONDITION", result["message"])

    def test_updates_are_refused_in_another_currency(self):
        google = FakeGoogle(currency="EUR")
        google.add_budget("AI Control Plane cap: proj-alpha", 100, state="CONFIGURED", currency="EUR")
        ctx = CapsCtx(google)
        plan = spend_caps.plan_caps(spend_caps.read_caps(ctx), {"proj-alpha": 100})
        self.assertEqual([(p["op"], p["blocked"]) for p in plan], [("update", "currency_mismatch")])
        self.assertEqual(spend_caps.apply_caps(ctx, plan)[0]["status"], "currency_mismatch")
        self.assertEqual(google.writes(), [])


class OwnershipTest(unittest.TestCase):
    def test_orphans_are_reported_and_kept(self):
        google = FakeGoogle()
        google.add_budget("AI Control Plane cap: proj-beta", 50, state="ENFORCED")
        ctx = CapsCtx(google)
        plan = spend_caps.plan_caps(spend_caps.read_caps(ctx), {})
        self.assertEqual([(p["project"], p["op"], p["amount"], p["current"], p["state"]) for p in plan],
                         [("proj-beta", "orphan", None, 50.0, "ENFORCED")])
        result = spend_caps.apply_caps(ctx, plan)[0]
        self.assertEqual(result["status"], "orphan")
        self.assertIn("kept", result["message"])
        self.assertEqual(google.writes(), [])

    def test_other_budgets_are_never_touched(self):
        google = FakeGoogle()
        others = [google.add_budget("Team budget", 500),
                  google.add_budget("My AI Control Plane cap: proj-alpha", 1, state="CONFIGURED"),
                  google.add_budget("AI Control Plane cap: Bad_Project!", 1, state="CONFIGURED"),
                  google.add_budget("ai control plane cap: proj-alpha", 1, state="ENFORCED")]
        before = copy.deepcopy(others)
        ctx = CapsCtx(google)
        read = spend_caps.read_caps(ctx)
        self.assertEqual((read["ok"], read["items"], read["currency"]), (True, {}, "USD"))
        results = spend_caps.apply_caps(ctx, spend_caps.plan_caps(read, {"proj-alpha": 10}))
        self.assertEqual([r["status"] for r in results], ["created"])
        self.assertEqual([call[0] for call in google.writes()], ["POST"])
        self.assertEqual(google.budgets[:4], before)


class ReadTest(unittest.TestCase):
    def test_items_and_pages(self):
        google = FakeGoogle(page=2)
        google.add_budget("Team budget", 500)
        google.add_budget("Other", 10)
        cap = google.add_budget("AI Control Plane cap: proj-alpha", 12, state="ENFORCED", nanos=500000000)
        cap["spendCap"].update(inputState="CONFIGURED", reconciling=True)
        google.add_budget("AI Control Plane cap: proj-alpha", 99, state="CONFIGURED")  # a second one is ignored
        ctx = CapsCtx(google)
        with self.assertLogs("controlplane.spend_caps", level="WARNING"):
            read = spend_caps.read_caps(ctx)
        self.assertEqual(read, {"ok": True, "error": None, "currency": "USD", "items": {"proj-alpha": {
            "budget": cap["name"], "amount": 12.5, "currency": "USD", "inputState": "CONFIGURED",
            "outputState": "ENFORCED", "etag": cap["etag"], "reconciling": True,
            "filterProjects": ["projects/1234567"], "filterServices": [SERVICE], "hasSpendCap": True}}})
        self.assertEqual([c[2] for c in google.calls], [{"pageSize": 100}, {"pageSize": 100, "pageToken": "2"}])

    def test_read_errors_never_raise(self):
        google = FakeGoogle()
        google.fail[("GET", BUDGETS_URL)] = GcpError("denied", 403, "", "PERMISSION_DENIED")
        ctx = CapsCtx(google)
        with self.assertLogs("controlplane.spend_caps", level="WARNING"):
            read = spend_caps.read_caps(ctx)
        self.assertEqual((read["ok"], read["items"]), (False, {}))
        self.assertIn("was denied", read["error"])
        self.assertEqual(spend_caps.plan_caps(read, {"proj-alpha": 5}), [])  # nothing is planned on a failed read
        with self.assertLogs("controlplane.spend_caps", level="WARNING"):
            results = spend_caps.apply_caps(ctx, [{"project": "proj-alpha", "op": "create", "amount": 5.0}])
        self.assertEqual([r["status"] for r in results], ["failed"])
        with self.assertLogs("controlplane.spend_caps", level="WARNING"):
            self.assertEqual(spend_caps.lift(ctx, "proj-alpha")["status"], "failed")
        google.fail = {("GET", BUDGETS_URL): GcpError("timeout", message="request timed out")}
        with self.assertLogs("controlplane.spend_caps", level="WARNING"):
            self.assertIn("timed out", spend_caps.read_caps(ctx)["error"])
        # A 403 for a disabled API names the API (seen live on a project without billingbudgets.googleapis.com).
        google.fail = {("GET", BUDGETS_URL): GcpError(
            "denied", 403, "Cloud Billing Budget API has not been used in project demo-project before or it is "
                           "disabled. Enable it by visiting the console.", "PERMISSION_DENIED")}
        with self.assertLogs("controlplane.spend_caps", level="WARNING"):
            error = spend_caps.read_caps(ctx)["error"]
        self.assertIn("billingbudgets.googleapis.com) is not enabled", error)
        self.assertNotIn("demo-project", error)   # upstream text is never passed on

    def test_too_many_budgets_are_not_managed(self):
        google = FakeGoogle(page=1)
        for i in range(3):
            google.add_budget("Team budget %d" % i, 5)
        with mock.patch.object(spend_caps, "MAX_BUDGET_PAGES", 2):
            read = spend_caps.read_caps(CapsCtx(google))
        self.assertFalse(read["ok"])
        self.assertIn("more than", read["error"])

    def test_plan_accepts_items_and_ignores_bad_wishes(self):
        items = {"proj-alpha": {"budget": "billingAccounts/%s/budgets/b-1" % ACCOUNT, "amount": 5.0,
                                "currency": "USD", "outputState": "CONFIGURED"}}
        plan = spend_caps.plan_caps(items, {"proj-alpha": 5, "proj-beta": -1, "Bad_Project!": 3, "proj-gamma": True})
        self.assertEqual([(p["project"], p["op"]) for p in plan], [("proj-alpha", "none")])
        bad = [None, {"project": "proj-alpha", "op": "delete"}, {"project": "x", "op": "create", "amount": 1.0},
               {"project": "proj-alpha", "op": "create", "amount": float("nan")}]
        self.assertEqual(spend_caps.apply_caps(CapsCtx(FakeGoogle()), bad), [])


class LiftTest(unittest.TestCase):
    def test_lift_patches_only_the_input_state(self):
        google = FakeGoogle()
        budget = google.add_budget("AI Control Plane cap: proj-alpha", 100, state="ENFORCED")
        ctx = CapsCtx(google)
        result = spend_caps.lift(ctx, " Proj-Alpha ")
        self.assertEqual((result["project"], result["op"], result["status"], result["budget"]),
                         ("proj-alpha", "lift", "lifted", budget["name"]))
        self.assertEqual([(c[1], c[2], c[3]) for c in google.writes()],
                         [("%s/%s" % (spend_caps.BUDGETS_V1, budget["name"]), {"updateMask": "spendCap.inputState"},
                           {"spendCap": {"inputState": "AWAITING_NEXT_PERIOD"}})])
        self.assertEqual(budget["spendCap"]["outputState"], "AWAITING_NEXT_PERIOD")
        self.assertEqual(budget["amount"]["specifiedAmount"]["units"], "100")

    def test_only_an_enforced_cap_can_be_lifted(self):
        google = FakeGoogle()
        google.add_budget("AI Control Plane cap: proj-alpha", 100, state="CONFIGURED")
        ctx = CapsCtx(google)
        self.assertEqual(spend_caps.lift(ctx, "proj-alpha")["status"], "not_enforced")
        self.assertEqual(spend_caps.lift(ctx, "proj-beta")["status"], "not_found")
        for value in ("Bad_Project!", None, ""):
            with self.assertRaises(ApiError):
                spend_caps.lift(ctx, value)
        self.assertEqual(google.writes(), [])

    def test_a_refused_lift_is_reported(self):
        google = FakeGoogle()
        google.add_budget("AI Control Plane cap: proj-alpha", 100, state="ENFORCED")
        google.fail[("PATCH", BUDGETS_URL)] = GcpError("denied", 403, "", "PERMISSION_DENIED")
        with self.assertLogs("controlplane.spend_caps", level="WARNING"):
            result = spend_caps.lift(CapsCtx(google), "proj-alpha")
        self.assertEqual(result["status"], "failed")
        self.assertIn("billing.budgets.configureSpendCap", result["message"])


class HandlerTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(governance, "write_audit", return_value=True)
        self.audit = patcher.start()
        self.addCleanup(patcher.stop)

    def test_status(self):
        google = FakeGoogle()
        ctx = CapsCtx(google)
        store_budgets(ctx, [hard_cap("proj-alpha", 25),
                            {"scope": "project", "project": "proj-beta", "monthlyPaidUsd": 9, "action": "notify"}])
        out = spend_caps.get_spend_caps(ctx)
        self.assertEqual((out["enabled"], out["accountConfigured"], out["desired"], out["error"]),
                         (True, True, {"proj-alpha": 25.0}, None))
        self.assertEqual([(p["project"], p["op"]) for p in out["plan"]], [("proj-alpha", "create")])
        self.assertEqual(google.writes(), [])

    def test_apply_is_a_dry_run_by_default(self):
        google = FakeGoogle()
        ctx = CapsCtx(google, body={})
        store_budgets(ctx, [hard_cap("proj-alpha", 25)])
        out = spend_caps.apply_spend_caps(ctx)
        self.assertEqual((out["dryRun"], [r["status"] for r in out["results"]], out["auditLogged"]),
                         (True, ["dry_run"], None))
        self.assertEqual(google.writes(), [])
        self.audit.assert_not_called()

    def test_apply_writes_and_audits(self):
        google = FakeGoogle()
        google.add_budget("AI Control Plane cap: proj-beta", 3, state="CONFIGURED")
        ctx = CapsCtx(google, body={"dryRun": False})
        store_budgets(ctx, [hard_cap("proj-alpha", 25)])
        out = spend_caps.apply_spend_caps(ctx)
        self.assertEqual({r["project"]: r["status"] for r in out["results"]},
                         {"proj-alpha": "created", "proj-beta": "orphan"})
        self.assertEqual([c[0] for c in google.writes()], ["POST"])
        self.audit.assert_called_once_with(ctx, "spend_caps_apply", {
            "results": {"created": 1, "orphan": 1}, "planned": 2,
            "changes": [{"project": "proj-alpha", "op": "create", "status": "created", "amount": 25.0}]})
        self.assertTrue(out["auditLogged"])
        self.assertEqual(ctx.app.invalidations, 1)
        self.assertEqual(sum(1 for c in google.calls if c[1] == BUDGETS_URL and c[0] == "GET"), 1)  # read once

    def test_apply_checks_the_caller_and_the_body(self):
        google = FakeGoogle()
        with self.assertRaises(ApiError) as caught:
            spend_caps.apply_spend_caps(CapsCtx(google, role="viewer", body={"dryRun": False}))
        self.assertEqual(caught.exception.code, "forbidden")
        with self.assertRaises(ApiError) as caught:
            spend_caps.apply_spend_caps(CapsCtx(google, body={"dryRun": "no"}))
        self.assertEqual(caught.exception.code, "bad_request")
        google.fail[("GET", BUDGETS_URL)] = GcpError("unavailable", 503)
        with self.assertRaises(ApiError) as caught, self.assertLogs("controlplane.spend_caps", level="WARNING"):
            spend_caps.apply_spend_caps(CapsCtx(google, body={"dryRun": False}))
        self.assertEqual(caught.exception.code, "upstream_error")
        self.assertEqual(google.writes(), [])

    def test_lift_handler_audits_the_outcome(self):
        google = FakeGoogle()
        google.add_budget("AI Control Plane cap: proj-alpha", 100, state="ENFORCED")
        ctx = CapsCtx(google, body={"project": "proj-alpha"})
        out = spend_caps.lift_spend_cap(ctx)
        self.assertEqual((out["status"], out["auditLogged"]), ("lifted", True))
        self.audit.assert_called_once_with(ctx, "spend_cap_lift", {"project": "proj-alpha", "status": "lifted"})
        self.assertEqual(ctx.app.invalidations, 1)
        with self.assertRaises(ApiError) as caught:
            spend_caps.lift_spend_cap(CapsCtx(google, role="viewer", body={"project": "proj-alpha"}))
        self.assertEqual(caught.exception.code, "forbidden")


class WorkspaceScopeTest(unittest.TestCase):
    """Spend caps are created or changed only for workspace projects (ctx.all_projects): the billing account can also
    pay for projects of other teams, which a dashboard administrator must not be able to pause."""

    def setUp(self):
        patcher = mock.patch.object(governance, "write_audit", return_value=True)
        self.audit = patcher.start()
        self.addCleanup(patcher.stop)

    def test_projects_outside_the_workspace_are_planned_blocked_and_never_touched(self):
        google = FakeGoogle()
        google.add_budget("AI Control Plane cap: proj-beta", 3, state="CONFIGURED", projects=("projects/2345678",))
        ctx = CapsCtx(google, workspace=("proj-alpha",))
        desired = {"proj-alpha": 10, "proj-beta": 5, "proj-other": 7}
        read = spend_caps.read_caps(ctx)
        plan = spend_caps.plan_caps(read, desired, budgets.workspace_projects(ctx))
        self.assertEqual({p["project"]: (p["op"], p["blocked"]) for p in plan},
                         {"proj-alpha": ("create", None), "proj-beta": ("update", "outside_workspace"),
                          "proj-other": ("create", "outside_workspace")})
        # apply_caps checks the membership again, whatever the plan says (here a plan made without the workspace).
        unchecked = spend_caps.plan_caps(read, desired)
        self.assertEqual({p["blocked"] for p in unchecked}, {None})
        for dry_run in (True, False):
            results = by_project(spend_caps.apply_caps(ctx, unchecked, dry_run=dry_run))
            self.assertEqual((results["proj-beta"]["status"], results["proj-other"]["status"]),
                             ("outside_workspace", "outside_workspace"), dry_run)
            self.assertIn("not part of this workspace", results["proj-other"]["message"])
        self.assertEqual([(c[0], c[3]["displayName"]) for c in google.writes()],
                         [("POST", "AI Control Plane cap: proj-alpha")])
        self.assertEqual([c for c in google.calls if c[1].endswith(("/proj-beta", "/proj-other"))], [])  # no lookup

    def test_status_and_apply_report_outside_projects_and_audit_each_project(self):
        google = FakeGoogle()
        ctx = CapsCtx(google, body={"dryRun": False}, workspace=("proj-alpha",))
        store_budgets(ctx, [hard_cap("proj-alpha", 25), hard_cap("proj-other", 7)])
        status = spend_caps.get_spend_caps(ctx)
        self.assertEqual({p["project"]: p["blocked"] for p in status["plan"]},
                         {"proj-alpha": None, "proj-other": "outside_workspace"})
        out = spend_caps.apply_spend_caps(ctx)
        self.assertEqual({r["project"]: r["status"] for r in out["results"]},
                         {"proj-alpha": "created", "proj-other": "outside_workspace"})
        self.audit.assert_called_once_with(ctx, "spend_caps_apply", {
            "results": {"created": 1, "outside_workspace": 1}, "planned": 2,
            "changes": [{"project": "proj-alpha", "op": "create", "status": "created", "amount": 25.0},
                        {"project": "proj-other", "op": "create", "status": "outside_workspace", "amount": 7.0}]})

    def test_the_audit_entry_lists_at_most_audit_changes_projects(self):
        google = FakeGoogle()
        ctx = CapsCtx(google, body={"dryRun": False}, workspace=())
        store_budgets(ctx, [hard_cap("proj-a%d" % i, 1) for i in range(3)])
        with mock.patch.object(spend_caps, "AUDIT_CHANGES", 2):
            spend_caps.apply_spend_caps(ctx)
        entry = self.audit.call_args.args[2]
        self.assertEqual((entry["results"], len(entry["changes"]), entry["changesOmitted"]),
                         ({"outside_workspace": 3}, 2, 1))
        self.assertNotIn("@", repr(entry))   # projects only, never people
        self.assertEqual(google.writes(), [])

    def test_an_unreadable_project_list_fails_closed(self):
        class NoProjects(CapsCtx):
            @property
            def all_projects(self):
                raise GcpError("unavailable", 503)
        google = FakeGoogle()
        ctx = NoProjects(google)
        with self.assertLogs("controlplane.budgets", level="WARNING"):
            self.assertEqual(budgets.workspace_projects(ctx), set())
        with self.assertLogs("controlplane.budgets", level="WARNING"):
            result = spend_caps.apply_caps(ctx, [{"project": "proj-alpha", "op": "create", "amount": 5.0}])[0]
        self.assertEqual(result["status"], "outside_workspace")
        self.assertEqual(google.writes(), [])

    def test_a_cap_outside_the_workspace_can_still_be_lifted(self):
        # Lifting removes a restriction, so it is not limited to the workspace: a cap made before the workspace changed
        # can still be lifted from the dashboard (the ownership check below still applies).
        google = FakeGoogle()
        google.add_budget("AI Control Plane cap: proj-alpha", 100, state="ENFORCED")
        self.assertEqual(spend_caps.lift(CapsCtx(google, workspace=()), "proj-alpha")["status"], "lifted")


class BudgetOwnershipTest(unittest.TestCase):
    """A budget named like the control plane's cap of a project is changed or lifted only when it covers exactly that
    project (by number) and Agent Platform and has a spend cap: anyone with budget rights on the billing account can
    give a budget that name."""

    def patches(self, google):
        return [call for call in google.calls if call[0] == "PATCH"]

    def test_updates_leave_budgets_that_are_not_the_cap_alone(self):
        for kwargs in ({"projects": ("projects/1234567", "projects/2345678")},   # another project as well
                       {"projects": ("projects/2345678",)},                       # the number of another project
                       {"services": (COMPUTE["name"],)},                          # another service
                       {"services": (SERVICE, COMPUTE["name"])},                  # another service as well
                       {"state": None}):                                          # no spend cap at all
            google = FakeGoogle()
            google.add_budget("AI Control Plane cap: proj-alpha", 100, **dict({"state": "CONFIGURED"}, **kwargs))
            ctx = CapsCtx(google)
            plan = spend_caps.plan_caps(spend_caps.read_caps(ctx), {"proj-alpha": 150})
            self.assertEqual([(p["op"], p["blocked"]) for p in plan], [("update", None)], kwargs)
            with self.assertLogs("controlplane.spend_caps", level="WARNING"):
                result = spend_caps.apply_caps(ctx, plan)[0]
            self.assertEqual((result["op"], result["status"]), ("update", "not_owned"), kwargs)
            self.assertIn("left unchanged", result["message"])
            self.assertEqual(self.patches(google), [], kwargs)

    def test_lifts_leave_budgets_that_are_not_the_cap_alone(self):
        google = FakeGoogle()
        google.add_budget("AI Control Plane cap: proj-alpha", 100, state="ENFORCED",
                          projects=("projects/1234567", "projects/2345678"))
        with self.assertLogs("controlplane.spend_caps", level="WARNING"):
            result = spend_caps.lift(CapsCtx(google), "proj-alpha")
        self.assertEqual((result["status"], result["budget"]), ("not_owned", google.budgets[0]["name"]))
        self.assertEqual(self.patches(google), [])

    def test_a_failed_ownership_lookup_changes_nothing(self):
        google = FakeGoogle()
        google.add_budget("AI Control Plane cap: proj-alpha", 100, state="ENFORCED")
        google.fail[("GET", spend_caps.CRM_V3)] = GcpError("unavailable", 503)
        ctx = CapsCtx(google)
        with self.assertLogs("controlplane.spend_caps", level="WARNING"):
            self.assertEqual(spend_caps.lift(ctx, "proj-alpha")["status"], "failed")
        google.budgets[0]["spendCap"]["outputState"] = "CONFIGURED"
        plan = spend_caps.plan_caps(spend_caps.read_caps(ctx), {"proj-alpha": 150})
        with self.assertLogs("controlplane.spend_caps", level="WARNING"):
            self.assertEqual(spend_caps.apply_caps(ctx, plan)[0]["status"], "failed")
        self.assertEqual(self.patches(google), [])


if __name__ == "__main__":
    unittest.main()
