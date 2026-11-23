"""Multi-project usage, workforce identities and license-tier credits in the Antigravity governance service."""

import datetime as dt
import unittest
from unittest import mock

import support
from test_inference_log_users import END, LOG_ID, START, LoggingCtx, QueryCtx, sample

from controlplane import config
from controlplane.schema import Source, TableSchema
from controlplane.services import credit_ledger
from controlplane.services import gemini_enterprise as ge
from controlplane.services import governance as gov
from controlplane.services.base import AGY_SINK

WF_RAW = "principal://iam.googleapis.com/locations/global/workforcePools/corp-pool/subject/Kim.Lee@example.com"
WF_KEY = "principal://iam.googleapis.com/locations/global/workforcepools/corp-pool/subject/kim.lee@example.com"


def sink_with_logname(created=None):
    schema = TableSchema.from_dicts([
        {"name": "timestamp", "type": "TIMESTAMP"},
        {"name": "logName", "type": "STRING"},
        {"name": "labels", "type": "RECORD", "fields": [
            {"name": name, "type": "STRING"} for name in ("model", "client_name", "user_id")]},
        {"name": "jsonPayload", "type": "RECORD", "fields": [
            {"name": "metadata", "type": "RECORD", "fields": [
                {"name": name, "type": "STRING"}
                for name in ("promptTokenCount", "cachedContentTokenCount", "candidatesTokenCount",
                             "totalTokenCount")]}]},
    ])
    return Source(support.PROJECT, "audit", AGY_SINK, "table", AGY_SINK, schema, created=created)


def bq_row(email, model, project, requests, tokens, prompt, cand):
    return {"email": email, "model": model, "project": project, "requests": requests, "tokens": tokens,
            "prompt_tokens": prompt, "candidate_tokens": cand, "cached_tokens": 0, "clients": [], "last_ts": None}


class ProjectDimensionTest(unittest.TestCase):
    def test_bigquery_usage_groups_by_the_project_in_the_log_name(self):
        rows = [bq_row("alice@example.com", "gemini-3-pro", "proj-a", 2, 100, 60, 40),
                bq_row("alice@example.com", "gemini-3-pro", "proj-b", 1, 50, 30, 20),
                bq_row("alice@example.com", "claude-sonnet-5-5", "proj-b", 1, 10, 5, 5),
                bq_row(WF_RAW.lower(), "gemini-3-pro", "proj-a", 3, 30, 20, 10)]
        ctx = QueryCtx(rows)
        usage = gov._bq_usage(ctx, sink_with_logname(), START, END)  # pylint: disable=protected-access
        sql = ctx.queries[0]
        self.assertIn("REGEXP_EXTRACT(", sql)
        self.assertIn("GROUP BY email, model, project", sql)
        alice = usage.users["alice@example.com"]
        self.assertEqual(alice["requests"], 4)
        self.assertEqual(sorted(alice["byProject"]), ["proj-a", "proj-b"])
        self.assertEqual(alice["byProject"]["proj-b"]["requests"], 2)
        self.assertEqual(sorted(alice["byProject"]["proj-b"]["byModel"]), ["claude-sonnet-5-5", "gemini-3-pro"])
        self.assertIn(WF_KEY, usage.users)  # workforce identities are people now
        self.assertEqual(usage.users[WF_KEY]["byProject"]["proj-a"]["tokens"], 30)

    def test_cloud_logging_usage_takes_the_project_from_the_entry(self):
        entries = [sample(user_id="user:alice@example.com"),
                   dict(sample(user_id="user:alice@example.com"),
                        logName="projects/proj-b/logs/businessaicode.googleapis.com%2Finference_response"),
                   sample(user_id=WF_RAW)]
        usage = gov._logging_usage(LoggingCtx(entries), START, END)  # pylint: disable=protected-access
        alice = usage.users["alice@example.com"]
        # "[PROJECT_ID]" in the verbatim sample is not a project id pattern match failure: the regex takes it.
        self.assertEqual(sorted(alice["byProject"]), sorted({"[PROJECT_ID]", "proj-b"}))
        self.assertEqual(usage.users[WF_KEY]["requests"], 1)

    def test_merge_keeps_projects(self):
        a, b = gov.Usage("bigquery"), gov.Usage("logging")
        for usage, project in ((a, "proj-a"), (b, "proj-b")):
            rec = usage.user("alice@example.com")
            rec["requests"] += 1
            gov.Usage.add_model(rec, "gemini-3-pro", 1, 10, 6, 4)
            gov.Usage.add_project(rec, project, "gemini-3-pro", 1, 10, 6, 4)
        a.merge(b)
        self.assertEqual(sorted(a.users["alice@example.com"]["byProject"]), ["proj-a", "proj-b"])
        self.assertEqual(a.users["alice@example.com"]["byModel"]["gemini-3-pro"]["requests"], 2)

    def test_project_split_spreads_the_credit_over_gemini_cost(self):
        pricing = gov.Pricing(currency="USD", models=(("gemini-3-pro", 1.0, 1.0), ("claude-sonnet-5-5", 1.0, 1.0)),
                              source="manual")
        rec = gov.Usage("x").user("alice@example.com")
        gov.Usage.add_project(rec, "proj-a", "gemini-3-pro", 1, 3_000_000, 1_000_000, 2_000_000)  # $3
        gov.Usage.add_project(rec, "proj-b", "gemini-3-pro", 1, 1_000_000, 500_000, 500_000)      # $1
        gov.Usage.add_project(rec, "proj-b", "claude-sonnet-5-5", 1, 2_000_000, 1_000_000, 1_000_000)  # $2
        rows = {r["project"]: r for r in gov.project_split(rec, pricing, ("gemini",), credit=2.0)}
        self.assertEqual((rows["proj-a"]["cost"], rows["proj-a"]["credit"], rows["proj-a"]["chargeable"]),
                         (3.0, 1.5, 1.5))
        self.assertEqual((rows["proj-b"]["cost"], rows["proj-b"]["credit"], rows["proj-b"]["chargeable"]),
                         (3.0, 0.5, 2.5))
        by_cost = {r["project"]: r for r in gov.project_split(rec, pricing, ("gemini",), credit=3.0, weight="cost")}
        self.assertEqual((by_cost["proj-a"]["credit"], by_cost["proj-b"]["credit"]), (1.5, 1.5))
        unknown = gov.project_split(rec, pricing, ("gemini",), credit=2.0, known=False)
        self.assertEqual([(r["requests"], r["cost"], r["credit"], r["chargeable"]) for r in unknown],
                         [(1, None, None, None), (2, None, None, None)])


class WorkforceTest(unittest.TestCase):
    def test_workforce_users_are_not_enforceable(self):
        idx = gov.PolicyIndex({"bindings": []})
        cfg = gov.gov_config(support.make_settings())
        self.assertEqual(gov.classify(WF_KEY, True, idx, cfg), (gov.NE, "WORKFORCE_IDENTITY"))
        self.assertIn("WORKFORCE_IDENTITY", gov.REMEDIATION)
        self.assertEqual(gov.classify(WF_KEY, True, gov.PolicyIndex(None, readable=False), cfg),
                         (gov.UNKNOWN, "POLICY_UNREADABLE"))

    def test_per_person_logging_filter_for_a_workforce_key(self):
        ctx = LoggingCtx([])
        gov._logging_usage_project(ctx, support.PROJECT, LOG_ID, START, END, email=WF_KEY)  # pylint: disable=protected-access
        flt = ctx.bodies[0]["filter"]
        self.assertIn('labels.user_id=~"(?i)^principal[:][/][/]iam[.]googleapis[.]com', flt)
        bad = WF_KEY + '"injected'
        with self.assertRaises(gov.GcpError):
            gov._logging_usage_project(LoggingCtx([]), support.PROJECT, LOG_ID, START, END,  # pylint: disable=protected-access
                                       email=bad.replace("kim", 'k"im'))


class LicenseCreditTest(unittest.TestCase):
    def index(self, people):
        idx = ge.LicenseIndex()
        idx.ok = True
        idx.people = people
        return idx

    def test_license_credits_pick_the_largest_tier(self):
        ctx = support.FakeCtx()
        idx = self.index({
            "alice@example.com": [{"tier": "SUBSCRIPTION_TIER_ENTERPRISE"},
                                  {"tier": "SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT"}],
            "bob@example.com": [{"tier": "SUBSCRIPTION_TIER_CONSUMPTION_ONLY"}],
            "carol@example.com": [{"tier": "SUBSCRIPTION_TIER_NOTEBOOK_LM"}]})
        with mock.patch.object(ge, "license_index", return_value=idx):
            tiers = gov.license_credits(ctx)
        self.assertEqual(tiers, {"alice@example.com": ("SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT", 15.0),
                                 "bob@example.com": ("SUBSCRIPTION_TIER_CONSUMPTION_ONLY", 0.0)})
        fixed = support.FakeCtx(support.make_settings(governance={"credit_source": "fixed"}))
        with mock.patch.object(ge, "license_index", return_value=idx) as lookup:
            self.assertEqual(gov.license_credits(fixed), {})
        lookup.assert_not_called()

    def test_ledgers_use_tier_credit_and_rules_win(self):
        ag = dict(support.make_settings()["governance"]["antigravity"], credit_per_user_month=10)
        now = dt.datetime(2026, 11, 19, 3, 0, tzinfo=dt.timezone.utc)
        segs, _, _ = credit_ledger.segments(credit_ledger.Cycle(tz="UTC", weekday=0, at="00:00"), now)
        parts = [gov.Usage("x") for _ in segs]
        terms = {}
        pricing = gov.Pricing(currency="USD", input_per_million=1, output_per_million=1, source="manual")
        exceptions = {"carol@example.com": {"type": "limit", "creditMonth": 30}}
        tiers = {"alice@example.com": ("SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT", 15.0),
                 "carol@example.com": ("SUBSCRIPTION_TIER_ENTERPRISE", 10.0)}
        ledgers = gov._credit_ledgers(ag, ["alice@example.com", "bob@example.com", "carol@example.com"],  # pylint: disable=protected-access
                                      exceptions, segs, parts, pricing, tiers=tiers, terms=terms)
        self.assertEqual(ledgers["alice@example.com"][0].credit_week, 3.75)
        self.assertEqual(ledgers["bob@example.com"][0].credit_week, 2.5)
        self.assertEqual(ledgers["carol@example.com"][0].credit_week, 7.5)
        self.assertEqual([terms[p]["creditSource"] for p in sorted(terms)], ["license", "fixed", "rule"])
        self.assertEqual(terms["alice@example.com"]["tier"], "SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT")


class LicenseIndexTest(unittest.TestCase):
    def test_assignments_map_to_their_config_tier(self):
        results = {
            ("configs", "proj-a", "global"): (True, ([{"name": "projects/123/locations/global/licenseConfigs/std",
                                                      "subscriptionTier": "SUBSCRIPTION_TIER_ENTERPRISE"},
                                                     {"name": "projects/proj-a/locations/global/licenseConfigs/plus",
                                                      "subscriptionTier": "SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT"}],
                                                    2)),
            ("users", "proj-a", "global"): (True, ([
                {"userPrincipal": "Alice@Example.com", "licenseAssignmentState": "ASSIGNED",
                 "licenseConfig": "projects/proj-a/locations/global/licenseConfigs/plus"},
                {"userPrincipal": WF_RAW, "licenseAssignmentState": "ASSIGNED",
                 "licenseConfig": "projects/123/locations/global/licenseConfigs/std"},
                {"userPrincipal": "gone@example.com", "licenseAssignmentState": "UNASSIGNED",
                 "licenseConfig": "projects/123/locations/global/licenseConfigs/std"},
                {"userPrincipal": "bot@p.iam.gserviceaccount.com", "licenseAssignmentState": "ASSIGNED"}], False)),
            ("configs", "proj-b", "global"): (False, RuntimeError("denied")),
            ("users", "proj-b", "global"): (False, RuntimeError("denied")),
        }
        index = ge.build_license_index(results, ["proj-a", "proj-b"], ["global"])
        self.assertTrue(index.ok)
        self.assertEqual(index.tiers("alice@example.com"), ["SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT"])
        self.assertEqual(index.tiers(WF_KEY), ["SUBSCRIPTION_TIER_ENTERPRISE"])
        self.assertNotIn("gone@example.com", index.people)
        self.assertEqual(len(index.people), 2)

    def test_license_pages_follow_the_setting(self):
        ctx = support.FakeCtx(support.make_settings(workspace={"license_max_pages": 3}))
        self.assertEqual(ge.license_max_pages(ctx), 3)
        self.assertEqual(ge.license_max_pages(support.FakeCtx()), config.DEFAULTS["workspace"]["license_max_pages"])


class CentralAuditGapTest(unittest.TestCase):
    def test_one_central_table_covers_every_project(self):
        created = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
        src = sink_with_logname(created=created)
        settings = support.make_settings(workspace={"projects": ["proj-b", "proj-c"]})
        ctx = support.FakeCtx(settings)
        self.assertEqual(set(gov.logging_gaps(ctx, src, START, END)), {"proj-b", "proj-c"})  # per-project sinks
        ctx.central_audit_project = support.PROJECT
        self.assertEqual(gov.logging_gaps(ctx, src, START, END), {})


if __name__ == "__main__":
    unittest.main()
