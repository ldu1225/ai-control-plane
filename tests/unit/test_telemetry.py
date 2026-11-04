"""Telemetry Extensions: dynamic source description (no built-in catalog), Gemini profiling, overrides, group
dashboards, connector bodies and Cloud Logging reads."""

import datetime as dt
import unittest
from unittest import mock

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane import schema as schema_mod
from controlplane import settings_store, timeutil
from controlplane.httpkit import ApiError
from controlplane.services import telemetry
from controlplane.services import telemetry_profile as profile


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

NOW = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def agent_entry(step, email, model="gemini-2.5-flash", tokens=512, status="OK", service="rag-orchestrator",
                log="run.googleapis.com%2Fstdout"):
    return {
        "timestamp": NOW, "logName": "projects/demo-project/logs/%s" % log, "severity": "INFO",
        "resource": {"type": "cloud_run_revision", "labels": {"service_name": service, "project_id": "demo-project",
                                                               "location": "us-central1"}},
        "jsonPayload": {"step_name": step, "actor_email": email, "model_id": model, "total_tokens": tokens,
                        "status": status, "prompt_text": "Summarize the quarterly report for the board " * 2},
    }


AGENT_ENTRIES = [agent_entry(step, "%s@example.org" % user, tokens=100 + i, status="OK" if i % 4 else "ERROR")
                 for i, (step, user) in enumerate([("retrieve", "alice"), ("rerank", "bob"), ("generate", "carol"),
                                                   ("retrieve", "dave"), ("generate", "alice"), ("rerank", "erin"),
                                                   ("retrieve", "bob"), ("generate", "frank")])]


class FieldInferenceTest(unittest.TestCase):
    """Dashboard fields come from value statistics plus configurable name hints, never from a list of known logs."""

    def test_mapping_from_sampled_values(self):
        stats = profile.entry_stats(AGENT_ENTRIES)
        mapping = profile.infer_mapping(stats, support.make_settings(), kind="logging")
        self.assertEqual(mapping["time"], "timestamp")
        self.assertEqual(mapping["user"], "jsonPayload.actor_email")
        self.assertEqual(mapping["tokens"], "jsonPayload.total_tokens")
        self.assertEqual(mapping["model"], "jsonPayload.model_id")
        self.assertEqual(mapping["status"], "jsonPayload.status")
        self.assertEqual(mapping["operation"], "jsonPayload.step_name")

    def test_user_found_by_email_values_without_a_hinted_name(self):
        entries = [{"timestamp": NOW, "jsonPayload": {"who": "%s@example.org" % n, "kind": k}}
                   for n, k in (("a", "x"), ("b", "y"), ("c", "x"), ("d", "y"))]
        mapping = profile.infer_mapping(profile.entry_stats(entries), support.make_settings())
        self.assertEqual(mapping["user"], "jsonPayload.who")

    def test_hints_are_settings(self):
        entries = [{"timestamp": NOW, "jsonPayload": {"zz_count": 10 + i, "kind": "a" if i % 2 else "b"}}
                   for i in range(6)]
        stats = profile.entry_stats(entries)
        self.assertIsNone(profile.infer_mapping(stats, support.make_settings())["tokens"])
        custom = support.make_settings(telemetry={"inference": dict(support.make_settings()["telemetry"]["inference"],
                                                                     token_hints=["zz_count"])})
        self.assertEqual(profile.infer_mapping(stats, custom)["tokens"], "jsonPayload.zz_count")

    def test_identifiers_and_free_text_are_not_breakdowns(self):
        entries = [{"timestamp": NOW, "jsonPayload": {"request_id": "%032x" % (i * 7919), "action": "a%d" % (i % 3),
                                                      "message": "free text number %d with many words in it" % i}}
                   for i in range(30)]
        mapping = profile.infer_mapping(profile.entry_stats(entries), support.make_settings())
        self.assertEqual(mapping["operation"], "jsonPayload.action")

    def test_schema_only_mapping_for_tables(self):
        rag = schema_mod.TableSchema.from_dicts([
            col("event_ts", "TIMESTAMP"), col("actor_email"), col("pipeline_step"), col("llm_model"),
            col("token_usage", "INTEGER"), col("outcome"), col("summary_text")])
        mapping = telemetry.suggested_mapping(rag.leaf_types(50), support.make_settings())
        self.assertEqual(mapping["time"], "event_ts")
        self.assertEqual(mapping["user"], "actor_email")
        self.assertEqual(mapping["tokens"], "token_usage")
        self.assertEqual(mapping["model"], "llm_model")
        self.assertEqual(mapping["status"], "outcome")
        self.assertEqual(mapping["operation"], "pipeline_step")

    def test_sensitive_free_text(self):
        settings = support.make_settings()
        self.assertTrue(profile.is_sensitive(profile.entry_stats(AGENT_ENTRIES), settings))
        quiet = [{"timestamp": NOW, "jsonPayload": {"step_name": "x", "status": "OK"}}]
        self.assertFalse(profile.is_sensitive(profile.entry_stats(quiet), settings))

    def test_field_labels(self):
        self.assertEqual(profile.field_label("protoPayload.methodName"), "Method name")
        self.assertEqual(profile.field_label("jsonPayload.step_name"), "Step name")
        self.assertEqual(profile.field_label("jsonPayload.certificates[0]"), "Certificates")
        self.assertIsNone(profile.field_label(None))


class DynamicDescriptionTest(unittest.TestCase):
    TITLES = {"run.googleapis.com": "Cloud Run Admin API", "aiplatform.googleapis.com": "Vertex AI API",
              "cloudscheduler.googleapis.com": "Cloud Scheduler API"}
    DESCRIPTORS = {"cloud_run_revision": {"name": "Cloud Run Revision", "description": "", "labels": ["service_name"]}}

    def test_log_and_table_ids(self):
        self.assertEqual(profile.sanitize_log_name("run.googleapis.com/requests"), "run_googleapis_com_requests")
        self.assertEqual(profile.sanitize_log_name("run.googleapis.com%2Frequests"), "run_googleapis_com_requests")
        self.assertEqual(profile.split_log("run.googleapis.com%2Frequests"), ("run.googleapis.com", "requests"))
        self.assertEqual(profile.split_log("my-app"), (None, "my-app"))
        self.assertEqual(profile.split_table("run_googleapis_com_requests"), ("run.googleapis.com", "requests"))
        self.assertEqual(profile.split_table("audit_events"), (None, "audit_events"))

    def test_product_names_come_from_service_titles_with_brand_renames(self):
        settings = support.make_settings()
        self.assertEqual(profile.service_display(self.TITLES, "aiplatform.googleapis.com", settings), "Agent Platform")
        self.assertEqual(profile.service_display(self.TITLES, "run.googleapis.com", settings), "Cloud Run")
        self.assertEqual(profile.service_display({}, "codemender.googleapis.com", settings), "Codemender")
        custom = support.make_settings(telemetry={"display_renames": ["Cloud Run=Serverless Runtime"]})
        self.assertEqual(profile.service_display(self.TITLES, "run.googleapis.com", custom), "Serverless Runtime")
        # Without the rename the Service Usage title is kept as is.
        plain = support.make_settings(telemetry={"display_renames": []})
        self.assertEqual(profile.service_display(self.TITLES, "aiplatform.googleapis.com", plain), "Vertex AI")

    def test_log_card(self):
        stats = profile.entry_stats(AGENT_ENTRIES)
        mapping = profile.infer_mapping(stats, support.make_settings())
        card = profile.describe("run_googleapis_com_stdout", short="run.googleapis.com/stdout", stats=stats,
                                mapping=mapping, settings=support.make_settings(), titles=self.TITLES,
                                descriptors=self.DESCRIPTORS)
        self.assertEqual(card["title"], "{product} · {log}")
        self.assertEqual(card["titleParams"], {"product": "Cloud Run", "log": "stdout"})
        self.assertEqual(profile.title_text(card), "Cloud Run · stdout")
        self.assertEqual(card["whatParams"]["resources"], "Cloud Run Revision")
        self.assertEqual(card["category"], profile.AI)  # model and token fields
        self.assertTrue(card["sensitive"])
        self.assertEqual(card["operationLabel"], "Step name")
        self.assertEqual(card["tokensLabel"], "Total tokens")
        self.assertEqual(card["product"], "Cloud Run")

    def test_categories_follow_the_data(self):
        settings = support.make_settings()
        audit = profile.entry_stats([{"timestamp": NOW, "protoPayload": {"@type": "type.googleapis.com/google.cloud.audit.AuditLog",
                                                                           "methodName": "x"}}])
        self.assertEqual(profile.classify(audit, "iam.googleapis.com", settings, {}), profile.SECURITY)
        self.assertEqual(profile.classify(profile.new_stats(), "aiplatform.googleapis.com", settings, {}), profile.AI)
        genai = profile.entry_stats([{"timestamp": NOW, "logName": "projects/p/logs/gen_ai.client.inference.operation.details"}])
        self.assertEqual(profile.classify(genai, None, settings, {}), profile.AI)
        ops = profile.entry_stats([{"timestamp": NOW, "resource": {"type": "cloud_run_revision"}}])
        self.assertEqual(profile.classify(ops, None, settings, {}), profile.OPERATIONS)
        self.assertEqual(profile.classify(profile.new_stats(), None, settings, {}), profile.OTHER)
        # The AI services are the configurable gateway.services list.
        custom = support.make_settings(gateway={"services": ["run.googleapis.com"]})
        self.assertEqual(profile.classify(ops, "run.googleapis.com", custom, {}), profile.AI)

    def test_table_cards(self):
        exported = profile.describe("run_googleapis_com_requests", dataset="ge_analytics", settings=support.make_settings(),
                                    titles=self.TITLES)
        self.assertEqual(profile.title_text(exported), "Cloud Run · Requests")
        self.assertEqual(exported["whatParams"], {"product": "Cloud Run", "dataset": "ge_analytics"})
        custom = profile.describe("rag_pipeline_events", dataset="agent_ops")
        self.assertEqual(profile.title_text(custom), "Rag Pipeline Events")
        self.assertEqual(custom["whatParams"], {"dataset": "agent_ops"})
        self.assertEqual(custom["category"], profile.OTHER)


class ConnectorBodiesTest(unittest.TestCase):
    def test_logging_connector(self):
        card = profile.describe("run_googleapis_com_requests", short="run.googleapis.com/requests")
        mapping = {"operation": "resource.labels.service_name", "status": "httpRequest.status"}
        body = profile.logging_connector(card, "projects/demo-project/logs/run.googleapis.com%2Frequests", mapping)
        self.assertEqual(body["logging"], {"filter": 'logName="projects/demo-project/logs/run.googleapis.com%2Frequests"'})
        self.assertEqual(body["mapping"]["time"], "timestamp")
        self.assertEqual(body["mapping"]["operation"], "resource.labels.service_name")
        self.assertEqual(body["mapping"]["status"], "httpRequest.status")
        for bad in ('projects/p/logs/a"b', "projects/p/logs/", "other/p/logs/x", None):
            self.assertIsNone(profile.logging_connector(card, bad, mapping), bad)

    def test_bigquery_path_resolution(self):
        self.assertEqual(profile.resolve_bigquery_path(AUDIT, "protoPayload.methodName"), "protopayload_auditlog.methodName")
        self.assertEqual(profile.resolve_bigquery_path(AUDIT, "protoPayload.authenticationInfo.principalEmail"),
                         "protopayload_auditlog.authenticationInfo.principalEmail")
        self.assertEqual(profile.resolve_bigquery_path(ERRORS, "jsonPayload.status.code"),
                         "jsonpayload_logging_errorlog.status.code")
        self.assertEqual(profile.resolve_bigquery_path(CONNECTOR_LOGS, "jsonPayload.connectorRunPayload.dataConnector"),
                         "jsonPayload.connectorrunpayload.dataconnector")
        self.assertEqual(profile.resolve_bigquery_path(CONNECTOR_LOGS, "jsonPayload.certificates[0]"),
                         "jsonPayload.certificates")
        self.assertEqual(profile.resolve_bigquery_path(JSON_TABLE, "jsonPayload.principalJobTitle"),
                         "jsonPayloadJson.principalJobTitle")
        self.assertIsNone(profile.resolve_bigquery_path(JSON_TABLE, "jsonPayload.reason[0].type"))
        self.assertIsNone(profile.resolve_bigquery_path(AUDIT, "jsonPayload.missing"))
        self.assertIsNone(profile.resolve_bigquery_path(AUDIT, None))

    def test_log_mapping_translates_to_the_exported_table(self):
        mapping = profile.table_mapping_from_log(AUDIT, {"operation": "protoPayload.methodName",
                                                         "user": "protoPayload.authenticationInfo.principalEmail",
                                                         "status": "severity", "model": "jsonPayload.model"})
        self.assertEqual(mapping["time"], "timestamp")
        self.assertEqual(mapping["operation"], "protopayload_auditlog.methodName")
        self.assertEqual(mapping["user"], "protopayload_auditlog.authenticationInfo.principalEmail")
        self.assertEqual(mapping["status"], "severity")
        self.assertIsNone(mapping["model"])

    def test_bigquery_connector(self):
        card = profile.describe("audit_events", dataset="d", mapping={"operation": "action"})
        body, labeled = profile.bigquery_connector(card, "p", "d", "audit_events", PLAIN,
                                                   {"time": "event_time", "user": "user_email", "operation": "action"})
        self.assertEqual(body["mapping"], {"time": "event_time", "operation": "action", "status": None,
                                           "user": "user_email", "model": None, "tokens": None})
        self.assertEqual(body["bigquery"], {"project": "p", "dataset": "d", "table": "audit_events"})
        self.assertEqual(labeled["operationLabel"], "Action")
        static_schema = schema_mod.TableSchema.from_dicts([col("customer_id"), col("plan")])
        self.assertEqual(profile.bigquery_connector(card, "p", "crm", "customers", static_schema, {}), (None, card))
        self.assertEqual(profile.bigquery_connector(card, "p", "d", "x", None, {}), (None, card))

    def test_source_ids_and_connector_cards(self):
        log = {"source": "logging", "logging": {"filter": 'logName="projects/p/logs/run.googleapis.com%2Frequests"'}}
        self.assertEqual(profile.connector_source_id(log), "run_googleapis_com_requests")
        self.assertIsNone(profile.connector_source_id({"source": "logging", "logging": {"filter": 'logName:"run"'}}))
        table = {"source": "bigquery", "bigquery": {"table": "CloudAudit_googleapis_com_activity_*"}}
        self.assertEqual(profile.connector_source_id(table), "cloudaudit_googleapis_com_activity")
        conn = {"source": "logging", "logging": {"filter": 'logName="projects/p/logs/my-app"'},
                "mapping": {"operation": "resource.type", "status": "jsonPayload.level"}}
        card = profile.for_connector(conn, support.make_settings())
        self.assertEqual((card["operationLabel"], card["statusLabel"]), ("Resource type", "Level"))
        self.assertIsNone(profile.for_connector({"source": "logging", "logging": {"filter": "severity>=ERROR"}}))

    def test_quick_add_bodies_pass_server_validation(self):
        ctx = type("Ctx", (), {})()
        card = profile.describe("cloudscheduler_googleapis_com_executions", short="cloudscheduler.googleapis.com/executions")
        body = profile.logging_connector(card, "projects/p/logs/cloudscheduler.googleapis.com%2Fexecutions",
                                         {"operation": "resource.labels.job_id", "status": "severity"})
        clean = telemetry.validate_connector(ctx, body)
        self.assertEqual(clean["mapping"]["operation"], "resource.labels.job_id")
        self.assertEqual(clean["logging"]["filter"], body["logging"]["filter"])
        # Group dashboards break down by source log ("logName" is accepted for Cloud Logging connectors only).
        group = dict(body, mapping={"time": "timestamp", "operation": "logName"})
        self.assertEqual(telemetry.validate_connector(ctx, group)["mapping"]["operation"], "logName")
        with self.assertRaises(ApiError):
            telemetry.validate_connector(ctx, dict(body, logging={"filter": "x" * 4001}))


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

    def __init__(self, pages, max_entries):
        self.pages = list(pages)
        self.codes = []
        self.bodies = []
        self.settings = support.make_settings(telemetry={"discovery": {"max_log_entries": max_entries}})

    def rest(self, method, url, json_body=None, timeout=None):
        self.bodies.append(json_body)
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

    def run_read(self, entries, token, max_entries=3):
        ctx = FakeLoggingCtx([{"entries": entries, "nextPageToken": token}], max_entries)
        out = telemetry._empty_data({}, self.RANGE)
        telemetry._logging_data(ctx, self.CONN, self.RANGE, out)
        return ctx, out

    def test_read_limit_is_the_setting(self):
        ctx, _ = self.run_read([self.entry(10)], None, max_entries=3)
        self.assertEqual(ctx.bodies[0]["pageSize"], 3)
        ctx, _ = self.run_read([self.entry(10)], None, max_entries=1000)
        self.assertEqual(ctx.bodies[0]["pageSize"], 1000)

    def test_cut_inside_the_period_is_unknown_not_zero(self):
        ctx, out = self.run_read([self.entry(10), self.entry(9, 500), self.entry(8)], "more")
        self.assertIn("truncated", ctx.codes)
        self.assertEqual(out["sample"], {"limit": 3, "truncated": True, "partial": True, "since": "2026-10-08T12:00:00Z"})
        values = out["trend"]["series"][0]["values"]
        self.assertEqual(values[:8], [None] * 8)
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

    def test_complete_read_counts_only_the_selected_period(self):
        _, out = self.run_read([self.entry(10), {"timestamp": "2026-09-28T00:00:00Z"}], None)
        self.assertEqual((out["sample"]["truncated"], out["sample"]["partial"]), (False, False))
        self.assertEqual(out["kpis"]["events"], {"value": 1, "previous": 1})


class ValueNamesTest(unittest.TestCase):
    def test_agent_engine_ids_get_display_names_for_any_source(self):
        out = {"topOperations": [{"operation": "5289064895730417664", "events": 3}, {"operation": "123", "events": 1}],
               "recent": [{"operation": "projects/p/locations/us-central1/reasoningEngines/777777777"}]}
        names = {"5289064895730417664": "support-agent", "777777777": "triage-agent"}
        for log in ("aiplatform.googleapis.com%2Freasoning_engine_stderr", "run.googleapis.com%2Frequests"):
            conn = {"source": "logging", "logging": {"filter": 'logName="projects/p/logs/%s"' % log}}
            with mock.patch.object(telemetry, "_engine_names", return_value=names):
                self.assertEqual(telemetry._value_names(None, conn, out), {"operation": {
                    "5289064895730417664": "support-agent",
                    "projects/p/locations/us-central1/reasoningEngines/777777777": "triage-agent"}})
        with mock.patch.object(telemetry, "_engine_names", return_value={}):
            self.assertIsNone(telemetry._value_names(None, {}, out))
        self.assertIsNone(telemetry._value_names(None, {}, {"topOperations": [{"operation": "GET /", "events": 1}]}))


class DatasetDiscoveryTest(unittest.TestCase):
    def test_live_gcp_lists_idle_datasets_except_billing_in_hint_order(self):
        Gcp = type("Gcp", (), {})

        class Ctx:
            gcp = Gcp()
            project = "demo-project"
            audit_dataset = "ge_analytics"
            selected_project = "all"
            settings = support.make_settings(workspace={"billing": {"project_id": "", "dataset": "billing_detailed_usage",
                                                                    "table": "t", "account_id": ""}})
            ws = settings["workspace"]

            def rest(self, _method, _url, **_kw):
                return {"datasets": [{"datasetReference": {"datasetId": d}} for d in
                                     ("billing_detailed_usage", "idle_marketing", "custom_agent_logs")]}

        found = telemetry.discover_datasets(Ctx())
        self.assertEqual(found, [("demo-project", "ge_analytics"), ("demo-project", "telemetry_routed"),
                                 ("demo-project", "custom_agent_logs"), ("demo-project", "idle_marketing")])

    def test_limits_are_settings(self):
        Gcp = type("Gcp", (), {})

        class Ctx:
            gcp = Gcp()
            project = "demo-project"
            audit_dataset = "ge_analytics"
            selected_project = "all"
            settings = support.make_settings(telemetry={"discovery": {"max_datasets_per_project": 0}})
            ws = settings["workspace"]

            def rest(self, _method, _url, **_kw):  # pragma: no cover - must not be called
                raise AssertionError("datasets listed although max_datasets_per_project is 0")

        self.assertEqual(telemetry.discover_datasets(Ctx()), [("demo-project", "ge_analytics"),
                                                              ("demo-project", "telemetry_routed")])


class SamplingTest(unittest.TestCase):
    def test_dominant_logs_are_left_out_of_the_shared_read_and_quiet_logs_get_targeted_reads(self):
        Gcp = type("Gcp", (), {})
        calls = []

        class Ctx(support.FakeCtx):
            def rest(self, method, url, json_body=None, **_kw):
                calls.append(json_body)
                if len(calls) == 1:
                    return {"entries": [{"timestamp": NOW, "logName": "projects/demo-project/logs/busy",
                                         "jsonPayload": {"a": 1}}]}
                short = json_body["filter"].split("/logs/", 1)[1].split('"', 1)[0]
                return {"entries": [{"timestamp": NOW, "logName": "projects/demo-project/logs/%s" % short,
                                     "jsonPayload": {"token_usage": 530}}]}

        ctx = Ctx()
        ctx.gcp = Gcp()
        logs = [{"shortName": "noisy", "logName": "projects/demo-project/logs/noisy", "project": "demo-project"},
                {"shortName": "busy", "logName": "projects/demo-project/logs/busy", "project": "demo-project"},
                {"shortName": "custom_rag_worker", "logName": "projects/demo-project/logs/custom_rag_worker",
                 "project": "demo-project"}]
        activity = {"noisy": [9000] * 7, "busy": [500] * 7, "custom_rag_worker": [3] * 7}
        entries, by_short = telemetry._sample_entries(ctx, logs, activity)
        self.assertIn('NOT logName="projects/demo-project/logs/noisy"', calls[0]["filter"])
        self.assertNotIn("busy", calls[0]["filter"].split("NOT", 1)[-1].replace("noisy", ""))
        # The dominant log still gets its own small targeted read, like every log the shared read missed.
        self.assertEqual(sorted(by_short), ["busy", "custom_rag_worker", "noisy"])
        self.assertEqual(len(entries), 3)
        self.assertEqual(sorted(c["filter"].split("/logs/", 1)[1].split('"', 1)[0] for c in calls[1:]),
                         ["custom_rag_worker", "noisy"])
        self.assertTrue(all(c["pageSize"] == 5 for c in calls[1:]))

    def test_workloads_sharing_a_runtime_log_become_sources(self):
        ctx = support.FakeCtx()
        entries = [agent_entry("retrieve", "a@example.org"), agent_entry("generate", "b@example.org"),
                   agent_entry("x", "c@example.org", service="billing-worker"),
                   agent_entry("y", "d@example.org", service="billing-worker")]
        streams = telemetry._workload_streams(ctx, entries, DynamicDescriptionTest.DESCRIPTORS, 7)
        self.assertEqual(sorted(s["svc"] for s in streams), ["billing-worker", "rag-orchestrator"])
        rag = next(s for s in streams if s["svc"] == "rag-orchestrator")
        self.assertEqual(rag["filter"], 'resource.type="cloud_run_revision" AND resource.labels.service_name='
                                        '"rag-orchestrator" AND jsonPayload:*')
        with mock.patch.dict("os.environ", {"K_SERVICE": "rag-orchestrator"}):
            own = telemetry._workload_streams(ctx, entries, DynamicDescriptionTest.DESCRIPTORS, 7)
        self.assertNotIn("rag-orchestrator", [s["svc"] for s in own])  # the control plane itself is skipped


class MockAi:
    def __init__(self, sources_resp, groups_resp=None):
        self.sources_resp = sources_resp
        self.groups_resp = groups_resp or {"groups": []}
        self.calls = []

    def effective_model(self, _settings, preferred):
        return preferred

    def generate(self, _settings, model, prompt, **kwargs):
        self.calls.append((model, kwargs.get("schema") is telemetry.GROUP_SCHEMA, prompt))
        assert kwargs.get("module") == "telemetry_ai"
        if kwargs.get("schema") is telemetry.GROUP_SCHEMA:
            return self.groups_resp
        return self.sources_resp


class AiProfileTest(unittest.TestCase):
    RAG = schema_mod.TableSchema.from_dicts([
        col("event_ts", "TIMESTAMP"), col("actor_email"), col("pipeline_step"), col("llm_model"),
        col("token_usage", "INTEGER"), col("outcome"), col("summary_text")])
    SKEY = ("demo-project", "agent_ops_archive", "rag_pipeline_events")

    def source(self):
        mapping = telemetry.suggested_mapping(self.RAG.leaf_types(50))
        card = profile.describe("rag_pipeline_events", dataset="agent_ops_archive")
        quick, card = profile.bigquery_connector(card, *self.SKEY, self.RAG, mapping)
        types = {p: t.split(" ")[0] for p, t in self.RAG.leaf_types(50)}
        return {"key": "bq:demo-project.agent_ops_archive.rag_pipeline_events", "kind": "bigquery", "rows": 900,
                "catalog": card, "quickAdd": quick, "suggestedMapping": mapping, "_fields": list(types),
                "_types": types, "_ident": "demo-project.agent_ops_archive.rag_pipeline_events", "_days": 7,
                "_skey": self.SKEY, "_digest": [{"path": p, "type": t} for p, t in types.items()],
                "_fp": telemetry._fingerprint("rag", ["%s:%s" % kv for kv in types.items()])}

    PROFILE = {
        "key": "bq:demo-project.agent_ops_archive.rag_pipeline_events", "category": "ai",
        "group": "RAG pipeline", "groupKo": "RAG 파이프라인", "title": "RAG Pipeline Execution Events",
        "titleKo": "RAG 파이프라인 실행 이벤트", "what": "Custom RAG retrieval and generation steps with token usage.",
        "whatKo": "커스텀 RAG 검색·생성 단계와 토큰 사용량입니다.", "sensitive": False,
        "time": "nonexistent_ts", "operation": "pipeline_step", "operationLabel": "Pipeline step",
        "operationLabelKo": "파이프라인 단계", "status": "outcome", "statusLabel": "Outcome", "statusLabelKo": "처리 결과",
        "user": "actor_email", "model": "llm_model", "tokens": "summary_text", "tokensLabel": "Tokens",
        "tokensLabelKo": "토큰 수", "value": 9, "routeRecommended": True, "routeReason": "High value history.",
        "routeReasonKo": "보관 가치가 높습니다."}

    def ctx(self, ai):
        ctx = support.FakeCtx()
        ctx.app.ai = ai
        return ctx

    def test_profiles_are_validated_applied_and_cached(self):
        ai = MockAi({"sources": [self.PROFILE]})
        ctx = self.ctx(ai)
        src = self.source()
        info = telemetry._ai_profile(ctx, [src], {self.SKEY: self.RAG})
        self.assertEqual((info["enabled"], info["model"], info["newlyProfiled"]), (True, "gemini-4-argon", 1))
        card = src["catalog"]
        self.assertTrue(card["aiProfiled"])
        self.assertEqual((card["group"], card["groupKo"], card["titleKo"]), ("RAG pipeline", "RAG 파이프라인",
                                                                             "RAG 파이프라인 실행 이벤트"))
        self.assertEqual(card["value"], 5)  # clamped to 1..5
        self.assertTrue(card["routeRecommended"])
        q = src["quickAdd"]["mapping"]
        self.assertEqual(q["time"], "event_ts")         # hallucinated time column rejected
        self.assertEqual(q["tokens"], "token_usage")     # STRING column rejected for tokens
        self.assertEqual((q["operation"], q["status"], q["user"], q["model"]),
                         ("pipeline_step", "outcome", "actor_email", "llm_model"))
        self.assertEqual(src["quickAdd"]["meta"]["titleKo"], "RAG 파이프라인 실행 이벤트")
        # The same source with the same fields is not sent to Gemini again.
        again = self.source()
        info = telemetry._ai_profile(ctx, [again], {self.SKEY: self.RAG})
        self.assertEqual(info["newlyProfiled"], 0)
        self.assertEqual(len(ai.calls), 1)
        self.assertEqual(again["catalog"]["titleKo"], "RAG 파이프라인 실행 이벤트")
        # A connector created from the quick-add body shows the Gemini text (saved as meta).
        restored = telemetry._card_with_meta(profile.for_connector(src["quickAdd"]), src["quickAdd"]["meta"],
                                             src["quickAdd"])
        self.assertEqual((restored["titleKo"], restored["groupKo"], restored["operationLabelKo"]),
                         ("RAG 파이프라인 실행 이벤트", "RAG 파이프라인", "파이프라인 단계"))

    def test_group_names_are_merged(self):
        p2 = dict(self.PROFILE, key="log:demo-project:rag-worker", group="RAG pipelines")
        ai = MockAi({"sources": [self.PROFILE, p2]}, {"groups": [
            {"name": "RAG pipeline", "canonical": "RAG pipeline", "canonicalKo": "RAG 파이프라인"},
            {"name": "RAG pipelines", "canonical": "RAG pipeline", "canonicalKo": "RAG 파이프라인"}]})
        ctx = self.ctx(ai)
        src1 = self.source()
        src2 = dict(self.source(), key="log:demo-project:rag-worker", kind="logging", quickAdd=None, _skey=None,
                    _fp="fp2")
        telemetry._ai_profile(ctx, [src1, src2], {self.SKEY: self.RAG})
        self.assertEqual(src2["catalog"]["group"], "RAG pipeline")
        self.assertTrue(any(is_group for _m, is_group, _p in ai.calls))

    def test_disabled_or_no_model(self):
        ai = MockAi({"sources": [self.PROFILE]})
        ctx = self.ctx(ai)
        ctx.settings = support.make_settings(telemetry={"ai": {"enabled": False}})
        self.assertEqual(telemetry._ai_profile(ctx, [self.source()], {})["enabled"], False)
        self.assertEqual(ai.calls, [])

    def test_override_and_refresh(self):
        ctx = self.ctx(MockAi({"sources": []}))
        key = "log:demo-project:run.googleapis.com/stdout"
        ctx.body = {"key": key, "title": "Agent runtime output", "titleKo": "에이전트 실행 출력", "category": "ai",
                    "group": "Agent runtime", "hidden": False}
        res = telemetry.save_override(ctx)
        self.assertEqual(res["override"]["title"], "Agent runtime output")
        src = {"key": key, "catalog": profile.describe("run_googleapis_com_stdout", short="run.googleapis.com/stdout"),
               "quickAdd": {"name": "x", "mapping": {}}}
        telemetry._apply_override(src, telemetry._overrides(ctx)[key])
        self.assertEqual((src["catalog"]["title"], src["catalog"]["titleParams"], src["catalog"]["category"]),
                         ("Agent runtime output", None, "ai"))
        self.assertTrue(src["catalog"]["overridden"])
        self.assertEqual(src["catalog"]["override"], {"title": "Agent runtime output", "titleKo": "에이전트 실행 출력",
                                                      "category": "ai", "group": "Agent runtime", "hidden": False})
        self.assertEqual(src["quickAdd"]["name"], "Agent runtime output")
        for bad in ({"key": "nope"}, {"key": key, "category": "finance"}):
            ctx.body = bad
            with self.assertRaises(ApiError):
                telemetry.save_override(ctx)
        ctx.body = {"key": key, "reset": True}
        telemetry.save_override(ctx)
        self.assertNotIn(key, telemetry._overrides(ctx))
        viewer = support.FakeCtx(role="viewer", body={"key": key, "title": "x"})
        with self.assertRaises(ApiError):
            telemetry.save_override(viewer)
        telemetry._save_profiles(ctx, {key: {"fp": "f", "at": "2026-11-01T00:00:00Z", "profile": {"group": "g"}}})
        ctx.body = {}
        self.assertEqual(telemetry.refresh_profiles(ctx), {"removed": 1})


class GroupDashboardTest(unittest.TestCase):
    def test_one_dashboard_for_a_group_of_logs(self):
        mgr = settings_store.SettingsManager(support.make_settings(), settings_store.MemoryStore())
        ctx = support.FakeCtx(settings=mgr.snapshot()[0])
        ctx.app.settings = mgr
        logs = ["projects/demo-project/logs/run.googleapis.com%2Fstdout", "projects/demo-project/logs/custom_rag_worker"]
        ctx.body = {"name": "RAG pipeline", "nameKo": "RAG 파이프라인", "logs": logs + ["not a log name"]}
        with mock.patch.object(telemetry, "_connector_health", return_value=("ok", None)):
            res = telemetry.group_dashboard(ctx)
            conn = res["connector"]
            self.assertEqual(conn["logging"]["filter"], 'logName=("%s" OR "%s")' % tuple(logs))
            self.assertEqual(conn["mapping"]["operation"], "logName")
            self.assertEqual(conn["meta"]["groupKo"], "RAG 파이프라인")
            # Creating it again returns the same connector.
            again = telemetry.group_dashboard(ctx)
            self.assertEqual(again["connector"]["id"], conn["id"])
            self.assertEqual(len(again["connectors"]), 1)
            # The group grew: the same dashboard now reads every current member.
            grown = logs + ["projects/demo-project/logs/rag_reranker"]
            ctx.body = {"name": "RAG pipeline", "nameKo": "RAG 파이프라인", "logs": grown}
            updated = telemetry.group_dashboard(ctx)
            self.assertEqual(updated["connector"]["id"], conn["id"])
            self.assertEqual(updated["connector"]["logging"]["filter"], 'logName=("%s" OR "%s" OR "%s")' % tuple(grown))
            self.assertEqual(len(updated["connectors"]), 1)
        ctx.body = {"name": "x", "logs": ["bad"]}
        with self.assertRaises(ApiError):
            telemetry.group_dashboard(ctx)

    def test_log_name_breakdown_reads_the_short_log_id(self):
        entry = {"logName": "projects/p/logs/run.googleapis.com%2Fstdout"}
        self.assertEqual(telemetry._get_path(entry, "logName"), "run.googleapis.com/stdout")


class DiscoverySnapshotAndOverlayTest(unittest.TestCase):
    def _make_env(self):
        base_settings = support.make_settings(workspace={"project_id": "demo-project", "projects": ["sub-project"],
                                                         "timezone": "UTC"})
        mgr = settings_store.SettingsManager(base_settings, settings_store.MemoryStore())
        ctx = support.FakeCtx(settings=mgr.snapshot()[0])
        ctx.app.settings = mgr
        ctx.app.gcp = object()
        return ctx, mgr

    @staticmethod
    def _fake_base():
        tb_demo = {
            "id": "rag_events", "key": "bq:demo-project.ops.rag_events", "kind": "bigquery",
            "project": "demo-project", "dataset": "ops", "logical": "rag_events", "table": "rag_events",
            "rows": 120, "bytes": 4096, "volume": {"bytesPerDay": 1024.0},
            "catalog": {"title": "RAG events", "titleKo": "RAG 이벤트", "category": "ai",
                        "group": "RAG pipeline", "groupKo": "RAG 파이프라인"},
            "quickAdd": {"name": "RAG events", "enabled": True, "source": "bigquery",
                         "bigquery": {"project": "demo-project", "dataset": "ops", "table": "rag_events"},
                         "logging": None, "mapping": {"time": "event_ts", "operation": "step"}},
        }
        lg_demo = {
            "id": "run_googleapis_com_stdout", "key": "log:demo-project:run.googleapis.com/stdout", "kind": "logging",
            "project": "demo-project", "logName": "projects/demo-project/logs/run.googleapis.com%2Fstdout",
            "shortName": "run.googleapis.com/stdout", "core": False, "volume": {"bytesPerDay": 2048.0},
            "catalog": {"title": "Cloud Run stdout", "titleKo": "Cloud Run 표준출력", "category": "runtime",
                        "group": "RAG pipeline", "groupKo": "RAG 파이프라인"},
            "quickAdd": {"name": "Cloud Run stdout", "enabled": True, "source": "logging", "bigquery": None,
                         "logging": {"filter": 'logName="projects/demo-project/logs/run.googleapis.com%2Fstdout"'},
                         "mapping": {"time": "timestamp", "operation": "severity"}},
        }
        lg_sub = {
            "id": "sub_worker", "key": "log:sub-project:sub_worker", "kind": "logging",
            "project": "sub-project", "logName": "projects/sub-project/logs/sub_worker",
            "shortName": "sub_worker", "core": False, "volume": {"bytesPerDay": 512.0},
            "catalog": {"title": "Sub worker", "titleKo": "서브 워커", "category": "ai",
                        "group": "Sub agents", "groupKo": "서브 에이전트"},
            "quickAdd": {"name": "Sub worker", "enabled": True, "source": "logging", "bigquery": None,
                         "logging": {"filter": 'logName="projects/sub-project/logs/sub_worker"'},
                         "mapping": {"time": "timestamp", "operation": "severity"}},
        }
        return {"tables": [tb_demo], "logs": [lg_demo, lg_sub], "activityDays": 7,
                "aiGrouping": {"enabled": True, "model": "gemini-4-argon", "profiled": 3, "newlyProfiled": 0, "pending": 0},
                "maxLogEntries": 2000}

    def test_snapshot_serves_cold_start_filters_projects_and_overlays_live_mutations(self):
        from controlplane.services import telemetry_routing as routing

        ctx, mgr = self._make_env()

        def fake_compute(full_ctx, allow_uncached_ai=True):
            full_ctx.warn("bigquery:demo-project.ops", "access_denied", "demo warning")
            full_ctx.warn("bigquery:sub-project.ops", "access_denied", "sub warning")
            full_ctx.add_source("api:logging.logs_list")
            return self._fake_base()

        with mock.patch.object(telemetry, "_compute_discover_base", side_effect=fake_compute) as comp:
            snap = telemetry.refresh_discovery_snapshot(ctx.app)
            self.assertEqual(comp.call_count, 1)
            self.assertIn("updatedAt", snap)

        # Simulate Cloud Run cold start: clear in-memory cache; discover() must serve from aux_docs with 0 scans.
        ctx.app.cache.clear()
        with mock.patch.object(telemetry, "_compute_discover_base", side_effect=AssertionError("must not rescan")):
            all_res = telemetry.discover(ctx)
        self.assertEqual(len(all_res["tables"]), 1)
        self.assertEqual(len(all_res["logs"]), 2)
        self.assertEqual({g["name"] for g in all_res["groups"]}, {"RAG pipeline", "Sub agents"})
        self.assertEqual(all_res["snapshotAt"], snap["updatedAt"])
        self.assertEqual(len(ctx.warnings), 2)

        # Single-project filter (?project=sub-project) slices tables, logs, groups and warnings from the shared snapshot.
        sub_ctx = support.FakeCtx(settings=mgr.snapshot()[0], params={"project": "sub-project"})
        sub_ctx.app = ctx.app
        with mock.patch.object(telemetry, "_compute_discover_base", side_effect=AssertionError("must not rescan")):
            sub_res = telemetry.discover(sub_ctx)
        self.assertEqual(sub_res["tables"], [])
        self.assertEqual([lg["project"] for lg in sub_res["logs"]], ["sub-project"])
        self.assertEqual([g["name"] for g in sub_res["groups"]], ["Sub agents"])
        self.assertEqual([w["source"] for w in sub_ctx.warnings], ["bigquery:sub-project.ops"])

        # Live overlay: connector registration, route state, source override, and override reset take effect immediately.
        conn_id = "cn_rag_events"
        settings_with_conn = support.make_settings(
            workspace={"project_id": "demo-project", "projects": ["sub-project"], "timezone": "UTC"},
            telemetry={"connectors": [{
                "id": conn_id, "name": "RAG events", "enabled": True, "source": "bigquery",
                "bigquery": {"project": "demo-project", "dataset": "ops", "table": "rag_events"},
                "mapping": {"time": "event_ts", "operation": "step"},
            }]},
        )
        ctx.settings = settings_with_conn
        settings_store.aux_docs(ctx.app).mutate(routing.STATE_DOC, lambda doc: doc.update(routes=[{
            "id": "rt_01", "title": "Runtime route", "status": "active", "sink": "acp-tx-rt",
            "logs": ["projects/demo-project/logs/run.googleapis.com%2Fstdout"],
        }]))
        ctx.body = {"key": "log:demo-project:run.googleapis.com/stdout", "title": "Custom Stdout", "hidden": False}
        telemetry.save_override(ctx)

        with mock.patch.object(telemetry, "_compute_discover_base", side_effect=AssertionError("must not rescan")), \
                mock.patch.object(telemetry, "_connector_health", return_value=("ok", None)):
            live_res = telemetry.discover(ctx)
        self.assertTrue(live_res["tables"][0]["registered"])
        self.assertEqual(live_res["tables"][0]["connectorId"], conn_id)
        stdout_log = next(lg for lg in live_res["logs"] if lg["id"] == "run_googleapis_com_stdout")
        self.assertEqual(stdout_log["routedBy"], "rt_01")
        self.assertEqual(stdout_log["catalog"]["title"], "Custom Stdout")
        self.assertTrue(stdout_log["catalog"]["overridden"])

        # Resetting the override restores the original Gemini-profiled title immediately without rescanning!
        ctx.body = {"key": "log:demo-project:run.googleapis.com/stdout", "reset": True}
        telemetry.save_override(ctx)
        with mock.patch.object(telemetry, "_compute_discover_base", side_effect=AssertionError("must not rescan")), \
                mock.patch.object(telemetry, "_connector_health", return_value=("ok", None)):
            reset_res = telemetry.discover(ctx)
        stdout_after = next(lg for lg in reset_res["logs"] if lg["id"] == "run_googleapis_com_stdout")
        self.assertEqual(stdout_after["catalog"]["title"], "Cloud Run stdout")
        self.assertFalse(stdout_after["catalog"].get("overridden", False))

    def test_non_blocking_ai_profile_skips_gemini_rpc_and_marks_pending(self):
        ai = MockAi({"sources": [AiProfileTest.PROFILE]})
        ctx = support.FakeCtx()
        ctx.app.ai = ai
        src = AiProfileTest().source()
        info = telemetry._ai_profile(ctx, [src], {AiProfileTest.SKEY: AiProfileTest.RAG}, allow_uncached=False)
        self.assertEqual(ai.calls, [])
        self.assertEqual(info["newlyProfiled"], 0)
        self.assertEqual(info["pending"], 1)


if __name__ == "__main__":
    unittest.main()

