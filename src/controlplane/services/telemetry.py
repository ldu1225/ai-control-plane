"""Telemetry extensions: schema/log auto-discovery, AI semantic grouping, one-click connectors and dashboards.

Discovery inspects BigQuery schemas and Cloud Logging entry structures, enriches and groups sources dynamically
with Gemini on Agent Platform (falling back cleanly to baseline catalog/heuristics when offline), and builds
validated one-click connectors.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import logging
import re
import secrets
import urllib.parse

from .. import config, timeutil
from ..gcp import GcpError
from ..httpkit import ApiError, now_iso
from ..schema import SEGMENT_RE, build_expr, expr, quote_ident, table_ref
from ..security import uid_for
from .base import (CORE_TABLES, bucket_or_sentinel, bucket_values, iso, parse_rfc3339, period_sql, range_params,
                   split_rollup, to_int)
from . import agents, monitoring
from . import telemetry_catalog as catalog

LOG = logging.getLogger("controlplane.telemetry")

LOGGING_V2 = "https://logging.googleapis.com/v2"
MAPPING_KEYS = ("time", "user", "model", "tokens", "operation", "status")
MAX_TABLES = 100
MAX_LOG_ENTRIES = 5000
MESSAGE_CHARS = 300
CONNECTOR_ID_RE = re.compile(r"^conn_[0-9a-f]{8}$")
HEALTH_TTL = 300
AI_PROFILE_TTL = 21600
TELEMETRY_AI_MODEL = "gemini-4-argon"
LOG_ENTRY_METRIC = "logging.googleapis.com/log_entry_count"
ACTIVITY_DAYS = 7
ENGINE_ID_RE = re.compile(r"(?:reasoningEngines/)?([0-9]{6,})$")
NAMES_TTL = 600
NUMERIC_TYPES = ("INT64", "INTEGER", "FLOAT64", "FLOAT", "NUMERIC", "BIGNUMERIC", "NUMBER")
HEURISTICS = {
    "user": (
        "principalemail", "useriamprincipal", "actor_email", "user_email", "caller_email",
        "user_id", "userid", "actor_id", "actor", "principal", "email", "user", "session_id", "sessionid",
    ),
    "model": (
        "model_id", "modelid", "llm_model", "model_name", "model_used", "model", "model_version", "engine_id",
    ),
    "tokens": (
        "totaltokencount", "total_tokens", "token_usage", "token_count", "tokens_used", "tokens",
        "input_tokens", "output_tokens", "prompt_tokens",
    ),
    "operation": (
        "methodname", "operationtype", "operation_type", "operation_name", "operation", "pipeline_step",
        "tool_name", "event_type", "eventtype", "event_name", "action", "step", "command", "task",
        "method", "route", "service_name",
    ),
    "status": (
        "severity", "status.code", "status_code", "http_status", "verdict", "outcome",
        "finish_reason", "result", "phase", "status", "level",
    ),
}


def suggested_mapping(leaf_types):
    """Case-insensitive leaf-name heuristics across BigQuery schemas and sampled JSON log payloads."""
    lower = [(path, path.lower(), (ftype or "").upper()) for path, ftype in (leaf_types or [])]
    mapping = {}
    time_col = None
    for wanted in ("timestamp", "receivetimestamp", "event_time", "event_timestamp", "event_ts",
                   "created_at", "occurred_at", "start_time", "logged_at", "recorded_at", "ts"):
        time_col = next((p for p, low, t in lower if low == wanted and t.startswith("TIMESTAMP") and "." not in p), None)
        if time_col:
            break
    if time_col is None:
        time_col = next((p for p, _low, t in lower if t.startswith("TIMESTAMP") and "." not in p), None)
    mapping["time"] = time_col
    for key, endings in HEURISTICS.items():
        found = None
        for ending in endings:
            for path, low, ftype in lower:
                if key == "tokens" and ftype and not any(ftype.startswith(nt) for nt in NUMERIC_TYPES):
                    continue
                if low == ending or low.endswith("." + ending):
                    found = path
                    break
            if found:
                break
        mapping[key] = found
    return mapping


sanitize_log_name = catalog.sanitize_log_name


def all_connectors(ctx):
    return [c for c in (ctx.settings["telemetry"].get("connectors") or []) if isinstance(c, dict)]


def discover_datasets(ctx):
    active_projects = list(getattr(ctx, "projects", None) or [ctx.project])
    sel = getattr(ctx, "selected_project", "all") or "all"
    out = []
    for p in active_projects[:8]:
        if config.valid_project(p or ""):
            pair = (p, ctx.audit_dataset)
            if pair not in out:
                out.append(pair)
    if not out:
        out.append((ctx.project, ctx.audit_dataset))
    for ref in ctx.ws.get("telemetry_datasets") or []:
        parsed = config.parse_dataset_ref(ref, ctx.project)
        if parsed and (sel == "all" or parsed[0] == sel) and parsed not in out:
            out.append(parsed)
    # On live GCP, also auto-discover other BigQuery datasets sitting in the workspace project(s) so idle
    # telemetry/log tables appear immediately without requiring manual entry in Settings > Workspace.
    if type(getattr(ctx, "gcp", None)).__name__ == "Gcp" and config.valid_project(ctx.project or ""):
        billing_cfg = ctx.ws.get("billing") or {}
        billing_pair = (config.billing_project(ctx.settings), billing_cfg.get("dataset") or "")
        scan_projects = [p for p in active_projects if config.valid_project(p or "")][:8]
        for proj in scan_projects:
            try:
                data = ctx.rest("GET", "https://bigquery.googleapis.com/bigquery/v2/projects/%s/datasets" % proj,
                                params={"maxResults": 30}, timeout=8)
                ds_ids = []
                for item in (data or {}).get("datasets") or []:
                    ds_id = ((item or {}).get("datasetReference") or {}).get("datasetId") or ""
                    if config.valid_dataset(ds_id):
                        ds_ids.append(ds_id)
                ds_ids.sort(key=lambda d: (
                    0 if any(k in d.lower() for k in ("log", "audit", "telemetry", "agent", "gemini", "ge_", "event")) else 1,
                    d,
                ))
                for ds_id in ds_ids[:20]:
                    pair = (proj, ds_id)
                    if pair == billing_pair:
                        continue
                    if pair not in out:
                        out.append(pair)
            except Exception:  # pylint: disable=broad-except
                continue
    return out


def _explicit_datasets(ctx):
    out = {(ctx.project, ctx.audit_dataset)}
    for ref in ctx.ws.get("telemetry_datasets") or []:
        parsed = config.parse_dataset_ref(ref, ctx.project)
        if parsed:
            out.add(parsed)
    return out


def _table_stats(ctx, project, dataset):
    sql = "SELECT table_id, row_count, size_bytes, last_modified_time FROM `%s.%s.__TABLES__`" % (project, dataset)
    rows = ctx.query(sql, {}, "telemetry")
    return {r["table_id"]: r for r in rows if r.get("table_id")}


def _discover_tables(ctx, connectors_by_table, schemas_out=None):
    """Tables of the audit, configured, and auto-discovered datasets, or None when no dataset could be scanned."""
    tables = []
    scanned = 0
    explicit = _explicit_datasets(ctx)
    for project, dataset in discover_datasets(ctx):
        is_explicit = (project, dataset) in explicit
        label = "bigquery:%s" % dataset if project == ctx.project else "bigquery:%s.%s" % (project, dataset)
        try:
            groups = ctx.registry.tables(project, dataset, ctx.quota_project, ctx.max_bytes)
        except GcpError as err:
            if is_explicit:
                ctx.warn_error(label, err, "BigQuery dataset %s" % dataset)
            continue
        ctx.add_source("bigquery:%s.INFORMATION_SCHEMA.TABLES" % dataset)
        scanned += 1
        try:
            stats = _table_stats(ctx, project, dataset)
        except GcpError as err:
            if is_explicit:
                ctx.warn_error(label, err, "Table statistics for %s" % dataset)
            stats = {}
        names = sorted(groups)
        if len(names) > MAX_TABLES:
            if is_explicit:
                ctx.warn(label, "truncated", "Only the first %d tables of %s are inspected." % (MAX_TABLES, dataset))
            names = names[:MAX_TABLES]
        schema_tasks = {n: (lambda n=n: ctx.registry.schema(project, dataset, groups[n]["latest"], ctx.quota_project))
                        for n in names}
        schemas = ctx.run(schema_tasks)
        failed_schemas = sum(1 for ok, _ in schemas.values() if not ok)
        if failed_schemas and is_explicit:
            ctx.warn(label, "partial_data", "The schema of %d table(s) in %s could not be read." % (failed_schemas,
                                                                                                    dataset))
        for name in names:
            info = groups[name]
            sharded = info["kind"] == "sharded"
            if sharded:
                members = [s for tid, s in stats.items() if tid.startswith(name + "_") and tid[len(name) + 1:].isdigit()]
            else:
                members = [stats[name]] if name in stats else []
            rows = sum(to_int(s.get("row_count")) for s in members) if members else None
            size = sum(to_int(s.get("size_bytes")) for s in members) if members else None
            modified = max((to_int(s.get("last_modified_time")) for s in members), default=None) if members else None
            ok, meta = schemas.get(name, (False, None))
            schema = meta["schema"] if ok and meta else None
            if schemas_out is not None and schema is not None:
                schemas_out[(project, dataset, name)] = schema
            columns = schema.leaf_paths(200) if schema is not None else None
            mapping = suggested_mapping(schema.leaf_types(200)) if schema is not None else None
            sid = name.lower()
            core = name in CORE_TABLES and (project, dataset) == (ctx.project, ctx.audit_dataset)
            card = catalog.describe(sid, dataset=dataset, built_in=None if core else [])
            quick, card = catalog.bigquery_connector(card, project, dataset, name, schema, mapping)
            conn_id = connectors_by_table.get((project, dataset, name))
            # Skip static lookup tables without a TIMESTAMP column in opportunistically scanned datasets.
            if not is_explicit and quick is None and conn_id is None:
                continue
            tables.append({"id": sid, "project": project, "dataset": dataset, "table": name + ("_*" if sharded else ""),
                           "logical": name, "sharded": sharded, "rows": rows, "sizeBytes": size,
                           "lastModified": iso(dt.datetime.fromtimestamp(modified / 1000.0, tz=dt.timezone.utc))
                           if modified else None,
                           "core": core, "registered": conn_id is not None, "connectorId": conn_id,
                           "suggestedMapping": mapping, "columns": columns, "catalog": card, "quickAdd": quick,
                           "entries7d": None, "daily": None})
    return tables if scanned else None


def _list_logs(ctx):
    """[{logName, shortName}] across scanned projects, or None when the log list failed."""
    tasks = {proj: (lambda p=proj: ctx.gcp.paginate("%s/projects/%s/logs" % (LOGGING_V2, p),
                                                    quota_project=ctx.quota_project, items_key="logNames",
                                                    page_size=500, max_pages=1))
             for proj in ctx.projects}
    results = ctx.run(tasks)
    out = []
    seen = set()
    succeeded = 0
    truncated_any = False
    for proj, (ok, val) in results.items():
        if not ok:
            if proj == ctx.project:
                ctx.warn_error("api:logging.logs", val, "The Cloud Logging log list")
            continue
        succeeded += 1
        names, truncated = val
        if truncated:
            truncated_any = True
        for log_name in names:
            if isinstance(log_name, str) and "/logs/" in log_name:
                short = urllib.parse.unquote(log_name.split("/logs/", 1)[1])
                if short not in seen:
                    seen.add(short)
                    out.append({"logName": log_name, "shortName": short, "project": proj})
    if not succeeded:
        return None
    ctx.add_source("api:logging.logs")
    if truncated_any:
        ctx.warn("api:logging.logs", "truncated", "Only the first 5000 log names are listed.")
    return out


def _log_activity(ctx):
    """{log id: [daily entry counts, oldest first]} over the last ACTIVITY_DAYS days, or None if unreadable.

    Uses the Cloud Monitoring log_entry_count metric, which costs one call for every log of the project
    (the Logging API would need one rate-limited call per log). A log may report one series per resource
    type, so counts are summed per log."""
    tz = timeutil.get_zone(ctx.tz_name)
    now = dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(tz).date()
    index = {today - dt.timedelta(days=ACTIVITY_DAYS - 1 - i): i for i in range(ACTIVITY_DAYS)}
    try:
        series = monitoring.time_series(ctx, LOG_ENTRY_METRIC, ["metric.label.log"],
                                        now - dt.timedelta(days=ACTIVITY_DAYS), now)
    except GcpError as err:
        ctx.warn_error("api:monitoring.log_entry_count", err, "Log activity from Cloud Monitoring")
        return None
    ctx.add_source("api:monitoring.log_entry_count")
    out = {}
    for labels, points in series:
        name = urllib.parse.unquote(str(labels.get("metric.label.log") or "")).strip()
        if not name:
            continue
        counts = out.setdefault(name, [0] * ACTIVITY_DAYS)
        for day, value in points:
            pos = index.get(day)
            if pos is not None:
                counts[pos] += to_int(value)
    return out


def _builtin_logs(ctx):
    """Logs that built-in pages read directly from Cloud Logging: {log id: [route ids]}."""
    agy = (ctx.settings.get("governance") or {}).get("antigravity") or {}
    out = {}
    inference = urllib.parse.unquote(agy.get("inference_log") or "")
    if inference:
        out[inference] = list(catalog.BUILT_IN.get(catalog.sanitize_log_name(inference), ["antigravity"]))
    audit = urllib.parse.unquote(agy.get("audit_log_name") or "")
    if audit:
        out[audit] = ["antigravity"]
    return out


def _task_result(ctx, result, source, what):
    ok, value = result
    if ok:
        return value
    ctx.warn_error(source, value, what)
    return None


def _logging_resource_names(ctx, flt):
    """Returns projects/<id> resourceNames for Cloud Logging queries."""
    all_projs = getattr(ctx, "projects", None) or [ctx.project]
    m = re.findall(r"projects/([a-z][a-z0-9\-]{4,29})/logs/", flt or "")
    m += re.findall(r'project_id\s*=\s*"([a-z][a-z0-9\-]{4,29})"', flt or "")
    projs = [p for p in dict.fromkeys(m) if p in all_projs]
    if projs:
        return ["projects/%s" % p for p in projs]
    return ["projects/%s" % ctx.project]


def _extract_entry_leaves(entry, max_leaves=80):
    """Extracts dotted scalar paths and inferred types from a Cloud Logging entry."""
    out = [("timestamp", "TIMESTAMP")]
    if isinstance(entry.get("severity"), str):
        out.append(("severity", "STRING"))
    rtype = ((entry.get("resource") or {}).get("type") if isinstance(entry.get("resource"), dict) else None)
    if isinstance(rtype, str) and rtype:
        out.append(("resource.type", "STRING"))

    def walk(prefix, obj, depth):
        if len(out) >= max_leaves or depth > 4:
            return
        if isinstance(obj, dict):
            for k, val in obj.items():
                if not isinstance(k, str) or not SEGMENT_RE.match(k):
                    continue
                nxt = "%s.%s" % (prefix, k) if prefix else k
                walk(nxt, val, depth + 1)
                if len(out) >= max_leaves:
                    return
        elif isinstance(obj, list):
            if obj and not isinstance(obj[0], (dict, list)):
                walk("%s[0]" % prefix, obj[0], depth + 1)
        elif isinstance(obj, bool):
            out.append((prefix, "BOOL"))
        elif isinstance(obj, (int, float)):
            out.append((prefix, "NUMBER"))
        elif isinstance(obj, str) and obj:
            out.append((prefix, "STRING"))

    res_labels = (entry.get("resource") or {}).get("labels") if isinstance(entry.get("resource"), dict) else None
    if isinstance(res_labels, dict):
        walk("resource.labels", res_labels, 1)
    for root in ("labels", "httpRequest", "jsonPayload", "protoPayload"):
        sub = entry.get(root)
        if isinstance(sub, dict):
            walk(root, sub, 1)
    return out


_WORKLOAD_RESOURCE_TYPES = {
    "cloud_run_revision": ("service_name",),
    "cloud_run_job": ("job_name", "job"),
    "cloud_function": ("function_name",),
    "k8s_container": ("container_name",),
    "gce_instance": ("instance_id",),
}
_SHARED_RUNTIME_LOGS = (
    "run.googleapis.com/stdout",
    "run.googleapis.com/stderr",
    "cloudfunctions.googleapis.com/cloud-functions",
    "stdout",
    "stderr",
    "python",
)


def _sample_logs_and_workloads(ctx):
    """Samples recent Cloud Logging entries to infer JSON leaf paths and discover structured workload streams."""
    if type(getattr(ctx, "gcp", None)).__name__ != "Gcp":
        return [], {}, {}
    tz = timeutil.get_zone(ctx.tz_name)
    now = dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(tz).date()
    index = {today - dt.timedelta(days=ACTIVITY_DAYS - 1 - i): i for i in range(ACTIVITY_DAYS)}
    since = iso(now - dt.timedelta(days=30))
    projs = [p for p in (getattr(ctx, "projects", None) or [ctx.project]) if config.valid_project(p or "")][:3]
    if not projs:
        return [], {}, {}
    body = {
        "resourceNames": ["projects/%s" % p for p in projs],
        "filter": (
            'timestamp>="%s" AND NOT logName:"cloudaudit.googleapis.com" '
            'AND NOT logName:"run.googleapis.com%%2Frequests" '
            'AND NOT logName:"run.googleapis.com%%2Fvarlog%%2Fsystem"' % since
        ),
        "orderBy": "timestamp desc",
        "pageSize": 200,
    }
    try:
        data = ctx.rest("POST", "%s/entries:list" % LOGGING_V2, json_body=body, timeout=6)
    except Exception:  # pylint: disable=broad-except
        return [], {}, {}
    workloads = {}
    short_counts = {}
    log_leaves = {}
    for entry in (data or {}).get("entries") or []:
        if not isinstance(entry, dict):
            continue
        ln = entry.get("logName") or ""
        short = urllib.parse.unquote(ln.split("/logs/", 1)[1]) if "/logs/" in ln else ""
        if short:
            short_counts[short] = short_counts.get(short, 0) + 1
        leaves = _extract_entry_leaves(entry)
        if short:
            bucket = log_leaves.setdefault(short, {})
            if len(bucket) < 120:
                for pth, ftype in leaves:
                    bucket.setdefault(pth, ftype)
        res = entry.get("resource") if isinstance(entry.get("resource"), dict) else {}
        rtype = res.get("type") or ""
        rl = res.get("labels") if isinstance(res.get("labels"), dict) else {}
        if rtype not in _WORKLOAD_RESOURCE_TYPES:
            continue
        if short == "run.googleapis.com/requests":
            continue
        payload = entry.get("jsonPayload")
        if not isinstance(payload, dict):
            continue
        json_leaves = [(p, t) for p, t in leaves if p.startswith("jsonPayload.")]
        if len(json_leaves) < 2:
            continue
        label_key, svc = None, None
        for lk in _WORKLOAD_RESOURCE_TYPES[rtype]:
            cand = (rl.get(lk) or "").strip()
            if cand and re.match(r"^[A-Za-z0-9_\-]{2,64}$", cand):
                label_key, svc = lk, cand
                break
        if not svc or svc.startswith("ai-control-plane"):
            continue
        proj = (rl.get("project_id") or ctx.project or "").strip()
        wkey = (proj, rtype, label_key, svc)
        item = workloads.get(wkey)
        if item is None:
            item = {"project": proj, "rtype": rtype, "label_key": label_key, "service": svc,
                    "entries30d": 0, "daily": [0] * ACTIVITY_DAYS, "leaves": {}, "shorts": set()}
            workloads[wkey] = item
        item["entries30d"] += 1
        if short:
            item["shorts"].add(short)
        for pth, ftype in leaves:
            if len(item["leaves"]) < 120:
                item["leaves"].setdefault(pth, ftype)
        ts = parse_rfc3339(entry.get("timestamp"))
        if ts is not None:
            day = ts.astimezone(tz).date()
            pos = index.get(day)
            if pos is not None:
                item["daily"][pos] += 1

    out = []
    for (proj, rtype, label_key, svc), info in sorted(workloads.items()):
        # Only emit a dedicated workload stream when its structured JSON logs share runtime stdout/stderr
        # or span multiple log names; single custom logNames are already listed directly in logs.
        shorts = info["shorts"]
        if len(shorts) == 1 and next(iter(shorts)) not in _SHARED_RUNTIME_LOGS:
            continue
        sid = "workload_%s_%s" % (catalog.sanitize_log_name(rtype), catalog.sanitize_log_name(svc))
        flt = ('resource.type="%s" AND resource.labels.%s="%s" AND jsonPayload:* '
               'AND NOT logName:"run.googleapis.com%%2Frequests" '
               'AND NOT logName:"run.googleapis.com%%2Fvarlog%%2Fsystem"' % (rtype, label_key, svc))
        leaf_list = list(info["leaves"].items())
        inferred = suggested_mapping(leaf_list)
        inferred["time"] = "timestamp"
        if not inferred.get("status"):
            inferred["status"] = "severity"
        card = {
            "id": sid,
            "known": False,
            "title": "%s Telemetry" % svc,
            "titleParams": None,
            "what": "Structured JSON telemetry emitted by workload %s (%s)." % (svc, rtype),
            "whatParams": None,
            "category": catalog.OPERATIONS,
            "sensitive": False,
            "operationLabel": "Operation" if inferred.get("operation") else None,
            "statusLabel": "Severity" if inferred.get("status") == "severity" else ("Status" if inferred.get("status") else None),
            "tokensLabel": "Tokens" if inferred.get("tokens") else None,
            "builtIn": [],
        }
        quick = {
            "name": ("%s Telemetry" % svc)[:80],
            "enabled": True,
            "source": "logging",
            "bigquery": None,
            "logging": {"filter": flt},
            "mapping": inferred,
        }
        out.append({
            "id": sid,
            "logName": None,
            "shortName": "%s (%s · %s)" % (svc, rtype, proj),
            "sinkFilter": flt,
            "project": proj,
            "routedToBigQuery": False,
            "core": False,
            "registered": False,
            "connectorId": None,
            "entries7d": sum(info["daily"]) or info["entries30d"],
            "daily": info["daily"],
            "columns": [p for p, _ in leaf_list],
            "_leaves": info["leaves"],
            "catalog": card,
            "quickAdd": quick,
        })
    return out, short_counts, log_leaves


# ---------------------------------------------------------------------------
# AI Semantic Profiling & Dynamic Source Grouping (gemini-4-argon)
# ---------------------------------------------------------------------------

AI_ENRICH_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "sources": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "key": {"type": "STRING"},
                    "category": {"type": "STRING", "enum": ["ai", "security", "operations", "other"]},
                    "group": {"type": "STRING"},
                    "groupKo": {"type": "STRING"},
                    "title": {"type": "STRING"},
                    "titleKo": {"type": "STRING"},
                    "what": {"type": "STRING"},
                    "whatKo": {"type": "STRING"},
                    "sensitive": {"type": "BOOLEAN"},
                    "time": {"type": "STRING"},
                    "operation": {"type": "STRING"},
                    "operationLabel": {"type": "STRING"},
                    "operationLabelKo": {"type": "STRING"},
                    "status": {"type": "STRING"},
                    "statusLabel": {"type": "STRING"},
                    "statusLabelKo": {"type": "STRING"},
                    "user": {"type": "STRING"},
                    "model": {"type": "STRING"},
                    "tokens": {"type": "STRING"},
                    "tokensLabel": {"type": "STRING"},
                    "tokensLabelKo": {"type": "STRING"},
                },
                "required": ["key", "category", "group", "groupKo", "title", "titleKo", "what", "whatKo", "sensitive"],
            },
        }
    },
    "required": ["sources"],
}

AI_ENRICH_SYSTEM = (
    "You are an enterprise cloud observability and governance architect. "
    "Given discovered BigQuery tables and Cloud Logging streams with their real schema columns or JSON payload paths, "
    "cluster related sources into concise semantic groups (2 to 5 groups across the workspace, with both English "
    "'group' and Korean 'groupKo' titles) and infer plain-language metadata for each source.\n"
    "Rules:\n"
    "1. Never invent column or field names. For 'time', 'operation', 'status', 'user', 'model', and 'tokens', "
    "choose ONLY exact paths present in the source's 'fields' list, or return an empty string \"\" when none fits.\n"
    "2. 'tokens' must only map to numeric token count fields (never strings, IDs, or timestamps).\n"
    "3. Keep 'title' and 'titleKo' under 55 characters. Keep 'what' and 'whatKo' to 1-2 clear sentences explaining "
    "what events or records the source contains and how it helps governance or operations.\n"
    "4. Set 'sensitive' to true only when fields appear to hold raw user prompts, LLM responses, chat messages, "
    "or personal document text.\n"
    "5. In product naming, always write 'Agent Platform' (never the legacy Cloud AI platform name)."
)

META_STR_LIMITS = {
    "title": 80, "titleKo": 80,
    "what": 320, "whatKo": 320,
    "group": 60, "groupKo": 60,
    "operationLabel": 40, "operationLabelKo": 40,
    "statusLabel": 40, "statusLabelKo": 40,
    "tokensLabel": 40, "tokensLabelKo": 40,
    "aiModel": 60,
}


def _clean_text(val, max_len):
    if not isinstance(val, str):
        return None
    cleaned = config.CONTROL_RE.sub("", val).strip()
    return cleaned[:max_len] if cleaned else None


def _sanitize_meta(meta):
    if not isinstance(meta, dict):
        return None
    out = {}
    for k, limit in META_STR_LIMITS.items():
        v = _clean_text(meta.get(k), limit)
        if v:
            out[k] = v
    cat = meta.get("category")
    if cat in catalog.CATEGORIES:
        out["category"] = cat
    for bkey in ("sensitive", "aiProfiled", "aiGrouped"):
        if isinstance(meta.get(bkey), bool):
            out[bkey] = meta[bkey]
    return out or None


def _extract_card_meta(card):
    if not isinstance(card, dict):
        return None
    raw = {k: card.get(k) for k in META_STR_LIMITS}
    if not raw.get("title"):
        raw["title"] = catalog.title_text(card)
    raw["category"] = card.get("category")
    raw["sensitive"] = bool(card.get("sensitive"))
    raw["aiProfiled"] = bool(card.get("aiProfiled"))
    raw["aiGrouped"] = bool(card.get("aiGrouped"))
    return _sanitize_meta(raw)


def _card_with_meta(base_card, meta, conn):
    clean = _sanitize_meta(meta)
    if not clean:
        return base_card
    mapping = (conn or {}).get("mapping") or {}
    if base_card is not None:
        card = dict(base_card)
    else:
        card = {
            "id": catalog.connector_source_id(conn or {}) or "custom",
            "known": False,
            "title": clean.get("title") or (conn or {}).get("name") or "Telemetry",
            "titleParams": None,
            "what": clean.get("what") or "",
            "whatParams": None,
            "category": clean.get("category") or catalog.OTHER,
            "sensitive": bool(clean.get("sensitive")),
            "operationLabel": "Operation" if mapping.get("operation") else None,
            "statusLabel": "Status" if mapping.get("status") else None,
            "tokensLabel": "Tokens" if mapping.get("tokens") else None,
            "builtIn": [],
        }
    for k in ("group", "groupKo", "aiModel"):
        if clean.get(k):
            card[k] = clean[k]
    for bkey in ("aiProfiled", "aiGrouped"):
        if clean.get(bkey):
            card[bkey] = True
    if not card.get("known"):
        if clean.get("title"):
            card["title"] = clean["title"]
            card["titleParams"] = None
        if clean.get("titleKo"):
            card["titleKo"] = clean["titleKo"]
        if clean.get("what"):
            card["what"] = clean["what"]
            card["whatParams"] = None
        if clean.get("whatKo"):
            card["whatKo"] = clean["whatKo"]
        if clean.get("category") in catalog.CATEGORIES:
            card["category"] = clean["category"]
        if "sensitive" in clean:
            card["sensitive"] = bool(clean["sensitive"])
        for fkey in ("operation", "status", "tokens"):
            if mapping.get(fkey):
                if clean.get("%sLabel" % fkey):
                    card["%sLabel" % fkey] = clean["%sLabel" % fkey]
                if clean.get("%sLabelKo" % fkey):
                    card["%sLabelKo" % fkey] = clean["%sLabelKo" % fkey]
            else:
                card["%sLabel" % fkey] = None
                card.pop("%sLabelKo" % fkey, None)
    return card


_STD_LOGGING_ENVELOPE = {
    "timestamp", "receiveTimestamp", "severity", "logName", "resource.type",
    "httpRequest.requestMethod", "httpRequest.status", "httpRequest.requestUrl", "httpRequest.remoteIp",
}


def _valid_logging_ai_path(path, observed):
    if not _valid_json_path(path):
        return False
    if observed:
        return path in observed or path in _STD_LOGGING_ENVELOPE
    return path in _STD_LOGGING_ENVELOPE or path.startswith("resource.labels.")


def _apply_ai_profile(target, kind, ai_item, model_used, schema=None, observed_leaves=None):
    """Applies verified AI semantic group + inferred descriptions/mappings to a discovered table or log."""
    card = dict(target.get("catalog") or {})
    grp = _clean_text(ai_item.get("group"), 60)
    grp_ko = _clean_text(ai_item.get("groupKo"), 60)
    if grp:
        card["group"] = grp
        card["groupKo"] = grp_ko or grp
        card["aiGrouped"] = True
        card["aiModel"] = model_used
    quick = dict(target["quickAdd"]) if isinstance(target.get("quickAdd"), dict) else None
    if not card.get("known"):
        cat = ai_item.get("category")
        if cat in catalog.CATEGORIES:
            card["category"] = cat
        title = _clean_text(ai_item.get("title"), 80)
        title_ko = _clean_text(ai_item.get("titleKo"), 80)
        what = _clean_text(ai_item.get("what"), 320)
        what_ko = _clean_text(ai_item.get("whatKo"), 320)
        if title:
            card["title"] = title
            card["titleParams"] = None
        if title_ko:
            card["titleKo"] = title_ko
        if what:
            card["what"] = what
            card["whatParams"] = None
        if what_ko:
            card["whatKo"] = what_ko
        if isinstance(ai_item.get("sensitive"), bool):
            card["sensitive"] = ai_item["sensitive"]
        card["aiProfiled"] = True
        card["aiModel"] = model_used
        if quick is not None:
            if title:
                quick["name"] = title[:80]
            mapping = dict(quick.get("mapping") or {})
            if kind == "bigquery" and schema is not None:
                ai_time = (ai_item.get("time") or "").strip()
                if ai_time:
                    tnode = schema.node(ai_time)
                    if tnode is not None and tnode.type == "TIMESTAMP" and "." not in ai_time:
                        mapping["time"] = tnode.name
                leaf_type_map = {p.lower(): (ft or "").upper() for p, ft in schema.leaf_types(200)}
                for fkey in ("operation", "status", "user", "model", "tokens"):
                    cand = (ai_item.get(fkey) or "").strip()
                    if not cand:
                        continue
                    resolved = catalog.resolve_bigquery_path(schema, cand)
                    if not resolved and catalog._usable(schema, cand):
                        resolved = cand
                    if resolved and "[" not in resolved and build_expr("t", schema, resolved) is not None:
                        if fkey == "tokens":
                            ft = leaf_type_map.get(resolved.lower(), "")
                            if not any(ft.startswith(nt) for nt in NUMERIC_TYPES):
                                continue
                        mapping[fkey] = resolved
            elif kind == "logging":
                obs = observed_leaves or {}
                for fkey in ("operation", "status", "user", "model", "tokens"):
                    cand = (ai_item.get(fkey) or "").strip()
                    if not cand or not _valid_logging_ai_path(cand, obs):
                        continue
                    if fkey == "tokens":
                        ft = (obs.get(cand) or "").upper()
                        if not any(ft.startswith(nt) for nt in NUMERIC_TYPES):
                            continue
                    mapping[fkey] = cand
            quick["mapping"] = mapping
            if kind == "bigquery":
                target["suggestedMapping"] = dict(mapping)
            for fkey, default_lbl in (("operation", "Operation"), ("status", "Status"), ("tokens", "Tokens")):
                if mapping.get(fkey):
                    lbl = _clean_text(ai_item.get("%sLabel" % fkey), 40)
                    lbl_ko = _clean_text(ai_item.get("%sLabelKo" % fkey), 40)
                    card["%sLabel" % fkey] = lbl or card.get("%sLabel" % fkey) or default_lbl
                    if lbl_ko:
                        card["%sLabelKo" % fkey] = lbl_ko
                else:
                    card["%sLabel" % fkey] = None
                    card.pop("%sLabelKo" % fkey, None)
    target["catalog"] = card
    if quick is not None:
        meta = _extract_card_meta(card)
        if meta:
            quick["meta"] = meta
        target["quickAdd"] = quick


def _ai_enrich_sources(ctx, tables, logs, schemas_by_table, log_leaves_by_short):
    """Profiles unknown sources and clusters all addable sources into semantic groups via Gemini (gemini-4-argon)."""
    app = getattr(ctx, "app", None)
    ai_engine = getattr(app, "ai", None) if app is not None else None
    if ai_engine is None:
        return {"enabled": False, "model": None}
    gcp = getattr(ctx, "gcp", None)
    is_live = type(gcp).__name__ == "Gcp" and "request" not in getattr(gcp, "__dict__", {})
    is_mock_ai = type(ai_engine).__name__ != "AiEngine"
    if not is_live and not is_mock_ai:
        return {"enabled": False, "model": None}

    candidates = []
    logical_tables = {tb["id"] for tb in (tables or [])}
    for tb in tables or []:
        if tb.get("core"):
            continue
        skey = (tb["project"], tb["dataset"], tb["logical"])
        schema = schemas_by_table.get(skey)
        fields = ["%s:%s" % (p, t) for p, t in (schema.leaf_types(60) if schema is not None else [])]
        fp = hashlib.sha1(("|".join(fields)).encode("utf-8")).hexdigest()[:12]
        ckey = ("bq:%s.%s.%s" % (tb["project"], tb["dataset"], tb["logical"]))
        candidates.append((ckey, "bigquery", tb, schema, None, fields, fp))
    for lg in logs or []:
        if lg.get("core") or (lg.get("routedToBigQuery") and lg.get("id") in logical_tables):
            continue
        obs = lg.get("_leaves") or log_leaves_by_short.get(lg.get("shortName") or "") or {}
        fields = ["%s:%s" % (p, t) for p, t in list(obs.items())[:60]]
        fp = hashlib.sha1(("|".join(fields)).encode("utf-8")).hexdigest()[:12]
        ckey = "log:%s:%s" % (lg.get("project") or ctx.project, lg.get("shortName") or lg.get("id"))
        candidates.append((ckey, "logging", lg, None, obs, fields, fp))

    if not candidates:
        return {"enabled": False, "model": None}

    cache = getattr(app, "cache", None)
    cached_profiles = {}
    uncached = []
    for item in candidates[:40]:
        ckey, _kind, _target, _schema, _obs, _fields, fp = item
        hit = cache.get(("telemetry_ai_profile", ctx.project, ckey, fp)) if cache is not None else None
        if isinstance(hit, dict) and hit.get("profile"):
            cached_profiles[ckey] = hit
        else:
            uncached.append(item)

    preferred = TELEMETRY_AI_MODEL
    used_model = ai_engine.effective_model(ctx.settings, preferred) if hasattr(ai_engine, "effective_model") else preferred
    if uncached:
        prompt_items = []
        for ckey, kind, target, _schema, _obs, fields, _fp in uncached:
            card = target.get("catalog") or {}
            prompt_items.append({
                "key": ckey,
                "sourceType": kind,
                "identifier": ("%s.%s.%s" % (target["project"], target["dataset"], target["logical"]))
                if kind == "bigquery" else (target.get("shortName") or target.get("id")),
                "baselineTitle": catalog.title_text(card) if card.get("known") else "",
                "baselineCategory": card.get("category") or "other",
                "fields": fields[:50],
            })
        prompt = (
            "Analyze the following telemetry sources discovered in project '%s' and return JSON matching the schema.\n"
            "Group related sources together under shared 'group' and 'groupKo' cluster names so administrators can "
            "browse a clean semantic catalog.\nSources:\n%s"
            % (ctx.project, json.dumps(prompt_items, ensure_ascii=False))
        )
        gen_meta = {}
        try:
            resp = ai_engine.generate(
                ctx.settings, preferred, prompt,
                system=AI_ENRICH_SYSTEM, schema=AI_ENRICH_SCHEMA,
                temperature=0.1, max_output_tokens=8192,
                thinking_level="LOW", meta_out=gen_meta, module="telemetry_ai",
            )
            used_model = gen_meta.get("model") or (
                ai_engine.effective_model(ctx.settings, preferred)
                if hasattr(ai_engine, "effective_model") else preferred
            )
            by_key = {
                s["key"]: s for s in ((resp or {}).get("sources") or [])
                if isinstance(s, dict) and isinstance(s.get("key"), str)
            }
            for ckey, _kind, _target, _schema, _obs, _fields, fp in uncached:
                prof = by_key.get(ckey)
                if prof:
                    entry = {"profile": prof, "model": used_model}
                    cached_profiles[ckey] = entry
                    if cache is not None:
                        cache.set(("telemetry_ai_profile", ctx.project, ckey, fp), entry, AI_PROFILE_TTL)
        except Exception as err:  # pylint: disable=broad-except
            LOG.warning("AI telemetry enrichment skipped: %s", type(err).__name__)

    applied = 0
    for ckey, kind, target, schema, obs, _fields, _fp in candidates[:40]:
        hit = cached_profiles.get(ckey)
        if not hit:
            continue
        _apply_ai_profile(target, kind, hit["profile"], hit.get("model") or used_model, schema=schema, observed_leaves=obs)
        applied += 1
    return {"enabled": applied > 0, "model": used_model if applied > 0 else None}


def _enrich_connector_meta(ctx, item):
    item.setdefault("bigquery", None)
    item.setdefault("logging", None)
    ds = (
        (ctx.ws.get("analytics_dataset") if hasattr(ctx, "ws") and isinstance(ctx.ws, dict) else None)
        or getattr(ctx, "audit_dataset", None)
        or "ge_analytics"
    )
    if item.get("source") == "bigquery" and isinstance(item.get("bigquery"), dict):
        bq = item["bigquery"]
        proj = bq.get("project") or ctx.project
        item["project"] = proj
        item["projectScope"] = "single"
        item["bigquerySink"] = "%s.%s" % (proj, bq.get("dataset") or ds)
        return
    flt = ((item.get("logging") or {}).get("filter") or "").strip()
    extracted = re.findall(r"projects/([a-z][a-z0-9\-]{4,29})/logs/", flt)
    extracted += re.findall(r'project_id\s*=\s*"([a-z][a-z0-9\-]{4,29})"', flt)
    if extracted:
        item["project"] = extracted[0]
        item["projectScope"] = "single"
        if item.get("sinkName") and not item.get("bigquerySink"):
            item["bigquerySink"] = "%s.%s" % (ctx.project, ds)
    else:
        item["project"] = item.get("project") or ctx.project
        item["projectScope"] = item.get("projectScope") or "single"


def _sample_missing_custom_logs(ctx, logs, log_leaves):
    """Samples up to 3 recent entries for uncatalogued custom logs missed by the global sample window."""
    gcp = getattr(ctx, "gcp", None)
    if not (type(gcp).__name__ == "Gcp" and "request" not in getattr(gcp, "__dict__", {})):
        return
    tasks = {}
    for lg in logs or []:
        if len(tasks) >= 10:
            break
        short = lg.get("shortName") or ""
        ln = lg.get("logName") or ""
        proj = lg.get("project") or ctx.project
        if not short or not ln or short in log_leaves or not config.valid_project(proj or ""):
            continue
        sid = catalog.sanitize_log_name(short)
        if sid in CORE_TABLES or catalog.describe(sid, short=short).get("known"):
            continue
        safe_ln = ln.replace("\\", "\\\\").replace('"', '\\"')
        tasks[short] = (
            lambda p=proj, flt=('logName="%s"' % safe_ln): ctx.rest(
                "POST", "%s/entries:list" % LOGGING_V2,
                json_body={"resourceNames": ["projects/%s" % p], "filter": flt, "orderBy": "timestamp desc", "pageSize": 3},
                timeout=4,
            )
        )
    if not tasks:
        return
    for short, (ok, data) in ctx.run(tasks).items():
        if not ok or not isinstance(data, dict):
            continue
        bucket = {}
        for entry in data.get("entries") or []:
            if isinstance(entry, dict):
                for pth, ftype in _extract_entry_leaves(entry):
                    bucket.setdefault(pth, ftype)
        if bucket:
            log_leaves[short] = bucket


def _compute_discover(ctx):
    by_table, by_log, by_filter = {}, {}, {}
    for conn in all_connectors(ctx):
        bq = conn.get("bigquery") or {}
        if conn.get("source") == "bigquery" and bq.get("table"):
            key = (bq.get("project"), bq.get("dataset"), bq["table"][:-2] if bq["table"].endswith("_*") else bq["table"])
            by_table.setdefault(key, conn.get("id"))
        elif conn.get("source") == "logging":
            sid = catalog.connector_source_id(conn)
            if sid:
                by_log.setdefault(sid, conn.get("id"))
            flt = ((conn.get("logging") or {}).get("filter") or "").strip()
            if flt:
                by_filter.setdefault(flt, conn.get("id"))
    schemas_by_table = {}
    res = ctx.run_composite({
        "tables": lambda: _discover_tables(ctx, by_table, schemas_by_table),
        "logs": lambda: _list_logs(ctx),
        "activity": lambda: _log_activity(ctx),
        "samples": lambda: _sample_logs_and_workloads(ctx),
    })
    tables = _task_result(ctx, res["tables"], "telemetry:tables", "Table discovery")
    logs = _task_result(ctx, res["logs"], "api:logging.logs", "The Cloud Logging log list")
    activity = _task_result(ctx, res["activity"], "api:monitoring.log_entry_count", "Log activity from Cloud Monitoring")
    workload_streams, short_30d, log_leaves = (
        res["samples"][1] if res.get("samples") and res["samples"][0] else ([], {}, {})
    )
    log_leaves = dict(log_leaves or {})
    _sample_missing_custom_logs(ctx, logs, log_leaves)
    logical = {tb["id"] for tb in tables or []}
    builtin = _builtin_logs(ctx)
    by_id = {}
    for log in logs or []:
        short = log["shortName"]
        sid = catalog.sanitize_log_name(short)
        counts = (activity.get(short) or [0] * ACTIVITY_DAYS) if activity is not None else None
        entries7d = sum(counts) if counts is not None else None
        if not entries7d and short in short_30d:
            entries7d = short_30d[short]
        core = sid in CORE_TABLES or short in builtin
        card = catalog.describe(sid, short=short, built_in=builtin.get(short) if short in builtin else
                                (None if core else []))
        quick = catalog.logging_connector(card, log["logName"])
        obs = log_leaves.get(short) or {}
        if quick is not None and not card.get("known") and obs:
            inferred = suggested_mapping(list(obs.items()))
            for fkey in ("operation", "status", "user", "model", "tokens"):
                if inferred.get(fkey):
                    quick["mapping"][fkey] = inferred[fkey]
            for fkey, dlbl in (("operation", "Operation"), ("status", "Status"), ("tokens", "Tokens")):
                if quick["mapping"].get(fkey) and not card.get("%sLabel" % fkey):
                    card["%sLabel" % fkey] = dlbl
        log.update({"id": sid, "routedToBigQuery": sid in logical, "core": core,
                    "registered": sid in by_log, "connectorId": by_log.get(sid),
                    "entries7d": entries7d, "daily": counts,
                    "columns": list(obs.keys()) if obs else None,
                    "catalog": card, "quickAdd": quick})
        by_id.setdefault(sid, log)
    if logs is not None and workload_streams:
        for stream in reversed(workload_streams):
            flt = stream["quickAdd"]["logging"]["filter"]
            conn_id = by_filter.get(flt)
            stream["registered"] = conn_id is not None
            stream["connectorId"] = conn_id
            logs.insert(0, stream)
    for tb in tables or []:
        log = by_id.get(tb["id"])
        if log is not None:
            tb["entries7d"], tb["daily"] = log["entries7d"], log["daily"]
    ai_info = _ai_enrich_sources(ctx, tables, logs, schemas_by_table, log_leaves)
    for lg in logs or []:
        lg.pop("_leaves", None)
    return {"tables": tables, "logs": logs, "activityDays": ACTIVITY_DAYS, "aiGrouping": ai_info}


def discover(ctx):
    """Every telemetry source of the project, described and grouped dynamically."""
    if type(getattr(ctx, "gcp", None)).__name__ == "Gcp" and getattr(ctx, "app", None):
        key = ("telemetry_discover", ctx.project, tuple(getattr(ctx, "projects", None) or [ctx.project]), getattr(ctx, "version", 0))
        val, _ = ctx.app.cache.get_or_compute(key, HEALTH_TTL, lambda: _compute_discover(ctx))
        return copy.deepcopy(val)
    return _compute_discover(ctx)


# ---------------------------------------------------------------------------
# Connectors
# ---------------------------------------------------------------------------

def _connector_health(ctx, conn):
    """Returns (health, lastEvent)."""
    try:
        if conn.get("source") == "bigquery":
            src, mapping = _bq_source(ctx, conn)
            time_col = mapping["time"]
            sql = ("WITH %s\nSELECT MAX(%s) AS last FROM s" % (
                src.cte("s", start="h_start", end="h_end", time_col=time_col), quote_ident(time_col)))
            now = dt.datetime.now(dt.timezone.utc)
            params = {"h_start": now - dt.timedelta(days=30), "h_end": now + dt.timedelta(minutes=5),
                      "sfx_start": (now - dt.timedelta(days=31)).strftime("%Y%m%d"),
                      "sfx_end": (now + dt.timedelta(days=1)).strftime("%Y%m%d")}
            rows = ctx.query(sql, params, "telemetry")
            return "ok", iso(rows[0].get("last")) if rows else None
        flt = (conn.get("logging") or {}).get("filter") or ""
        since = iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30))
        body = {"resourceNames": _logging_resource_names(ctx, flt),
                "filter": "(%s) AND timestamp>=\"%s\"" % (flt, since),
                "orderBy": "timestamp desc", "pageSize": 1}
        data = ctx.rest("POST", "%s/entries:list" % LOGGING_V2, json_body=body, timeout=6)
        entries = data.get("entries") or []
        return "ok", iso(parse_rfc3339(entries[0].get("timestamp"))) if entries else None
    except (GcpError, ValueError, ApiError) as err:
        LOG.info("connector health check failed id=%s kind=%s", conn.get("id"), getattr(err, "kind", type(err).__name__))
        return "error", None


def with_health(ctx, conn):
    item = copy.deepcopy(conn)
    _enrich_connector_meta(ctx, item)
    key = ("connector_health", json.dumps(item, sort_keys=True, default=str))
    (health, last), _ = ctx.app.cache.get_or_compute(key, HEALTH_TTL, lambda: _connector_health(ctx, item))
    item["health"] = health
    item["lastEvent"] = last
    item["catalog"] = _card_with_meta(catalog.for_connector(item), item.get("meta"), item)
    return item


def list_connectors(ctx):
    conns = all_connectors(ctx)
    res = ctx.run({str(i): (lambda c=c: with_health(ctx, c)) for i, c in enumerate(conns)})
    out = []
    for i in range(len(conns)):
        ok, value = res[str(i)]
        if ok:
            out.append(value)
        else:
            item = copy.deepcopy(conns[i])
            item.update({"health": "error", "lastEvent": None,
                         "catalog": _card_with_meta(catalog.for_connector(conns[i]), conns[i].get("meta"), conns[i])})
            out.append(item)
    return {"connectors": out}


def _valid_json_path(path):
    if not isinstance(path, str) or not path or len(path) > 300:
        return False
    return all(SEGMENT_RE.match(seg) for seg in path.split("."))


def _bq_source(ctx, conn):
    bq = conn.get("bigquery") or {}
    project, dataset, table = bq.get("project"), bq.get("dataset"), (bq.get("table") or "")
    if table.endswith("_*"):
        table = table[:-2]
    if not (config.valid_project(project or "") and config.valid_dataset(dataset or "") and config.valid_table(table)):
        raise ApiError("bad_request", "The BigQuery table reference is invalid.")
    src = ctx.registry.source(project, dataset, table, ctx.quota_project, ctx.max_bytes)
    if src is None:
        src = ctx.registry.table_source(project, dataset, table, ctx.quota_project)
    mapping = dict(conn.get("mapping") or {})
    if not mapping.get("time"):
        mapping["time"] = src.time_column()
    return src, mapping


def validate_connector(ctx, body):
    name = body.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80 or config.CONTROL_RE.search(name):
        raise ApiError("bad_request", "Field 'name' must be 1 to 80 characters.")
    source = body.get("source")
    if source not in ("bigquery", "logging"):
        raise ApiError("bad_request", "Field 'source' must be 'bigquery' or 'logging'.")
    enabled = body.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ApiError("bad_request", "Field 'enabled' must be a boolean.")
    mapping = body.get("mapping") or {}
    if not isinstance(mapping, dict) or set(mapping) - set(MAPPING_KEYS):
        raise ApiError("bad_request", "Field 'mapping' may only contain: %s." % ", ".join(MAPPING_KEYS))
    clean_map = {}
    for key in MAPPING_KEYS:
        value = mapping.get(key)
        if value in (None, ""):
            clean_map[key] = None
            continue
        if not _valid_json_path(value):
            raise ApiError("bad_request", "Mapping '%s' must be a dotted path of identifiers." % key)
        clean_map[key] = value
    out = {"name": name.strip(), "enabled": enabled, "source": source, "bigquery": None, "logging": None,
           "mapping": clean_map}
    clean_meta = _sanitize_meta(body.get("meta"))
    if clean_meta:
        out["meta"] = clean_meta
    if source == "bigquery":
        bq = body.get("bigquery")
        if not isinstance(bq, dict):
            raise ApiError("bad_request", "Field 'bigquery' must contain project, dataset and table.")
        project = (bq.get("project") or "").strip().lower()
        dataset = (bq.get("dataset") or "").strip()
        table = (bq.get("table") or "").strip()
        bare = table[:-2] if table.endswith("_*") else table
        if not (config.valid_project(project) and config.valid_dataset(dataset) and config.valid_table(bare)):
            raise ApiError("bad_request", "Field 'bigquery' must contain a valid project, dataset and table.")
        out["bigquery"] = {"project": project, "dataset": dataset, "table": table}
        try:
            src, _ = _bq_source(ctx, out)
        except GcpError as err:
            if err.kind == "not_found":
                raise ApiError("bad_request", "The BigQuery table was not found.")
            LOG.warning("connector validation failed kind=%s", err.kind)
            raise ApiError("upstream_error", "The BigQuery table schema could not be read.")
        for key, path in clean_map.items():
            if path is None:
                continue
            if "[" in path or build_expr("t", src.schema, path) is None:
                raise ApiError("bad_request", "Mapping '%s' refers to a column that does not exist." % key)
        time_path = clean_map["time"] or src.time_column()
        node = src.schema.node(time_path) if time_path else None
        if node is None or node.type != "TIMESTAMP" or "." in time_path:
            raise ApiError("bad_request", "Mapping 'time' must be a top-level TIMESTAMP column.")
        out["mapping"]["time"] = node.name
    else:
        lg = body.get("logging")
        flt = lg.get("filter") if isinstance(lg, dict) else None
        if not isinstance(flt, str) or not flt.strip() or len(flt) > 2000 or re.search(r"[\x00-\x08\x0b-\x1f\x7f]", flt):
            raise ApiError("bad_request", "Field 'logging.filter' must be 1 to 2000 characters.")
        out["logging"] = {"filter": flt.strip()}
        if not out["mapping"]["time"]:
            out["mapping"]["time"] = "timestamp"
    return out


def save_connector(ctx):
    clean = validate_connector(ctx, ctx.body)
    conn_id = ctx.body.get("id")
    if conn_id is not None and (not isinstance(conn_id, str) or not CONNECTOR_ID_RE.match(conn_id)):
        raise ApiError("bad_request", "Field 'id' is invalid.")
    if conn_id and conn_id in ctx.app.settings.config_connector_ids():
        raise ApiError("conflict", "This connector is defined in config.yaml and cannot be edited from the UI.")
    actor = ctx.identity.email

    def mutate(connectors):
        if conn_id:
            for idx, existing in enumerate(connectors):
                if existing.get("id") == conn_id:
                    updated = dict(existing)
                    updated.update(clean)
                    updated["updatedAt"] = now_iso()
                    updated["updatedBy"] = actor
                    connectors[idx] = updated
                    return updated
            raise ApiError("not_found", "Connector not found.")
        if len(connectors) >= 100:
            raise ApiError("conflict", "The maximum number of connectors has been reached.")
        created = dict(clean, id="conn_%s" % secrets.token_hex(4), createdAt=now_iso(), createdBy=actor)
        connectors.append(created)
        return created

    saved = ctx.app.settings.save_connectors(mutate, actor)
    ctx.app.on_settings_changed()
    settings, version = ctx.app.settings.snapshot()
    ctx.settings, ctx.version = settings, version
    return {"connector": with_health(ctx, saved), "connectors": list_connectors(ctx)["connectors"]}


def delete_connector(ctx):
    conn_id = ctx.body.get("id")
    if not isinstance(conn_id, str) or not CONNECTOR_ID_RE.match(conn_id):
        raise ApiError("bad_request", "Field 'id' is invalid.")
    if conn_id in ctx.app.settings.config_connector_ids():
        raise ApiError("conflict", "This connector is defined in config.yaml and cannot be deleted from the UI.")

    def mutate(connectors):
        before = len(connectors)
        connectors[:] = [c for c in connectors if c.get("id") != conn_id]
        if len(connectors) == before:
            raise ApiError("not_found", "Connector not found.")
        return conn_id

    ctx.app.settings.save_connectors(mutate, ctx.identity.email)
    ctx.app.on_settings_changed()
    settings, version = ctx.app.settings.snapshot()
    ctx.settings, ctx.version = settings, version
    return {"deleted": conn_id, "connectors": list_connectors(ctx)["connectors"]}


def create_sink(ctx):
    """One-click Cloud Logging -> BigQuery sink provisioning + dataset IAM grant + optional connector creation."""
    body = ctx.body or {}
    log_names = [str(n).strip() for n in (body.get("logNames") or []) if isinstance(n, str) and n.strip()]
    raw_filter = (body.get("filter") or "").strip()
    extra_filter = (body.get("extraFilter") or "").strip()
    if not raw_filter:
        parts = []
        if len(log_names) == 1:
            parts.append('logName="%s"' % log_names[0].replace("\\", "\\\\").replace('"', '\\"'))
        elif len(log_names) > 1:
            parts.append("(" + " OR ".join('logName="%s"' % n.replace("\\", "\\\\").replace('"', '\\"') for n in log_names) + ")")
        if extra_filter:
            parts.append("(%s)" % extra_filter if parts else extra_filter)
        raw_filter = " AND ".join(parts)
    if not raw_filter or len(raw_filter) > 4000 or re.search(r"[\x00-\x08\x0b-\x1f\x7f]", raw_filter):
        raise ApiError("bad_request", "A valid Cloud Logging filter is required.")

    extracted = re.findall(r"projects/([a-z][a-z0-9\-]{4,29})/logs/", raw_filter)
    sink_project = (body.get("project") or (extracted[0] if extracted else "") or ctx.project or "").strip().lower()
    if not config.valid_project(sink_project):
        sink_project = ctx.project
    dest_project = ctx.project
    dataset = (body.get("dataset") or ctx.ws.get("analytics_dataset") or "ge_analytics").strip()
    if not config.valid_dataset(dataset):
        raise ApiError("bad_request", "Field 'dataset' must be a valid BigQuery dataset name.")

    sink_name = (body.get("sinkName") or "").strip()
    if not sink_name:
        slug = re.sub(r"[^a-zA-Z0-9_]+", "_", (body.get("sourceId") or "telemetry")).strip("_").lower()
        sink_name = ("cp_sink_%s" % (slug or "telemetry"))[:80]
    if not re.match(r"^[A-Za-z0-9_\-]{1,100}$", sink_name):
        raise ApiError("bad_request", "Field 'sinkName' must contain only letters, digits, underscores or hyphens.")

    bq_base = "https://bigquery.googleapis.com/bigquery/v2"
    try:
        ctx.rest("GET", "%s/projects/%s/datasets/%s" % (bq_base, dest_project, dataset))
    except GcpError as err:
        if err.kind == "not_found":
            ctx.rest("POST", "%s/projects/%s/datasets" % (bq_base, dest_project),
                     json_body={"datasetReference": {"projectId": dest_project, "datasetId": dataset}})
        else:
            raise ApiError("upstream_error", "Could not verify BigQuery dataset '%s.%s'." % (dest_project, dataset))

    destination = "bigquery.googleapis.com/projects/%s/datasets/%s" % (dest_project, dataset)
    sink_payload = {
        "name": sink_name,
        "destination": destination,
        "filter": raw_filter,
        "bigqueryOptions": {"usePartitionedTables": True},
    }
    sink_url = "%s/projects/%s/sinks" % (LOGGING_V2, urllib.parse.quote(sink_project, safe=""))
    try:
        sink_resp = ctx.rest("POST", "%s?uniqueWriterIdentity=true" % sink_url, json_body=sink_payload)
    except GcpError as err:
        if err.status == 409 or err.kind == "conflict":
            put_url = "%s/%s?uniqueWriterIdentity=true&updateMask=destination,filter,bigqueryOptions" % (
                sink_url, urllib.parse.quote(sink_name, safe=""))
            sink_resp = ctx.rest("PUT", put_url, json_body=sink_payload)
        else:
            LOG.warning("create_sink failed project=%s sink=%s kind=%s", sink_project, sink_name, err.kind)
            raise ApiError("upstream_error", "Could not create or update Cloud Logging sink '%s'." % sink_name)

    writer_identity = (sink_resp or {}).get("writerIdentity") or ""
    iam_granted = False
    if writer_identity.startswith("serviceAccount:"):
        sa_email = writer_identity.split(":", 1)[1].strip()
        try:
            ds_meta = ctx.rest("GET", "%s/projects/%s/datasets/%s" % (bq_base, dest_project, dataset))
            access = list((ds_meta or {}).get("access") or [])
            already = any(
                isinstance(a, dict) and a.get("userByEmail") == sa_email
                and a.get("role") in ("WRITER", "OWNER", "roles/bigquery.dataEditor", "roles/bigquery.admin")
                for a in access
            )
            if not already:
                access.append({"role": "WRITER", "userByEmail": sa_email})
                ctx.rest("PATCH", "%s/projects/%s/datasets/%s" % (bq_base, dest_project, dataset),
                         json_body={"access": access})
            iam_granted = True
        except GcpError as err:
            LOG.warning("dataset IAM grant failed dataset=%s sa=%s kind=%s", dataset, sa_email, err.kind)

    saved_conn = None
    quick_add = body.get("quickAdd")
    if body.get("createConnector", True) and isinstance(quick_add, dict):
        try:
            clean = validate_connector(ctx, quick_add)
            flt_target = ((clean.get("logging") or {}).get("filter") or "").strip()
            existing = next(
                (c for c in all_connectors(ctx)
                 if ((c.get("logging") or {}).get("filter") or "").strip() == flt_target
                 or c.get("name") == clean.get("name")),
                None,
            )
            if existing is not None:
                saved_conn = existing
            else:
                actor = ctx.identity.email

                def mutate(connectors):
                    created = dict(clean, id="conn_%s" % secrets.token_hex(4), createdAt=now_iso(), createdBy=actor)
                    connectors.append(created)
                    return created

                saved_conn = ctx.app.settings.save_connectors(mutate, actor)
                ctx.app.on_settings_changed()
                settings, version = ctx.app.settings.snapshot()
                ctx.settings, ctx.version = settings, version
        except ApiError:
            pass

    ctx.app.cache.clear()
    return {
        "sink": {
            "name": (sink_resp or {}).get("name") or sink_name,
            "project": sink_project,
            "destination": destination,
            "dataset": dataset,
            "filter": raw_filter,
            "writerIdentity": writer_identity,
            "iamGranted": iam_granted,
        },
        "connector": with_health(ctx, saved_conn) if saved_conn else None,
        "connectors": list_connectors(ctx)["connectors"],
    }


# ---------------------------------------------------------------------------
# GET /api/v1/telemetry/connectors/data
# ---------------------------------------------------------------------------

def _get_path(obj, path):
    """Resolves a dotted path (with optional [n] indexes) on a LogEntry; returns scalars only."""
    if not isinstance(obj, dict) or not path:
        return None
    if path == "logName":
        ln = obj.get("logName")
        if isinstance(ln, str) and "/logs/" in ln:
            return urllib.parse.unquote(ln.split("/logs/", 1)[1])
    cur = obj
    for seg in path.split("."):
        m = SEGMENT_RE.match(seg)
        if not m or not isinstance(cur, dict):
            return None
        key, idx = m.group(1), m.group(2)
        cur = cur.get(key)
        if cur is None:
            return None
        if idx is not None:
            if isinstance(cur, list) and int(idx) < len(cur):
                cur = cur[int(idx)]
            else:
                return None
    if isinstance(cur, (dict, list)):
        return None
    return cur


_SENSITIVE_SUMMARY_KEYS = {
    "content", "prompt", "response", "input", "output", "system_instruction", "messages", "parts", "text",
}


def _summarize_structured_payload(payload):
    """Compact scalar key=value summary for structured telemetry dicts without exposing prompt/content fields."""
    if not isinstance(payload, dict) or len(payload) < 2:
        return None
    pairs = []

    def collect(prefix, obj, depth):
        if len(pairs) >= 6 or depth > 2 or not isinstance(obj, dict):
            return
        for k, val in obj.items():
            if len(pairs) >= 6 or not isinstance(k, str):
                break
            if k.lower() in _SENSITIVE_SUMMARY_KEYS:
                continue
            label = "%s.%s" % (prefix, k) if prefix else k
            if isinstance(val, dict):
                collect(label, val, depth + 1)
            elif isinstance(val, (int, float, bool)):
                pairs.append("%s=%s" % (k, val))
            elif isinstance(val, str) and val.strip() and len(val) <= 80:
                pairs.append("%s=%s" % (k, val.strip()))

    collect("", payload, 1)
    return " · ".join(pairs)[:MESSAGE_CHARS] if len(pairs) >= 2 else None


def _log_message(entry):
    """The human-readable line of a LogEntry: textPayload, a JSON message, audit status, HTTP request or metric summary."""
    payload = entry.get("jsonPayload") if isinstance(entry.get("jsonPayload"), dict) else {}
    text = entry.get("textPayload")
    if not isinstance(text, str):
        text = next((payload[k] for k in ("message", "msg", "summary", "error") if isinstance(payload.get(k), str)), None)
        if not isinstance(text, str):
            content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
            parts = content.get("parts") if isinstance(content.get("parts"), list) else []
            for part in parts:
                if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip():
                    role = content.get("role") or ""
                    text = ("%s: %s" % (role, part["text"].strip())) if role else part["text"].strip()
                    break
        if not isinstance(text, str) and "gen_ai." in (entry.get("logName") or ""):
            ev = (entry.get("labels") or {}).get("event.name") or urllib.parse.unquote(
                (entry.get("logName") or "").split("/logs/", 1)[-1]
            )
            finish = payload.get("finish_reason")
            ver = (((entry.get("otel") or {}).get("resource") or {}).get("attributes") or {}).get("faas.version") or ""
            cnt = payload.get("content") if isinstance(payload.get("content"), str) else ""
            info = ["[%s]" % ev]
            if finish:
                info.append("finish_reason=%s" % finish)
            if cnt:
                info.append("content=%s" % cnt)
            if ver:
                info.append("(%s)" % ver)
            text = " · ".join(info)
    if not isinstance(text, str):
        proto = entry.get("protoPayload") if isinstance(entry.get("protoPayload"), dict) else {}
        status = proto.get("status") if isinstance(proto.get("status"), dict) else {}
        text = status.get("message") if isinstance(status.get("message"), str) else None
    if not isinstance(text, str):
        req = entry.get("httpRequest") if isinstance(entry.get("httpRequest"), dict) else {}
        if req.get("requestMethod") or req.get("requestUrl"):
            text = "%s %s (%s)" % (req.get("requestMethod") or "HTTP", req.get("requestUrl") or "", req.get("status") or "-")
    if not isinstance(text, str) and payload:
        text = _summarize_structured_payload(payload)
    text = text.strip() if isinstance(text, str) else ""
    return text[:MESSAGE_CHARS] or None


def _message_expr(schema):
    """SQL for the same line in an exported or custom table (typed payload columns are named jsonpayload_<type>)."""
    candidates = ["textPayload", "message", "summary", "error_message", "jsonPayload.message"]
    candidates += ["%s.message" % node.name for name, node in schema.tree.items()
                   if name.startswith("jsonpayload") and name != "jsonpayload"]
    candidates.extend([
        "protopayload_auditlog.status.message",
        "schemaErrorDetail",
        "jsonPayload.gen_ai_output_messages[0].parts[0].content",
        "jsonPayload.content.parts[0].text",
    ])
    sql = expr("t", schema, *candidates, cast="STRING")
    return "SUBSTR(%s, 1, %d)" % (sql, MESSAGE_CHARS) if sql else "CAST(NULL AS STRING)"


def _empty_data(conn, rng):
    return {"connector": conn, "kpis": {"events": {"value": None, "previous": None}, "users": None, "tokens": None,
                                        "lastEvent": None},
            "trend": None, "topOperations": None, "topStatuses": None, "topUsers": None, "topModels": None,
            "recent": None, "sample": None, "valueNames": None}


def _bq_data(ctx, conn, rng, out):
    src, mapping = _bq_source(ctx, conn)
    s = src.schema
    time_col = mapping["time"]
    ts = "t.%s" % quote_ident(time_col)
    null = "CAST(NULL AS STRING)"
    user = expr("t", s, mapping["user"], cast="STRING") if mapping.get("user") else None
    model = expr("t", s, mapping["model"], cast="STRING") if mapping.get("model") else None
    tokens = expr("t", s, mapping["tokens"], cast="INT64") if mapping.get("tokens") else None
    op = expr("t", s, mapping["operation"], cast="STRING") if mapping.get("operation") else None
    status = expr("t", s, mapping["status"], cast="STRING") if mapping.get("status") else None
    proj = ("d AS (SELECT %s AS ts, %s AS user, %s AS model, %s AS tokens, %s AS operation, %s AS status FROM src t)"
            % (ts, user or null, model or null, tokens or "CAST(NULL AS INT64)", op or null, status or null))
    label = src.label
    params_prev = range_params(rng)
    params_cur = range_params(rng, with_prev=False)
    kpi_sql = ("WITH %s,\n%s\nSELECT period, bucket, COUNT(*) AS events, SUM(tokens) AS tokens, "
               "COUNT(DISTINCT user) AS users, MAX(ts) AS last FROM (SELECT d.*, %s AS period, %s AS bucket FROM d)\n"
               "GROUP BY ROLLUP(period, bucket)" % (src.cte("src", start="prev_start_ts", time_col=time_col), proj,
                                                   period_sql("ts"), bucket_or_sentinel("ts")))
    tops_sql = ("WITH %s,\n%s\nSELECT 'operation' AS kind, operation AS k, COUNT(*) AS events, SUM(tokens) AS tokens "
                "FROM d GROUP BY k\nUNION ALL SELECT 'user', user, COUNT(*), SUM(tokens) FROM d GROUP BY user\n"
                "UNION ALL SELECT 'model', model, COUNT(*), SUM(tokens) FROM d GROUP BY model\n"
                "UNION ALL SELECT 'status', status, COUNT(*), SUM(tokens) FROM d GROUP BY status"
                % (src.cte("src", start="start_ts", time_col=time_col), proj))
    recent_sql = ("WITH %s\nSELECT %s AS ts, %s AS user, %s AS operation, %s AS model, %s AS tokens, %s AS status, "
                  "%s AS message, SUBSTR(TO_JSON_STRING(t), 1, 2048) AS payload FROM src t ORDER BY ts DESC LIMIT 20"
                  % (src.cte("src", start="start_ts", time_col=time_col), ts, user or null, op or null, model or null,
                     tokens or "CAST(NULL AS INT64)", status or null, _message_expr(s)))
    res = ctx.run({"kpi": lambda: ctx.query(kpi_sql, params_prev, "telemetry"),
                   "tops": lambda: ctx.query(tops_sql, params_cur, "telemetry"),
                   "recent": lambda: ctx.query(recent_sql, params_cur, "telemetry")})
    ok, rows = res["kpi"]
    if not ok:
        ctx.warn_error(label, rows, "The connector table")
        return out
    ctx.add_source(label)
    totals, buckets = split_rollup(rows)
    cur, prev = totals.get("cur", {}), totals.get("prev", {})
    out["kpis"] = {"events": {"value": to_int(cur.get("events")), "previous": to_int(prev.get("events"))},
                   "users": to_int(cur.get("users")) if user else None,
                   "tokens": to_int(cur.get("tokens")) if tokens else None, "lastEvent": iso(cur.get("last"))}
    series = [timeutil.series("events", "Events", bucket_values(rng, buckets, "events"))]
    if tokens:
        series.append(timeutil.series("tokens", "Tokens", bucket_values(rng, buckets, "tokens")))
    out["trend"] = timeutil.timeline(rng, series)
    ok, rows = res["tops"]
    if ok:
        groups = {"operation": [], "user": [], "model": [], "status": []}
        for row in rows:
            if row.get("k") is None:
                continue
            groups.setdefault(row.get("kind"), []).append(row)
        for key in groups:
            groups[key].sort(key=lambda r: -to_int(r.get("events")))
        # Unmapped fields have no source: their lists stay null.
        out["topOperations"] = [{"operation": r["k"], "events": to_int(r.get("events"))}
                                for r in groups["operation"][:10]] if op else None
        out["topStatuses"] = [{"status": r["k"], "events": to_int(r.get("events"))}
                              for r in groups["status"][:10]] if status else None
        out["topUsers"] = [{"uid": uid_for(r["k"]), "user": r["k"], "events": to_int(r.get("events"))}
                           for r in groups["user"][:10]] if user else None
        out["topModels"] = [{"model": r["k"], "events": to_int(r.get("events")),
                             "tokens": to_int(r.get("tokens")) if tokens else None}
                            for r in groups["model"][:10]] if model else None
    else:
        ctx.warn_error(label, rows, "The connector table")
    ok, rows = res["recent"]
    if ok:
        bq_proj = (conn.get("bigquery") or {}).get("project") or ctx.project
        out["recent"] = [{"time": iso(r.get("ts")), "project": str(bq_proj), "user": r.get("user"),
                          "operation": r.get("operation"), "model": r.get("model"),
                          "tokens": to_int(r.get("tokens")) if r.get("tokens") is not None else None,
                          "status": r.get("status"), "message": r.get("message") or None,
                          "payload": r.get("payload")} for r in rows]
    else:
        ctx.warn_error(label, rows, "The connector table")
    return out


def _logging_data(ctx, conn, rng, out):
    flt = (conn.get("logging") or {}).get("filter") or ""
    is_live = type(getattr(ctx, "gcp", None)).__name__ == "Gcp"
    mapping = conn.get("mapping") or {}
    prev = rng.previous()
    label = "logging:%s" % conn.get("name")
    if is_live:
        earliest = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
        query_start = max(min(prev.start_ts(), rng.end_ts() - dt.timedelta(days=30)), earliest)
    else:
        query_start = prev.start_ts()
    body_filter = '(%s) AND timestamp>="%s" AND timestamp<"%s"' % (flt, iso(query_start), iso(rng.end_ts()))
    entries = []
    token = None
    truncated = False
    resource_names = _logging_resource_names(ctx, flt)
    try:
        pages = 0
        max_pages = 1 if is_live else 20
        page_cap = 600 if is_live else 1000
        while pages < max_pages:
            pages += 1
            body = {"resourceNames": resource_names, "filter": body_filter,
                    "orderBy": "timestamp desc", "pageSize": min(page_cap, MAX_LOG_ENTRIES - len(entries))}
            if token:
                body["pageToken"] = token
            data = ctx.rest("POST", "%s/entries:list" % LOGGING_V2, json_body=body, timeout=10 if is_live else 45)
            entries.extend(e for e in data.get("entries") or [] if isinstance(e, dict))
            token = data.get("nextPageToken")
            if not token:
                break
            if len(entries) >= MAX_LOG_ENTRIES or (is_live and pages >= max_pages):
                truncated = len(entries) >= MAX_LOG_ENTRIES
                break
    except GcpError as err:
        ctx.warn_error(label, err, "Cloud Logging entries for this connector")
        return out
    ctx.add_source(label)
    if truncated:
        ctx.warn(label, "truncated", "Log entries were truncated at %d." % MAX_LOG_ENTRIES)
    tz = timeutil.get_zone(ctx.tz_name)
    time_path = mapping.get("time") or "timestamp"
    cur_events = prev_events = 0
    tokens_total = 0
    users = set()
    last = None
    ev_pairs, tok_pairs = [], []
    ops, statuses, top_users, models = {}, {}, {}, {}
    recent = []
    has_in_window = any(
        (parse_rfc3339(_get_path(e, time_path) if time_path != "timestamp" else e.get("timestamp")) or rng.start_ts()) >= rng.start_ts()
        for e in entries
    ) if is_live else True
    include_30d = is_live and (not has_in_window or "jsonPayload:*" in flt or "gen_ai." in flt)
    for entry in entries:
        ts = parse_rfc3339(_get_path(entry, time_path) if time_path != "timestamp" else entry.get("timestamp"))
        if ts is None:
            continue
        user = _get_path(entry, mapping["user"]) if mapping.get("user") else None
        model = _get_path(entry, mapping["model"]) if mapping.get("model") else None
        tok_raw = _get_path(entry, mapping["tokens"]) if mapping.get("tokens") else None
        tok = to_int(tok_raw) if tok_raw is not None else None
        op = _get_path(entry, mapping["operation"]) if mapping.get("operation") else None
        status = _get_path(entry, mapping["status"]) if mapping.get("status") else None
        if ts < rng.start_ts() and not include_30d:
            if ts >= prev.start_ts():
                prev_events += 1
            continue
        cur_events += 1
        day = ts.astimezone(tz).date()
        ev_pairs.append((day, 1))
        if tok is not None:
            tokens_total += tok
            tok_pairs.append((day, tok))
        if user is not None:
            users.add(str(user))
            top_users[str(user)] = top_users.get(str(user), 0) + 1
        if op is not None:
            ops[str(op)] = ops.get(str(op), 0) + 1
        if status is not None:
            statuses[str(status)] = statuses.get(str(status), 0) + 1
        if model is not None:
            agg = models.setdefault(str(model), [0, 0])
            agg[0] += 1
            agg[1] += tok or 0
        last = ts if last is None or ts > last else last
        if len(recent) < 20:
            entry_proj = ((entry.get("resource") or {}).get("labels") or {}).get("project_id") or conn.get("project") or ctx.project
            recent.append({"time": iso(ts), "project": str(entry_proj), "user": str(user) if user is not None else None,
                           "operation": str(op) if op is not None else None,
                           "model": str(model) if model is not None else None, "tokens": tok,
                           "status": str(status) if status is not None else None,
                           "message": _log_message(entry),
                           "payload": json.dumps(entry, separators=(",", ":"), default=str)[:2048]})
    # Entries are read newest first: a truncated read misses the oldest entries. When the cut falls inside the
    # selected period, counts are lower bounds and the days up to the cut are unknown (null), never zero.
    oldest = parse_rfc3339(entries[-1].get("timestamp")) if truncated and entries else None
    partial = bool(oldest is not None and oldest >= rng.start_ts())
    out["sample"] = {"limit": MAX_LOG_ENTRIES, "truncated": truncated, "partial": partial,
                     "since": iso(oldest) if partial else None}
    out["kpis"] = {"events": {"value": cur_events, "previous": None if truncated else prev_events},
                   "users": len(users) if mapping.get("user") else None,
                   "tokens": tokens_total if mapping.get("tokens") else None, "lastEvent": iso(last)}
    trend_rng = rng
    if include_30d and ev_pairs:
        min_day = min(d for d, _ in ev_pairs)
        if min_day < rng.start:
            trend_rng = timeutil.DateRange(
                min(min_day, rng.end - dt.timedelta(days=29)), rng.end, rng.granularity, rng.tz_name
            )
    ev_values = timeutil.fold(trend_rng, ev_pairs, int)
    tok_values = timeutil.fold(trend_rng, tok_pairs, int) if mapping.get("tokens") else None
    if partial:
        cut = trend_rng.bucket_index(oldest)
        for i in range((cut if cut is not None else -1) + 1):
            ev_values[i] = None
            if tok_values is not None:
                tok_values[i] = None
    series = [timeutil.series("events", "Events", ev_values)]
    if tok_values is not None:
        series.append(timeutil.series("tokens", "Tokens", tok_values))
    out["trend"] = timeutil.timeline(trend_rng, series)
    out["topOperations"] = ([{"operation": k, "events": v} for k, v in sorted(ops.items(), key=lambda kv: -kv[1])[:10]]
                            if mapping.get("operation") else None)
    out["topStatuses"] = ([{"status": k, "events": v} for k, v in sorted(statuses.items(), key=lambda kv: -kv[1])[:10]]
                          if mapping.get("status") else None)
    out["topUsers"] = ([{"uid": uid_for(k), "user": k, "events": v}
                        for k, v in sorted(top_users.items(), key=lambda kv: -kv[1])[:10]]
                       if mapping.get("user") else None)
    out["topModels"] = ([{"model": k, "events": v[0], "tokens": v[1] if mapping.get("tokens") else None}
                         for k, v in sorted(models.items(), key=lambda kv: -kv[1][0])[:10]]
                        if mapping.get("model") else None)
    out["recent"] = recent
    return out


def _engine_names(ctx):
    """{Agent Engine ID: display name} of the configured regions; {} when they cannot be listed.

    Names only label chart values, so listing failures are not reported as data source notices here (the
    Agent Registry page reports them)."""
    regions = tuple(ctx.ws.get("agent_engine_regions") or [])

    def build():
        child = ctx.child()
        engines, _ = agents.reasoning_engines(child)
        return {item["name"].rsplit("/", 1)[-1]: item["displayName"] for _, item in engines if item.get("displayName")}

    if not regions:
        return {}
    try:
        names, _ = ctx.app.cache.get_or_compute(("engine_names", ctx.project, regions), NAMES_TTL, build)
    except (GcpError, ApiError):
        return {}
    return names or {}


def _value_names(ctx, conn, out):
    """Display names for operation values the catalog knows how to name, for example Agent Engine IDs."""
    entry = catalog.lookup(catalog.connector_source_id(conn) or "")
    if not entry or entry.get("names") != "reasoning_engine":
        return None
    values = {o["operation"] for o in out.get("topOperations") or [] if o.get("operation")}
    values |= {r["operation"] for r in out.get("recent") or [] if r.get("operation")}
    if not values:
        return None
    names = _engine_names(ctx)
    found = {}
    for value in values:
        m = ENGINE_ID_RE.search(str(value))
        if m and names.get(m.group(1)):
            found[str(value)] = names[m.group(1)]
    return {"operation": found} if found else None


def _compute_connector_data(ctx, conn_id, rng):
    conn = next((c for c in all_connectors(ctx) if c.get("id") == conn_id), None)
    if conn is None:
        raise ApiError("not_found", "Connector not found.")
    item = copy.deepcopy(conn)
    _enrich_connector_meta(ctx, item)
    item["health"] = "ok"
    item["lastEvent"] = None
    item["catalog"] = _card_with_meta(catalog.for_connector(item), item.get("meta"), item)
    out = _empty_data(item, rng)
    if not conn.get("enabled", True):
        ctx.warn("connector:%s" % conn_id, "disabled", "This connector is disabled.")
        return out
    try:
        if conn.get("source") == "bigquery":
            _bq_data(ctx, item, rng, out)
        else:
            _logging_data(ctx, item, rng, out)
    except GcpError as err:
        item["health"] = "error"
        ctx.warn_error("connector:%s" % conn_id, err, "The connector source")
        return out
    item["lastEvent"] = (out.get("kpis") or {}).get("lastEvent")
    out["valueNames"] = _value_names(ctx, item, out)
    return out


def connector_data(ctx):
    rng = ctx.range
    conn_id = ctx.params.get("id") or ""
    if type(getattr(ctx, "gcp", None)).__name__ == "Gcp" and getattr(ctx, "app", None):
        key = (
            "telemetry_connector_data",
            conn_id,
            str(rng.start),
            str(rng.end),
            rng.granularity,
            ctx.project,
            getattr(ctx, "version", 0),
        )
        val, _ = ctx.app.cache.get_or_compute(key, HEALTH_TTL, lambda: _compute_connector_data(ctx, conn_id, rng))
        return copy.deepcopy(val)
    return _compute_connector_data(ctx, conn_id, rng)
