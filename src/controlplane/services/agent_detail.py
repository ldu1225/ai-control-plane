"""Per-agent views of an Agent Engine deployment: telemetry, models and tools, revisions, traces, topology,
evaluation and the playground.

Sources, all read with the control plane's own identity:

- Cloud Monitoring: the built-in runtime metrics (aiplatform.googleapis.com/reasoning_engine/*), the
  OpenTelemetry metrics an agent reports itself and online evaluation scores. Metrics sent through the Telemetry
  (OTLP) API are stored as prometheus.googleapis.com/{name}/{suffix} on the prometheus_target resource; an Agent
  Engine deployment gets cluster "__vae__" and job = the engine id (its OpenTelemetry service.name).
- Gemini Platform API: runtime revisions and traffic (v1beta1), online evaluators, evaluation runs and sessions
  (v1), and query / streamQuery for the playground.
- Cloud Trace API v1: the traces an agent exports.

Every endpoint resolves its agent against the engines this workspace lists (target), so the control plane's
identity is never pointed at a project, region or engine the workspace does not already show. Span attributes
pass through an allowlist: prompts, responses and tool payloads are never returned by the read endpoints.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time

from .. import config, timeutil
from ..gcp import GcpError
from ..httpkit import ApiError
from . import agents, governance, monitoring
from .base import describe_error, iso, parse_rfc3339, ratio, to_int
from .monitoring import Distribution

LOG = logging.getLogger("controlplane.agent_detail")

ENGINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,62}$")
TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")

RUNTIME_PREFIX = "aiplatform.googleapis.com/reasoning_engine/"
REQUEST_COUNT = RUNTIME_PREFIX + "request_count"
REQUEST_LATENCIES = RUNTIME_PREFIX + "request_latencies"
CPU_ALLOCATION = RUNTIME_PREFIX + "cpu/allocation_time"
MEMORY_ALLOCATION = RUNTIME_PREFIX + "memory/allocation_time"
EVAL_COUNT = "aiplatform.googleapis.com/online_evaluator/evaluations_count"
EVAL_SCORES = "aiplatform.googleapis.com/online_evaluator/scores"

OTEL_PREFIX = "prometheus.googleapis.com/gen_ai."
OTEL_CLUSTER = "__vae__"
# Logical name -> OpenTelemetry instrument reported by the Agent Development Kit (GenAI semantic conventions).
OTEL_METRICS = {
    "agent": "gen_ai.invoke_agent.duration",
    "workflow": "gen_ai.invoke_workflow.duration",
    "tool": "gen_ai.execute_tool.duration",
    "model": "gen_ai.client.operation.duration",
    "tokens": "gen_ai.client.token.usage",
    "inference_calls": "gen_ai.invoke_agent.inference_calls",
    "tool_calls": "gen_ai.invoke_agent.tool_calls",
}
# Suffix by OTLP point kind and temporality; the aligner depends on the resulting metric kind.
OTEL_SUFFIXES = (("histogram", "ALIGN_DELTA"), ("histogram:delta", "ALIGN_SUM"))

RUNTIME_LABEL = "monitoring:aiplatform.googleapis.com/reasoning_engine"
OTEL_LABEL = "monitoring:prometheus.googleapis.com/gen_ai"
EVAL_LABEL = "monitoring:aiplatform.googleapis.com/online_evaluator"
TRACE_LABEL = "api:cloudtrace.traces"
REVISION_LABEL = "api:aiplatform.runtimeRevisions"
EVALUATOR_LABEL = "api:aiplatform.onlineEvaluators"
RUNS_LABEL = "api:aiplatform.evaluationRuns"
SESSION_LABEL = "api:aiplatform.sessions"

TRACE_URL = "https://cloudtrace.googleapis.com/v1/projects/%s/traces"
TRACE_LIST_LIMIT = 100
TOPOLOGY_SAMPLE = 25
MAX_SPANS = 500
SESSION_PAGES = 10
TREND_SERIES = 6

PLAYGROUND_MAX_CHARS = 4000
PLAYGROUND_MAX_EVENTS = 200
PLAYGROUND_TEXT_CHARS = 20000
PLAYGROUND_JSON_CHARS = 2000
PLAYGROUND_DEADLINE_SECONDS = 110
STREAM_METHODS = ("async_stream_query", "stream_query")
SESSION_METHODS = ("async_create_session", "create_session")

# Span attribute -> response key. Everything else (prompts, responses, tool arguments and results, system
# instructions) stays on the server.
SPAN_ATTRIBUTES = (
    ("gen_ai.operation.name", "operation"), ("gen_ai.agent.name", "agent"), ("gen_ai.tool.name", "tool"),
    ("gen_ai.tool.type", "toolType"), ("gen_ai.request.model", "model"), ("gen_ai.response.model", "responseModel"),
    ("gen_ai.usage.input_tokens", "inputTokens"), ("gen_ai.usage.output_tokens", "outputTokens"),
    ("gen_ai.response.finish_reasons", "finishReason"), ("gen_ai.conversation.id", "sessionId"),
    ("gcp.vertex.agent.session_id", "sessionId"), ("gcp.vertex.agent.invocation_id", "invocationId"),
    ("service.version", "revision"), ("user.id", "userId"),
)
TOKEN_KEYS = ("inputTokens", "outputTokens")


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------

class Target:
    """One Agent Engine deployment that this workspace lists."""

    __slots__ = ("project", "region", "engine_id", "engine", "name")

    def __init__(self, project, region, engine_id, engine):
        self.project = project
        self.region = region
        self.engine_id = engine_id
        self.engine = engine
        # Rebuilt from validated parts; the listed name can carry the project number instead of the id.
        self.name = "projects/%s/locations/%s/reasoningEngines/%s" % (project, region, engine_id)


def _arg(ctx, key):
    body = ctx.body if isinstance(ctx.body, dict) else {}
    value = body[key] if key in body else ctx.params.get(key)
    return value.strip() if isinstance(value, str) else ""


def target(ctx):
    """Resolves project, region and engine (query parameters, or body fields of a POST) to a listed engine.

    The three identifiers are validated before use: they become parts of host names, URL paths and Monitoring
    filters. The engine must then appear in the inventory of a project connected to this workspace."""
    project = _arg(ctx, "project").lower()
    region = _arg(ctx, "region").lower()
    engine_id = _arg(ctx, "engine")
    if not config.valid_project(project):
        raise ApiError("bad_request", "Parameter 'project' must be a Google Cloud project ID.")
    if not config.LOCATION_RE.match(region) or region not in (ctx.ws.get("agent_engine_regions") or []):
        raise ApiError("bad_request", "Parameter 'region' must be one of the configured Agent Engine regions.")
    if not ENGINE_ID_RE.match(engine_id):
        raise ApiError("bad_request", "Parameter 'engine' must be an Agent Engine resource ID.")
    ctx.params["project"] = project  # scopes the inventory (and its cache entry) to this project
    if project not in ctx.all_projects:
        raise ApiError("bad_request", "Parameter 'project' must be a project connected to this workspace.")
    engines, _ok = ctx.memo("reasoning_engines", "all", agents.reasoning_engines, ttl=300)
    for listed_region, engine in engines:
        if listed_region == region and (engine.get("_project") or ctx.project) == project and \
                str(engine.get("name") or "").rsplit("/", 1)[-1] == engine_id:
            return Target(project, region, engine_id, engine)
    raise ApiError("not_found", "The agent was not found in the configured Agent Engine regions.")


def _host(region):
    return "https://%s-aiplatform.googleapis.com" % region


def _same_engine(resource_name, t):
    """True when an API resource name (with the project id or the project number) refers to the target engine."""
    return isinstance(resource_name, str) and resource_name.endswith(
        "/locations/%s/reasoningEngines/%s" % (t.region, t.engine_id))


def _num(value, digits=1):
    return None if value is None else round(float(value), digits)


def _ms(seconds):
    return None if seconds is None else round(float(seconds) * 1000.0, 1)


def _label(labels, key):
    """Label by OpenTelemetry attribute name. The Telemetry API keeps the dots of attribute names in label keys;
    the underscore spelling of Prometheus exporters is read as well."""
    value = labels.get(key)
    if value is None:
        value = labels.get(key.replace(".", "_"))
    return value if isinstance(value, str) and value else None


def _short_model(model):
    return str(model).rsplit("/", 1)[-1][:120] if model else None


# ---------------------------------------------------------------------------
# GET /api/v1/agent-platform/agent
# ---------------------------------------------------------------------------

def _method_names(engine):
    spec = engine.get("spec") or {}
    return [m["name"] for m in spec.get("classMethods") or []
            if isinstance(m, dict) and isinstance(m.get("name"), str)]


def _switch(raw):
    """True or False for an explicit on/off value. None when the variable is absent or holds anything else:
    deployments that do not choose carry the value "unspecified", which leaves the choice to the platform."""
    text = str(raw).strip().lower() if raw is not None else ""
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    return None


def _telemetry_flags(deployment_spec):
    """Telemetry switches of a deployment, read from two well-known environment variables. No other variable
    name and no other value leaves the server."""
    spec = deployment_spec if isinstance(deployment_spec, dict) else {}
    env = {}
    for item in spec.get("env") or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            env[item["name"]] = item.get("value")
    capture = env.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT")
    return {"enabled": _switch(env.get("GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY")),
            "contentCapture": str(capture)[:40] if capture not in (None, "") else None,
            "envCount": len(env), "secretEnvCount": len(spec.get("secretEnv") or [])}


def _instances(deployment_spec, key):
    value = (deployment_spec or {}).get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _summary(t):
    engine = t.engine
    spec = engine.get("spec") or {}
    dep = spec.get("deploymentSpec") or {}
    methods = _method_names(engine)
    stream_method = next((m for m in STREAM_METHODS if m in methods), None)
    return {"id": t.engine_id, "name": engine.get("displayName") or t.engine_id,
            "description": engine.get("description") or "", "project": t.project, "region": t.region,
            "resourceName": t.name, "framework": spec.get("agentFramework") or None,
            "pythonVersion": (spec.get("packageSpec") or {}).get("pythonVersion") or None,
            "identity": spec.get("effectiveIdentity") or spec.get("serviceAccount") or None,
            "identityType": spec.get("identityType") or None,
            "minInstances": _instances(dep, "minInstances"), "maxInstances": _instances(dep, "maxInstances"),
            "createTime": iso(parse_rfc3339(engine.get("createTime"))),
            "updateTime": iso(parse_rfc3339(engine.get("updateTime"))),
            "methods": methods[:40], "telemetry": _telemetry_flags(dep),
            "playground": {"supported": stream_method is not None, "method": stream_method,
                           "sessions": any(m in methods for m in SESSION_METHODS)}}


def overview(ctx):
    return {"agent": _summary(target(ctx))}


# ---------------------------------------------------------------------------
# GET /api/v1/agent-platform/agent/telemetry
# ---------------------------------------------------------------------------

def _runtime_filter(t, metric):
    return ('metric.type="%s" AND resource.type="aiplatform.googleapis.com/ReasoningEngine" AND '
            'resource.labels.reasoning_engine_id="%s" AND resource.labels.location="%s"'
            % (metric, t.engine_id, t.region))


def _code_class(code, label):
    text = str(label or code or "")
    if text.startswith("4"):
        return "client"
    if text.startswith("5"):
        return "server"
    return "ok"


def _percentile_series(rng, buckets, scale=1.0):
    out = []
    for key, q in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99)):
        values = [_num(d.percentile(q) * scale) if d.count else None for d in buckets]
        out.append(timeutil.series(key, key, values))
    return timeutil.timeline(rng, out)


def _runtime(ctx, t, rng):
    """Built-in Agent Runtime metrics: requests by response code, request latency, CPU and memory allocation."""
    out = {"kpis": {"requests": {"value": None, "previous": None}, "errors": None,
                    "errorRate": {"value": None, "previous": None}, "latencyP50Ms": None,
                    "latencyP95Ms": {"value": None, "previous": None}, "latencyP99Ms": None,
                    "cpuSeconds": None, "memoryGibSeconds": None},
           "requestTrend": None, "latencyTrend": None, "resourceTrend": None, "byCode": None, "byType": None}
    prev = rng.previous()
    start, end = prev.start_ts(), rng.end_ts()

    def read(metric):
        return monitoring.aligned_series(ctx, t.project, _runtime_filter(t, metric), start, end, "ALIGN_SUM")

    res = ctx.run({"requests": lambda: read(REQUEST_COUNT), "latency": lambda: read(REQUEST_LATENCIES),
                   "cpu": lambda: read(CPU_ALLOCATION), "memory": lambda: read(MEMORY_ALLOCATION)})
    kpis = out["kpis"]
    ok, series = res["requests"]
    if ok:
        ctx.add_source(RUNTIME_LABEL)
        total = errors = prev_total = prev_errors = 0
        classes = {"ok": [], "client": [], "server": []}
        by_code, by_type = {}, {}
        for item in series:
            code = str(item["metric"].get("response_code") or "")
            klass = _code_class(code, item["metric"].get("response_code_class"))
            kind = str(item["metric"].get("type") or "")
            for day, value in item["points"]:
                n = to_int(value)
                if n <= 0:
                    continue
                if rng.contains(day):
                    total += n
                    errors += n if klass != "ok" else 0
                    classes[klass].append((day, n))
                    by_code[code] = by_code.get(code, 0) + n
                    by_type[kind] = by_type.get(kind, 0) + n
                elif prev.contains(day):
                    prev_total += n
                    prev_errors += n if klass != "ok" else 0
        kpis["requests"] = {"value": total, "previous": prev_total}
        kpis["errors"] = errors
        kpis["errorRate"] = {"value": ratio(errors, total), "previous": ratio(prev_errors, prev_total)}
        out["requestTrend"] = timeutil.timeline(rng, [
            timeutil.series("ok", "Successful", timeutil.fold(rng, classes["ok"], int)),
            timeutil.series("client", "Client errors (4xx)", timeutil.fold(rng, classes["client"], int)),
            timeutil.series("server", "Server errors (5xx)", timeutil.fold(rng, classes["server"], int))])
        out["byCode"] = sorted(({"code": code or None, "class": _code_class(code, None), "requests": n}
                                for code, n in by_code.items()), key=lambda r: (-r["requests"], r["code"] or ""))
        out["byType"] = sorted(({"type": kind or None, "requests": n} for kind, n in by_type.items()),
                               key=lambda r: (-r["requests"], r["type"] or ""))
    else:
        ctx.warn_error(RUNTIME_LABEL, series, "Cloud Monitoring runtime metrics")
    ok, series = res["latency"]
    if ok:
        ctx.add_source(RUNTIME_LABEL)
        cur, before = Distribution(), Distribution()
        buckets = [Distribution() for _ in rng.buckets()]
        for item in series:
            for day, value in item["points"]:
                if rng.contains(day):
                    cur.add(value)
                    buckets[rng.bucket_index(day)].add(value)
                elif prev.contains(day):
                    before.add(value)
        kpis["latencyP50Ms"] = _num(cur.percentile(0.5))
        kpis["latencyP95Ms"] = {"value": _num(cur.percentile(0.95)), "previous": _num(before.percentile(0.95))}
        kpis["latencyP99Ms"] = _num(cur.percentile(0.99))
        out["latencyTrend"] = _percentile_series(rng, buckets)
    else:
        ctx.warn_error(RUNTIME_LABEL, series, "Cloud Monitoring runtime metrics")
    usage = {}
    for key in ("cpu", "memory"):
        ok, series = res[key]
        if not ok:
            ctx.warn_error(RUNTIME_LABEL, series, "Cloud Monitoring runtime metrics")
            continue
        ctx.add_source(RUNTIME_LABEL)
        usage[key] = [(day, float(value)) for item in series for day, value in item["points"]
                      if rng.contains(day) and isinstance(value, (int, float))]
    if "cpu" in usage:
        kpis["cpuSeconds"] = _num(sum(v for _, v in usage["cpu"]))
    if "memory" in usage:
        kpis["memoryGibSeconds"] = _num(sum(v for _, v in usage["memory"]))
    if usage:
        out["resourceTrend"] = timeutil.timeline(rng, [
            timeutil.series(key, name, [_num(v, 2) for v in timeutil.fold(rng, usage[key])])
            for key, name in (("cpu", "CPU allocation (vCPU-seconds)"), ("memory", "Memory allocation (GiB-seconds)"))
            if key in usage])
    return out


def _otel_types(ctx, t):
    """{logical name: [(metric type, aligner)]} for the agent-reported metrics stored in the engine's project."""
    known = ctx.memo("otel_metric_types", t.project,
                     lambda child: monitoring.metric_types(child, t.project, OTEL_PREFIX), ttl=300)
    out = {}
    for logical, name in OTEL_METRICS.items():
        found = []
        for suffix, aligner in OTEL_SUFFIXES:
            metric_type = "prometheus.googleapis.com/%s/%s" % (name, suffix)
            if metric_type in known:
                found.append((metric_type, aligner))
        out[logical] = found
    return out


def _otel_read(ctx, t, variants, start, end):
    """Series of one agent-reported metric for the target engine, over every stored variant."""
    out = []
    for metric_type, aligner in variants:
        flt = ('metric.type="%s" AND resource.type="prometheus_target" AND resource.labels.cluster="%s" AND '
               'resource.labels.job="%s"' % (metric_type, OTEL_CLUSTER, t.engine_id))
        out.extend(monitoring.aligned_series(ctx, t.project, flt, start, end, aligner))
    return out


def _agent_scoped(series):
    """Model metrics can be reported twice: by the Agent Development Kit (labelled with the agent name) and by
    the model SDK instrumentation (no agent name). The labelled series win when both exist."""
    scoped = [item for item in series if _label(item["metric"], "gen_ai.agent.name")]
    return scoped or series


def _otel_run(ctx, t, types, names, start, end):
    """Reads the named logical metrics in parallel. Returns ({name: series}, read_any, failed)."""
    tasks = {name: (lambda n=name: _otel_read(ctx, t, types[n], start, end)) for name in names if types.get(name)}
    data, failed = {}, False
    for name, (ok, value) in ctx.run(tasks).items():
        if ok:
            data[name] = value
        else:
            failed = True
            ctx.warn_error(OTEL_LABEL, value, "Cloud Monitoring agent metrics")
    if data:
        ctx.add_source(OTEL_LABEL)
    return data, bool(tasks), failed


def _agent_reported(ctx, t, rng):
    """Metrics the agent reports through OpenTelemetry: invocations and latency per agent, and token usage."""
    out = {"reported": None,
           "kpis": {"invocations": {"value": None, "previous": None}, "errors": None, "agentLatencyP95Ms": None,
                    "turns": None, "turnLatencyP50Ms": None, "turnLatencyP95Ms": None, "inputTokens": None,
                    "outputTokens": None},
           "invocationTrend": None, "latencyTrend": None, "tokenTrend": None, "byAgent": None}
    try:
        types = _otel_types(ctx, t)
    except GcpError as err:
        ctx.warn_error(OTEL_LABEL, err, "Cloud Monitoring agent metrics")
        return out
    prev = rng.previous()
    data, asked, failed = _otel_run(ctx, t, types, ("agent", "workflow", "tokens"), prev.start_ts(), rng.end_ts())
    if not asked or (not failed and not any(data.values())):
        out["reported"] = False
        return out
    if not data:
        return out
    out["reported"] = True
    kpis = out["kpis"]
    if "agent" in data:
        total, prev_count, errors = Distribution(), 0, 0
        buckets = [Distribution() for _ in rng.buckets()]
        by_agent = {}
        for item in data["agent"]:
            name = _label(item["metric"], "gen_ai.agent.name") or "unknown"
            failed_series = bool(_label(item["metric"], "error.type"))
            entry = by_agent.setdefault(name, {"dist": Distribution(), "errors": 0})
            for day, value in item["points"]:
                dist = Distribution().add(value)
                if not dist.count:
                    continue
                if rng.contains(day):
                    total.merge(dist)
                    entry["dist"].merge(dist)
                    buckets[rng.bucket_index(day)].merge(dist)
                    if failed_series:
                        errors += dist.count
                        entry["errors"] += dist.count
                elif prev.contains(day):
                    prev_count += dist.count
        kpis["invocations"] = {"value": total.count, "previous": prev_count}
        kpis["errors"] = errors
        kpis["agentLatencyP95Ms"] = _ms(total.percentile(0.95))
        out["invocationTrend"] = timeutil.timeline(rng, [
            timeutil.series("invocations", "Agent invocations", [d.count for d in buckets])])
        out["latencyTrend"] = _percentile_series(rng, buckets, 1000.0)
        out["byAgent"] = sorted(({"agent": name, "invocations": e["dist"].count, "errors": e["errors"],
                                  "p50Ms": _ms(e["dist"].percentile(0.5)), "p95Ms": _ms(e["dist"].percentile(0.95))}
                                 for name, e in by_agent.items() if e["dist"].count),
                                key=lambda r: (-r["invocations"], r["agent"]))
    if "workflow" in data:
        turns = Distribution()
        buckets = [Distribution() for _ in rng.buckets()]
        for item in data["workflow"]:
            for day, value in item["points"]:
                if rng.contains(day):
                    turns.add(value)
                    buckets[rng.bucket_index(day)].add(value)
        kpis["turns"] = turns.count
        kpis["turnLatencyP50Ms"] = _ms(turns.percentile(0.5))
        kpis["turnLatencyP95Ms"] = _ms(turns.percentile(0.95))
        if turns.count:
            out["latencyTrend"] = _percentile_series(rng, buckets, 1000.0)
    if "tokens" in data:
        pairs = {"input": [], "output": []}
        for item in _agent_scoped(data["tokens"]):
            kind = (_label(item["metric"], "gen_ai.token.type") or "").lower()
            if kind not in pairs:
                continue
            for day, value in item["points"]:
                if rng.contains(day):
                    pairs[kind].append((day, Distribution().add(value).total))
        kpis["inputTokens"] = int(round(sum(v for _, v in pairs["input"])))
        kpis["outputTokens"] = int(round(sum(v for _, v in pairs["output"])))
        out["tokenTrend"] = timeutil.timeline(rng, [
            timeutil.series("input", "Input tokens", [int(round(v)) for v in timeutil.fold(rng, pairs["input"])]),
            timeutil.series("output", "Output tokens", [int(round(v)) for v in timeutil.fold(rng, pairs["output"])])])
    return out


def _session_page(ctx, url, token, ordered):
    params = {"pageSize": 100}
    if ordered:
        params["orderBy"] = "update_time desc"
    if token:
        params["pageToken"] = token
    return ctx.rest("GET", url, params=params, timeout=30)


def _sessions(ctx, t, rng):
    """Sessions stored in Gemini Platform Sessions that were active in the range (count only).

    The list is asked for newest first, which lets the scan stop at the first page that reaches sessions older
    than the range. The order is checked rather than trusted: a session of the range that follows an older one
    means the list is not sorted, and the scan then reads every page (up to SESSION_PAGES)."""
    url = "%s/v1/%s/sessions" % (_host(t.region), t.name)
    start, end = rng.start_ts(), rng.end_ts()
    ordered = True
    count, users, token, pages, more = 0, set(), None, 0, False
    while pages < SESSION_PAGES:
        try:
            payload = _session_page(ctx, url, token, ordered)
        except GcpError as err:
            if ordered and pages == 0 and err.kind == "invalid":
                ordered = False  # the list does not take orderBy: scan without the early stop
                continue
            raise
        pages += 1
        older = False
        for item in payload.get("sessions") or []:
            if not isinstance(item, dict):
                continue
            updated = parse_rfc3339(item.get("updateTime")) or parse_rfc3339(item.get("createTime"))
            if updated is None:
                continue
            if updated < start:
                older = True
                continue
            if older:
                ordered = False
            if updated < end:
                count += 1
                if item.get("userId"):
                    users.add(item["userId"])
        token = payload.get("nextPageToken")
        more = bool(token)
        if not token or (ordered and older):
            more = False
            break
    return {"count": count, "users": len(users), "truncated": more}


def telemetry(ctx):
    t = target(ctx)
    rng = ctx.range
    res = ctx.run_composite({"runtime": lambda: _runtime(ctx, t, rng),
                             "agent": lambda: _agent_reported(ctx, t, rng),
                             "sessions": lambda: _sessions(ctx, t, rng)})
    out = {"runtime": None, "agent": None, "sessions": None, "telemetry": _telemetry_flags(
        (t.engine.get("spec") or {}).get("deploymentSpec"))}
    for key, label, what in (("runtime", RUNTIME_LABEL, "Cloud Monitoring runtime metrics"),
                             ("agent", OTEL_LABEL, "Cloud Monitoring agent metrics"),
                             ("sessions", SESSION_LABEL, "Gemini Platform Sessions")):
        ok, value = res[key]
        if ok:
            out[key] = value
            if key == "sessions":
                ctx.add_source(SESSION_LABEL)
        else:
            ctx.warn_error(label, value, what)
    return out


# ---------------------------------------------------------------------------
# GET /api/v1/agent-platform/agent/models
# ---------------------------------------------------------------------------

def models_tools(ctx):
    """Model and tool calls the agent reports through OpenTelemetry."""
    t = target(ctx)
    rng = ctx.range
    out = {"reported": None,
           "kpis": {"modelCalls": {"value": None, "previous": None}, "modelErrors": None, "modelErrorRate": None,
                    "modelLatencyP95Ms": None, "inputTokens": None, "outputTokens": None, "toolCalls": None,
                    "toolErrors": None, "inferenceCallsPerInvocation": None, "toolCallsPerInvocation": None},
           "models": None, "modelTrend": None, "tools": None}
    try:
        types = _otel_types(ctx, t)
    except GcpError as err:
        ctx.warn_error(OTEL_LABEL, err, "Cloud Monitoring agent metrics")
        return out
    prev = rng.previous()
    names = ("model", "tokens", "tool", "inference_calls", "tool_calls")
    data, asked, failed = _otel_run(ctx, t, types, names, prev.start_ts(), rng.end_ts())
    if not asked or (not failed and not any(data.values())):
        out["reported"] = False
        out["models"], out["tools"] = [], []
        return out
    if not data:
        return out
    out["reported"] = True
    kpis = out["kpis"]
    models = {}

    def model_entry(labels):
        key = _short_model(_label(labels, "gen_ai.response.model") or _label(labels, "gen_ai.request.model")) \
            or "unknown"
        return models.setdefault(key, {"dist": Distribution(), "errors": 0, "errorTypes": {}, "input": 0.0,
                                       "output": 0.0, "days": []})

    if "model" in data:
        total, prev_calls, errors = Distribution(), 0, 0
        for item in _agent_scoped(data["model"]):
            entry = model_entry(item["metric"])
            error_type = _label(item["metric"], "error.type")
            for day, value in item["points"]:
                dist = Distribution().add(value)
                if not dist.count:
                    continue
                if rng.contains(day):
                    total.merge(dist)
                    entry["dist"].merge(dist)
                    entry["days"].append((day, dist.count))
                    if error_type:
                        errors += dist.count
                        entry["errors"] += dist.count
                        entry["errorTypes"][error_type] = entry["errorTypes"].get(error_type, 0) + dist.count
                elif prev.contains(day):
                    prev_calls += dist.count
        kpis["modelCalls"] = {"value": total.count, "previous": prev_calls}
        kpis["modelErrors"] = errors
        kpis["modelErrorRate"] = ratio(errors, total.count)
        kpis["modelLatencyP95Ms"] = _ms(total.percentile(0.95))
    if "tokens" in data:
        totals = {"input": 0.0, "output": 0.0}
        for item in _agent_scoped(data["tokens"]):
            kind = (_label(item["metric"], "gen_ai.token.type") or "").lower()
            if kind not in totals:
                continue
            entry = model_entry(item["metric"])
            for day, value in item["points"]:
                if rng.contains(day):
                    tokens = Distribution().add(value).total
                    totals[kind] += tokens
                    entry[kind] += tokens
        kpis["inputTokens"] = int(round(totals["input"]))
        kpis["outputTokens"] = int(round(totals["output"]))
    if "model" in data or "tokens" in data:
        rows = []
        for key, entry in models.items():
            calls = entry["dist"].count
            if not calls and not entry["input"] and not entry["output"]:
                continue
            rows.append({"model": key, "calls": calls if "model" in data else None,
                         "errors": entry["errors"] if "model" in data else None,
                         "errorRate": ratio(entry["errors"], calls) if "model" in data else None,
                         "p50Ms": _ms(entry["dist"].percentile(0.5)), "p95Ms": _ms(entry["dist"].percentile(0.95)),
                         "inputTokens": int(round(entry["input"])) if "tokens" in data else None,
                         "outputTokens": int(round(entry["output"])) if "tokens" in data else None,
                         "errorTypes": sorted(({"type": k[:120], "count": n} for k, n in entry["errorTypes"].items()),
                                              key=lambda e: (-e["count"], e["type"]))[:8]})
        rows.sort(key=lambda r: (-(r["calls"] or 0), r["model"]))
        out["models"] = rows
        if "model" in data:
            top = [r["model"] for r in rows if r["calls"]][:TREND_SERIES]
            series = [timeutil.series(key, key, timeutil.fold(rng, models[key]["days"], int)) for key in top]
            rest = [pair for key, entry in models.items() if key not in top for pair in entry["days"]]
            if rest:
                series.append(timeutil.series("other", "Other models", timeutil.fold(rng, rest, int)))
            out["modelTrend"] = timeutil.timeline(rng, series)
    if "tool" in data:
        tools = {}
        for item in data["tool"]:
            name = _label(item["metric"], "gen_ai.tool.name") or "unknown"
            entry = tools.setdefault(name, {"dist": Distribution(), "errors": 0,
                                            "type": _label(item["metric"], "gen_ai.tool.type"), "agents": set()})
            failed_series = bool(_label(item["metric"], "error.type"))
            agent = _label(item["metric"], "gen_ai.agent.name")
            for day, value in item["points"]:
                if not rng.contains(day):
                    continue
                dist = Distribution().add(value)
                if not dist.count:
                    continue
                entry["dist"].merge(dist)
                if failed_series:
                    entry["errors"] += dist.count
                if agent:
                    entry["agents"].add(agent)
        rows = [{"tool": name[:160], "type": (e["type"] or "")[:80] or None, "calls": e["dist"].count,
                 "errors": e["errors"], "errorRate": ratio(e["errors"], e["dist"].count),
                 "p50Ms": _ms(e["dist"].percentile(0.5)), "p95Ms": _ms(e["dist"].percentile(0.95)),
                 "agents": sorted(e["agents"])[:10]} for name, e in tools.items() if e["dist"].count]
        rows.sort(key=lambda r: (-r["calls"], r["tool"]))
        out["tools"] = rows
        kpis["toolCalls"] = sum(r["calls"] for r in rows)
        kpis["toolErrors"] = sum(r["errors"] for r in rows)
    for name, key in (("inference_calls", "inferenceCallsPerInvocation"), ("tool_calls", "toolCallsPerInvocation")):
        if name in data:
            dist = Distribution()
            for item in data[name]:
                for day, value in item["points"]:
                    if rng.contains(day):
                        dist.add(value)
            kpis[key] = _num(dist.mean(), 2)
    return out


# ---------------------------------------------------------------------------
# GET /api/v1/agent-platform/agent/revisions
# ---------------------------------------------------------------------------

def _revision(raw):
    name = str(raw.get("name") or "")
    spec = raw.get("spec") if isinstance(raw.get("spec"), dict) else {}
    dep = spec.get("deploymentSpec") or {}
    return {"id": name.rsplit("/", 1)[-1][:120], "state": raw.get("state") or None,
            "createTime": iso(parse_rfc3339(raw.get("createTime"))), "traffic": None, "latest": False,
            "framework": spec.get("agentFramework") or None,
            "pythonVersion": (spec.get("packageSpec") or {}).get("pythonVersion") or None,
            "identityType": spec.get("identityType") or None,
            "minInstances": _instances(dep, "minInstances"), "maxInstances": _instances(dep, "maxInstances"),
            "methods": len(spec.get("classMethods") or []), "telemetry": _telemetry_flags(dep)}


def revisions(ctx):
    """Runtime revisions and the traffic each one receives (Gemini Platform API v1beta1)."""
    t = target(ctx)
    out = {"splitMode": None, "kpis": {"total": None, "active": None, "archived": None}, "revisions": None}
    base = "%s/v1beta1/%s" % (_host(t.region), t.name)
    res = ctx.run({
        "engine": lambda: ctx.rest("GET", base, timeout=30),
        "revisions": lambda: ctx.gcp.paginate(base + "/runtimeRevisions", quota_project=ctx.quota_project,
                                              items_key="reasoningEngineRuntimeRevisions", page_size=100,
                                              max_pages=10)})
    ok, value = res["revisions"]
    if not ok:
        ctx.warn_error(REVISION_LABEL, value, "Runtime revisions")
        return out
    ctx.add_source(REVISION_LABEL)
    items, truncated = value
    if truncated:
        ctx.warn(REVISION_LABEL, "truncated", "Only the first 1000 runtime revisions are listed.")
    rows = [_revision(raw) for raw in items if isinstance(raw, dict) and raw.get("name")]
    rows.sort(key=lambda r: r["createTime"] or "", reverse=True)
    active = [r for r in rows if r["state"] == "ACTIVE"]
    latest = (active or rows)[0] if rows else None
    if latest:
        latest["latest"] = True
    ok, engine = res["engine"]
    if ok:
        traffic = engine.get("trafficConfig") if isinstance(engine.get("trafficConfig"), dict) else {}
        manual = traffic.get("trafficSplitManual")
        if isinstance(manual, dict):
            out["splitMode"] = "manual"
            shares = {}
            for item in manual.get("targets") or []:
                if isinstance(item, dict) and isinstance(item.get("percent"), int):
                    rev_id = str(item.get("runtimeRevisionName") or "").rsplit("/", 1)[-1]
                    shares[rev_id] = shares.get(rev_id, 0) + item["percent"]
            for row in rows:
                row["traffic"] = shares.get(row["id"], 0)
        else:
            out["splitMode"] = "latest"
            for row in rows:
                row["traffic"] = 100 if row is latest else 0
    else:
        ctx.warn_error(REVISION_LABEL, engine, "The traffic configuration")
    out["revisions"] = rows
    out["kpis"] = {"total": len(rows), "active": len(active),
                   "archived": sum(1 for r in rows if r["state"] == "ARCHIVED")}
    return out


# ---------------------------------------------------------------------------
# Traces (Cloud Trace API v1)
# ---------------------------------------------------------------------------

def _trace_filters(t):
    """Trace filters that select the agent traces of one engine, tried in this order.

    Cloud Trace returns the OpenTelemetry resource attributes of a span as labels, and an Agent Engine deployment
    has service.name = the engine id. The runtime also writes traces of its own (client calls at start-up), so
    a second term keeps the traces that hold an agent, tool or model span: spans of the GenAI conventions carry
    gen_ai.operation.name, and model calls of earlier instrumentation carry gen_ai.request.model."""
    return ("+service.name:%s label:gen_ai.operation.name" % t.engine_id,
            "+service.name:%s label:gen_ai.request.model" % t.engine_id)


def _remember_traces(ctx, t, trace_ids):
    """Keeps the ids of traces listed for an engine, so that the detail endpoint can accept them."""
    cache = getattr(ctx.app, "cache", None)
    if cache is None or not trace_ids:
        return
    key = ("agent_trace_ids", t.project, t.region, t.engine_id)
    _hit, known = cache.get(key)
    merged = list(dict.fromkeys(list(trace_ids) + list(known or ())))[:1000]
    cache.set(key, tuple(merged), 3600)


def _list_traces(ctx, t, start, end, view, limit):
    """Most recent traces of the target engine. Returns (traces, more, filter)."""
    cache = getattr(ctx.app, "cache", None)
    key = ("agent_trace_filter", t.project, t.region, t.engine_id)
    filters = list(_trace_filters(t))
    if cache is not None:
        hit, known = cache.get(key)
        if hit and known in filters:
            filters = [known]
    end = min(end, dt.datetime.now(dt.timezone.utc))
    for flt in filters:
        payload = ctx.rest("GET", TRACE_URL % t.project, timeout=45, params={
            "startTime": iso(start), "endTime": iso(end), "filter": flt, "view": view, "orderBy": "start desc",
            "pageSize": limit})
        found = [tr for tr in payload.get("traces") or []
                 if isinstance(tr, dict) and TRACE_ID_RE.match(str(tr.get("traceId") or ""))]
        if found:
            if cache is not None:
                cache.set(key, flt, 3600)
            _remember_traces(ctx, t, [tr["traceId"] for tr in found])
            return found[:limit], bool(payload.get("nextPageToken")), flt
    return [], False, None


def _classify(name, labels):
    """(kind, subject) of a span: agent, tool, model, workflow or plain span."""
    operation = str(labels.get("gen_ai.operation.name") or "")

    def rest(prefix):
        return name[len(prefix):].strip() if name.startswith(prefix) else ""

    if operation == "invoke_agent" or name.startswith("invoke_agent"):
        return "agent", str(labels.get("gen_ai.agent.name") or rest("invoke_agent") or name)[:160]
    if operation == "execute_tool" or name.startswith("execute_tool"):
        return "tool", str(labels.get("gen_ai.tool.name") or rest("execute_tool") or name)[:160]
    if operation in ("generate_content", "chat", "text_completion") or \
            name.startswith(("generate_content", "call_llm")):
        model = labels.get("gen_ai.request.model") or labels.get("gen_ai.response.model") or rest("generate_content")
        return "model", _short_model(model) or name[:160]
    if operation == "invoke_workflow" or name.startswith("invoke_workflow"):
        return "workflow", str(labels.get("gen_ai.workflow.name") or rest("invoke_workflow") or name)[:160]
    return "span", None


def _span_error(labels):
    """Error type of a failed span, without the error message (which can quote user content)."""
    if labels.get("error.type"):
        return str(labels["error.type"])[:120]
    status = str(labels.get("otel.status_code") or "").upper()
    if status == "ERROR" or labels.get("/error/name") or labels.get("/error/message"):
        return str(labels.get("/error/name") or "error")[:120]
    return None


def _span(raw):
    labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
    name = str(raw.get("name") or "")[:200]
    kind, subject = _classify(name, labels)
    attrs = {}
    for source, key in SPAN_ATTRIBUTES:
        value = labels.get(source)
        if value in (None, "") or key in attrs:
            continue
        attrs[key] = to_int(value) if key in TOKEN_KEYS else str(value)[:200]
    return {"id": str(raw.get("spanId") or ""), "parent": str(raw.get("parentSpanId") or "") or None,
            "name": name, "kind": kind, "subject": subject, "start": parse_rfc3339(raw.get("startTime")),
            "end": parse_rfc3339(raw.get("endTime")), "error": _span_error(labels), "attrs": attrs,
            "labels": labels}


def _duration_ms(start, end):
    if start is None or end is None:
        return None
    return round(max(0.0, (end - start).total_seconds()) * 1000.0, 1)


def _engine_span(labels, t):
    # cloud.resource_id names the engine with the project number; _same_engine compares region and engine id.
    return labels.get("service.name") == t.engine_id or _same_engine(labels.get("cloud.resource_id"), t)


MERGED_TOOLS = "(merged tools)"


def _typed_parent(span, by_id, kinds=("agent", "tool", "model")):
    """Nearest ancestor of a span that is one of `kinds` (agent, tool or model call)."""
    seen, current = set(), span
    while current["parent"] and current["parent"] in by_id and current["parent"] not in seen:
        seen.add(current["parent"])
        current = by_id[current["parent"]]
        if current["kind"] in kinds and current["subject"]:
            return current
    return None


def _outer_model(span, by_id):
    """The model call that contains this model span, if any. The model SDK instrumentation writes its own span
    inside the model span of the Agent Development Kit: the two spans describe one call."""
    if span["kind"] != "model":
        return None
    parent = _typed_parent(span, by_id)
    return parent if parent is not None and parent["kind"] == "model" else None


def _caller(span, by_id):
    """The agent or tool on whose behalf a span ran. The Agent Development Kit runs a tool inside the span of
    the model call that asked for it; the caller is still the agent, so model spans are passed over."""
    return _typed_parent(span, by_id, kinds=("agent", "tool"))


def _is_call(span, by_id):
    """False for spans that are not a call of their own: the second span of one model call, and the span the
    Agent Development Kit writes for the merged response of parallel tool calls."""
    if span["kind"] == "tool" and span["subject"] == MERGED_TOOLS:
        return False
    return _outer_model(span, by_id) is None


def traces(ctx):
    """Recent traces of the agent (root span of each)."""
    t = target(ctx)
    rng = ctx.range
    out = {"traces": None, "truncated": False}
    try:
        found, more, _flt = _list_traces(ctx, t, rng.start_ts(), rng.end_ts(), "ROOTSPAN", TRACE_LIST_LIMIT)
    except GcpError as err:
        ctx.warn_error(TRACE_LABEL, err, "Cloud Trace")
        return out
    ctx.add_source(TRACE_LABEL)
    rows = []
    for item in found:
        spans = [_span(s) for s in item.get("spans") or [] if isinstance(s, dict)]
        root = next((s for s in spans if not s["parent"]), spans[0] if spans else None)
        if root is None:
            continue
        attrs = root["attrs"]
        rows.append({"traceId": item["traceId"], "name": root["name"], "kind": root["kind"],
                     "subject": root["subject"],
                     "start": iso(root["start"]), "durationMs": _duration_ms(root["start"], root["end"]),
                     "agent": attrs.get("agent"), "sessionId": attrs.get("sessionId"),
                     "userId": attrs.get("userId"), "revision": attrs.get("revision"), "error": root["error"]})
    out["traces"] = rows
    out["truncated"] = bool(more)
    return out


def _ordered(spans):
    """Spans in depth-first order (children by start time) with their depth; orphans attach to the top level."""
    ids = {s["id"] for s in spans}
    children = {}
    for span in spans:
        parent = span["parent"] if span["parent"] in ids else None
        children.setdefault(parent, []).append(span)
    floor = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    out, seen = [], set()

    def walk(parent, depth):
        for span in sorted(children.get(parent, []), key=lambda s: s["start"] or floor):
            if span["id"] in seen:
                continue
            seen.add(span["id"])
            out.append((span, depth))
            walk(span["id"], depth + 1)

    walk(None, 0)
    return out


def trace(ctx):
    """One trace as a span tree with timings and allowlisted attributes."""
    t = target(ctx)
    trace_id = (ctx.params.get("trace") or "").strip().lower()
    if not TRACE_ID_RE.match(trace_id):
        raise ApiError("bad_request", "Parameter 'trace' must be a 32-character hexadecimal trace ID.")
    out = {"trace": None}
    try:
        payload = ctx.rest("GET", "%s/%s" % (TRACE_URL % t.project, trace_id), timeout=45)
    except GcpError as err:
        if err.kind == "not_found":
            raise ApiError("not_found", "The trace was not found.")
        ctx.warn_error(TRACE_LABEL, err, "Cloud Trace")
        return out
    spans = [_span(s) for s in payload.get("spans") or [] if isinstance(s, dict)]
    cache = getattr(ctx.app, "cache", None)
    _hit, listed = cache.get(("agent_trace_ids", t.project, t.region, t.engine_id)) if cache is not None \
        else (False, None)
    # A trace is shown only when it belongs to this agent: its spans name the engine, or the trace was listed
    # for the engine a moment ago. Any other trace of the project answers as not found.
    if not any(_engine_span(s["labels"], t) for s in spans) and trace_id not in (listed or ()):
        raise ApiError("not_found", "The trace was not found.")
    ctx.add_source(TRACE_LABEL)
    starts = [s["start"] for s in spans if s["start"] is not None]
    ends = [s["end"] for s in spans if s["end"] is not None]
    origin = min(starts) if starts else None
    rows, tokens = [], {"inputTokens": 0, "outputTokens": 0}
    counts = {"agent": 0, "tool": 0, "model": 0}
    errors, agents_seen, session_id, user_id = 0, [], None, None
    ordered = _ordered(spans)
    by_id = {s["id"]: s for s in spans}
    for span, depth in ordered[:MAX_SPANS]:
        attrs = span["attrs"]
        rows.append({"id": span["id"], "parent": span["parent"], "depth": depth, "name": span["name"],
                     "kind": span["kind"], "subject": span["subject"],
                     "offsetMs": _duration_ms(origin, span["start"]),
                     "durationMs": _duration_ms(span["start"], span["end"]), "error": span["error"],
                     "attributes": {k: v for k, v in attrs.items() if k != "userId"}})
    for span, _depth in ordered:
        attrs = span["attrs"]
        outer = _outer_model(span, by_id)
        if span["kind"] in counts and _is_call(span, by_id):
            counts[span["kind"]] += 1
        if span["kind"] == "agent" and span["subject"] and span["subject"] not in agents_seen:
            agents_seen.append(span["subject"])
        # Usage reported by both spans of one model call is counted once (the outer span's).
        if outer is None or not any(outer["attrs"].get(key) for key in TOKEN_KEYS):
            for key in TOKEN_KEYS:
                tokens[key] += attrs.get(key) or 0
        errors += 1 if span["error"] else 0
        session_id = session_id or attrs.get("sessionId")
        user_id = user_id or attrs.get("userId")
    out["trace"] = {"traceId": trace_id, "start": iso(origin),
                    "durationMs": _duration_ms(origin, max(ends) if ends else None), "spanCount": len(spans),
                    "truncated": len(ordered) > MAX_SPANS, "agents": agents_seen[:20],
                    "agentCalls": counts["agent"], "toolCalls": counts["tool"], "modelCalls": counts["model"],
                    "inputTokens": tokens["inputTokens"], "outputTokens": tokens["outputTokens"], "errors": errors,
                    "sessionId": session_id, "userId": user_id, "spans": rows}
    return out


# ---------------------------------------------------------------------------
# GET /api/v1/agent-platform/agent/topology
# ---------------------------------------------------------------------------

def topology(ctx):
    """Call graph of the agent (sub-agents, tools and models), aggregated from its most recent traces."""
    t = target(ctx)
    rng = ctx.range
    out = {"nodes": None, "edges": None, "sampledTraces": None, "truncated": False}
    try:
        found, more, _flt = _list_traces(ctx, t, rng.start_ts(), rng.end_ts(), "COMPLETE", TOPOLOGY_SAMPLE)
    except GcpError as err:
        ctx.warn_error(TRACE_LABEL, err, "Cloud Trace")
        return out
    ctx.add_source(TRACE_LABEL)
    nodes, edges = {}, {}
    for item in found:
        spans = [_span(s) for s in item.get("spans") or [] if isinstance(s, dict)]
        by_id = {s["id"]: s for s in spans}
        typed = {s["id"]: "%s:%s" % (s["kind"], s["subject"]) for s in spans
                 if s["kind"] in ("agent", "tool", "model") and s["subject"]}
        for span in spans:
            node_id = typed.get(span["id"])
            if node_id is None or not _is_call(span, by_id):
                continue
            parent = _caller(span, by_id)
            node = nodes.setdefault(node_id, {"id": node_id, "kind": span["kind"], "name": span["subject"],
                                              "calls": 0, "errors": 0, "totalMs": 0.0, "timed": 0})
            node["calls"] += 1
            node["errors"] += 1 if span["error"] else 0
            duration = _duration_ms(span["start"], span["end"])
            if duration is not None:
                node["totalMs"] += duration
                node["timed"] += 1
            source = typed[parent["id"]] if parent is not None else "client"
            edge = edges.setdefault((source, node_id), {"source": source, "target": node_id, "calls": 0})
            edge["calls"] += 1
    rows = [{"id": n["id"], "kind": n["kind"], "name": n["name"], "calls": n["calls"], "errors": n["errors"],
             "avgMs": round(n["totalMs"] / n["timed"], 1) if n["timed"] else None} for n in nodes.values()]
    if rows:
        rows.insert(0, {"id": "client", "kind": "client", "name": "Client", "calls": None, "errors": None,
                        "avgMs": None})
    out["nodes"] = sorted(rows, key=lambda n: (n["kind"] != "client", -(n["calls"] or 0), n["name"]))
    out["edges"] = sorted(edges.values(), key=lambda e: (-e["calls"], e["source"], e["target"]))
    out["sampledTraces"] = len(found)
    out["truncated"] = bool(more)
    return out


# ---------------------------------------------------------------------------
# GET /api/v1/agent-platform/agent/evaluation
# ---------------------------------------------------------------------------

def _monitor(raw):
    name = str(raw.get("name") or "")
    metrics = []
    for source in raw.get("metricSources") or []:
        if not isinstance(source, dict):
            continue
        metric = source.get("metric") if isinstance(source.get("metric"), dict) else {}
        label = ((metric.get("metadata") or {}).get("title")
                 or (metric.get("predefinedMetricSpec") or {}).get("metricSpecName")
                 or str(source.get("metricResourceName") or "").rsplit("/", 1)[-1])
        if label:
            metrics.append(str(label)[:80])
    cfg = raw.get("config") if isinstance(raw.get("config"), dict) else {}
    percent = (cfg.get("randomSampling") or {}).get("percentage")
    observability = raw.get("cloudObservability") if isinstance(raw.get("cloudObservability"), dict) else {}
    details = [d.get("message") for d in raw.get("stateDetails") or [] if isinstance(d, dict) and d.get("message")]
    return {"id": name.rsplit("/", 1)[-1][:120], "name": raw.get("displayName") or name.rsplit("/", 1)[-1],
            "state": raw.get("state") or None, "message": str(details[0])[:300] if details else None,
            "metrics": metrics[:20], "samplingPercent": percent if isinstance(percent, int) else None,
            "maxSamplesPerRun": to_int(cfg.get("maxEvaluatedSamplesPerRun")) or None,
            "scope": "session" if "sessionScope" in observability else (
                "trace" if "traceScope" in observability else None),
            "createTime": iso(parse_rfc3339(raw.get("createTime"))),
            "updateTime": iso(parse_rfc3339(raw.get("updateTime")))}


def _run_targets(raw, t):
    configs = raw.get("inferenceConfigs") if isinstance(raw.get("inferenceConfigs"), dict) else {}
    return any(isinstance(cfg, dict) and _same_engine((cfg.get("agentRunConfig") or {}).get("agentEngine"), t)
               for cfg in configs.values())


def _evaluation_run(raw):
    name = str(raw.get("name") or "")
    results = raw.get("evaluationResults") if isinstance(raw.get("evaluationResults"), dict) else {}
    summary = results.get("summaryMetrics") if isinstance(results.get("summaryMetrics"), dict) else {}
    values = summary.get("metrics") if isinstance(summary.get("metrics"), dict) else {}
    metrics = [{"name": str(key)[:80], "value": round(float(value), 4)} for key, value in sorted(values.items())
               if isinstance(value, (int, float)) and not isinstance(value, bool)]
    return {"id": name.rsplit("/", 1)[-1][:120], "name": raw.get("displayName") or name.rsplit("/", 1)[-1],
            "state": raw.get("state") or None, "createTime": iso(parse_rfc3339(raw.get("createTime"))),
            "completionTime": iso(parse_rfc3339(raw.get("completionTime"))),
            "totalItems": summary.get("totalItems") if isinstance(summary.get("totalItems"), int) else None,
            "failedItems": summary.get("failedItems") if isinstance(summary.get("failedItems"), int) else None,
            "metrics": metrics[:12]}


def evaluation(ctx):
    """Online monitors of the agent with their scores, and evaluation runs that ran against it."""
    t = target(ctx)
    rng = ctx.range
    out = {"kpis": {"monitors": None, "activeMonitors": None, "evaluations": None, "failedEvaluations": None},
           "monitors": None, "scores": None, "scoreTrend": None, "runs": None}
    base = "%s/v1/projects/%s/locations/%s" % (_host(t.region), t.project, t.region)

    def page(path, key, size, pages):
        return ctx.gcp.paginate(base + path, quota_project=ctx.quota_project, items_key=key, page_size=size,
                                max_pages=pages)

    def read(metric):
        flt = 'metric.type="%s" AND resource.type="aiplatform.googleapis.com/OnlineEvaluator"' % metric
        return monitoring.aligned_series(ctx, t.project, flt, rng.start_ts(), rng.end_ts(), "ALIGN_SUM")

    res = ctx.run({"monitors": lambda: page("/onlineEvaluators", "onlineEvaluators", 100, 5),
                   "runs": lambda: page("/evaluationRuns", "evaluationRuns", 50, 4),
                   "scores": lambda: read(EVAL_SCORES), "counts": lambda: read(EVAL_COUNT)})
    monitor_ids = set()
    ok, value = res["monitors"]
    if ok:
        ctx.add_source(EVALUATOR_LABEL)
        rows = [_monitor(raw) for raw in value[0]
                if isinstance(raw, dict) and _same_engine(raw.get("agentResource"), t)]
        rows.sort(key=lambda r: (r["state"] != "ACTIVE", r["name"] or ""))
        monitor_ids = {r["id"] for r in rows}
        out["monitors"] = rows
        out["kpis"]["monitors"] = len(rows)
        out["kpis"]["activeMonitors"] = sum(1 for r in rows if r["state"] == "ACTIVE")
    else:
        ctx.warn_error(EVALUATOR_LABEL, value, "Online monitors")
    ok, value = res["runs"]
    if ok:
        ctx.add_source(RUNS_LABEL)
        rows = [_evaluation_run(raw) for raw in value[0] if isinstance(raw, dict) and _run_targets(raw, t)]
        rows.sort(key=lambda r: r["createTime"] or "", reverse=True)
        out["runs"] = rows[:50]
    else:
        ctx.warn_error(RUNS_LABEL, value, "Evaluation runs")

    def mine(item):
        return _same_engine(item["metric"].get("agent_resource_name"), t) or \
            item["resource"].get("online_evaluator_id") in monitor_ids

    ok, value = res["scores"]
    if ok:
        ctx.add_source(EVAL_LABEL)
        metrics = {}
        for item in value:
            if not mine(item):
                continue
            name = str(item["metric"].get("evaluation_metric_name") or "unknown")[:80]
            entry = metrics.setdefault(name, {"dist": Distribution(),
                                              "buckets": [Distribution() for _ in rng.buckets()]})
            for day, point in item["points"]:
                if rng.contains(day):
                    entry["dist"].add(point)
                    entry["buckets"][rng.bucket_index(day)].add(point)
        rows = [{"metric": name, "average": _num(e["dist"].mean(), 4), "samples": e["dist"].count}
                for name, e in metrics.items() if e["dist"].count]
        rows.sort(key=lambda r: r["metric"])
        out["scores"] = rows
        out["scoreTrend"] = timeutil.timeline(rng, [
            timeutil.series(r["metric"], r["metric"],
                            [_num(d.mean(), 4) if d.count else None for d in metrics[r["metric"]]["buckets"]])
            for r in rows[:TREND_SERIES + 2]])
    else:
        ctx.warn_error(EVAL_LABEL, value, "Online evaluation metrics")
    ok, value = res["counts"]
    if ok:
        ctx.add_source(EVAL_LABEL)
        total = failed = 0
        for item in value:
            if not mine(item):
                continue
            status = str(item["metric"].get("evaluation_status") or "").upper()
            n = sum(to_int(point) for day, point in item["points"] if rng.contains(day))
            total += n
            failed += n if status and not status.startswith(("SUCCE", "OK")) else 0
        out["kpis"]["evaluations"] = total
        out["kpis"]["failedEvaluations"] = failed
    else:
        ctx.warn_error(EVAL_LABEL, value, "Online evaluation metrics")
    return out


# ---------------------------------------------------------------------------
# POST /api/v1/agent-platform/agent/playground
# ---------------------------------------------------------------------------

def _json_text(value):
    try:
        return json.dumps(value, ensure_ascii=False, default=str)[:PLAYGROUND_JSON_CHARS]
    except (TypeError, ValueError):
        return ""


def _playground_event(obj):
    """One agent event reduced to what the playground shows: author, text, tool calls and tool results."""
    content = obj.get("content") if isinstance(obj.get("content"), dict) else {}
    raw_parts = content.get("parts")
    parts = []
    for part in raw_parts if isinstance(raw_parts, list) else ():   # the agent's output decides the shape
        if not isinstance(part, dict):
            continue
        call = part.get("function_call") or part.get("functionCall")
        result = part.get("function_response") or part.get("functionResponse")
        if isinstance(part.get("text"), str):
            if part["text"]:
                parts.append({"type": "text", "text": part["text"][:PLAYGROUND_TEXT_CHARS],
                              "thought": bool(part.get("thought"))})
        elif isinstance(call, dict):
            parts.append({"type": "call", "name": str(call.get("name") or "")[:160],
                          "text": _json_text(call.get("args") or {})})
        elif isinstance(result, dict):
            parts.append({"type": "result", "name": str(result.get("name") or "")[:160],
                          "text": _json_text(result.get("response") or {})})
    error = obj.get("error_message") or obj.get("errorMessage") or obj.get("error_code") or obj.get("errorCode")
    return {"author": str(obj.get("author") or "")[:160] or None, "role": content.get("role") or None,
            "parts": parts, "error": str(error)[:500] if error else None}


def _parse_events(lines):
    events = []
    for line in lines:
        text = line[5:].strip() if line.startswith("data:") else line.strip()
        if not text or text.startswith(":"):
            continue
        try:
            obj = json.loads(text)
        except (ValueError, RecursionError):   # not JSON, or nested deeper than the parser allows
            continue
        if isinstance(obj, dict):
            event = _playground_event(obj)
            if event["parts"] or event["error"]:
                events.append(event)
    return events


def playground(ctx):
    """Sends one message to the agent and returns the events of its answer.

    Administrators only (route option). The agent runs with the user id of the signed-in administrator, and
    every call is written to the governance audit log without the message text.

    The message reaches the agent unchanged: the control plane does not screen it. Prompt and response screening
    (for example Model Armor) is whatever the deployed agent and its project have in place."""
    t = target(ctx)
    message = ctx.body.get("message")
    if not isinstance(message, str) or not message.strip() or len(message) > PLAYGROUND_MAX_CHARS:
        raise ApiError("bad_request", "Field 'message' must be 1 to %d characters." % PLAYGROUND_MAX_CHARS)
    session_id = ctx.body.get("sessionId")
    if session_id is not None and (not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id)):
        raise ApiError("bad_request", "Field 'sessionId' is not a valid session ID.")
    methods = _method_names(t.engine)
    stream_method = next((m for m in STREAM_METHODS if m in methods), None)
    if stream_method is None:
        raise ApiError("bad_request", "This agent does not expose a streaming query method.")
    user_id = (ctx.identity.email or "control-plane")[:128]
    base = "%s/v1/%s" % (_host(t.region), t.name)
    started = time.monotonic()
    events, truncated, failure, done = [], False, None, False
    try:
        create = next((m for m in SESSION_METHODS if m in methods), None)
        if session_id is None and create is not None:
            created = ctx.rest("POST", base + ":query", timeout=60,
                               json_body={"class_method": create, "input": {"user_id": user_id}})
            output = created.get("output") if isinstance(created, dict) else None
            new_id = output.get("id") if isinstance(output, dict) else None
            if isinstance(new_id, str) and SESSION_ID_RE.match(new_id):
                session_id = new_id
        query = {"user_id": user_id, "message": message}
        if session_id is not None:
            query["session_id"] = session_id
        lines, truncated = ctx.gcp.stream_lines(
            "POST", base + ":streamQuery", quota_project=ctx.quota_project, timeout=60,
            json_body={"class_method": stream_method, "input": query},
            deadline_seconds=PLAYGROUND_DEADLINE_SECONDS)
        events = _parse_events(lines)
        if len(events) > PLAYGROUND_MAX_EVENTS:
            events, truncated = events[:PLAYGROUND_MAX_EVENTS], True
        done = True
    except GcpError as err:
        failure = err
    finally:
        # The agent may have acted before anything failed, so every call is audited, also when its answer
        # could not be read or the code itself failed (that error still reaches the caller after this).
        elapsed = int((time.monotonic() - started) * 1000)
        audit_logged = governance.write_audit(ctx, "agent_playground_query", {
            "result": "success" if done else "failed", "target": t.name, "project": t.project,
            "region": t.region, "sessionId": session_id, "messageChars": len(message), "events": len(events),
            "latencyMs": elapsed})
    if failure is not None:
        LOG.warning("playground query failed kind=%s", failure.kind)
        raise ApiError("upstream_error", describe_error(failure, "The agent")[1])
    return {"sessionId": session_id, "userId": user_id, "events": events, "truncated": truncated,
            "latencyMs": elapsed, "auditLogged": audit_logged}
