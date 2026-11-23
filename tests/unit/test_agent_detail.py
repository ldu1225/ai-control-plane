"""Per-agent views (services/agent_detail.py): target resolution, telemetry, models and tools, revisions,
traces, topology, evaluation and the playground.

The real application runs on 127.0.0.1 with fake Google Cloud responses shaped like the documented API
payloads (Agent Platform reasoningEngines / runtimeRevisions / onlineEvaluators / evaluationRuns, Cloud
Monitoring timeSeries and metricDescriptors, Cloud Trace v1). No network call is made.
"""

from __future__ import annotations

import datetime as dt
import http.client
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane import app as app_module
from controlplane.gcp import GcpError
from controlplane.security import Identity
from controlplane.services import agent_detail
from controlplane.services.monitoring import Distribution

PROJECT = "demo-project"
LINKED = "linked-project"
REGION = "us-central1"
ENGINE = "1234567890"
OTHER_ENGINE = "5550001111"
ENGINE_NAME = "projects/987654321098/locations/%s/reasoningEngines/%s" % (REGION, ENGINE)
RANGE = "start=2026-10-13&end=2026-10-19"
TRACE_A = "a" * 32
TRACE_B = "b" * 32
TRACE_C = "c" * 32
TRACE_FOREIGN = "f" * 32
SECRET_PROMPT = "the quarterly forecast is confidential"

CONFIG = """workspace:
  project_id: demo-project
  projects: [linked-project]
  scan_all_projects: false
  audit_dataset: ge_analytics
  timezone: UTC
security:
  auth_mode: local
storage:
  backend: memory
"""

ADMIN = Identity("admin@example.com", "admin", "google", "user:admin@example.com")
VIEWER = Identity("viewer@example.com", "viewer", "google", "user:viewer@example.com")


def day_point(day, value):
    """A daily aligned point: the interval ends at the next local midnight (the workspace time zone is UTC)."""
    end = dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc) + dt.timedelta(days=1)
    return {"interval": {"startTime": day + "T00:00:00Z", "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ")},
            "value": value}


def dist(bounds, counts, mean):
    return {"distributionValue": {"count": str(sum(counts)), "mean": mean,
                                  "bucketOptions": {"explicitBuckets": {"bounds": bounds}},
                                  "bucketCounts": [str(c) for c in counts]}}


def series(metric_labels, points, resource_labels=None):
    return {"metric": {"labels": metric_labels}, "resource": {"labels": resource_labels or {}}, "points": points}


LATENCY_MS = [100, 200, 400, 800]
SECONDS = [0.5, 1, 2, 4]
TOKENS = [100, 1000, 10000]
CALLS = [0, 1, 2, 4]
SCORES = [0.25, 0.5, 0.75, 1.0]

ENGINE_RESOURCE = {
    "name": ENGINE_NAME, "displayName": "support-agent", "description": "Answers support questions",
    "createTime": "2026-09-01T00:00:00Z", "updateTime": "2026-10-18T00:00:00Z",
    "spec": {"agentFramework": "google-adk", "effectiveIdentity": "agent-sa@demo-project.iam.gserviceaccount.com",
             "identityType": "SERVICE_ACCOUNT", "packageSpec": {"pythonVersion": "3.12"},
             "deploymentSpec": {"minInstances": 1, "maxInstances": 4, "env": [
                 {"name": "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY", "value": "true"},
                 {"name": "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "value": "EVENT_ONLY"},
                 {"name": "BACKEND_API_KEY", "value": "do-not-leak"}],
                 "secretEnv": [{"name": "DB_PASSWORD", "secretRef": {"secret": "db", "version": "1"}}]},
             "classMethods": [{"name": "async_stream_query", "api_mode": "async_stream"},
                              {"name": "async_create_session", "api_mode": "async"}]},
}
PLAIN_ENGINE = {"name": "projects/987654321098/locations/%s/reasoningEngines/%s" % (REGION, OTHER_ENGINE),
                "displayName": "batch-agent", "spec": {"agentFramework": "langchain",
                                                       "classMethods": [{"name": "query", "api_mode": ""}]}}

TIME_SERIES = {
    agent_detail.REQUEST_COUNT: [
        series({"response_code": "200", "response_code_class": "2xx", "type": "stream_query"},
               [day_point("2026-10-19", {"int64Value": "100"}), day_point("2026-10-18", {"int64Value": "90"}),
                day_point("2026-10-10", {"int64Value": "50"})]),
        series({"response_code": "500", "response_code_class": "5xx", "type": "stream_query"},
               [day_point("2026-10-19", {"int64Value": "10"})]),
    ],
    agent_detail.REQUEST_LATENCIES: [
        series({"response_code": "200"}, [day_point("2026-10-19", dist(LATENCY_MS, [0, 10, 60, 25, 5], 330.0)),
                                          day_point("2026-10-10", dist(LATENCY_MS, [0, 50, 0, 0, 0], 150.0))]),
    ],
    agent_detail.CPU_ALLOCATION: [series({}, [day_point("2026-10-19", {"doubleValue": 120.5}),
                                              day_point("2026-10-18", {"doubleValue": 30.0})])],
    agent_detail.MEMORY_ALLOCATION: [series({}, [day_point("2026-10-19", {"doubleValue": 64.25})])],
    "prometheus.googleapis.com/gen_ai.invoke_agent.duration/histogram": [
        series({"gen_ai.agent.name": "root_agent"}, [day_point("2026-10-19", dist(SECONDS, [0, 0, 6, 4, 0], 2.0)),
                                                     day_point("2026-10-11", dist(SECONDS, [0, 4, 0, 0, 0], 0.8))]),
        series({"gen_ai.agent.name": "billing_agent"}, [day_point("2026-10-19", dist(SECONDS, [8, 2, 0, 0, 0], 0.4))]),
        series({"gen_ai.agent.name": "billing_agent", "error.type": "TimeoutError"},
               [day_point("2026-10-19", dist(SECONDS, [0, 0, 0, 0, 2], 6.0))]),
    ],
    "prometheus.googleapis.com/gen_ai.client.operation.duration/histogram": [
        series({"gen_ai.agent.name": "root_agent", "gen_ai.request.model": "gemini-3.8-flash",
                "gen_ai.response.model": "gemini-3.8-flash"},
               [day_point("2026-10-19", dist(SECONDS, [0, 10, 10, 0, 0], 1.0)),
                day_point("2026-10-12", dist(SECONDS, [5, 0, 0, 0, 0], 0.3))]),
        series({"gen_ai.agent.name": "root_agent", "gen_ai.request.model": "gemini-3.8-flash",
                "error.type": "429"}, [day_point("2026-10-19", dist(SECONDS, [5, 0, 0, 0, 0], 0.2))]),
        series({"gen_ai.agent.name": "billing_agent",
                "gen_ai.request.model": "publishers/google/models/gemini-3.8-pro"},
               [day_point("2026-10-18", dist(SECONDS, [0, 0, 4, 0, 0], 1.5))]),
        # Reported a second time by the model SDK instrumentation (no agent name): must not be counted twice.
        series({"gen_ai.request.model": "gemini-3.8-flash"},
               [day_point("2026-10-19", dist(SECONDS, [0, 10, 10, 0, 0], 1.0))]),
    ],
    "prometheus.googleapis.com/gen_ai.client.token.usage/histogram": [
        series({"gen_ai.agent.name": "root_agent", "gen_ai.request.model": "gemini-3.8-flash",
                "gen_ai.token.type": "input"}, [day_point("2026-10-19", dist(TOKENS, [0, 20, 0, 0], 500.0))]),
        series({"gen_ai.agent.name": "root_agent", "gen_ai.request.model": "gemini-3.8-flash",
                "gen_ai.token.type": "output"}, [day_point("2026-10-19", dist(TOKENS, [20, 0, 0, 0], 60.0))]),
        series({"gen_ai.request.model": "gemini-3.8-flash", "gen_ai.token.type": "input"},
               [day_point("2026-10-19", dist(TOKENS, [0, 20, 0, 0], 500.0))]),
    ],
    "prometheus.googleapis.com/gen_ai.execute_tool.duration/histogram:delta": [
        series({"gen_ai.agent.name": "billing_agent", "gen_ai.tool.name": "lookup_invoice",
                "gen_ai.tool.type": "FunctionTool"}, [day_point("2026-10-19", dist(SECONDS, [6, 0, 0, 0, 0], 0.2))]),
        series({"gen_ai.agent.name": "billing_agent", "gen_ai.tool.name": "lookup_invoice",
                "gen_ai.tool.type": "FunctionTool", "error.type": "ValueError"},
               [day_point("2026-10-19", dist(SECONDS, [2, 0, 0, 0, 0], 0.1))]),
    ],
    "prometheus.googleapis.com/gen_ai.invoke_agent.inference_calls/histogram": [
        series({"gen_ai.agent.name": "root_agent"}, [day_point("2026-10-19", dist(CALLS, [0, 0, 10, 0, 0], 2.5))]),
    ],
    agent_detail.EVAL_SCORES: [
        series({"agent_resource_name": ENGINE_NAME, "evaluation_metric_name": "safety_v1"},
               [day_point("2026-10-19", dist(SCORES, [0, 0, 0, 10, 0], 0.9))], {"online_evaluator_id": "mon-1"}),
        series({"agent_resource_name": "projects/987654321098/locations/us-central1/reasoningEngines/999",
                "evaluation_metric_name": "safety_v1"},
               [day_point("2026-10-19", dist(SCORES, [10, 0, 0, 0, 0], 0.1))], {"online_evaluator_id": "mon-9"}),
    ],
    agent_detail.EVAL_COUNT: [
        series({"agent_resource_name": ENGINE_NAME, "evaluation_metric_name": "safety_v1",
                "evaluation_status": "SUCCEEDED"}, [day_point("2026-10-19", {"int64Value": "10"})],
               {"online_evaluator_id": "mon-1"}),
        series({"agent_resource_name": ENGINE_NAME, "evaluation_metric_name": "safety_v1",
                "evaluation_status": "FAILED"}, [day_point("2026-10-19", {"int64Value": "2"})],
               {"online_evaluator_id": "mon-1"}),
    ],
}
DESCRIPTORS = [{"type": t, "metricKind": "DELTA" if t.endswith(":delta") else "CUMULATIVE",
                "valueType": "DISTRIBUTION"} for t in TIME_SERIES if t.startswith("prometheus.googleapis.com/")]

ENGINE_LABELS = {"service.name": ENGINE, "cloud.platform": "gcp.agent_engine"}


def span(span_id, name, start, end, parent=None, **labels):
    out = {"spanId": span_id, "name": name, "startTime": "2026-10-19T03:00:%sZ" % start,
           "endTime": "2026-10-19T03:00:%sZ" % end, "labels": dict(ENGINE_LABELS, **labels)}
    if parent:
        out["parentSpanId"] = parent
    return out


SPANS_A = [
    span("1", "invoke_agent root_agent", "00.000", "04.000", **{
        "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "root_agent",
        "gen_ai.conversation.id": "session-1", "user.id": "alice@example.com"}),
    span("2", "call_llm", "00.100", "01.100", parent="1", **{
        "gen_ai.request.model": "gemini-3.8-flash", "gen_ai.usage.input_tokens": "1200",
        "gen_ai.usage.output_tokens": "80", "gcp.vertex.agent.llm_request": SECRET_PROMPT,
        "gcp.vertex.agent.llm_response": SECRET_PROMPT}),
    span("3", "generate_content gemini-3.8-flash", "00.200", "01.000", parent="2", **{
        "gen_ai.operation.name": "generate_content", "gen_ai.request.model": "gemini-3.8-flash",
        "gen_ai.usage.input_tokens": "1200", "gen_ai.usage.output_tokens": "80",
        "gen_ai.input.messages": SECRET_PROMPT}),
    span("4", "invoke_agent billing_agent", "01.200", "03.900", parent="1", **{
        "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "billing_agent"}),
    # The Agent Development Kit runs a tool inside the span of the model call that asked for it, and writes
    # one more span for the merged response of parallel tool calls.
    span("6", "generate_content gemini-3.8-pro", "01.300", "03.800", parent="4", **{
        "gen_ai.operation.name": "generate_content", "gen_ai.request.model": "gemini-3.8-pro",
        "gen_ai.usage.input_tokens": "300", "gen_ai.usage.output_tokens": "40"}),
    span("5", "execute_tool lookup_invoice", "01.500", "02.000", parent="6", **{
        "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "lookup_invoice",
        "gcp.vertex.agent.tool_call_args": SECRET_PROMPT, "error.type": "ValueError"}),
    span("7", "execute_tool (merged)", "02.000", "02.001", parent="6", **{
        "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "(merged tools)"}),
]
TRACES = {
    TRACE_A: {"projectId": PROJECT, "traceId": TRACE_A, "spans": SPANS_A},
    TRACE_B: {"projectId": PROJECT, "traceId": TRACE_B, "spans": [
        span("9", "invoke_agent root_agent", "10.000", "11.000", **{
            "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "root_agent"})]},
    # Not listed; only the nested SDK span of the model call reports usage.
    TRACE_C: {"projectId": PROJECT, "traceId": TRACE_C, "spans": [
        span("1", "invoke_agent root_agent", "20.000", "21.000", **{
            "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "root_agent"}),
        span("2", "call_llm", "20.100", "20.900", parent="1", **{"gen_ai.request.model": "gemini-3.8-flash"}),
        span("3", "generate_content gemini-3.8-flash", "20.200", "20.800", parent="2", **{
            "gen_ai.operation.name": "generate_content", "gen_ai.request.model": "gemini-3.8-flash",
            "gen_ai.usage.input_tokens": "10", "gen_ai.usage.output_tokens": "5"})]},
    TRACE_FOREIGN: {"projectId": PROJECT, "traceId": TRACE_FOREIGN, "spans": [
        {"spanId": "1", "name": "GET /internal/payroll", "startTime": "2026-10-19T03:00:00Z",
         "endTime": "2026-10-19T03:00:01Z", "labels": {"service.name": "payroll-api"}}]},
}

REVISIONS = [
    {"name": ENGINE_NAME + "/runtimeRevisions/rev-3", "state": "ACTIVE", "createTime": "2026-10-18T00:00:00Z",
     "spec": {"agentFramework": "google-adk", "packageSpec": {"pythonVersion": "3.12"},
              "deploymentSpec": {"minInstances": 1, "maxInstances": 4, "env": [
                  {"name": "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY", "value": "true"},
                  {"name": "BACKEND_API_KEY", "value": "do-not-leak"}]},
              "classMethods": [{"name": "async_stream_query"}]}},
    {"name": ENGINE_NAME + "/runtimeRevisions/rev-2", "state": "ACTIVE", "createTime": "2026-10-10T00:00:00Z",
     "spec": {"agentFramework": "google-adk"}},
    {"name": ENGINE_NAME + "/runtimeRevisions/rev-1", "state": "ARCHIVED", "createTime": "2026-09-01T00:00:00Z",
     "spec": {}},
]
MONITORS = [
    {"name": "projects/987654321098/locations/us-central1/onlineEvaluators/mon-1", "displayName": "Safety monitor",
     "agentResource": ENGINE_NAME, "state": "ACTIVE", "createTime": "2026-10-01T00:00:00Z",
     "metricSources": [{"metric": {"predefinedMetricSpec": {"metricSpecName": "safety_v1"}}}],
     "config": {"randomSampling": {"percentage": 20}, "maxEvaluatedSamplesPerRun": "50"},
     "cloudObservability": {"traceScope": {}}},
    {"name": "projects/987654321098/locations/us-central1/onlineEvaluators/mon-9", "displayName": "Other agent",
     "agentResource": "projects/987654321098/locations/us-central1/reasoningEngines/999", "state": "FAILED",
     "stateDetails": [{"message": "permission denied"}]},
]
RUNS = [
    {"name": "projects/987654321098/locations/us-central1/evaluationRuns/run-1", "displayName": "Release check",
     "state": "SUCCEEDED", "createTime": "2026-10-17T00:00:00Z", "completionTime": "2026-10-17T00:10:00Z",
     "inferenceConfigs": {"candidate": {"agentRunConfig": {"agentEngine": ENGINE_NAME}}},
     "evaluationResults": {"summaryMetrics": {"totalItems": 20, "failedItems": 1,
                                              "metrics": {"safety_v1/AVERAGE": 0.95, "label": "n/a"}}}},
    {"name": "projects/987654321098/locations/us-central1/evaluationRuns/run-2", "displayName": "Recorded data",
     "state": "SUCCEEDED", "createTime": "2026-10-16T00:00:00Z"},
]
SESSIONS = [
    {"name": ENGINE_NAME + "/sessions/s3", "userId": "alice@example.com", "updateTime": "2026-10-19T02:00:00Z"},
    {"name": ENGINE_NAME + "/sessions/s2", "userId": "bob@example.com", "updateTime": "2026-10-15T02:00:00Z"},
    {"name": ENGINE_NAME + "/sessions/s1", "userId": "alice@example.com", "updateTime": "2026-10-01T02:00:00Z"},
]
STREAM_LINES = [
    json.dumps({"author": "root_agent", "content": {"role": "model", "parts": [
        {"function_call": {"name": "lookup_invoice", "args": {"invoice": "A-1"}}}]}}),
    json.dumps({"author": "root_agent", "content": {"role": "user", "parts": [
        {"function_response": {"name": "lookup_invoice", "response": {"status": "paid"}}}]}}),
    "data: " + json.dumps({"author": "root_agent", "content": {"role": "model", "parts": [
        {"text": "Invoice A-1 is paid."}]}}),
    "not json",
]


class Upstream:
    """Fake Google Cloud: answers by URL and records every call."""

    def __init__(self):
        self.calls = []
        self.audit = []
        self.streams = []
        self.fail = {}
        self.trace_filter_hits = None  # None: every engine filter matches

    def request(self, method, url, **kwargs):
        params = kwargs.get("params")
        params = dict(params) if params else {}
        self.calls.append((method, url, params, kwargs.get("json_body")))
        for needle, err in self.fail.items():
            if needle in url:
                raise err
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path
        if path.endswith("/entries:write"):
            self.audit.extend(e["jsonPayload"] for e in kwargs["json_body"]["entries"])
            return {}, {}
        if parsed.hostname == "monitoring.googleapis.com":
            if path.endswith("/metricDescriptors"):
                return {"metricDescriptors": DESCRIPTORS if "/%s/" % PROJECT in path else []}, {}
            metric = params["filter"].split('metric.type="', 1)[1].split('"', 1)[0]
            return {"timeSeries": TIME_SERIES.get(metric, []) if "/%s/" % PROJECT in path else []}, {}
        if parsed.hostname == "cloudtrace.googleapis.com":
            tail = path.rsplit("/traces", 1)[1].strip("/")
            if tail:
                if tail not in TRACES:
                    raise GcpError("not_found", status=404)
                return TRACES[tail], {}
            if self.trace_filter_hits is not None and params["filter"] not in self.trace_filter_hits:
                return {}, {}
            items = [TRACES[TRACE_A], TRACES[TRACE_B]]
            if params["view"] == "ROOTSPAN":
                items = [dict(tr, spans=tr["spans"][:1]) for tr in items]
            return {"traces": items}, {}
        if path.endswith(":query"):
            return {"output": {"id": "session-new", "userId": kwargs["json_body"]["input"]["user_id"]}}, {}
        if path.endswith("/reasoningEngines"):
            if "/%s/" % LINKED in path:
                return {"reasoningEngines": []}, {}
            return {"reasoningEngines": [ENGINE_RESOURCE, PLAIN_ENGINE]}, {}
        if path.endswith("/runtimeRevisions"):
            return {"reasoningEngineRuntimeRevisions": REVISIONS}, {}
        if path.endswith("/onlineEvaluators"):
            return {"onlineEvaluators": MONITORS}, {}
        if path.endswith("/evaluationRuns"):
            return {"evaluationRuns": RUNS}, {}
        if path.endswith("/sessions"):
            return {"sessions": SESSIONS}, {}
        if path.endswith("/reasoningEngines/%s" % ENGINE):
            return dict(ENGINE_RESOURCE, trafficConfig=self.traffic), {}
        return {}, {}

    traffic = {"trafficSplitAlwaysLatest": {}}

    def stream_lines(self, method, url, **kwargs):
        self.streams.append((method, url, kwargs.get("json_body")))
        for needle, err in self.fail.items():
            if needle in url:
                raise err
        return list(STREAM_LINES), False


class DistributionTest(unittest.TestCase):
    def test_percentiles_interpolate_inside_explicit_buckets(self):
        d = Distribution().add(dist(LATENCY_MS, [0, 10, 60, 25, 5], 330.0)["distributionValue"])
        self.assertEqual(d.count, 100)
        self.assertAlmostEqual(d.mean(), 330.0)
        self.assertAlmostEqual(d.percentile(0.5), 200 + 200 * 40 / 60)
        self.assertAlmostEqual(d.percentile(0.95), 800.0)
        self.assertAlmostEqual(d.percentile(0.99), 800.0)  # overflow bucket: its lower bound

    def test_underflow_bucket_starts_at_zero(self):
        d = Distribution().add(dist(LATENCY_MS, [10, 0, 0, 0, 0], 50.0)["distributionValue"])
        self.assertAlmostEqual(d.percentile(0.5), 50.0)

    def test_exponential_and_linear_layouts(self):
        expo = {"count": "4", "mean": 3.0, "bucketCounts": ["0", "0", "4"],
                "bucketOptions": {"exponentialBuckets": {"numFiniteBuckets": 3, "growthFactor": 2, "scale": 1}}}
        self.assertAlmostEqual(Distribution().add(expo).percentile(0.5), 3.0)  # bucket [2, 4)
        linear = {"count": "2", "mean": 15.0, "bucketCounts": ["0", "0", "2"],
                  "bucketOptions": {"linearBuckets": {"numFiniteBuckets": 3, "width": 10, "offset": 0}}}
        self.assertAlmostEqual(Distribution().add(linear).percentile(1.0), 20.0)  # bucket [10, 20)

    def test_merge_and_empty(self):
        a = Distribution().add(dist(LATENCY_MS, [0, 10, 0, 0, 0], 150.0)["distributionValue"])
        b = Distribution().add(dist(LATENCY_MS, [0, 0, 10, 0, 0], 300.0)["distributionValue"])
        merged = Distribution().merge(a).merge(b)
        self.assertEqual(merged.count, 20)
        self.assertAlmostEqual(merged.mean(), 225.0)
        self.assertAlmostEqual(merged.percentile(0.5), 200.0)
        empty = Distribution().add({"count": "0"}).add("not a distribution").add(None)
        self.assertEqual(empty.count, 0)
        self.assertIsNone(empty.percentile(0.95))
        self.assertIsNone(empty.mean())


class AgentDetailTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = os.path.join(cls.tmp.name, "config.yaml")
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write(CONFIG)
        with mock.patch.dict(os.environ, {"CONFIG_PATH": cfg}):
            cls.app = app_module.App(environ={"CONFIG_PATH": cfg})
        cls.server = app_module.make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.up = Upstream()
        self.app.gcp.request = self.up.request
        self.app.gcp.stream_lines = self.up.stream_lines
        self.app.cache.clear()
        self.app.limiter = app_module.RateLimiter()

    def call(self, method, path, body=None, identity=None, csrf=True):
        headers = {}
        raw = None
        if method == "POST":
            raw = json.dumps(body or {})
            headers["Content-Type"] = "application/json"
            if csrf:
                token = "t" * 40
                headers.update({"X-CSRF-Token": token, "Cookie": "cp_csrf=%s" % token})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            with mock.patch.object(app_module, "authenticate", return_value=identity or ADMIN):
                conn.request(method, path, body=raw, headers=headers)
                resp = conn.getresponse()
                return resp.status, json.loads(resp.read() or b"{}")
        finally:
            conn.close()

    def get(self, view, extra="", engine=ENGINE, project=PROJECT, region=REGION, identity=None):
        path = "/api/v1/agent-platform/agent%s?project=%s&region=%s&engine=%s%s" % (
            ("/" + view) if view else "", project, region, engine, ("&" + extra) if extra else "")
        return self.call("GET", path, identity=identity)

    def data(self, view, extra=""):
        status, payload = self.get(view, extra)
        self.assertEqual(status, 200, payload)
        codes = {w["code"] for w in payload["meta"]["warnings"]}
        self.assertFalse(codes & {"internal_error"}, payload["meta"])
        return payload["data"], payload["meta"]

    # -- target resolution ----------------------------------------------------------------------
    def test_target_must_be_a_listed_engine_of_a_connected_project(self):
        for kwargs, code in (({"project": "other-project"}, "bad_request"),
                             ({"project": "Bad Project!"}, "bad_request"),
                             ({"region": "europe-west4"}, "bad_request"),  # valid region, not configured
                             ({"region": "us-central1.evil.example"}, "bad_request"),
                             ({"engine": "12/../34"}, "bad_request"),
                             ({"engine": '1" OR resource.type="x'}, "bad_request"),
                             ({"engine": "777"}, "not_found"),
                             ({"project": LINKED}, "not_found")):
            params = {"project": PROJECT, "region": REGION, "engine": ENGINE}
            params.update(kwargs)
            status, payload = self.call("GET", "/api/v1/agent-platform/agent?" + urllib.parse.urlencode(params))
            self.assertEqual(payload["error"]["code"], code, (kwargs, payload))
            self.assertIn(status, (400, 404))
        # Nothing was requested from Google Cloud for the rejected identifiers except the engine inventory.
        for _method, url, _params, _body in self.up.calls:
            self.assertTrue(url.endswith("/reasoningEngines"), url)
            self.assertIn("%s-aiplatform.googleapis.com" % REGION, url)

    def test_overview_summarizes_the_engine_without_environment_values(self):
        data, meta = self.data("")
        agent = data["agent"]
        self.assertEqual((agent["id"], agent["name"], agent["framework"], agent["pythonVersion"]),
                         (ENGINE, "support-agent", "google-adk", "3.12"))
        self.assertEqual(agent["resourceName"],
                         "projects/%s/locations/%s/reasoningEngines/%s" % (PROJECT, REGION, ENGINE))
        self.assertEqual((agent["minInstances"], agent["maxInstances"]), (1, 4))
        self.assertEqual(agent["telemetry"], {"enabled": True, "contentCapture": "EVENT_ONLY", "envCount": 3,
                                              "secretEnvCount": 1})
        self.assertEqual(agent["playground"], {"supported": True, "method": "async_stream_query", "sessions": True})
        self.assertNotIn("do-not-leak", json.dumps(data))
        self.assertNotIn("BACKEND_API_KEY", json.dumps(data))
        self.assertIn("api:aiplatform.reasoningEngines", meta["sources"])
        status, payload = self.get("", engine=OTHER_ENGINE)
        self.assertEqual(payload["data"]["agent"]["playground"]["supported"], False)
        self.assertIsNone(payload["data"]["agent"]["telemetry"]["enabled"])

    def test_telemetry_switch_reads_explicit_values_only(self):
        def enabled(value):
            env = [{"name": "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY", "value": value}]
            return agent_detail._telemetry_flags({"env": env})["enabled"]
        self.assertEqual([enabled(v) for v in ("true", "TRUE", "1", "false", "False", "0")],
                         [True, True, True, False, False, False])
        # Deployments that do not choose carry "unspecified": that is not "off".
        self.assertEqual([enabled(v) for v in ("unspecified", "", None)], [None, None, None])
        self.assertIsNone(agent_detail._telemetry_flags({})["enabled"])

    # -- telemetry ------------------------------------------------------------------------------
    def test_runtime_metrics(self):
        data, meta = self.data("telemetry", RANGE)
        rt = data["runtime"]
        self.assertEqual(rt["kpis"]["requests"], {"value": 200, "previous": 50})
        self.assertEqual(rt["kpis"]["errors"], 10)
        self.assertEqual(rt["kpis"]["errorRate"], {"value": 0.05, "previous": 0.0})
        self.assertEqual(rt["kpis"]["latencyP50Ms"], 333.3)
        self.assertEqual(rt["kpis"]["latencyP95Ms"], {"value": 800.0, "previous": 195.0})
        self.assertEqual(rt["kpis"]["cpuSeconds"], 150.5)
        self.assertEqual(rt["kpis"]["memoryGibSeconds"], 64.2)
        trend = {s["key"]: s["values"] for s in rt["requestTrend"]["series"]}
        self.assertEqual(rt["requestTrend"]["buckets"][-2:], ["2026-10-18", "2026-10-19"])
        self.assertEqual(trend["ok"][-2:], [90, 100])
        self.assertEqual(trend["server"][-2:], [0, 10])
        self.assertEqual(sum(trend["client"]), 0)
        latency = {s["key"]: s["values"] for s in rt["latencyTrend"]["series"]}
        self.assertEqual(latency["p95"][-1], 800.0)
        self.assertIsNone(latency["p95"][0])  # no samples that day: unknown, not zero
        self.assertEqual(rt["byCode"], [{"code": "200", "class": "ok", "requests": 190},
                                        {"code": "500", "class": "server", "requests": 10}])
        self.assertEqual(rt["byType"], [{"type": "stream_query", "requests": 200}])
        self.assertIn(agent_detail.RUNTIME_LABEL, meta["sources"])
        filters = [p["filter"] for _m, url, p, _b in self.up.calls if url.endswith("/timeSeries")]
        runtime = [f for f in filters if agent_detail.REQUEST_COUNT in f][0]
        self.assertIn('resource.labels.reasoning_engine_id="%s"' % ENGINE, runtime)
        self.assertIn('resource.labels.location="%s"' % REGION, runtime)

    def test_agent_reported_metrics(self):
        data, meta = self.data("telemetry", RANGE)
        ag = data["agent"]
        self.assertIs(ag["reported"], True)
        self.assertEqual(ag["kpis"]["invocations"], {"value": 22, "previous": 4})
        self.assertEqual(ag["kpis"]["errors"], 2)
        self.assertEqual((ag["kpis"]["inputTokens"], ag["kpis"]["outputTokens"]), (10000, 1200))
        self.assertIsNone(ag["kpis"]["turns"])  # no workflow metric stored
        by_agent = {r["agent"]: r for r in ag["byAgent"]}
        self.assertEqual((by_agent["billing_agent"]["invocations"], by_agent["billing_agent"]["errors"]), (12, 2))
        self.assertEqual(by_agent["root_agent"]["invocations"], 10)
        self.assertEqual(by_agent["root_agent"]["p50Ms"], 1833.3)  # bucket [1, 2) s: 1 + 5/6
        self.assertEqual(ag["invocationTrend"]["series"][0]["values"][-1], 22)
        self.assertEqual([s["values"][-1] for s in ag["tokenTrend"]["series"]], [10000, 1200])
        self.assertEqual(data["sessions"], {"count": 2, "users": 2, "truncated": False})
        self.assertEqual(data["telemetry"]["enabled"], True)
        self.assertIn(agent_detail.OTEL_LABEL, meta["sources"])
        calls = [(p["filter"], p["aggregation.perSeriesAligner"]) for _m, url, p, _b in self.up.calls
                 if url.endswith("/timeSeries") and "prometheus.googleapis.com" in p["filter"]]
        self.assertTrue(calls)
        for flt, aligner in calls:
            self.assertIn('resource.type="prometheus_target"', flt)
            self.assertIn('resource.labels.cluster="__vae__"', flt)
            self.assertIn('resource.labels.job="%s"' % ENGINE, flt)
            self.assertEqual(aligner, "ALIGN_SUM" if ":delta" in flt else "ALIGN_DELTA")

    def test_agent_that_reports_no_metrics(self):
        with mock.patch.dict(TIME_SERIES, clear=False):
            for key in [k for k in TIME_SERIES if k.startswith("prometheus.googleapis.com/")]:
                TIME_SERIES[key] = []
            data, meta = self.data("telemetry", RANGE)
            self.assertIs(data["agent"]["reported"], False)
            self.assertIsNone(data["agent"]["byAgent"])
            models, _ = self.data("models", RANGE)
            self.assertIs(models["reported"], False)
            self.assertEqual((models["models"], models["tools"]), ([], []))
        self.assertEqual(meta["warnings"], [])

    def test_sessions_are_counted_when_the_list_is_not_sorted(self):
        # orderBy is accepted but the answer is not newest first: an old session comes before two of the range.
        with mock.patch(__name__ + ".SESSIONS", [SESSIONS[2], SESSIONS[0], SESSIONS[1]]):
            data, _ = self.data("telemetry", RANGE)
        self.assertEqual(data["sessions"], {"count": 2, "users": 2, "truncated": False})

    def test_monitoring_failure_is_a_notice_not_a_zero(self):
        self.up.fail["monitoring.googleapis.com"] = GcpError("denied", status=403)
        status, payload = self.get("telemetry", RANGE)
        self.assertEqual(status, 200)
        self.assertIsNone(payload["data"]["runtime"]["kpis"]["requests"]["value"])
        self.assertIsNone(payload["data"]["runtime"]["requestTrend"])
        self.assertIsNone(payload["data"]["agent"]["reported"])
        self.assertEqual({w["code"] for w in payload["meta"]["warnings"]}, {"access_denied"})
        self.assertEqual(payload["data"]["sessions"]["count"], 2)

    # -- models and tools -----------------------------------------------------------------------
    def test_models_and_tools(self):
        data, _ = self.data("models", RANGE)
        self.assertIs(data["reported"], True)
        k = data["kpis"]
        # 25 calls to flash (20 ok + 5 failed) and 4 to pro; the unlabelled duplicate series is ignored.
        self.assertEqual(k["modelCalls"], {"value": 29, "previous": 5})
        self.assertEqual((k["modelErrors"], k["modelErrorRate"]), (5, round(5 / 29, 4)))
        self.assertEqual((k["inputTokens"], k["outputTokens"]), (10000, 1200))
        self.assertEqual((k["toolCalls"], k["toolErrors"]), (8, 2))
        self.assertEqual(k["inferenceCallsPerInvocation"], 2.5)
        self.assertIsNone(k["toolCallsPerInvocation"])  # metric not stored
        models = {m["model"]: m for m in data["models"]}
        self.assertEqual(sorted(models), ["gemini-3.8-flash", "gemini-3.8-pro"])  # resource path shortened
        flash = models["gemini-3.8-flash"]
        self.assertEqual((flash["calls"], flash["errors"], flash["errorRate"]), (25, 5, 0.2))
        self.assertEqual(flash["errorTypes"], [{"type": "429", "count": 5}])
        self.assertEqual((flash["inputTokens"], flash["outputTokens"]), (10000, 1200))
        self.assertEqual(models["gemini-3.8-pro"]["p50Ms"], 1500.0)  # bucket [1, 2) s
        trend = {s["key"]: s["values"] for s in data["modelTrend"]["series"]}
        self.assertEqual((trend["gemini-3.8-flash"][-1], trend["gemini-3.8-pro"][-2]), (25, 4))
        self.assertEqual(data["tools"], [{"tool": "lookup_invoice", "type": "FunctionTool", "calls": 8, "errors": 2,
                                          "errorRate": 0.25, "p50Ms": 250.0, "p95Ms": 475.0,
                                          "agents": ["billing_agent"]}])

    # -- revisions ------------------------------------------------------------------------------
    def test_revisions_with_latest_and_manual_traffic(self):
        data, meta = self.data("revisions")
        self.assertEqual(data["splitMode"], "latest")
        self.assertEqual(data["kpis"], {"total": 3, "active": 2, "archived": 1})
        rows = {r["id"]: r for r in data["revisions"]}
        self.assertEqual([r["id"] for r in data["revisions"]], ["rev-3", "rev-2", "rev-1"])
        self.assertEqual([(r["traffic"], r["latest"]) for r in data["revisions"]],
                         [(100, True), (0, False), (0, False)])
        self.assertEqual((rows["rev-3"]["pythonVersion"], rows["rev-3"]["maxInstances"], rows["rev-3"]["methods"]),
                         ("3.12", 4, 1))
        self.assertEqual(rows["rev-3"]["telemetry"]["enabled"], True)
        self.assertNotIn("do-not-leak", json.dumps(data))
        self.assertIn(agent_detail.REVISION_LABEL, meta["sources"])
        urls = [url for _m, url, _p, _b in self.up.calls if "runtimeRevisions" in url]
        self.assertEqual(urls, ["https://%s-aiplatform.googleapis.com/v1beta1/projects/%s/locations/%s/"
                                "reasoningEngines/%s/runtimeRevisions" % (REGION, PROJECT, REGION, ENGINE)])
        self.app.cache.clear()
        with mock.patch.object(Upstream, "traffic", {"trafficSplitManual": {"targets": [
                {"runtimeRevisionName": ENGINE_NAME + "/runtimeRevisions/rev-3", "percent": 30},
                {"runtimeRevisionName": "rev-2", "percent": 70}]}}):
            data, _ = self.data("revisions")
        self.assertEqual(data["splitMode"], "manual")
        self.assertEqual([r["traffic"] for r in data["revisions"]], [30, 70, 0])

    def test_revisions_unavailable(self):
        self.up.fail["runtimeRevisions"] = GcpError("denied", status=403)
        status, payload = self.get("revisions")
        self.assertEqual(status, 200)
        self.assertIsNone(payload["data"]["revisions"])
        self.assertEqual([w["code"] for w in payload["meta"]["warnings"]], ["access_denied"])

    # -- traces ---------------------------------------------------------------------------------
    def test_trace_list(self):
        data, meta = self.data("traces", RANGE)
        self.assertEqual([t["traceId"] for t in data["traces"]], [TRACE_A, TRACE_B])
        first = data["traces"][0]
        self.assertEqual((first["name"], first["kind"], first["agent"], first["sessionId"], first["durationMs"]),
                         ("invoke_agent root_agent", "agent", "root_agent", "session-1", 4000.0))
        self.assertEqual(first["subject"], "root_agent")
        self.assertEqual(first["userId"], "alice@example.com")
        self.assertIn(agent_detail.TRACE_LABEL, meta["sources"])
        listed = [p for _m, url, p, _b in self.up.calls if url.endswith("/traces")]
        self.assertEqual(len(listed), 1)
        # Traces of the engine that hold a span of the GenAI conventions: the runtime's own traces stay out.
        self.assertEqual((listed[0]["filter"], listed[0]["view"], listed[0]["orderBy"]),
                         ("+service.name:%s label:gen_ai.operation.name" % ENGINE, "ROOTSPAN", "start desc"))
        self.assertEqual(listed[0]["startTime"], "2026-10-13T00:00:00Z")

    def test_trace_list_falls_back_to_the_model_call_filter(self):
        first, second = agent_detail._trace_filters(agent_detail.Target(PROJECT, REGION, ENGINE, {}))
        self.assertEqual(second, "+service.name:%s label:gen_ai.request.model" % ENGINE)
        self.up.trace_filter_hits = {second}  # spans without gen_ai.operation.name (earlier instrumentation)
        data, _ = self.data("traces", RANGE)
        self.assertEqual(len(data["traces"]), 2)
        self.app.cache.clear("route")
        data, _ = self.data("traces", RANGE + "&granularity=week")  # the working filter is remembered
        filters = [p["filter"] for _m, url, p, _b in self.up.calls if url.endswith("/traces")]
        self.assertEqual(filters, [first, second, second])
        self.up.trace_filter_hits = set()
        self.app.cache.clear()
        self.up.calls.clear()
        data, meta = self.data("traces", RANGE)
        self.assertEqual((data["traces"], meta["warnings"]), ([], []))
        # No agent trace: each filter was asked once.
        filters = [p["filter"] for _m, url, p, _b in self.up.calls if url.endswith("/traces")]
        self.assertEqual(filters, [first, second])

    def test_trace_detail_returns_allowlisted_attributes_only(self):
        data, _ = self.data("trace", "trace=" + TRACE_A)
        tr = data["trace"]
        # call_llm and the generate_content span nested in it are one model call, and both report its usage:
        # two model calls, and 1200 + 300 input tokens (not 2400 + 300).
        # The span for merged tool responses is not a tool call.
        self.assertEqual((tr["spanCount"], tr["durationMs"], tr["agentCalls"], tr["toolCalls"], tr["modelCalls"]),
                         (7, 4000.0, 2, 1, 2))
        self.assertEqual((tr["inputTokens"], tr["outputTokens"], tr["errors"]), (1500, 120, 1))
        self.assertEqual((tr["agents"], tr["sessionId"], tr["userId"]),
                         (["root_agent", "billing_agent"], "session-1", "alice@example.com"))
        self.assertEqual([(s["id"], s["depth"]) for s in tr["spans"]],
                         [("1", 0), ("2", 1), ("3", 2), ("4", 1), ("6", 2), ("5", 3), ("7", 3)])
        tool = tr["spans"][5]
        self.assertEqual((tool["kind"], tool["subject"], tool["error"], tool["offsetMs"], tool["durationMs"]),
                         ("tool", "lookup_invoice", "ValueError", 1500.0, 500.0))
        self.assertEqual(tr["spans"][2]["attributes"], {"operation": "generate_content", "model": "gemini-3.8-flash",
                                                        "inputTokens": 1200, "outputTokens": 80})
        dumped = json.dumps(data)
        self.assertNotIn(SECRET_PROMPT, dumped)
        for key in ("llm_request", "llm_response", "tool_call_args", "gen_ai.input.messages", "labels"):
            self.assertNotIn(key, dumped)

    def test_trace_usage_reported_by_the_nested_model_span_only(self):
        data, _ = self.data("trace", "trace=" + TRACE_C)
        tr = data["trace"]
        self.assertEqual((tr["spanCount"], tr["modelCalls"], tr["inputTokens"], tr["outputTokens"]), (3, 1, 10, 5))

    def test_trace_of_another_workload_is_not_found(self):
        status, payload = self.get("trace", "trace=" + TRACE_FOREIGN)
        self.assertEqual((status, payload["error"]["code"]), (404, "not_found"))
        self.assertNotIn("payroll", json.dumps(payload))
        status, payload = self.get("trace", "trace=" + "d" * 32)
        self.assertEqual((status, payload["error"]["code"]), (404, "not_found"))
        for bad in ("xyz", "A" * 32 + "0", TRACE_A + "/../" + TRACE_FOREIGN):
            status, payload = self.get("trace", "trace=" + urllib.parse.quote(bad, safe=""))
            self.assertEqual((status, payload["error"]["code"]), (400, "bad_request"), bad)

    def test_viewer_masking_hides_user_ids(self):
        # snapshot() hands every request a copy of the effective settings.
        with mock.patch.dict(self.app.settings._effective["security"], {"mask_pii_for_viewers": True}):
            status, payload = self.get("traces", RANGE, identity=VIEWER)
            self.assertEqual(status, 200)
            self.assertIsNone(payload["data"]["traces"][0]["userId"])
            self.assertIs(payload["data"]["traces"][0]["restricted"], True)
            status, payload = self.get("trace", "trace=" + TRACE_A, identity=VIEWER)
            self.assertIsNone(payload["data"]["trace"]["userId"])
            self.assertNotIn("alice@example.com", json.dumps(payload))

    # -- topology -------------------------------------------------------------------------------
    def test_topology_from_sampled_traces(self):
        data, _ = self.data("topology", RANGE)
        nodes = {n["id"]: n for n in data["nodes"]}
        self.assertEqual(data["nodes"][0], {"id": "client", "kind": "client", "name": "Client", "calls": None,
                                            "errors": None, "avgMs": None})
        self.assertEqual(sorted(nodes), ["agent:billing_agent", "agent:root_agent", "client",
                                         "model:gemini-3.8-flash", "model:gemini-3.8-pro", "tool:lookup_invoice"])
        self.assertEqual((nodes["agent:root_agent"]["calls"], nodes["agent:root_agent"]["avgMs"]), (2, 2500.0))
        # call_llm and the nested generate_content span of the SDK are one model call.
        self.assertEqual(nodes["model:gemini-3.8-flash"]["calls"], 1)
        # The tool span sits inside the model span, but the agent is its caller (asserted by the edges below);
        # the span for merged tool responses is not a node.
        self.assertNotIn("tool:(merged tools)", nodes)
        self.assertEqual((nodes["tool:lookup_invoice"]["calls"], nodes["tool:lookup_invoice"]["errors"]), (1, 1))
        edges = {(e["source"], e["target"]): e["calls"] for e in data["edges"]}
        self.assertEqual(edges, {("client", "agent:root_agent"): 2,
                                 ("agent:root_agent", "model:gemini-3.8-flash"): 1,
                                 ("agent:root_agent", "agent:billing_agent"): 1,
                                 ("agent:billing_agent", "tool:lookup_invoice"): 1,
                                 ("agent:billing_agent", "model:gemini-3.8-pro"): 1})
        self.assertEqual(data["sampledTraces"], 2)
        listed = [p for _m, url, p, _b in self.up.calls if url.endswith("/traces")]
        self.assertEqual((listed[0]["view"], listed[0]["pageSize"]), ("COMPLETE", agent_detail.TOPOLOGY_SAMPLE))
        self.assertNotIn(SECRET_PROMPT, json.dumps(data))

    # -- evaluation -----------------------------------------------------------------------------
    def test_evaluation(self):
        data, meta = self.data("evaluation", RANGE)
        self.assertEqual(data["kpis"], {"monitors": 1, "activeMonitors": 1, "evaluations": 12,
                                        "failedEvaluations": 2})
        monitor = data["monitors"][0]
        self.assertEqual((monitor["id"], monitor["name"], monitor["state"], monitor["metrics"]),
                         ("mon-1", "Safety monitor", "ACTIVE", ["safety_v1"]))
        self.assertEqual((monitor["samplingPercent"], monitor["maxSamplesPerRun"], monitor["scope"]), (20, 50, "trace"))
        self.assertEqual(data["scores"], [{"metric": "safety_v1", "average": 0.9, "samples": 10}])
        self.assertEqual(data["scoreTrend"]["series"][0]["values"][-1], 0.9)
        self.assertIsNone(data["scoreTrend"]["series"][0]["values"][0])
        self.assertEqual([r["id"] for r in data["runs"]], ["run-1"])  # run-2 names no agent
        run = data["runs"][0]
        self.assertEqual((run["state"], run["totalItems"], run["failedItems"]), ("SUCCEEDED", 20, 1))
        self.assertEqual(run["metrics"], [{"name": "safety_v1/AVERAGE", "value": 0.95}])
        for label in (agent_detail.EVALUATOR_LABEL, agent_detail.RUNS_LABEL, agent_detail.EVAL_LABEL):
            self.assertIn(label, meta["sources"])

    # -- playground -----------------------------------------------------------------------------
    def play(self, identity=None, csrf=True, **body):
        payload = {"project": PROJECT, "region": REGION, "engine": ENGINE, "message": SECRET_PROMPT}
        payload.update(body)
        return self.call("POST", "/api/v1/agent-platform/agent/playground", payload, identity=identity, csrf=csrf)

    def test_playground_runs_as_the_signed_in_administrator_and_is_audited(self):
        status, payload = self.play()
        self.assertEqual(status, 200, payload)
        data = payload["data"]
        self.assertEqual((data["sessionId"], data["userId"], data["truncated"], data["auditLogged"]),
                         ("session-new", "admin@example.com", False, True))
        self.assertEqual([[p["type"] for p in e["parts"]] for e in data["events"]], [["call"], ["result"], ["text"]])
        self.assertEqual(data["events"][0]["parts"][0], {"type": "call", "name": "lookup_invoice",
                                                         "text": '{"invoice": "A-1"}'})
        self.assertEqual(data["events"][2]["parts"][0]["text"], "Invoice A-1 is paid.")
        base = "https://%s-aiplatform.googleapis.com/v1/projects/%s/locations/%s/reasoningEngines/%s" % (
            REGION, PROJECT, REGION, ENGINE)
        created = [(url, body) for m, url, _p, body in self.up.calls if url.endswith(":query")]
        self.assertEqual(created, [(base + ":query", {"class_method": "async_create_session",
                                                      "input": {"user_id": "admin@example.com"}})])
        self.assertEqual(self.up.streams, [("POST", base + ":streamQuery", {
            "class_method": "async_stream_query",
            "input": {"user_id": "admin@example.com", "message": SECRET_PROMPT, "session_id": "session-new"}})])
        self.assertEqual(len(self.up.audit), 1)
        entry = self.up.audit[0]
        self.assertEqual((entry["action"], entry["actor"], entry["result"], entry["tool"]),
                         ("agent_playground_query", "admin@example.com", "success", "control-plane"))
        self.assertEqual((entry["target"], entry["sessionId"], entry["messageChars"], entry["events"]),
                         (base.split("/v1/")[1], "session-new", len(SECRET_PROMPT), 3))
        self.assertNotIn(SECRET_PROMPT, json.dumps(self.up.audit))
        # A second message continues the session and creates no new one.
        status, payload = self.play(sessionId="session-new", message="and the next one?")
        self.assertEqual(payload["data"]["sessionId"], "session-new")
        self.assertEqual(len([1 for _m, url, _p, _b in self.up.calls if url.endswith(":query")]), 1)

    def test_playground_is_for_administrators_only(self):
        status, payload = self.play(identity=VIEWER)
        self.assertEqual((status, payload["error"]["code"]), (403, "forbidden"))
        status, payload = self.play(csrf=False)
        self.assertEqual((status, payload["error"]["code"]), (403, "csrf_failed"))
        guest = Identity("", "viewer", "guest", "guest:198.51.100.7")
        status, payload = self.play(identity=guest)
        self.assertEqual(status, 403)
        status, payload = self.call("GET", "/api/v1/agent-platform/agent/playground?project=%s&region=%s&engine=%s"
                                    % (PROJECT, REGION, ENGINE))
        self.assertEqual(status, 405)
        self.assertEqual((self.up.streams, self.up.audit), ([], []))

    def test_playground_validation(self):
        for body in ({"message": ""}, {"message": "   "}, {"message": "x" * 4001}, {"message": 5},
                     {"sessionId": "bad id!"}, {"sessionId": 7}, {"engine": OTHER_ENGINE}):
            status, payload = self.play(**body)
            self.assertEqual((status, payload["error"]["code"]), (400, "bad_request"), body)
        status, payload = self.play(engine="777")
        self.assertEqual((status, payload["error"]["code"]), (404, "not_found"))
        status, payload = self.play(region="europe-west4")
        self.assertEqual((status, payload["error"]["code"]), (400, "bad_request"))
        self.assertEqual((self.up.streams, self.up.audit), ([], []))

    def test_playground_failure_is_audited_and_reported_without_upstream_text(self):
        self.up.fail[":streamQuery"] = GcpError("denied", status=403, message="caller lacks permission XYZ")
        status, payload = self.play()
        self.assertEqual((status, payload["error"]["code"]), (502, "upstream_error"))
        self.assertEqual(payload["error"]["message"], "Access to the agent was denied for the service identity.")
        self.assertNotIn("XYZ", json.dumps(payload))
        self.assertEqual([(e["action"], e["result"]) for e in self.up.audit], [("agent_playground_query", "failed")])

    def test_playground_is_audited_even_when_the_answer_breaks_the_code(self):
        # The agent may have acted before anything failed: the call is audited, and the error still reaches the caller.
        def broken(method, url, **kwargs):
            self.up.streams.append((method, url, kwargs.get("json_body")))
            raise RuntimeError("unexpected answer")
        self.app.gcp.stream_lines = broken
        with self.assertLogs("controlplane.http", level="ERROR"):
            status, payload = self.play()
        self.assertEqual((status, payload["error"]["code"]), (500, "internal"))
        self.assertEqual(len(self.up.streams), 1)
        self.assertEqual([(e["action"], e["result"], e["events"]) for e in self.up.audit],
                         [("agent_playground_query", "failed", 0)])
        self.assertNotIn(SECRET_PROMPT, json.dumps(self.up.audit))

    def test_playground_tolerates_odd_session_replies_and_event_shapes(self):
        request = self.up.request

        def odd_session(method, url, **kwargs):
            if url.endswith(":query"):
                self.up.calls.append((method, url, {}, kwargs.get("json_body")))
                return ["not", "an", "object"], {}
            return request(method, url, **kwargs)
        self.app.gcp.request = odd_session
        status, payload = self.play()
        self.assertEqual(status, 200, payload)
        self.assertIsNone(payload["data"]["sessionId"])
        self.assertNotIn("session_id", self.up.streams[0][2]["input"])
        self.assertEqual([(e["action"], e["result"]) for e in self.up.audit], [("agent_playground_query", "success")])
        # Parts that are not a list and JSON nested deeper than the parser allows are skipped, not fatal.
        lines = [json.dumps({"author": "a", "content": {"parts": "text, not a list"}}),
                 json.dumps({"author": "a", "content": {"parts": {"text": "a dict"}}}),
                 "data: " + "[" * 100000 + "]" * 100000,
                 json.dumps({"author": "a", "content": {"parts": [{"text": "ok"}]}})]
        events = agent_detail._parse_events(lines)  # pylint: disable=protected-access
        self.assertEqual([[p["text"] for p in e["parts"]] for e in events], [["ok"]])

    def test_playground_is_rate_limited(self):
        self.app.limiter = app_module.RateLimiter(dict(app_module.RateLimiter().limits, ai=2))
        statuses = [self.play(sessionId="session-new")[0] for _ in range(3)]
        self.assertEqual(statuses, [200, 200, 429])


if __name__ == "__main__":
    unittest.main()
