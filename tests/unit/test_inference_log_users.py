"""Antigravity inference log users (businessaicode.googleapis.com/inference_response).

The official format (Gemini Enterprise docs, "Track third-party model token usage") names the caller as an IAM
principal: labels.user_id = "user:user@example.com". The IAM side keys people by the plain email of direct "user:"
bindings (PolicyIndex.direct_users), so usage must use the same key. Otherwise caps never enforce and every person
shows up twice (once as "user:..." with usage, once from IAM without usage).
"""

import copy
import json
import unittest

import support
from support import ACCESS_ROLE, CAP_MARKER
from test_governance_actions import Harness, members

from controlplane import timeutil
from controlplane.schema import Source, TableSchema
from controlplane.security import uid_for
from controlplane.services import governance as gov
from controlplane.services import telemetry, users
from controlplane.services.base import AGY_SINK

# Verbatim sample entry from https://cloud.google.com/gemini/enterprise/docs/ai-developer-tools-track-token-usage
OFFICIAL_SAMPLE = json.loads("""
{
  "logName": "projects/[PROJECT_ID]/logs/businessaicode.googleapis.com%2Finference_response",
  "timestamp": "2026-10-07T17:23:03.495323480Z",
  "labels": {
    "model": "claude-sonnet-5-5",
    "model_provider": "Anthropic",
    "client_name": "antigravity_cli",
    "user_id": "user:user@example.com",
    "trajectory_id": "25cd0b58-58ea-4beb-b2d7-42e0d3fdd96d",
    "request_id": "25cd0b58-58ea-4beb-b2d7-42e0d3fdd96d-19"
  },
  "jsonPayload": {
    "@type": "type.googleapis.com/google.cloud.businessaicode.logging.v1.InferenceResponseLog",
    "metadata": {
      "promptTokenCount": "70825",
      "cachedContentTokenCount": "69079",
      "candidatesTokenCount": "14215",
      "totalTokenCount": "85040"
    }
  }
}
""")
WINDOW = timeutil.parse_range({"start": "2026-10-01", "end": "2026-10-31"}, "UTC", 31)
START, END = WINDOW.start_ts(), WINDOW.end_ts()
LOG_ID = "businessaicode.googleapis.com%2Finference_response"


def sample(**labels):
    entry = copy.deepcopy(OFFICIAL_SAMPLE)
    entry["labels"].update(labels)
    return entry


class LoggingCtx(support.FakeCtx):
    """Answers Cloud Logging entries:list with the given entries and records the request bodies."""

    def __init__(self, entries, **kwargs):
        super().__init__(**kwargs)
        self.entries = entries
        self.bodies = []

    def rest(self, method, url, **kwargs):  # pylint: disable=unused-argument
        self.bodies.append(kwargs.get("json_body"))
        return {"entries": copy.deepcopy(self.entries)}


class QueryCtx(support.FakeCtx):
    """Records BigQuery SQL; the per-user aggregation query answers with the given rows."""

    def __init__(self, rows, sources=None):
        super().__init__()
        self.rows = rows
        self.srcs = sources or {}
        self.queries = []
        self.params = []

    def audit_source(self, logical, optional=False):
        return self.srcs.get(logical)

    def query(self, sql, params, module, **kwargs):
        self.queries.append(sql)
        self.params.append(params)
        return self.rows if "GROUP BY email, model" in sql else []


def sink_source():
    """BigQuery Log Router table of the inference log: labels as columns, token counts as strings."""
    schema = TableSchema.from_dicts([
        {"name": "timestamp", "type": "TIMESTAMP"},
        {"name": "labels", "type": "RECORD", "fields": [
            {"name": name, "type": "STRING"}
            for name in ("model", "model_provider", "client_name", "user_id", "trajectory_id", "request_id")]},
        {"name": "jsonPayload", "type": "RECORD", "fields": [
            {"name": "metadata", "type": "RECORD", "fields": [
                {"name": name, "type": "STRING"}
                for name in ("promptTokenCount", "cachedContentTokenCount", "candidatesTokenCount",
                             "totalTokenCount")]}]},
    ])
    return Source(support.PROJECT, "audit", AGY_SINK, "table", AGY_SINK, schema)


class PrincipalTest(unittest.TestCase):
    def test_usage_user_keeps_people_only(self):
        self.assertEqual(gov.usage_user("user:user@example.com"), "user@example.com")
        self.assertEqual(gov.usage_user("User:Dev.Lead@Example.com "), "dev.lead@example.com")
        self.assertEqual(gov.usage_user("user@example.com"), "user@example.com")  # entries without the prefix
        for other in ("serviceAccount:bot@demo-project.iam.gserviceaccount.com",
                      "bot@demo-project.iam.gserviceaccount.com",
                      "principal://iam.googleapis.com/locations/global/workforcePools/p/subject/s",
                      "group:team@example.com", "", None):
            self.assertIsNone(gov.usage_user(other), other)


class LoggingPathTest(unittest.TestCase):
    def test_official_sample_aggregates_under_the_plain_email(self):
        usage = gov._logging_usage(LoggingCtx([OFFICIAL_SAMPLE]), START, END)  # pylint: disable=protected-access
        self.assertTrue(usage.ok)
        self.assertEqual(list(usage.users), ["user@example.com"])
        rec = usage.users["user@example.com"]
        self.assertEqual((rec["requests"], rec["tokens"], rec["promptTokens"], rec["candidateTokens"]),
                         (1, 85040, 70825, 14215))
        self.assertEqual(rec["models"], {"claude-sonnet-5-5"})
        self.assertEqual(rec["clients"], {"antigravity_cli"})
        self.assertEqual(usage.records, 1)

    def test_prefixed_and_bare_forms_are_one_person_and_service_accounts_no_person(self):
        entries = [OFFICIAL_SAMPLE, sample(user_id="user@example.com"), sample(user_id="User:USER@example.com"),
                   sample(user_id="serviceAccount:bot@demo-project.iam.gserviceaccount.com")]
        usage = gov._logging_usage(LoggingCtx(entries), START, END)  # pylint: disable=protected-access
        self.assertEqual(list(usage.users), ["user@example.com"])
        self.assertEqual(usage.users["user@example.com"]["requests"], 3)
        self.assertEqual(usage.records, 4)  # the service account call is telemetry, not a person

    def test_per_user_filter_matches_the_prefixed_form(self):
        ctx = LoggingCtx([OFFICIAL_SAMPLE])
        gov._logging_usage_project(ctx, support.PROJECT, LOG_ID, START, END,  # pylint: disable=protected-access
                                   email="user@example.com")
        self.assertIn('labels.user_id=~"(?i)^(user:)?user@example[.]com$"',
                      ctx.bodies[0]["filter"])
        usage = gov._logging_usage(LoggingCtx([OFFICIAL_SAMPLE]), START, END,  # pylint: disable=protected-access
                                   email="user@example.com")
        self.assertEqual(usage.by_model["claude-sonnet-5-5"]["tokens"], 85040)
        self.assertEqual(usage.by_client["antigravity_cli"]["requests"], 1)


class BigQueryPathTest(unittest.TestCase):
    def normalized(self):
        return gov.agy_user_sql("t.`labels`.`user_id`")

    def test_sql_normalizes_the_principal_and_filters_on_it(self):
        self.assertEqual(self.normalized(), "LOWER(REGEXP_REPLACE(TRIM(t.`labels`.`user_id`), r'(?i)^user:', ''))")
        rows = [{"email": "user@example.com", "model": "claude-sonnet-5-5", "requests": 1, "tokens": 85040,
                 "prompt_tokens": 70825, "candidate_tokens": 14215, "clients": ["antigravity_cli"], "last_ts": None},
                {"email": "serviceaccount:bot@demo-project.iam.gserviceaccount.com", "model": "claude-sonnet-5-5",
                 "requests": 2, "tokens": 10, "prompt_tokens": 5, "candidate_tokens": 5, "clients": [], "last_ts": None}]
        ctx = QueryCtx(rows)
        usage = gov._bq_usage(ctx, sink_source(), START, END, email="user@example.com")  # pylint: disable=protected-access
        sql = ctx.queries[0]
        self.assertIn("%s AS email" % self.normalized(), sql)
        self.assertIn("%s = @email" % self.normalized(), sql)
        self.assertEqual(ctx.params[0]["email"], "user@example.com")
        self.assertEqual(list(usage.users), ["user@example.com"])
        self.assertEqual(usage.users["user@example.com"]["tokens"], 85040)
        self.assertEqual(usage.records, 3)

    def test_users_page_uses_the_same_normalization(self):
        ctx = QueryCtx([], sources={AGY_SINK: sink_source()})
        ctes = users.long_ctes(ctx)[0]
        agy = next(c for c in ctes if c.startswith("agy AS ("))
        self.assertIn(self.normalized(), agy)


class EnforcementTest(unittest.TestCase):
    POLICY = {"version": 3, "etag": "BwX1", "bindings": [{"role": ACCESS_ROLE, "members": ["user:user@example.com"]}]}

    def usage(self):
        return gov._logging_usage(LoggingCtx([OFFICIAL_SAMPLE]), START, END)  # pylint: disable=protected-access

    def test_sample_user_matches_the_iam_binding_once(self):
        ctx = support.FakeCtx(support.make_settings())
        with Harness(policies=[self.POLICY], usage=self.usage()):
            payload, _ = gov.compute_status(ctx, "monthly", "rolling", 50000, 0, mode="auto")
        self.assertEqual([u["email"] for u in payload["users"]], ["user@example.com"])  # no "user:..." duplicate
        row = payload["users"][0]
        self.assertEqual((row["state"], row["tokens"], row["overCap"], row["plannedAction"]),
                         (gov.EA, 85040, True, "block"))
        self.assertEqual(payload["plan"]["block"], [uid_for("user@example.com")])

    def test_over_cap_sample_user_is_suspended(self):
        ctx = support.FakeCtx(support.make_settings(), body={"mode": "apply", "tokenCap": 50000})
        with Harness(policies=[self.POLICY], usage=self.usage()) as h:
            out = gov.cap(ctx)
        self.assertTrue(out["executed"])
        self.assertEqual([(c["email"], c["action"], c["status"]) for c in out["changes"]],
                         [("user@example.com", "block", "applied")])
        written = h.written_policies()[0]
        self.assertIn("user:user@example.com", members(written, CAP_MARKER))
        self.assertNotIn("user:user@example.com", members(written, ACCESS_ROLE))


class ConnectorTest(unittest.TestCase):
    def test_connector_users_drop_the_iam_prefix(self):
        self.assertEqual(telemetry._connector_user("user:user@example.com"), "user@example.com")  # pylint: disable=protected-access
        self.assertEqual(telemetry._connector_user("alice@example.com"), "alice@example.com")  # pylint: disable=protected-access
        self.assertEqual(telemetry._connector_user("serviceAccount:bot@demo-project.iam.gserviceaccount.com"),  # pylint: disable=protected-access
                         "serviceAccount:bot@demo-project.iam.gserviceaccount.com")
        self.assertIsNone(telemetry._connector_user(None))  # pylint: disable=protected-access


if __name__ == "__main__":
    unittest.main()
