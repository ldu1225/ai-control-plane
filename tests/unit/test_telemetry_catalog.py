"""Telemetry source catalog: card text, one-click connector bodies and BigQuery path resolution."""

import datetime as dt
import os
import re
import unittest
from unittest import mock

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane import schema as schema_mod
from controlplane import timeutil
from controlplane.services import telemetry
from controlplane.services import telemetry_catalog as catalog

KO_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "src", "web", "assets", "js", "i18n", "ko.js")
KEY_RE = re.compile(r"^\s*'((?:[^'\\]|\\.)*)'\s*:", re.M)


def rec(name, *fields, mode="NULLABLE"):
    return {"name": name, "type": "RECORD", "mode": mode, "fields": list(fields)}


def col(name, ftype="STRING", mode="NULLABLE"):
    return {"name": name, "type": ftype, "mode": mode}


COMMON = [col("logName"), col("severity"), col("timestamp", "TIMESTAMP"), col("textPayload"),
          rec("resource", col("type"), rec("labels", col("service_name"), col("method"), col("reasoning_engine_id")))]
AUDIT = schema_mod.TableSchema.from_dicts(COMMON + [rec(
    "protopayload_auditlog", col("methodName"), col("serviceName"), rec("authenticationInfo", col("principalEmail")),
    rec("status", col("code", "INTEGER"), col("message")))])
ERRORS = schema_mod.TableSchema.from_dicts(COMMON + [rec(
    "jsonpayload_logging_errorlog", col("message"), rec("status", col("code", "INTEGER"), col("message")))])
CONNECTOR_LOGS = schema_mod.TableSchema.from_dicts(COMMON + [rec(
    "jsonPayload", rec("connectorrunpayload", col("dataconnector"), col("entity")),
    col("certificates", mode="REPEATED"))])
JSON_TABLE = schema_mod.TableSchema.from_dicts(COMMON + [col("jsonPayloadJson", "JSON")])
PLAIN = schema_mod.TableSchema.from_dicts([col("event_time", "TIMESTAMP"), col("user_email"), col("action")])


def ko_keys():
    with open(KO_PATH, encoding="utf-8") as fh:
        text = fh.read()
    return {m.group(1).replace("\\'", "'").replace("\\\\", "\\") for m in KEY_RE.finditer(text)}


class CatalogEntriesTest(unittest.TestCase):
    def test_entries_are_complete_and_consistent(self):
        for sid, entry in catalog.ENTRIES.items():
            self.assertRegex(sid, r"^[a-z0-9_]+$", sid)
            self.assertIn(entry["category"], catalog.CATEGORIES, sid)
            self.assertTrue(entry["title"] and entry["what"].endswith("."), sid)
            self.assertLessEqual(len(entry["title"]), 80, sid)
            for key in catalog.LABELED:
                has_path = bool(entry["paths"][key])
                self.assertEqual(has_path, bool(entry["labels"][key]), "%s %s" % (sid, key))
            for key in catalog.FIELDS:
                for source in ("logging", "bigquery"):
                    path = catalog._source_path(entry["paths"][key], source)
                    if path:
                        self.assertTrue(telemetry._valid_json_path(path), "%s %s %s" % (sid, key, path))

    def test_every_card_text_has_a_korean_translation(self):
        keys = ko_keys()
        texts = []
        for entry in catalog.ENTRIES.values():
            texts += [entry["title"], entry["what"]] + [v for v in entry["labels"].values() if v]
        texts += list(catalog.GENERIC_LABELS.values()) + list(catalog.FALLBACK_LABELS.values())
        for sid, short, dataset in (("x_googleapis_com_y", None, "d"), ("run_googleapis_com_jobs", "run.googleapis.com/jobs",
                                                                        None), ("custom", "custom", None)):
            texts.append(catalog.describe(sid, short=short, dataset=dataset)["what"])
        missing = sorted({t for t in texts if t and t not in keys})
        self.assertEqual(missing, [])

    def test_built_in_pages_cover_the_core_tables(self):
        from controlplane.services.base import CORE_TABLES
        for table in CORE_TABLES:
            self.assertIn(table, catalog.BUILT_IN)
            self.assertIsNotNone(catalog.lookup(table), table)

    def test_generic_descriptions(self):
        card = catalog.describe("run_googleapis_com_jobs", short="run.googleapis.com/jobs")
        self.assertFalse(card["known"])
        self.assertEqual(card["category"], catalog.OPERATIONS)
        self.assertEqual(card["titleParams"], {"product": "Cloud Run", "log": "jobs"})
        self.assertEqual(catalog.title_text(card), "Cloud Run · jobs")
        self.assertEqual((card["operationLabel"], card["statusLabel"]), ("Resource type", "Severity"))
        custom = catalog.describe("codemender", short="codemender")
        self.assertEqual((custom["category"], catalog.title_text(custom)), (catalog.OTHER, "codemender"))
        table = catalog.describe("codemender_googleapis_com_events", dataset="telemetry")
        self.assertEqual(catalog.title_text(table), "Codemender Events")
        self.assertEqual(table["whatParams"], {"dataset": "telemetry"})
        alias = catalog.describe("run_googleapis_com__var_log_system", short="run.googleapis.com//var/log/system")
        self.assertEqual(alias["title"], "Cloud Run system events")


class ConnectorBodiesTest(unittest.TestCase):
    def test_logging_connector(self):
        card = catalog.describe("run_googleapis_com_requests", short="run.googleapis.com/requests")
        body = catalog.logging_connector(card, "projects/demo-project/logs/run.googleapis.com%2Frequests")
        self.assertEqual(body["logging"], {"filter": 'logName="projects/demo-project/logs/run.googleapis.com%2Frequests"'})
        self.assertEqual(body["mapping"]["time"], "timestamp")
        self.assertEqual(body["mapping"]["operation"], "resource.labels.service_name")
        self.assertEqual(body["mapping"]["status"], "httpRequest.status")
        self.assertEqual(body["name"], "Cloud Run requests")
        for bad in ('projects/p/logs/a"b', "projects/p/logs/", "other/p/logs/x", None):
            self.assertIsNone(catalog.logging_connector(card, bad), bad)
        unknown = catalog.logging_connector(catalog.describe("codemender", short="codemender"), "projects/p/logs/codemender")
        self.assertEqual((unknown["mapping"]["operation"], unknown["mapping"]["status"]), ("resource.type", "severity"))
        # OpenTelemetry keys with dots cannot be read from Cloud Logging; they stay unmapped there.
        otel = catalog.logging_connector(catalog.describe("gen_ai_client_inference_operation_details"),
                                         "projects/p/logs/gen_ai.client.inference.operation.details")
        self.assertIsNone(otel["mapping"]["tokens"])

    def test_bigquery_path_resolution(self):
        self.assertEqual(catalog.resolve_bigquery_path(AUDIT, "protoPayload.methodName"),
                         "protopayload_auditlog.methodName")
        self.assertEqual(catalog.resolve_bigquery_path(AUDIT, "protoPayload.authenticationInfo.principalEmail"),
                         "protopayload_auditlog.authenticationInfo.principalEmail")
        self.assertEqual(catalog.resolve_bigquery_path(ERRORS, "jsonPayload.status.code"),
                         "jsonpayload_logging_errorlog.status.code")
        self.assertEqual(catalog.resolve_bigquery_path(CONNECTOR_LOGS, "jsonPayload.connectorRunPayload.dataConnector"),
                         "jsonPayload.connectorrunpayload.dataconnector")
        self.assertEqual(catalog.resolve_bigquery_path(CONNECTOR_LOGS, "jsonPayload.certificates[0]"),
                         "jsonPayload.certificates")
        self.assertEqual(catalog.resolve_bigquery_path(JSON_TABLE, "jsonPayload.principalJobTitle"),
                         "jsonPayloadJson.principalJobTitle")
        self.assertIsNone(catalog.resolve_bigquery_path(JSON_TABLE, "jsonPayload.reason[0].type"))
        self.assertIsNone(catalog.resolve_bigquery_path(AUDIT, "jsonPayload.missing"))
        self.assertIsNone(catalog.resolve_bigquery_path(AUDIT, None))

    def test_bigquery_connector_known_source(self):
        card = catalog.describe("discoveryengine_googleapis_com_api_errors", dataset="ge_analytics")
        body, labeled = catalog.bigquery_connector(card, "demo-project", "ge_analytics",
                                                   "discoveryengine_googleapis_com_api_errors", ERRORS,
                                                   {"time": "timestamp", "status": "severity"})
        self.assertEqual(body["bigquery"], {"project": "demo-project", "dataset": "ge_analytics",
                                            "table": "discoveryengine_googleapis_com_api_errors"})
        self.assertEqual(body["mapping"]["operation"], "resource.labels.method")
        self.assertEqual(body["mapping"]["status"], "jsonpayload_logging_errorlog.status.code")
        self.assertEqual(body["mapping"]["time"], "timestamp")
        self.assertEqual((labeled["operationLabel"], labeled["statusLabel"]), ("API method", "Error code"))
        # A catalog field missing from the table falls back to the suggestion with a generic label.
        card = catalog.describe("cloudaudit_googleapis_com_access_transparency")
        body, labeled = catalog.bigquery_connector(card, "p", "d", "cloudaudit_googleapis_com_access_transparency",
                                                   JSON_TABLE, {"time": "timestamp", "status": "severity"})
        self.assertIsNone(body["mapping"]["operation"])
        self.assertIsNone(labeled["operationLabel"])
        self.assertEqual(body["mapping"]["status"], "jsonPayloadJson.principalJobTitle")
        self.assertEqual(labeled["statusLabel"], "Accessor role")

    def test_bigquery_connector_unknown_table(self):
        card = catalog.describe("audit_events", dataset="d")
        body, labeled = catalog.bigquery_connector(card, "p", "d", "audit_events", PLAIN,
                                                   {"time": "event_time", "user": "user_email", "operation": "action"})
        self.assertEqual(body["mapping"], {"time": "event_time", "operation": "action", "status": None,
                                           "user": "user_email", "model": None, "tokens": None})
        self.assertEqual((labeled["operationLabel"], labeled["statusLabel"]), ("Operation", None))
        # An unknown exported log falls back to resource type and severity.
        card = catalog.describe("codemender_events", dataset="d")
        body, labeled = catalog.bigquery_connector(card, "p", "d", "codemender_events", AUDIT, {"time": "timestamp"})
        self.assertEqual((body["mapping"]["operation"], body["mapping"]["status"]), ("resource.type", "severity"))
        self.assertEqual((labeled["operationLabel"], labeled["statusLabel"]), ("Resource type", "Severity"))
        self.assertEqual(catalog.bigquery_connector(card, "p", "d", "x", None, {}), (None, card))


class ConnectorCardsTest(unittest.TestCase):
    def test_source_ids(self):
        log = {"source": "logging", "logging": {"filter": 'logName="projects/p/logs/run.googleapis.com%2Frequests"'}}
        self.assertEqual(catalog.connector_source_id(log), "run_googleapis_com_requests")
        self.assertIsNone(catalog.connector_source_id({"source": "logging", "logging": {"filter": 'logName:"run"'}}))
        table = {"source": "bigquery", "bigquery": {"table": "CloudAudit_googleapis_com_activity_*"}}
        self.assertEqual(catalog.connector_source_id(table), "cloudaudit_googleapis_com_activity")

    def test_labels_follow_the_connector_mapping(self):
        conn = {"source": "bigquery", "bigquery": {"project": "p", "dataset": "d", "table": "cloudaudit_googleapis_com_policy"},
                "mapping": {"operation": "protopayload_auditlog.methodName", "status": "severity", "tokens": None}}
        card = catalog.for_connector(conn)
        self.assertEqual(card["title"], "Policy denied audit logs")
        self.assertEqual(card["operationLabel"], "API method")
        self.assertEqual(card["statusLabel"], "Status")  # edited away from the catalog field
        self.assertIsNone(card["tokensLabel"])
        log = {"source": "logging", "logging": {"filter": 'logName="projects/p/logs/my-app"'},
               "mapping": {"operation": "resource.type", "status": "jsonPayload.level"}}
        card = catalog.for_connector(log)
        self.assertEqual((card["operationLabel"], card["statusLabel"]), ("Resource type", "Status"))
        self.assertIsNone(catalog.for_connector({"source": "logging", "logging": {"filter": "severity>=ERROR"}}))

    def test_quick_add_bodies_pass_server_validation(self):
        class Ctx:
            pass
        ctx = Ctx()
        card = catalog.describe("cloudscheduler_googleapis_com_executions", short="cloudscheduler.googleapis.com/executions")
        body = catalog.logging_connector(card, "projects/p/logs/cloudscheduler.googleapis.com%2Fexecutions")
        clean = telemetry.validate_connector(ctx, body)
        self.assertEqual(clean["mapping"]["operation"], "resource.labels.job_id")
        self.assertEqual(clean["logging"]["filter"], body["logging"]["filter"])
        cert = catalog.logging_connector(catalog.describe("certificatemanager_googleapis_com_certificates_expiry"),
                                         "projects/p/logs/certificatemanager.googleapis.com%2Fcertificates_expiry")
        self.assertEqual(telemetry.validate_connector(ctx, cert)["mapping"]["operation"], "jsonPayload.certificates[0]")


class LogMessageTest(unittest.TestCase):
    def test_message_extraction(self):
        self.assertEqual(telemetry._log_message({"textPayload": "  boot ok \n"}), "boot ok")
        self.assertEqual(telemetry._log_message({"jsonPayload": {"message": "failed"}}), "failed")
        self.assertEqual(telemetry._log_message({"protoPayload": {"status": {"message": "denied"}}}), "denied")
        self.assertIsNone(telemetry._log_message({"jsonPayload": {"content": "secret"}}))
        self.assertEqual(len(telemetry._log_message({"textPayload": "x" * 1000})), telemetry.MESSAGE_CHARS)

    def test_message_expression(self):
        self.assertIn("jsonpayload_logging_errorlog", telemetry._message_expr(ERRORS))
        self.assertIn("textPayload", telemetry._message_expr(AUDIT))
        self.assertEqual(telemetry._message_expr(PLAIN), "CAST(NULL AS STRING)")


class FakeLoggingCtx:
    """Minimal request context for _logging_data: serves pre-built entries:list pages."""
    project = "demo-project"
    tz_name = "UTC"

    def __init__(self, pages):
        self.pages = list(pages)
        self.codes = []

    def rest(self, method, url, json_body=None, timeout=None):
        return self.pages.pop(0)

    def add_source(self, label):
        pass

    def warn(self, source, code, message):
        self.codes.append(code)

    def warn_error(self, source, err, what):
        raise AssertionError(what)


class LogTruncationTest(unittest.TestCase):
    RANGE = timeutil.DateRange(dt.date(2026, 10, 1), dt.date(2026, 10, 10), "day", "UTC")
    CONN = {"name": "Cloud Run requests", "logging": {"filter": 'logName="projects/p/logs/x"'},
            "mapping": {"time": "timestamp", "status": "httpRequest.status"}}

    @staticmethod
    def entry(day, status=200):
        return {"timestamp": "2026-10-%02dT12:00:00Z" % day, "httpRequest": {"status": status}}

    def run_read(self, entries, token):
        ctx = FakeLoggingCtx([{"entries": entries, "nextPageToken": token}])
        out = telemetry._empty_data({}, self.RANGE)
        with mock.patch.object(telemetry, "MAX_LOG_ENTRIES", 3):
            telemetry._logging_data(ctx, self.CONN, self.RANGE, out)
        return ctx, out

    def test_cut_inside_the_period_is_unknown_not_zero(self):
        ctx, out = self.run_read([self.entry(10), self.entry(9, 500), self.entry(8)], "more")
        self.assertIn("truncated", ctx.codes)
        self.assertEqual(out["sample"], {"limit": 3, "truncated": True, "partial": True,
                                         "since": "2026-10-08T12:00:00Z"})
        values = out["trend"]["series"][0]["values"]
        self.assertEqual(values[:8], [None] * 8)  # up to and including the partly read day
        self.assertEqual(values[8:], [1, 1])
        self.assertEqual(out["kpis"]["events"], {"value": 3, "previous": None})
        self.assertEqual(out["topStatuses"], [{"status": "200", "events": 2}, {"status": "500", "events": 1}])

    def test_cut_in_the_previous_period_keeps_the_period_complete(self):
        entries = [self.entry(10), self.entry(2), {"timestamp": "2026-09-25T00:00:00Z"}]
        _, out = self.run_read(entries, "more")
        self.assertEqual((out["sample"]["truncated"], out["sample"]["partial"]), (True, False))
        values = out["trend"]["series"][0]["values"]
        self.assertNotIn(None, values)
        self.assertEqual(sum(values), 2)

    def test_complete_read(self):
        _, out = self.run_read([self.entry(10)], None)
        self.assertEqual((out["sample"]["truncated"], out["sample"]["partial"]), (False, False))


class ValueNamesTest(unittest.TestCase):
    def test_agent_engine_ids_get_display_names(self):
        conn = {"source": "logging",
                "logging": {"filter": 'logName="projects/p/logs/aiplatform.googleapis.com%2Freasoning_engine_stderr"'}}
        out = {"topOperations": [{"operation": "5289064895730417664", "events": 3}, {"operation": "123", "events": 1}],
               "recent": [{"operation": "projects/p/locations/us-central1/reasoningEngines/777777777"}]}
        names = {"5289064895730417664": "support-agent", "777777777": "triage-agent"}
        with mock.patch.object(telemetry, "_engine_names", return_value=names):
            self.assertEqual(telemetry._value_names(None, conn, out), {"operation": {
                "5289064895730417664": "support-agent",
                "projects/p/locations/us-central1/reasoningEngines/777777777": "triage-agent"}})
            other = {"source": "logging", "logging": {"filter": 'logName="projects/p/logs/run.googleapis.com%2Frequests"'}}
            self.assertIsNone(telemetry._value_names(None, other, out))
        with mock.patch.object(telemetry, "_engine_names", return_value={}):
            self.assertIsNone(telemetry._value_names(None, conn, out))


class IdleDatasetDiscoveryTest(unittest.TestCase):
    def test_table_without_timestamp_column_has_no_quick_add(self):
        static_schema = schema_mod.TableSchema.from_dicts([col("customer_id"), col("plan")])
        card = catalog.describe("customers", dataset="crm")
        self.assertEqual(catalog.bigquery_connector(card, "demo-project", "crm", "customers", static_schema, {}),
                         (None, card))

    def test_live_gcp_auto_discovers_idle_datasets_except_billing(self):
        Gcp = type("Gcp", (), {})

        class Ctx:
            gcp = Gcp()
            project = "demo-project"
            audit_dataset = "ge_analytics"
            selected_project = "all"
            settings = {"workspace": {"project_id": "demo-project", "projects": [],
                                      "billing": {"project_id": "", "dataset": "billing_detailed_usage", "table": "t"}}}
            ws = settings["workspace"]

            def rest(self, _method, _url, **_kw):
                return {"datasets": [
                    {"datasetReference": {"datasetId": "billing_detailed_usage"}},
                    {"datasetReference": {"datasetId": "idle_marketing"}},
                    {"datasetReference": {"datasetId": "custom_agent_logs"}},
                ]}

        found = telemetry.discover_datasets(Ctx())
        self.assertEqual(found, [
            ("demo-project", "ge_analytics"),
            ("demo-project", "custom_agent_logs"),
            ("demo-project", "idle_marketing"),
        ])

    def test_ai_enrich_sources_clusters_and_validates_schema_fields(self):
        rag_schema = schema_mod.TableSchema.from_dicts([
            col("event_ts", "TIMESTAMP"),
            col("actor_email", "STRING"),
            col("pipeline_step", "STRING"),
            col("llm_model", "STRING"),
            col("token_usage", "INTEGER"),
            col("outcome", "STRING"),
            col("summary_text", "STRING"),
        ])
        mapping = telemetry.suggested_mapping(rag_schema.leaf_types(50))
        base_card = catalog.describe("rag_pipeline_events", dataset="agent_ops_archive", built_in=[])
        quick, card = catalog.bigquery_connector(
            base_card, "demo-project", "agent_ops_archive", "rag_pipeline_events", rag_schema, mapping
        )
        tables = [{
            "id": "rag_pipeline_events",
            "project": "demo-project",
            "dataset": "agent_ops_archive",
            "table": "rag_pipeline_events",
            "logical": "rag_pipeline_events",
            "core": False,
            "registered": False,
            "connectorId": None,
            "suggestedMapping": mapping,
            "columns": rag_schema.leaf_paths(50),
            "catalog": card,
            "quickAdd": quick,
        }]

        class MockAi:
            def effective_model(self, _settings, preferred):
                return preferred

            def generate(self, _settings, model, _prompt, **kwargs):
                assert model == "gemini-4-argon"
                assert kwargs.get("module") == "telemetry_ai"
                return {
                    "sources": [{
                        "key": "bq:demo-project.agent_ops_archive.rag_pipeline_events",
                        "category": "ai",
                        "group": "RAG Pipeline Telemetry",
                        "groupKo": "RAG 파이프라인 텔레메트리",
                        "title": "RAG Pipeline Execution Events",
                        "titleKo": "RAG 파이프라인 실행 이벤트",
                        "what": "Tracks custom RAG retrieval and generation steps with token usage.",
                        "whatKo": "커스텀 RAG 검색·생성 단계와 토큰 사용량을 추적합니다.",
                        "sensitive": False,
                        "time": "nonexistent_ts",  # hallucinated -> must keep verified event_ts
                        "operation": "pipeline_step",
                        "operationLabel": "Pipeline step",
                        "operationLabelKo": "파이프라인 단계",
                        "status": "outcome",
                        "statusLabel": "Outcome",
                        "statusLabelKo": "처리 결과",
                        "user": "actor_email",
                        "model": "llm_model",
                        "tokens": "summary_text",  # STRING column -> must reject and keep numeric token_usage
                        "tokensLabel": "Tokens",
                        "tokensLabelKo": "토큰 수",
                    }]
                }

        class App:
            ai = MockAi()
            cache = None

        class FakeRegistry:
            @staticmethod
            def source(_p, _d, _t, _q, _m):
                return type("Src", (), {"schema": rag_schema, "time_column": lambda self: "event_ts"})()

        class Ctx:
            app = App()
            gcp = object()
            project = "demo-project"
            quota_project = "demo-project"
            max_bytes = 0
            registry = FakeRegistry()
            settings = {}

        info = telemetry._ai_enrich_sources(
            Ctx(), tables, [], {("demo-project", "agent_ops_archive", "rag_pipeline_events"): rag_schema}, {}
        )
        self.assertEqual(info, {"enabled": True, "model": "gemini-4-argon"})
        enriched_card = tables[0]["catalog"]
        self.assertTrue(enriched_card["aiProfiled"])
        self.assertEqual(enriched_card["group"], "RAG Pipeline Telemetry")
        self.assertEqual(enriched_card["groupKo"], "RAG 파이프라인 텔레메트리")
        self.assertEqual(enriched_card["titleKo"], "RAG 파이프라인 실행 이벤트")
        self.assertEqual(enriched_card["operationLabelKo"], "파이프라인 단계")
        q = tables[0]["quickAdd"]
        self.assertEqual(q["mapping"]["time"], "event_ts")
        self.assertEqual(q["mapping"]["operation"], "pipeline_step")
        self.assertEqual(q["mapping"]["status"], "outcome")
        self.assertEqual(q["mapping"]["user"], "actor_email")
        self.assertEqual(q["mapping"]["model"], "llm_model")
        self.assertEqual(q["mapping"]["tokens"], "token_usage")
        # Round-trip connector validation preserves AI meta and restores localized catalog fields.
        saved = telemetry.validate_connector(Ctx(), q)
        self.assertIn("meta", saved)
        restored = telemetry._card_with_meta(catalog.for_connector(saved), saved.get("meta"), saved)
        self.assertEqual(restored["titleKo"], "RAG 파이프라인 실행 이벤트")
        self.assertEqual(restored["groupKo"], "RAG 파이프라인 텔레메트리")
        self.assertEqual(restored["operationLabelKo"], "파이프라인 단계")
        # Manual custom filter without meta keeps catalog=None.
        manual = {"source": "logging", "logging": {"filter": 'severity>="ERROR"'}, "mapping": {"time": "timestamp"}}
        self.assertIsNone(telemetry._card_with_meta(catalog.for_connector(manual), None, manual))

    def test_sample_logs_and_workloads_discovers_structured_json_service(self):
        Gcp = type("Gcp", (), {})
        now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        class Ctx:
            gcp = Gcp()
            project = "demo-project"
            projects = ["demo-project"]
            tz_name = "UTC"

            def rest(self, _method, _url, **_kw):
                return {"entries": [
                    {
                        "timestamp": now_iso,
                        "logName": "projects/demo-project/logs/run.googleapis.com%2Fstdout",
                        "severity": "INFO",
                        "resource": {"type": "cloud_run_revision", "labels": {"service_name": "rag-orchestrator"}},
                        "jsonPayload": {
                            "step_name": "rerank",
                            "actor_email": "alice@example.org",
                            "model_id": "gemini-2.5-flash",
                            "total_tokens": 512,
                            "status": "OK",
                        },
                    },
                    {
                        "timestamp": now_iso,
                        "logName": "projects/demo-project/logs/run.googleapis.com%2Fstdout",
                        "severity": "INFO",
                        "resource": {"type": "cloud_run_revision", "labels": {"service_name": "rag-orchestrator"}},
                        "jsonPayload": {
                            "step_name": "generate",
                            "actor_email": "bob@example.org",
                            "model_id": "gemini-2.5-flash",
                            "total_tokens": 920,
                            "status": "OK",
                        },
                    },
                ]}

        streams, _counts, _leaves = telemetry._sample_logs_and_workloads(Ctx())
        self.assertEqual(len(streams), 1)
        st = streams[0]
        self.assertIn('resource.labels.service_name="rag-orchestrator"', st["quickAdd"]["logging"]["filter"])
        self.assertEqual(st["quickAdd"]["mapping"]["tokens"], "jsonPayload.total_tokens")
        self.assertEqual(st["quickAdd"]["mapping"]["user"], "jsonPayload.actor_email")
        self.assertEqual(st["quickAdd"]["mapping"]["model"], "jsonPayload.model_id")

    def test_sample_missing_custom_logs_populates_leaves_when_drowned_out(self):
        Gcp = type("Gcp", (), {})

        class Ctx:
            gcp = Gcp()
            project = "demo-project"

            def rest(self, _method, _url, **_kw):
                return {"entries": [{
                    "timestamp": "2026-10-19T12:00:00Z",
                    "jsonPayload": {"step_name": "retrieve", "token_usage": 530, "actor_email": "a@example.org"},
                }]}

            def run(self, tasks):
                return {k: (True, fn()) for k, fn in tasks.items()}

        leaves = {}
        telemetry._sample_missing_custom_logs(
            Ctx(),
            [{"shortName": "custom_rag_worker", "logName": "projects/demo-project/logs/custom_rag_worker", "project": "demo-project"}],
            leaves,
        )
        self.assertEqual(leaves["custom_rag_worker"]["jsonPayload.token_usage"], "NUMBER")

    def test_users_long_ctes_aliases_metric_and_value_without_ge_tables(self):
        from controlplane.services import users as users_svc
        from controlplane.services.base import AUDIT_DATA_ACCESS
        da_schema = schema_mod.TableSchema.from_dicts(COMMON + [rec(
            "protopayload_auditlog", col("methodName"), col("serviceName"), col("resourceName"),
            rec("authenticationInfo", col("principalEmail")),
            rec("status", col("code", "INTEGER"), col("message")))])

        class FakeSrc:
            schema = da_schema
            label = "bigquery:ge_analytics.cloudaudit_googleapis_com_data_access"

            @staticmethod
            def time_column():
                return "timestamp"

            @staticmethod
            def cte(alias, start="start_ts"):
                return "%s AS (SELECT * FROM t WHERE timestamp >= @%s)" % (alias, start)

        class Ctx:
            settings = {"governance": {"antigravity": {"usage_source": "auto"}}}

            @staticmethod
            def audit_source(name, optional=False):
                return FakeSrc() if name == AUDIT_DATA_ACCESS else None

        ctes, parts, _labels, _agy, available = users_svc.long_ctes(Ctx())
        self.assertIn("agentPlatformCalls", available)
        sql = users_svc.pivot_sql(ctes, parts)
        self.assertIn("AS metric, 1 AS value, ts AS ts FROM nb", sql)


if __name__ == "__main__":
    unittest.main()
