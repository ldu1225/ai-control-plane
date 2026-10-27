"""End-to-end GET endpoint coverage without Google Cloud access.

Every GET endpoint of the real application runs against fake BigQuery schemas (all core tables
present, some date-sharded, a billing export with credits/resource columns), fake REST payloads
and fake query results. The tests verify 200 responses without internal errors, that every
generated query binds all of its @parameters (as BigQuery requires), and the null-vs-empty
semantics when sources succeed with no data.
"""

import datetime as dt
import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane import app as app_module
from controlplane import schema as schema_mod
from controlplane.gcp import Gcp, GcpError, QueryResult

try:  # build_params needs the BigQuery client library (pinned in requirements.txt)
    import google.cloud.bigquery  # noqa: F401
    HAVE_BIGQUERY = True
except ImportError:  # pragma: no cover
    HAVE_BIGQUERY = False

PROJECT = "demo-project"
BILLING_TABLE = "gcp_billing_export_resource_v1_ABCDEF_ABCDEF_ABCDEF"


def rec(name, *fields, mode="NULLABLE"):
    return {"name": name, "type": "RECORD", "mode": mode, "fields": list(fields)}


def col(name, ftype="STRING"):
    return {"name": name, "type": ftype}


COMMON = [col("logName"), col("severity"), col("insertId"), col("trace"), col("timestamp", "TIMESTAMP"),
          col("receiveTimestamp", "TIMESTAMP")]
AUDIT = COMMON + [rec("protopayload_auditlog", col("serviceName"), col("methodName"), col("resourceName"),
                      rec("authenticationInfo", col("principalEmail")),
                      rec("status", col("code", "INTEGER"), col("message")),
                      rec("requestMetadata", col("callerIp"), col("callerSuppliedUserAgent")))]
JSON_LOG = COMMON + [col("jsonPayload", "JSON"),
                     rec("labels", col("modelarmor_googleapis_com_client_correlation_id"), col("user_id"),
                         col("client_name"), col("model_id"))]
ARMOR = JSON_LOG + [col("jsonpayload_v1_sanitizeoperationlogentry", "JSON")]
BILLING = [col("billing_account_id"), rec("service", col("id"), col("description")),
           rec("sku", col("id"), col("description")), col("usage_start_time", "TIMESTAMP"),
           col("usage_end_time", "TIMESTAMP"), rec("project", col("id"), col("name")),
           rec("resource", col("name"), col("global_name")), col("cost", "FLOAT"), col("currency"),
           rec("usage", col("amount", "FLOAT"), col("unit"), col("pricing_unit")),
           rec("credits", col("name"), col("amount", "FLOAT"), col("type"), mode="REPEATED")]
TABLES = {
    "cloudaudit_googleapis_com_activity": AUDIT,
    "cloudaudit_googleapis_com_data_access_20261018": AUDIT,
    "cloudaudit_googleapis_com_data_access_20261019": AUDIT,
    "discoveryengine_googleapis_com_gen_ai_user_message_20261019": JSON_LOG,
    "discoveryengine_googleapis_com_gemini_enterprise_user_activity": JSON_LOG,
    "modelarmor_googleapis_com_sanitize_operations": ARMOR,
    "businessaicode_googleapis_com_inference_response": JSON_LOG,
    "codemender_googleapis_com_events": JSON_LOG,
}
NUMERIC = {"n", "calls", "cost", "amount", "gross", "credits", "ai_gross", "tokens", "prompt_tokens",
           "candidate_tokens", "total", "prompts", "users", "ops", "events", "requests", "errors", "notebooks",
           "uploads", "agent_calls", "principals", "humans", "services", "callers", "code", "row_count",
           "size_bytes", "prev_calls", "model_calls", "invocations", "prompt_blocks", "response_blocks",
           "geactivity", "notebooklmactivity", "geprompts", "agentcalls", "notebooklmprompts",
           "antigravityrequests", "antigravitytokens", "agentplatformcalls", "armorblocks", "fileuploads"}
CONFIG = """workspace:
  project_id: demo-project
  audit_dataset: ge_analytics
  timezone: Asia/Seoul
  billing:
    dataset: billing_detailed_usage
    table: gcp_billing_export_resource_v1_ABCDEF_ABCDEF_ABCDEF
    account_id: ABCDEF-ABCDEF-ABCDEF
security:
  auth_mode: local
storage:
  backend: memory
telemetry:
  connectors:
    - id: conn_0000abcd
      name: CodeMender
      enabled: true
      source: bigquery
      bigquery: {project: demo-project, dataset: ge_analytics, table: codemender_googleapis_com_events}
      mapping: {time: timestamp, user: labels.user_id, model: labels.model_id,
                tokens: jsonPayload.metadata.totalTokenCount, operation: logName, status: severity}
    - id: conn_0000beef
      name: Inference
      enabled: true
      source: logging
      logging: {filter: 'logName:"inference"'}
      mapping: {time: timestamp, user: labels.user_id, model: labels.model_id,
                tokens: jsonPayload.metadata.totalTokenCount, operation: labels.client_name, status: severity}
"""
ENDPOINTS = [
    "/api/v1/overview/summary", "/api/v1/overview/summary?granularity=week&start=2026-09-01&end=2026-10-19",
    "/api/v1/gemini-enterprise/summary", "/api/v1/gemini-enterprise/licenses",
    "/api/v1/gemini-enterprise/file-uploads", "/api/v1/notebooklm/summary", "/api/v1/models/usage",
    "/api/v1/models/catalog", "/api/v1/agent-platform/gateway", "/api/v1/agent-platform/registry",
    "/api/v1/agent-platform/deployments", "/api/v1/model-armor/summary",
    "/api/v1/model-armor/events?verdict=all&filter=sdp", "/api/v1/users", "/api/v1/governance/antigravity/status",
    "/api/v1/governance/antigravity/status?period=weekly&window=rolling", "/api/v1/governance/audit-log",
    "/api/v1/finops/summary", "/api/v1/finops/summary?project=demo-project", "/api/v1/telemetry/discover",
    "/api/v1/telemetry/connectors", "/api/v1/telemetry/connectors/data?id=conn_0000abcd",
    "/api/v1/telemetry/connectors/data?id=conn_0000beef", "/api/v1/health",
]


class AnyRow(dict):
    """A result row that returns a plausible value for any column name."""

    def get(self, key, default=None):
        k = key.lower()
        if k == "period":
            return "cur"
        if k in ("bucket", "day"):
            return dt.date(2026, 10, 19)
        if k in ("ts", "last", "last_active", "last_seen", "last_call", "last_used", "last_blocked"):
            return dt.datetime(2026, 10, 19, 3, 0, tzinfo=dt.timezone.utc)
        if k in ("email", "principal", "user", "k"):
            return "alice@example.com"
        if k in ("table_id", "table_name"):
            return "cloudaudit_googleapis_com_activity"
        if k in ("blocked", "is_prompt", "is_upload") or k.startswith("f_"):
            return True
        values = {"kind": "operation", "outcome": "success", "currency": "USD", "model": "gemini-3.8-flash",
                  "sku": "Gemini 3.8 Flash Text Input Tokens", "service": "aiplatform.googleapis.com",
                  "engine": "123", "agent": "123", "agent_id": "123", "last_modified_time": 1760842800000,
                  "unit": "count", "pricing_unit": "count", "project": PROJECT, "account": "ABCDEF-ABCDEF-ABCDEF",
                  "file_name": "plan.pdf", "mime": "application/pdf"}
        if k in values:
            return values[k]
        if k in NUMERIC or k.endswith(("_count", "_tokens", "_calls")):
            return 2
        return "x"

    def __getitem__(self, key):
        return self.get(key)

    def __contains__(self, key):
        return True


@unittest.skipUnless(HAVE_BIGQUERY, "google-cloud-bigquery is not installed")
class OfflineEndpointsTest(unittest.TestCase):
    mode = "empty"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = os.path.join(cls.tmp.name, "config.yaml")
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write(CONFIG)
        with mock.patch.dict(os.environ, {"CONFIG_PATH": cfg}):
            cls.app = app_module.App(environ={"CONFIG_PATH": cfg})
        cls.param_failures = []
        cls.queries = []
        cls.app.registry.tables = cls.fake_tables
        cls.app.registry.schema = cls.fake_schema
        cls.app.gcp.query = cls.fake_query
        cls.app.gcp.request = cls.fake_request
        cls.server = app_module.make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    @staticmethod
    def fake_tables(project, dataset, quota_project, max_bytes):
        if dataset == "ge_analytics":
            return schema_mod.group_tables([(t, "BASE TABLE") for t in TABLES])
        if dataset == "billing_detailed_usage":
            return schema_mod.group_tables([(BILLING_TABLE, "BASE TABLE")])
        raise GcpError("not_found", status=404)

    @staticmethod
    def fake_schema(project, dataset, table, quota_project):
        if table == BILLING_TABLE:
            return {"schema": schema_mod.TableSchema.from_dicts(BILLING, ingestion_partitioned=True), "rows": 1,
                    "bytes": 1, "modified": None}
        if table not in TABLES:
            raise GcpError("not_found", status=404)
        return {"schema": schema_mod.TableSchema.from_dicts(TABLES[table]), "rows": 1, "bytes": 1, "modified": None}

    @classmethod
    def fake_query(cls, sql, params=None, **kwargs):
        cls.queries.append(sql)
        try:
            Gcp.build_params(sql, params)
        except ValueError as err:
            cls.param_failures.append((kwargs.get("module"), str(err)))
            raise GcpError("invalid", message=str(err))
        rows = [] if cls.mode == "empty" else [AnyRow(), AnyRow()]
        return QueryResult(rows, [], 0, len(rows))

    @classmethod
    def fake_request(cls, method, url, **kwargs):
        if url.endswith(":getIamPolicy"):
            return {"version": 3, "etag": "BwX1", "bindings": [
                {"role": "roles/discoveryengine.agentspaceUser", "members": ["user:alice@example.com"]}]}, {}
        if "getAncestry" in url:
            return {"ancestor": [{"resourceId": {"type": "project", "id": PROJECT}}]}, {}
        if "entries:list" in url and cls.mode == "rows":
            return {"entries": [{"timestamp": "2026-10-19T01:00:00Z",
                                 "labels": {"user_id": "alice@example.com", "client_name": "antigravity_cli",
                                            "model_id": "gemini-3.8-pro"},
                                 "jsonPayload": {"metadata": {"totalTokenCount": 1200, "promptTokenCount": 1000,
                                                              "candidatesTokenCount": 200}}}]}, {}
        return {}, {}

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read() or b"{}")
        finally:
            conn.close()

    def run_all(self):
        results = {}
        for path in ENDPOINTS:
            status, payload = self.get(path)
            self.assertEqual(status, 200, "%s -> %s" % (path, payload))
            codes = {w["code"] for w in payload["meta"]["warnings"]}
            self.assertFalse(codes & {"internal_error", "query_failed"}, "%s -> %s" % (path, payload["meta"]))
            results[path] = payload["data"]
        self.assertEqual(self.param_failures, [])
        self.assertGreater(len(self.queries), 50)
        return results

    def test_all_endpoints(self):
        results = self.run_all()
        discovered = results["/api/v1/telemetry/discover"]
        self.assertEqual(discovered["activityDays"], 7)
        for tb in discovered["tables"]:
            self.assertIn(tb["catalog"]["category"], ("ai", "security", "operations", "other"))
            self.assertEqual(tb["quickAdd"]["bigquery"], {"project": PROJECT, "dataset": "ge_analytics",
                                                          "table": tb["logical"]})
            self.assertEqual(tb["core"], bool(tb["catalog"]["builtIn"]))
        codemender = next(tb for tb in discovered["tables"] if tb["logical"] == "codemender_googleapis_com_events")
        self.assertEqual(codemender["connectorId"], "conn_0000abcd")
        connectors = {c["id"]: c for c in results["/api/v1/telemetry/connectors"]["connectors"]}
        self.assertEqual(connectors["conn_0000abcd"]["catalog"]["id"], "codemender_googleapis_com_events")
        self.assertIsNone(connectors["conn_0000beef"]["catalog"])  # custom filter
        for path in ("/api/v1/telemetry/connectors/data?id=conn_0000abcd", "/api/v1/telemetry/connectors/data?id=conn_0000beef"):
            self.assertIn("topStatuses", results[path])
        if self.mode == "empty":
            # Sources succeeded without rows: lists are empty (not null) and counters are zero.
            self.assertEqual(results["/api/v1/agent-platform/registry"]["agents"], [])
            self.assertEqual(results["/api/v1/agent-platform/registry"]["kpis"]["total"], 0)
            self.assertEqual(results["/api/v1/model-armor/summary"]["byApp"], [])
            self.assertEqual(results["/api/v1/gemini-enterprise/summary"]["kpis"]["activeUsers"]["value"], 0)
            self.assertEqual(results["/api/v1/finops/summary"]["kpis"]["gross"], 0)
            self.assertIsNotNone(results["/api/v1/models/usage"]["tokenTrend"])
            self.assertEqual(len(results["/api/v1/telemetry/discover"]["tables"]), len(
                schema_mod.group_tables([(t, "BASE TABLE") for t in TABLES])))
        else:
            users = results["/api/v1/users"]["users"]
            self.assertTrue(users)
            status, payload = self.get("/api/v1/users/detail?uid=%s" % users[0]["uid"])
            self.assertEqual(status, 200, payload)
            self.assertFalse({w["code"] for w in payload["meta"]["warnings"]} & {"internal_error"})


class OfflineEndpointsWithRowsTest(OfflineEndpointsTest):
    mode = "rows"


if __name__ == "__main__":
    unittest.main()
