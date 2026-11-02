"""Plain-language catalog of telemetry sources for Telemetry Extensions.

Every Cloud Logging log and BigQuery table that discovery finds is shown as a card that says what
the data is and what its dashboard shows, and can be added with one click. Entries are keyed by the
source id: the sanitized log id, which is also the table name a Log Router sink creates in BigQuery
(run.googleapis.com/requests -> run_googleapis_com_requests). A log and its exported table therefore
share one entry.

Field paths use Cloud Logging LogEntry names (case-sensitive, as the Logging API returns them).
bigquery_connector() resolves them against an exported table schema, where payload columns may be
renamed (protoPayload -> protopayload_auditlog, jsonPayload -> jsonpayload_<type>) and lower-cased.
A path can differ per source: {"logging": ..., "bigquery": ...}; None means the field cannot be read
from that source (for example OpenTelemetry keys that contain dots).

Sources without an entry get an honest generic description derived from the product name.
Text is English; the web UI translates it (src/web/assets/js/i18n/ko.js).
"""

from __future__ import annotations

import re
import urllib.parse

from ..schema import SEGMENT_RE, build_expr

AI, SECURITY, OPERATIONS, OTHER = "ai", "security", "operations", "other"
CATEGORIES = (AI, SECURITY, OPERATIONS, OTHER)
FIELDS = ("operation", "status", "user", "model", "tokens")
LABELED = ("operation", "status", "tokens")
# Labels of mapped fields whose meaning is not known (a custom or automatically suggested field).
GENERIC_LABELS = {"operation": "Operation", "status": "Status", "tokens": "Tokens"}
# Breakdowns every log entry supports, used for logs without a catalog entry.
FALLBACK_PATHS = {"operation": "resource.type", "status": "severity", "user": None, "model": None, "tokens": None}
FALLBACK_LABELS = {"operation": "Resource type", "status": "Severity", "tokens": "Tokens"}
LOG_NAME_RE = re.compile(r"^projects/[a-z0-9][a-z0-9.:\-]{0,98}/logs/[A-Za-z0-9_.%\-]{1,512}$")
FILTER_RE = re.compile(r'^\s*logName\s*=\s*"(projects/[^/"]+/logs/[^"]+)"\s*$')
INDEX_RE = re.compile(r"\[\d{1,3}\]")


def sanitize_log_name(short):
    """BigQuery table name of a log id (the Log Router naming rule)."""
    return re.sub(r"[./\-]", "_", short.replace("%2F", "/")).lower()


def _entry(title, what, category, operation=None, status=("severity", "Severity"), user=None, model=None,
           tokens=None, sensitive=False, names=None):
    """operation/status/tokens are (path, label) tuples; user/model are paths. Paths may be per-source dicts.

    names says how operation values can be shown by name (for example "reasoning_engine": IDs of Agent Engine
    agents, resolved to their display names)."""
    paths = {"operation": operation[0] if operation else None, "status": status[0] if status else None,
             "user": user, "model": model, "tokens": tokens[0] if tokens else None}
    labels = {"operation": operation[1] if operation else None, "status": status[1] if status else None,
              "tokens": tokens[1] if tokens else None}
    return {"title": title, "what": what, "category": category, "paths": paths, "labels": labels,
            "sensitive": sensitive, "names": names}


# Payload keys of OpenTelemetry generative AI events contain dots (gen_ai.agent.name), which a dotted
# Logging path cannot address; the BigQuery export flattens them to gen_ai_agent_name.
OTEL_AGENT = {"logging": None, "bigquery": "jsonPayload.gen_ai_agent_name"}
OTEL_USER = {"logging": None, "bigquery": "jsonPayload.user_id"}
OTEL_INPUT_TOKENS = {"logging": None, "bigquery": "jsonPayload.gen_ai_usage_input_tokens"}

ENTRIES = {
    # -- AI and agents ---------------------------------------------------------------------------
    "aiplatform_googleapis_com_reasoning_engine_stdout": _entry(
        "Agent Engine output logs",
        "Standard output of agents deployed to Agent Engine (Agent Platform): what each agent printed while it handled "
        "requests.",
        AI, operation=("resource.labels.reasoning_engine_id", "Agent Engine"), status=None, names="reasoning_engine"),
    "aiplatform_googleapis_com_reasoning_engine_stderr": _entry(
        "Agent Engine error and startup logs",
        "Standard error of agents deployed to Agent Engine (Agent Platform), including startup messages and exceptions. "
        "A sudden rise usually means an agent is failing.",
        AI, operation=("resource.labels.reasoning_engine_id", "Agent Engine"), status=None, names="reasoning_engine"),
    "aiplatform_googleapis_com_reasoning_engine_build": _entry(
        "Agent Engine build logs",
        "Container build steps and status messages when deploying agents to Agent Engine (Agent Platform).",
        AI, operation=("resource.labels.reasoning_engine_id", "Agent Engine"),
        status=("labels.build_step", "Build step"), names="reasoning_engine"),
    "aiplatform_googleapis_com_online_evaluator": _entry(
        "Agent quality evaluation runs",
        "Runs of Agent Platform online evaluators, which score agent traces against quality metrics. Shows which agents "
        "were evaluated and how each run went.",
        AI, operation=("labels.agent_resource", "Agent"), names="reasoning_engine"),
    "aiplatform_googleapis_com_prediction_container": _entry(
        "Agent Platform endpoint container logs",
        "Logs written by the containers that serve models on Agent Platform endpoints.",
        AI, operation=("resource.labels.endpoint_id", "Endpoint")),
    "gen_ai_user_message": _entry(
        "Agent prompts (OpenTelemetry)",
        "Prompts that users sent to your own agents, recorded by OpenTelemetry generative AI instrumentation, for "
        "example in agents built with ADK. Each entry contains the prompt text.",
        AI, operation=("resource.labels.job", "Agent service"), status=None, sensitive=True),
    "gen_ai_choice": _entry(
        "Agent responses (OpenTelemetry)",
        "Model responses returned to your own agents, recorded by OpenTelemetry generative AI instrumentation, with "
        "the finish reason such as STOP, MAX_TOKENS or SAFETY. Each entry contains the response text.",
        AI, operation=("resource.labels.job", "Agent service"), status=("jsonPayload.finish_reason", "Finish reason"),
        sensitive=True),
    "gen_ai_client_inference_operation_details": _entry(
        "Agent model calls (OpenTelemetry)",
        "One entry per model call made by your own agents, recorded by OpenTelemetry generative AI instrumentation, "
        "with the agent name, token usage and the input and output messages.",
        AI, operation=(OTEL_AGENT, "Agent"), user=OTEL_USER, tokens=(OTEL_INPUT_TOKENS, "Input tokens"), status=None,
        sensitive=True),
    "discoveryengine_googleapis_com_gen_ai_client_inference_operation_details": _entry(
        "Gemini Enterprise agent model calls",
        "One entry per model call made by agents running in Gemini Enterprise, with the agent name, token usage and "
        "the input and output messages.",
        AI, operation=(OTEL_AGENT, "Agent"),
        status=({"logging": None, "bigquery": "jsonPayload.gen_ai_response_finish_reasons[0]"}, "Finish reason"),
        user=OTEL_USER, tokens=(OTEL_INPUT_TOKENS, "Input tokens"),
        sensitive=True),
    "discoveryengine_googleapis_com_gen_ai_choice": _entry(
        "Gemini Enterprise agent responses",
        "Responses generated by agents in Gemini Enterprise, with the finish reason. Each entry contains the "
        "response text.",
        AI, operation=("resource.labels.agent_id", "Agent"), status=("jsonPayload.finish_reason", "Finish reason"),
        sensitive=True),
    "discoveryengine_googleapis_com_api_error": _entry(
        "Gemini Enterprise data connector logs",
        "Sync activity and errors of Gemini Enterprise data connectors, for example Confluence, Jira or Outlook: "
        "which connector ran, which content was synced and whether it failed.",
        AI, operation=("jsonPayload.connectorRunPayload.dataConnector", "Data connector"),
        status=("jsonPayload.connectorRunPayload.syncType", "Sync type")),
    "discoveryengine_googleapis_com_api_errors": _entry(
        "Gemini Enterprise API errors",
        "Failed Gemini Enterprise API calls, such as data connector runs and document imports, with the API method "
        "and the error code.",
        AI, operation=("resource.labels.method", "API method"), status=("jsonPayload.status.code", "Error code")),
    # Built-in sources (already shown on the built-in pages).
    "discoveryengine_googleapis_com_gemini_enterprise_user_activity": _entry(
        "Gemini Enterprise user activity",
        "Who used Gemini Enterprise and when: searches, assistant conversations and agent calls, with the agent and "
        "model used.",
        AI),
    "discoveryengine_googleapis_com_gen_ai_user_message": _entry(
        "Gemini Enterprise prompts",
        "Prompt text sent in Gemini Enterprise, recorded when message logging is turned on.",
        AI, sensitive=True),
    "modelarmor_googleapis_com_sanitize_operations": _entry(
        "Model Armor screening results",
        "Every prompt and response screened by Model Armor, with the filters that matched and whether it was "
        "blocked.",
        AI),
    "businessaicode_googleapis_com_inference_response": _entry(
        "Antigravity inference logs",
        "Token usage of each Antigravity request, used for model usage and per-user caps.",
        AI),
    "agy_admin_admin_actions": _entry(
        "Antigravity governance actions",
        "Suspensions, restorations and policy changes made by this control plane, kept as an audit trail.",
        AI),
    # -- Security and audit ----------------------------------------------------------------------
    "cloudaudit_googleapis_com_activity": _entry(
        "Admin Activity audit logs",
        "Configuration changes in the project: who created, changed or deleted which resource, such as agents, "
        "service accounts or IAM policies.",
        SECURITY, operation=("protoPayload.methodName", "API method"),
        user="protoPayload.authenticationInfo.principalEmail"),
    "cloudaudit_googleapis_com_data_access": _entry(
        "Data Access audit logs",
        "API calls that read data or use AI services, such as Agent Platform model calls and Gemini Enterprise or "
        "NotebookLM requests, with the caller identity.",
        SECURITY, operation=("protoPayload.methodName", "API method"),
        user="protoPayload.authenticationInfo.principalEmail"),
    "cloudaudit_googleapis_com_system_event": _entry(
        "System event audit logs",
        "Changes that Google Cloud systems made to your resources rather than people, for example Cloud Run "
        "revision updates, VM live migrations and expired tables.",
        SECURITY, operation=("protoPayload.methodName", "Event"), status=("protoPayload.serviceName", "Service")),
    "cloudaudit_googleapis_com_policy": _entry(
        "Policy denied audit logs",
        "Requests that Google Cloud denied because they violated a security policy, such as VPC Service Controls, "
        "with the caller and the API that was blocked.",
        SECURITY, operation=("protoPayload.methodName", "API method"),
        status=("protoPayload.serviceName", "Service"), user="protoPayload.authenticationInfo.principalEmail"),
    "cloudaudit_googleapis_com_access_transparency": _entry(
        "Access Transparency logs",
        "Records of Google personnel accessing your content, with the reason for the access and the role of the "
        "person. Written only when Access Transparency is turned on for the organization.",
        SECURITY, operation=("jsonPayload.reason[0].type", "Access reason"),
        status=("jsonPayload.principalJobTitle", "Accessor role")),
    "compute_googleapis_com_shielded_vm_integrity": _entry(
        "Shielded VM boot integrity",
        "Boot integrity checks of Shielded VMs. Each boot is compared with a known-good baseline; failures can "
        "indicate tampering.",
        SECURITY, operation=("resource.labels.instance_id", "VM instance")),
    "networkanalyzer_googleapis_com_analyzer_reports": _entry(
        "Network Analyzer findings",
        "Network configuration findings from Network Intelligence Center, such as IP address exhaustion or firewall "
        "rules that block expected traffic.",
        SECURITY, operation=("jsonPayload.causeCode", "Finding"), status=("jsonPayload.priority", "Priority")),
    "certificatemanager_googleapis_com_certificates_expiry": _entry(
        "Certificate expiry alerts",
        "Notifications from Certificate Manager about certificates that are about to expire or have expired.",
        SECURITY, operation=("jsonPayload.certificates[0]", "Certificate"), status=("jsonPayload.state", "State")),
    # -- Operations ------------------------------------------------------------------------------
    "run_googleapis_com_requests": _entry(
        "Cloud Run requests",
        "Every HTTP request served by your Cloud Run services, with the service and the response code. Use it to "
        "spot traffic changes and error spikes.",
        OPERATIONS, operation=("resource.labels.service_name", "Service"),
        status=("httpRequest.status", "HTTP status")),
    "run_googleapis_com_stdout": _entry(
        "Cloud Run application output",
        "What your Cloud Run services and jobs print to standard output.",
        OPERATIONS, operation=("resource.labels.service_name", "Service")),
    "run_googleapis_com_stderr": _entry(
        "Cloud Run application errors",
        "What your Cloud Run services and jobs print to standard error, typically errors and stack traces.",
        OPERATIONS, operation=("resource.labels.service_name", "Service")),
    "run_googleapis_com_varlog_system": _entry(
        "Cloud Run system events",
        "Platform messages about your Cloud Run containers, such as instance starts, scaling and shutdowns.",
        OPERATIONS, operation=("resource.labels.service_name", "Service")),
    "cloudfunctions_googleapis_com_cloud_functions": _entry(
        "Cloud Run functions logs",
        "Logs written by functions deployed with Cloud Functions or Cloud Run functions.",
        OPERATIONS, operation=("resource.labels.function_name", "Function")),
    "cloudbuild": _entry(
        "Cloud Build logs",
        "Output of Cloud Build builds, one entry per log line, grouped by build.",
        OPERATIONS, operation=("resource.labels.build_id", "Build")),
    "cloudscheduler_googleapis_com_executions": _entry(
        "Cloud Scheduler job runs",
        "Runs of Cloud Scheduler jobs. Each run writes a start entry and a result entry with the HTTP response "
        "code of the target.",
        OPERATIONS, operation=("resource.labels.job_id", "Job"), status=("httpRequest.status", "HTTP status")),
    "clouderrorreporting_googleapis_com_insights": _entry(
        "Error Reporting alerts",
        "New and recurring application errors detected by Error Reporting, with the service and the exception "
        "type.",
        OPERATIONS, operation=("jsonPayload.errorEvent.serviceContext.service", "Service"),
        status=("labels.exceptionType", "Exception type")),
    "compute_googleapis_com_instance_group_manager_events": _entry(
        "Managed instance group events",
        "Events of Compute Engine managed instance groups, such as instances created, recreated or deleted by "
        "autoscaling and autohealing.",
        OPERATIONS, operation=("resource.labels.instance_group_manager_name", "Instance group")),
    "requests": _entry(
        "Load balancer requests",
        "HTTP(S) requests through external Application Load Balancers, with the backend service and the response "
        "code.",
        OPERATIONS, operation=("resource.labels.backend_service_name", "Backend service"),
        status=("httpRequest.status", "HTTP status")),
    "logging_googleapis_com_sink_error": _entry(
        "Log routing errors",
        "Errors of Log Router sinks that could not deliver logs to their destination, for example because the "
        "destination permission was removed.",
        OPERATIONS, operation=("resource.labels.name", "Sink"), status=("labels.error_code", "Error code")),
    "export_errors": _entry(
        "BigQuery log export errors",
        "Log entries that a Log Router sink could not write to BigQuery because a field did not match the table "
        "schema. These entries are missing from the exported tables.",
        OPERATIONS, operation=("logName", "Log"), status=("sink", "Sink")),
}
ALIASES = {"run_googleapis_com__var_log_system": "run_googleapis_com_varlog_system"}

# Pages that already show the built-in sources (route ids of the web UI).
BUILT_IN = {
    "cloudaudit_googleapis_com_activity": ["registry", "deployments", "gateway"],
    "cloudaudit_googleapis_com_data_access": ["overview", "gateway", "notebooklm", "users"],
    "discoveryengine_googleapis_com_gemini_enterprise_user_activity": ["gemini-enterprise", "model-armor", "users"],
    "discoveryengine_googleapis_com_gen_ai_user_message": ["gemini-enterprise", "overview", "users"],
    "modelarmor_googleapis_com_sanitize_operations": ["model-armor", "overview", "users"],
    "businessaicode_googleapis_com_inference_response": ["antigravity", "models", "users"],
}

# Product names for sources without an entry: (log id prefix, product, category).
PRODUCTS = (
    ("aiplatform.googleapis.com", "Agent Platform", AI),
    ("discoveryengine.googleapis.com", "Gemini Enterprise", AI),
    ("modelarmor.googleapis.com", "Model Armor", AI),
    ("generativelanguage.googleapis.com", "Gemini API", AI),
    ("dialogflow.googleapis.com", "Dialogflow", AI),
    ("businessaicode.googleapis.com", "Antigravity", AI),
    ("gen_ai.", "OpenTelemetry generative AI", AI),
    ("cloudaudit.googleapis.com", "Cloud Audit Logs", SECURITY),
    ("iam.googleapis.com", "IAM", SECURITY),
    ("securitycenter.googleapis.com", "Security Command Center", SECURITY),
    ("accesscontextmanager.googleapis.com", "VPC Service Controls", SECURITY),
    ("certificatemanager.googleapis.com", "Certificate Manager", SECURITY),
    ("networkanalyzer.googleapis.com", "Network Analyzer", SECURITY),
    ("run.googleapis.com", "Cloud Run", OPERATIONS),
    ("cloudfunctions.googleapis.com", "Cloud Run functions", OPERATIONS),
    ("cloudbuild", "Cloud Build", OPERATIONS),
    ("cloudscheduler.googleapis.com", "Cloud Scheduler", OPERATIONS),
    ("compute.googleapis.com", "Compute Engine", OPERATIONS),
    ("container.googleapis.com", "Google Kubernetes Engine", OPERATIONS),
    ("logging.googleapis.com", "Cloud Logging", OPERATIONS),
    ("clouderrorreporting.googleapis.com", "Error Reporting", OPERATIONS),
    ("bigquery.googleapis.com", "BigQuery", OPERATIONS),
    ("cloudsql.googleapis.com", "Cloud SQL", OPERATIONS),
    ("pubsub.googleapis.com", "Pub/Sub", OPERATIONS),
    ("dataflow.googleapis.com", "Dataflow", OPERATIONS),
    ("workflows.googleapis.com", "Workflows", OPERATIONS),
    ("appengine.googleapis.com", "App Engine", OPERATIONS),
)


def _product(source_id, short=None):
    for prefix, name, category in PRODUCTS:
        if (short and short.startswith(prefix)) or source_id.startswith(sanitize_log_name(prefix)):
            return name, category, prefix
    return None, OTHER, None


def _pretty(logical):
    words = [w for w in re.sub(r"_googleapis_com", "", logical).split("_") if w]
    return " ".join(w[:1].upper() + w[1:] for w in words) or logical


def lookup(source_id):
    return ENTRIES.get(ALIASES.get(source_id, source_id))


def describe(source_id, short=None, dataset=None, built_in=None):
    """The card text of a source: what it is, its category and the labels of its dashboard breakdowns.

    short is the log id for Cloud Logging sources; dataset names the dataset of a BigQuery table."""
    entry = lookup(source_id)
    out = {"id": source_id, "known": entry is not None, "titleParams": None, "whatParams": None,
           "builtIn": list(built_in if built_in is not None else BUILT_IN.get(source_id, []))}
    if entry:
        out.update({"title": entry["title"], "what": entry["what"], "category": entry["category"],
                    "sensitive": entry["sensitive"], "operationLabel": entry["labels"]["operation"],
                    "statusLabel": entry["labels"]["status"], "tokensLabel": entry["labels"]["tokens"]})
        return out
    product, category, prefix = _product(source_id, short)
    if short is not None:
        rest = short[len(prefix) + 1:] if prefix and short.startswith(prefix + "/") else short
        if product:
            out.update({"title": "{product} · {log}", "titleParams": {"product": product, "log": rest},
                        "what": "Log entries that {product} writes to Cloud Logging under {log}.",
                        "whatParams": {"product": product, "log": short}})
        else:
            out.update({"title": "{log}", "titleParams": {"log": short},
                        "what": "Log entries written to Cloud Logging under the name {log}.",
                        "whatParams": {"log": short}})
        labels = FALLBACK_LABELS
    else:
        out.update({"title": "{name}", "titleParams": {"name": _pretty(source_id)},
                    "what": "A table in the BigQuery dataset {dataset}. Its dashboard counts rows over time and breaks "
                            "them down by the fields detected automatically.",
                    "whatParams": {"dataset": dataset or ""}})
        labels = GENERIC_LABELS
    out.update({"category": category, "sensitive": False, "operationLabel": labels["operation"],
                "statusLabel": labels["status"], "tokensLabel": None})
    return out


def title_text(card):
    """English display name of a card (the web UI translates the template instead)."""
    title = card["title"]
    for key, value in (card.get("titleParams") or {}).items():
        title = title.replace("{%s}" % key, str(value))
    return title[:80]


def _source_path(value, source):
    if isinstance(value, dict):
        return value.get(source)
    return value


def entry_paths(source_id, source):
    """Mapping paths of a source for "logging" or "bigquery": the catalog entry, else the fallback breakdowns
    (resource type and severity, which every log entry has)."""
    entry = lookup(source_id)
    if entry is None:
        return dict(FALLBACK_PATHS)
    return {k: _source_path(entry["paths"][k], source) for k in FIELDS}


def logging_connector(card, log_name):
    """A ready connector body that reads one log through the Logging API, or None for an unusual name."""
    if not isinstance(log_name, str) or not LOG_NAME_RE.match(log_name):
        return None
    mapping = {"time": "timestamp"}
    mapping.update(entry_paths(card["id"], "logging"))
    return {"name": title_text(card), "enabled": True, "source": "logging", "bigquery": None,
            "logging": {"filter": 'logName="%s"' % log_name}, "mapping": mapping}


def _canonical(schema, path):
    """The schema spelling of a column path (BigQuery names are case-insensitive), or None."""
    out = []
    children = schema.tree
    for part in path.split("."):
        node = children.get(part.lower())
        if node is None:
            return None
        out.append(node.name)
        children = node.children
    return ".".join(out)


def _bq_candidates(schema, path):
    yield path
    head, _, rest = path.partition(".")
    low = head.lower()
    if rest and low in ("protopayload", "jsonpayload"):
        for name, node in schema.tree.items():
            if name != low and name.startswith(low):
                yield "%s.%s" % (node.name, rest)


def resolve_bigquery_path(schema, path):
    """Translates a LogEntry path to a column path of an exported table, or None if it does not exist."""
    if not path or schema is None:
        return None
    indexed = "[" in path
    bare = INDEX_RE.sub("", path)
    for candidate in _bq_candidates(schema, bare):
        if indexed:
            # A repeated record resolves to its first element; inside a JSON column the index is required.
            node = schema.node(candidate)
            if node is not None and node.type not in ("STRUCT", "JSON"):
                return _canonical(schema, candidate)
            continue
        if build_expr("t", schema, candidate) is not None:
            return _canonical(schema, candidate) or candidate
    return None


def _usable(schema, path):
    return bool(path) and "[" not in path and all(SEGMENT_RE.match(s) for s in path.split(".")) and \
        build_expr("t", schema, path) is not None


def bigquery_connector(card, project, dataset, logical, schema, suggested):
    """A ready connector body for a table, and the card with labels that match the resolved mapping.

    Known sources use their catalog paths and fall back to the automatic suggestions; other tables use the
    suggestions and fall back to resource type and severity. The time column comes from the suggestions
    (None lets the server pick the table time column)."""
    if schema is None:
        return None, card
    suggested = suggested or {}
    entry = lookup(card["id"])
    card = dict(card)
    time = suggested.get("time")
    node = schema.node(time) if time else None
    mapping = {"time": node.name if node is not None and node.type == "TIMESTAMP" and "." not in time else None}
    for key in FIELDS:
        path, label = None, None
        suggestion = suggested.get(key) if _usable(schema, suggested.get(key)) else None
        if entry is not None:
            want = _source_path(entry["paths"][key], "bigquery")
            if want:
                path, label = resolve_bigquery_path(schema, want), entry["labels"].get(key)
                if path is None:
                    path, label = suggestion, GENERIC_LABELS.get(key)
        else:
            path, label = suggestion, GENERIC_LABELS.get(key)
            if path is None and FALLBACK_PATHS.get(key):
                path, label = resolve_bigquery_path(schema, FALLBACK_PATHS[key]), FALLBACK_LABELS.get(key)
        mapping[key] = path
        if key in LABELED:
            card["%sLabel" % key] = label if path else None
    body = {"name": title_text(card), "enabled": True, "source": "bigquery", "logging": None,
            "bigquery": {"project": project, "dataset": dataset, "table": logical}, "mapping": mapping}
    return body, card


# ---------------------------------------------------------------------------
# Connectors
# ---------------------------------------------------------------------------

def _filter_log(conn):
    m = FILTER_RE.match((conn.get("logging") or {}).get("filter") or "")
    return urllib.parse.unquote(m.group(1).split("/logs/", 1)[1]) if m else None


def connector_source_id(conn):
    """The source id a connector reads (one log or one table), or None for a custom filter."""
    if conn.get("source") == "bigquery":
        table = ((conn.get("bigquery") or {}).get("table") or "").strip()
        if table.endswith("_*"):
            table = table[:-2]
        return table.lower() or None
    short = _filter_log(conn)
    return sanitize_log_name(short) if short else None


def _norm(path):
    if not path:
        return None
    low = INDEX_RE.sub("", path).lower()
    return re.sub(r"^(protopayload|jsonpayload)[a-z0-9_]*\.", r"\1.", low)


def for_connector(conn):
    """Card text for a connector that reads one known log or table, with labels that match its mapping.

    Returns None for custom filters, whose meaning only the author knows."""
    sid = connector_source_id(conn)
    if sid is None:
        return None
    source = "bigquery" if conn.get("source") == "bigquery" else "logging"
    if source == "bigquery":
        card = describe(sid, dataset=(conn.get("bigquery") or {}).get("dataset"))
    else:
        card = describe(sid, short=_filter_log(conn))
    entry = lookup(sid)
    expected = entry_paths(sid, source)
    mapping = conn.get("mapping") or {}
    for key in LABELED:
        have = mapping.get(key)
        if not have:
            card["%sLabel" % key] = None
        elif _norm(have) != _norm(expected.get(key)):
            card["%sLabel" % key] = GENERIC_LABELS[key]
        elif entry is None:
            card["%sLabel" % key] = FALLBACK_LABELS[key]
    return card
