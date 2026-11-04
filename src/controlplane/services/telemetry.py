"""Telemetry Extensions: discovery of what the project already collects, Gemini profiling and grouping, one-click
dashboards for single sources and whole groups, and administrator overrides.

There is no built-in list of known logs. Sources are found from the project itself (the Cloud Logging log list, log
volume from Cloud Monitoring, sampled entries and BigQuery schemas) and described dynamically
(services/telemetry_profile.py). Gemini (telemetry.ai.model) profiles every source: title, description, category,
group, sensitivity, dashboard fields and whether routing it to BigQuery is worth its cost. Profiles are kept per source
fingerprint in the telemetry_profiles document, so only new or changed sources are sent again. Administrators can
override any card (telemetry_overrides). Every limit is a setting under telemetry.discovery / telemetry.ai.
Routing to BigQuery with cost controls lives in services/telemetry_routing.py.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import logging
import os
import re
import secrets
import urllib.parse

from .. import config, timeutil
from ..gcp import GcpError
from ..httpkit import ApiError, now_iso
from ..schema import SEGMENT_RE, build_expr, expr, quote_ident
from ..security import uid_for
from ..settings_store import aux_docs
from .base import (BUILTIN_PAGES, CORE_TABLES, bucket_or_sentinel, bucket_values, iso, parse_rfc3339, period_sql,
                   range_params, require_identity_admin, split_rollup, to_int)
from . import agents, monitoring
from . import telemetry_profile as profile
from . import telemetry_routing as routing

LOG = logging.getLogger("controlplane.telemetry")

LOGGING_V2 = "https://logging.googleapis.com/v2"
MAPPING_KEYS = ("time", "user", "model", "tokens", "operation", "status")
MAX_LOG_ENTRIES = config.DEFAULTS["telemetry"]["discovery"]["max_log_entries"]
MESSAGE_CHARS = 300
CONNECTOR_ID_RE = re.compile(r"^conn_[0-9a-f]{8}$")
SOURCE_KEY_RE = re.compile(r"^(log|bq|wl):[A-Za-z0-9_.:%/@\-]{1,600}$")
HEALTH_TTL = 300
LOG_ENTRY_METRIC = "logging.googleapis.com/log_entry_count"
ENGINE_ID_RE = re.compile(r"(?:reasoningEngines/)?([0-9]{6,})$")
NAMES_TTL = 600
NUMERIC_TYPES = profile.NUMERIC
PROFILES_DOC = "telemetry_profiles"
OVERRIDES_DOC = "telemetry_overrides"
MAX_PROFILES = 600
# Labels that only locate a resource and never name a workload.
LOCATION_LABELS = {"project_id", "location", "zone", "region"}

sanitize_log_name = profile.sanitize_log_name


def _tcfg(settings, section):
    tel = (settings or {}).get("telemetry") or {}
    merged = dict(config.DEFAULTS["telemetry"][section])
    merged.update(tel.get(section) or {})
    return merged


def _live(ctx):
    gcp = getattr(ctx, "gcp", None)
    return type(gcp).__name__ == "Gcp" and "request" not in getattr(gcp, "__dict__", {})


def suggested_mapping(leaf_types, settings=None):
    """Mapping for a table from its column names and types only (no values), using the name hints in settings."""
    stats = profile.new_stats()
    for path, ftype in leaf_types or []:
        if "REPEATED" not in (ftype or ""):
            stats["fields"][path] = profile._new_stat((ftype or "").split(" ")[0])
    return profile.infer_mapping(stats, settings, kind="bigquery")


def all_connectors(ctx):
    return [c for c in (ctx.settings["telemetry"].get("connectors") or []) if isinstance(c, dict)]


# ---------------------------------------------------------------------------
# BigQuery tables
# ---------------------------------------------------------------------------

def _routing_dataset(ctx):
    return routing.policy(ctx.settings)["dataset"]


def _explicit_datasets(ctx):
    out = {(ctx.project, ctx.audit_dataset), (ctx.project, _routing_dataset(ctx))}
    for ref in ctx.ws.get("telemetry_datasets") or []:
        parsed = config.parse_dataset_ref(ref, ctx.project)
        if parsed:
            out.add(parsed)
    return out


def discover_datasets(ctx):
    disc = _tcfg(ctx.settings, "discovery")
    active_projects = list(getattr(ctx, "projects", None) or [ctx.project])
    sel = getattr(ctx, "selected_project", "all") or "all"
    out = []
    for p in active_projects[:disc["max_projects"]]:
        if config.valid_project(p or "") and (p, ctx.audit_dataset) not in out:
            out.append((p, ctx.audit_dataset))
    if not out:
        out.append((ctx.project, ctx.audit_dataset))
    routed = (ctx.project, _routing_dataset(ctx))
    if routed not in out and (sel == "all" or ctx.project == sel):
        out.append(routed)
    for ref in ctx.ws.get("telemetry_datasets") or []:
        parsed = config.parse_dataset_ref(ref, ctx.project)
        if parsed and (sel == "all" or parsed[0] == sel) and parsed not in out:
            out.append(parsed)
    # On live GCP, other datasets of the scanned projects are listed too, so idle tables appear without settings.
    if type(getattr(ctx, "gcp", None)).__name__ == "Gcp" and config.valid_project(ctx.project or "") and \
            disc["max_datasets_per_project"] > 0:
        billing_cfg = ctx.ws.get("billing") or {}
        billing_pair = (config.billing_project(ctx.settings), billing_cfg.get("dataset") or "")
        hints = [h.lower() for h in disc["dataset_hints"] or []]
        for proj in [p for p in active_projects if config.valid_project(p or "")][:disc["max_projects"]]:
            try:
                data = ctx.rest("GET", "https://bigquery.googleapis.com/bigquery/v2/projects/%s/datasets" % proj,
                                params={"maxResults": max(30, disc["max_datasets_per_project"])}, timeout=8)
            except Exception:  # pylint: disable=broad-except
                continue
            ds_ids = [((item or {}).get("datasetReference") or {}).get("datasetId") or ""
                      for item in (data or {}).get("datasets") or []]
            ds_ids = [d for d in ds_ids if config.valid_dataset(d)]
            ds_ids.sort(key=lambda d: (0 if any(h in d.lower() for h in hints) else 1, d))
            for ds_id in ds_ids[:disc["max_datasets_per_project"]]:
                pair = (proj, ds_id)
                if pair != billing_pair and pair not in out:
                    out.append(pair)
    return out


def _table_stats(ctx, project, dataset):
    sql = "SELECT table_id, row_count, size_bytes, last_modified_time FROM `%s.%s.__TABLES__`" % (project, dataset)
    rows = ctx.query(sql, {}, "telemetry")
    return {r["table_id"]: r for r in rows if r.get("table_id")}


def _discover_tables(ctx, connectors_by_table, schemas_out):
    """Tables of the audit, routing, configured and auto-discovered datasets, or None when none could be scanned."""
    disc = _tcfg(ctx.settings, "discovery")
    tables = []
    scanned = 0
    explicit = _explicit_datasets(ctx)
    for project, dataset in discover_datasets(ctx):
        is_explicit = (project, dataset) in explicit
        label = "bigquery:%s" % dataset if project == ctx.project else "bigquery:%s.%s" % (project, dataset)
        try:
            groups = ctx.registry.tables(project, dataset, ctx.quota_project, ctx.max_bytes)
        except GcpError as err:
            if is_explicit and dataset != _routing_dataset(ctx):
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
        if len(names) > disc["max_tables_per_dataset"]:
            if is_explicit:
                ctx.warn(label, "truncated", "Only the first %d tables of %s are inspected." % (
                    disc["max_tables_per_dataset"], dataset))
            names = names[:disc["max_tables_per_dataset"]]
        schema_tasks = {n: (lambda n=n: ctx.registry.schema(project, dataset, groups[n]["latest"], ctx.quota_project))
                        for n in names}
        schemas = ctx.run(schema_tasks)
        failed = sum(1 for ok, _ in schemas.values() if not ok)
        if failed and is_explicit:
            ctx.warn(label, "partial_data", "The schema of %d table(s) in %s could not be read." % (failed, dataset))
        for name in names:
            info = groups[name]
            sharded = info["kind"] == "sharded"
            if sharded:
                members = [s for tid, s in stats.items() if tid.startswith(name + "_") and tid[len(name) + 1:].isdigit()]
            else:
                members = [stats[name]] if name in stats else []
            ok, meta = schemas.get(name, (False, None))
            schema = meta["schema"] if ok and meta else None
            has_time = schema is not None and any(n.type == "TIMESTAMP" and n.mode != "REPEATED" for n in schema.tree.values())
            conn_id = connectors_by_table.get((project, dataset, name))
            if not is_explicit and not has_time and conn_id is None:
                continue  # static lookup tables in opportunistically scanned datasets
            if schema is not None:
                schemas_out[(project, dataset, name)] = schema
            modified = max((to_int(s.get("last_modified_time")) for s in members), default=None) if members else None
            tables.append({
                "id": name.lower(), "project": project, "dataset": dataset, "table": name + ("_*" if sharded else ""),
                "logical": name, "sharded": sharded,
                "rows": sum(to_int(s.get("row_count")) for s in members) if members else None,
                "sizeBytes": sum(to_int(s.get("size_bytes")) for s in members) if members else None,
                "lastModified": iso(dt.datetime.fromtimestamp(modified / 1000.0, tz=dt.timezone.utc)) if modified else None,
                "core": name in CORE_TABLES and (project, dataset) == (ctx.project, ctx.audit_dataset),
                "routedDataset": dataset == _routing_dataset(ctx) and project == ctx.project,
                "registered": conn_id is not None, "connectorId": conn_id,
                "columns": schema.leaf_paths(200) if schema is not None else None,
                "entries7d": None, "daily": None,
            })
    return tables if scanned else None


# ---------------------------------------------------------------------------
# Cloud Logging logs, activity and samples
# ---------------------------------------------------------------------------

def _list_logs(ctx):
    """[{logName, shortName, project}] across scanned projects, or None when the log list failed."""
    tasks = {proj: (lambda p=proj: ctx.gcp.paginate("%s/projects/%s/logs" % (LOGGING_V2, p),
                                                    quota_project=ctx.quota_project, items_key="logNames",
                                                    page_size=500, max_pages=1))
             for proj in ctx.projects}
    results = ctx.run(tasks)
    hidden = [h.lower() for h in _tcfg(ctx.settings, "discovery")["hidden_logs"] or []]
    out, seen, succeeded, truncated_any = [], set(), 0, False
    for proj, (ok, val) in results.items():
        if not ok:
            if proj == ctx.project:
                ctx.warn_error("api:logging.logs", val, "The Cloud Logging log list")
            continue
        succeeded += 1
        names, truncated = val
        truncated_any = truncated_any or truncated
        for log_name in names:
            if isinstance(log_name, str) and "/logs/" in log_name:
                short = urllib.parse.unquote(log_name.split("/logs/", 1)[1])
                if any(h in short.lower() for h in hidden):
                    continue
                if (proj, short) not in seen:
                    seen.add((proj, short))
                    out.append({"logName": log_name, "shortName": short, "project": proj})
    if not succeeded:
        return None
    ctx.add_source("api:logging.logs")
    if truncated_any:
        ctx.warn("api:logging.logs", "truncated", "Only the first 500 log names per project are listed.")
    return out


def _log_metric(ctx, metric, days):
    """{log id: [daily values, oldest first]} over the last `days` days from a per-log Cloud Logging metric."""
    tz = timeutil.get_zone(ctx.tz_name)
    now = dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(tz).date()
    index = {today - dt.timedelta(days=days - 1 - i): i for i in range(days)}
    series = monitoring.time_series(ctx, metric, ["metric.label.log"], now - dt.timedelta(days=days), now)
    out = {}
    for labels, points in series:
        name = urllib.parse.unquote(str(labels.get("metric.label.log") or "")).strip()
        if not name:
            continue
        counts = out.setdefault(name, [0] * days)
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
        out[inference] = list(BUILTIN_PAGES.get(sanitize_log_name(inference), ("antigravity",)))
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
    """projects/<id> resource names for Cloud Logging queries (projects named in the filter, else the primary)."""
    all_projs = getattr(ctx, "projects", None) or [ctx.project]
    m = re.findall(r"projects/([a-z][a-z0-9\-]{4,29})/logs/", flt or "")
    m += re.findall(r'project_id\s*=\s*"([a-z][a-z0-9\-]{4,29})"', flt or "")
    projs = [p for p in dict.fromkeys(m) if p in all_projs]
    if projs:
        return ["projects/%s" % p for p in projs]
    return ["projects/%s" % ctx.project]


def _quote_log(name):
    return '"%s"' % name.replace("\\", "\\\\").replace('"', '\\"')


def _sample_entries(ctx, logs, activity):
    """(entries, {short: entries}) read from Cloud Logging to learn payload fields.

    One shared read covers the scanned projects; logs that dominate the recent volume are left out of it (by measured
    share, not by name) so quieter logs are seen too. Logs the shared read missed get a few targeted reads each,
    busiest first, up to telemetry.discovery.targeted_samples."""
    disc = _tcfg(ctx.settings, "discovery")
    if not _live(ctx) or not logs:
        return [], {}
    projs = [p for p in (getattr(ctx, "projects", None) or [ctx.project]) if config.valid_project(p or "")][:3]
    since = iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30))
    totals = {k: sum(v) for k, v in (activity or {}).items()}
    grand = float(sum(totals.values()) or 0)
    heavy = [lg for lg in logs if grand and totals.get(lg["shortName"], 0) / grand > 0.2][:8]
    clauses = ['timestamp>="%s"' % since] + ["NOT logName=%s" % _quote_log(lg["logName"]) for lg in heavy]
    entries = []
    if disc["sample_entries"] > 0 and projs:
        try:
            data = ctx.rest("POST", "%s/entries:list" % LOGGING_V2, json_body={
                "resourceNames": ["projects/%s" % p for p in projs], "filter": " AND ".join(clauses),
                "orderBy": "timestamp desc", "pageSize": min(1000, disc["sample_entries"])}, timeout=10)
            entries = [e for e in (data or {}).get("entries") or [] if isinstance(e, dict)]
        except Exception as err:  # pylint: disable=broad-except
            LOG.info("shared log sample skipped: %s", type(err).__name__)
    by_short = {}
    for entry in entries:
        ln = entry.get("logName") or ""
        if "/logs/" in ln:
            by_short.setdefault(urllib.parse.unquote(ln.split("/logs/", 1)[1]), []).append(entry)
    missing = [lg for lg in logs if lg["shortName"] not in by_short]
    missing.sort(key=lambda lg: -totals.get(lg["shortName"], 0))
    tasks = {}
    for lg in missing[:disc["targeted_samples"]]:
        tasks[lg["shortName"]] = (lambda l=lg: ctx.rest("POST", "%s/entries:list" % LOGGING_V2, json_body={
            "resourceNames": ["projects/%s" % l["project"]], "filter": "logName=%s AND timestamp>=\"%s\"" % (
                _quote_log(l["logName"]), since), "orderBy": "timestamp desc", "pageSize": 5}, timeout=5))
    for short, (ok, data) in (ctx.run(tasks).items() if tasks else []):
        if ok and isinstance(data, dict):
            found = [e for e in data.get("entries") or [] if isinstance(e, dict)]
            if found:
                by_short[short] = found
                entries.extend(found)
    return entries, by_short


def _workload_label(rtype, labels, descriptors, distinct):
    """The resource label that names a workload: the candidate with the fewest distinct values in the sample."""
    order = (descriptors.get(rtype) or {}).get("labels") or sorted(labels)
    cands = [k for k in order if k not in LOCATION_LABELS and (k.endswith("_name") or k.endswith("_id")) and labels.get(k)]
    if any(k.endswith("_name") for k in cands):
        cands = [k for k in cands if k.endswith("_name")]
    if not cands:
        return None
    return min(cands, key=lambda k: (len(distinct.get((rtype, k), ())), order.index(k) if k in order else 99))


def _workload_streams(ctx, entries, descriptors, activity_days):
    """Structured JSON streams of individual workloads that share runtime logs (for example several services writing
    to the same stdout log), found from the sampled entries."""
    tz = timeutil.get_zone(ctx.tz_name)
    today = dt.datetime.now(dt.timezone.utc).astimezone(tz).date()
    index = {today - dt.timedelta(days=activity_days - 1 - i): i for i in range(activity_days)}
    distinct = {}
    for entry in entries:
        res = entry.get("resource") if isinstance(entry.get("resource"), dict) else {}
        for key, value in (res.get("labels") or {}).items():
            distinct.setdefault((res.get("type"), key), set()).add(value)
    own = os.environ.get("K_SERVICE") or ""
    workloads, log_users = {}, {}
    for entry in entries:
        payload = entry.get("jsonPayload")
        res = entry.get("resource") if isinstance(entry.get("resource"), dict) else {}
        rtype, rl = res.get("type") or "", res.get("labels") if isinstance(res.get("labels"), dict) else {}
        if not isinstance(payload, dict) or len(payload) < 2 or not rtype or rtype == "global":
            continue
        label = _workload_label(rtype, rl, descriptors, distinct)
        svc = (rl.get(label) or "").strip() if label else ""
        if not svc or not re.match(r"^[A-Za-z0-9_\-]{2,64}$", svc) or (own and svc == own):
            continue  # the control plane's own Cloud Run service is not a telemetry source
        ln = entry.get("logName") or ""
        short = urllib.parse.unquote(ln.split("/logs/", 1)[1]) if "/logs/" in ln else ""
        proj = (rl.get("project_id") or ctx.project or "").strip()
        key = (proj, rtype, label, svc)
        item = workloads.setdefault(key, {"entries": [], "shorts": set(), "daily": [0] * activity_days})
        item["entries"].append(entry)
        item["shorts"].add(short)
        log_users.setdefault(short, set()).add(key)
        ts = parse_rfc3339(entry.get("timestamp"))
        pos = index.get(ts.astimezone(tz).date()) if ts is not None else None
        if pos is not None:
            item["daily"][pos] += 1
    out = []
    for (proj, rtype, label, svc), info in sorted(workloads.items()):
        shared = any(len(log_users.get(s, ())) > 1 for s in info["shorts"])
        if not shared and len(info["shorts"]) < 2:
            continue  # a workload with its own log is already listed as that log
        flt = 'resource.type="%s" AND resource.labels.%s="%s" AND jsonPayload:*' % (rtype, label, svc)
        out.append({"proj": proj, "rtype": rtype, "label": label, "svc": svc, "filter": flt, "entries": info["entries"],
                    "daily": info["daily"], "shorts": sorted(info["shorts"])})
    return out


# ---------------------------------------------------------------------------
# Describing sources
# ---------------------------------------------------------------------------

def _stats_for(entries):
    return profile.entry_stats(entries) if entries else None


def _describe_log(ctx, log, stats, titles, descriptors, builtin):
    short = log["shortName"]
    sid = sanitize_log_name(short)
    core = sid in CORE_TABLES or short in builtin
    mapping = profile.infer_mapping(stats, ctx.settings, kind="logging") if stats else \
        {"time": "timestamp", "operation": None, "status": "severity", "user": None, "model": None, "tokens": None}
    pages = builtin.get(short) or list(BUILTIN_PAGES.get(sid, ())) if core else []
    card = profile.describe(sid, short=short, built_in=pages, stats=stats, mapping=mapping, settings=ctx.settings,
                            titles=titles, descriptors=descriptors)
    quick = profile.logging_connector(card, log["logName"], mapping)
    return card, quick, mapping


def _describe_table(ctx, tb, schema, log_stats, titles, descriptors):
    stats = profile.schema_stats(schema)
    if log_stats:
        log_map = profile.infer_mapping(log_stats, ctx.settings, kind="logging")
        mapping = profile.table_mapping_from_log(schema, log_map)
        stats = dict(stats, resources=log_stats.get("resources") or {}, genai=log_stats.get("genai"),
                     audit=log_stats.get("audit") or stats.get("audit"))
    else:
        mapping = profile.infer_mapping(stats, ctx.settings, kind="bigquery")
    pages = list(BUILTIN_PAGES.get(tb["logical"], ())) if tb["core"] else []
    card = profile.describe(tb["id"], dataset=tb["dataset"], built_in=pages, stats=stats, mapping=mapping,
                            settings=ctx.settings, titles=titles, descriptors=descriptors)
    quick, card = profile.bigquery_connector(card, tb["project"], tb["dataset"], tb["logical"], schema, mapping)
    return card, quick, (quick or {}).get("mapping") or mapping


def _fingerprint(identifier, field_list):
    return hashlib.sha1(("%s|%s" % (identifier, "|".join(sorted(field_list)))).encode("utf-8")).hexdigest()[:16]


def _field_list(stats, limit=60):
    out = []
    for path, s in list(((stats or {}).get("fields") or {}).items())[:limit]:
        out.append("%s:%s" % (path, s.get("type")))
    return out


def _mask_value(text):
    text = str(text)
    if profile.EMAIL_VALUE_RE.match(text):
        return "user@" + text.rsplit("@", 1)[-1]
    if profile.ID_VALUE_RE.match(text):
        return "<id>"
    return text[:40]


def _field_digest(stats, settings, share_examples):
    """Compact field description for the Gemini prompt: type, coverage, distinct values and masked examples."""
    n = (stats or {}).get("n") or 0
    sens = profile._settings_hints(settings, "sensitive_hints")
    out = []
    for path, s in list(((stats or {}).get("fields") or {}).items())[:60]:
        item = {"path": path, "type": s.get("type")}
        if n and s.get("count"):
            item["coverage"] = round(s["count"] / float(n), 2)
            item["distinct"] = len(s["values"]) if not s["overflow"] else "%d+" % len(s["values"])
            leaf = profile._leaf_name(path)
            if share_examples and not s["overflow"] and len(s["values"]) <= 12 and not any(h in leaf for h in sens):
                item["examples"] = [_mask_value(v) for v in list(s["values"])[:4]]
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Gemini profiling (persistent per source fingerprint)
# ---------------------------------------------------------------------------

AI_SCHEMA = {
    "type": "OBJECT",
    "properties": {"sources": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "key": {"type": "STRING"},
        "category": {"type": "STRING", "enum": list(profile.CATEGORIES)},
        "group": {"type": "STRING"}, "groupKo": {"type": "STRING"},
        "title": {"type": "STRING"}, "titleKo": {"type": "STRING"},
        "what": {"type": "STRING"}, "whatKo": {"type": "STRING"},
        "sensitive": {"type": "BOOLEAN"},
        "time": {"type": "STRING"},
        "operation": {"type": "STRING"}, "operationLabel": {"type": "STRING"}, "operationLabelKo": {"type": "STRING"},
        "status": {"type": "STRING"}, "statusLabel": {"type": "STRING"}, "statusLabelKo": {"type": "STRING"},
        "user": {"type": "STRING"}, "model": {"type": "STRING"},
        "tokens": {"type": "STRING"}, "tokensLabel": {"type": "STRING"}, "tokensLabelKo": {"type": "STRING"},
        "value": {"type": "INTEGER"},
        "routeRecommended": {"type": "BOOLEAN"}, "routeReason": {"type": "STRING"}, "routeReasonKo": {"type": "STRING"},
    }, "required": ["key", "category", "group", "groupKo", "title", "titleKo", "what", "whatKo", "sensitive"]}}},
    "required": ["sources"],
}

GROUP_SCHEMA = {"type": "OBJECT", "properties": {"groups": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "name": {"type": "STRING"}, "canonical": {"type": "STRING"}, "canonicalKo": {"type": "STRING"}},
    "required": ["name", "canonical", "canonicalKo"]}}}, "required": ["groups"]}


def _ai_system(settings):
    renames = "; ".join("write '%s' instead of '%s'" % (right, left) for left, right in profile._renames(settings))
    return (
        "You are an enterprise cloud observability and governance architect. You receive telemetry sources that a "
        "Google Cloud project already collects (Cloud Logging logs, BigQuery tables, workload streams) with their real "
        "fields, value statistics and daily volume. For every source return plain-language metadata and cluster related "
        "sources into a small number of meaningful groups that administrators can turn into dashboards.\n"
        "Rules:\n"
        "1. Never invent field names. For time, operation, status, user, model and tokens use ONLY exact paths from the "
        "source's fields, or an empty string when none fits.\n"
        "2. tokens must be a numeric token count; user must identify a person or principal; operation and status must be "
        "low-cardinality breakdowns.\n"
        "3. Titles under 55 characters; 'what' and 'whatKo' one or two sentences on what the records are and how they "
        "help governance, security, cost or operations. Korean texts must be natural Korean.\n"
        "4. sensitive is true only when fields hold raw prompts, model responses, chat messages or personal document text.\n"
        "5. value: 1 (noise) to 5 (essential) for AI governance and operations. routeRecommended: true only when keeping "
        "longer history in BigQuery is worth the storage cost (high value, analytical use); explain in routeReason and "
        "routeReasonKo (one short sentence each).\n"
        "6. Group names describe a purpose or product area (for example 'Agent runtime', 'Security audit'), are reused "
        "across sources, and come with a Korean name in groupKo.\n"
        "7. Product naming: %s." % (renames or "use the product names as given")
    )


def _profiles_doc(ctx):
    doc, _ = aux_docs(getattr(ctx, "app", None)).load(PROFILES_DOC)
    doc.setdefault("profiles", {})
    doc.setdefault("groups", {})
    return doc


def _save_profiles(ctx, new_profiles, group_map=None):
    def apply(doc):
        profiles = doc.setdefault("profiles", {})
        profiles.update(new_profiles)
        if len(profiles) > MAX_PROFILES:
            for key, _ in sorted(profiles.items(), key=lambda kv: kv[1].get("at") or "")[:len(profiles) - MAX_PROFILES]:
                profiles.pop(key, None)
        if group_map:
            doc.setdefault("groups", {}).update(group_map)
        return True
    try:
        aux_docs(getattr(ctx, "app", None)).mutate(PROFILES_DOC, apply)
    except Exception as err:  # pylint: disable=broad-except
        LOG.warning("telemetry profiles not saved: %s", type(err).__name__)


def _fresh(entry, fp, ttl_hours):
    if not isinstance(entry, dict) or entry.get("fp") != fp or not entry.get("profile"):
        return False
    at = parse_rfc3339(entry.get("at"))
    return at is not None and (dt.datetime.now(dt.timezone.utc) - at).total_seconds() < ttl_hours * 3600


def _clean(val, limit):
    if not isinstance(val, str):
        return None
    cleaned = config.CONTROL_RE.sub("", val).strip()
    return cleaned[:limit] if cleaned else None


def _valid_logging_path(path, observed):
    return _valid_json_path(path) and (path in observed or path in ("timestamp", "severity", "resource.type"))


def _apply_profile(src, prof, model_used, schema=None, settings=None):
    """Applies a validated Gemini profile to a source: card text, group, category and verified field mapping."""
    card = dict(src.get("catalog") or {})
    quick = dict(src["quickAdd"]) if isinstance(src.get("quickAdd"), dict) else None
    for key, limit in (("title", 80), ("titleKo", 80), ("what", 320), ("whatKo", 320), ("group", 60),
                       ("groupKo", 60), ("routeReason", 200), ("routeReasonKo", 200)):
        val = _clean(prof.get(key), limit)
        if val:
            card[key] = profile.apply_renames(val, settings)[:limit]
            if key == "title":
                card["titleParams"] = None
            if key == "what":
                card["whatParams"] = None
    if prof.get("category") in profile.CATEGORIES:
        card["category"] = prof["category"]
    if isinstance(prof.get("sensitive"), bool):
        card["sensitive"] = prof["sensitive"]
    if isinstance(prof.get("value"), int):
        card["value"] = max(1, min(5, prof["value"]))
    if isinstance(prof.get("routeRecommended"), bool):
        card["routeRecommended"] = prof["routeRecommended"]
    card["aiProfiled"] = True
    card["aiGrouped"] = bool(card.get("group"))
    card["aiModel"] = model_used
    if quick is not None:
        mapping = dict(quick.get("mapping") or {})
        observed = set(src.get("_fields") or [])
        types = src.get("_types") or {}
        for fkey in ("operation", "status", "user", "model", "tokens"):
            cand = (prof.get(fkey) or "").strip()
            if not cand:
                continue
            if quick.get("source") == "bigquery":
                resolved = cand if (schema is not None and profile.usable_column(schema, cand)) else \
                    profile.resolve_bigquery_path(schema, cand)
                if not resolved or "[" in resolved:
                    continue
                ftype = types.get(resolved) or (schema.node(resolved).type if schema is not None and schema.node(resolved) else "")
            else:
                if not _valid_logging_path(cand, observed):
                    continue
                resolved, ftype = cand, types.get(cand, "")
            if fkey == "tokens" and not any((ftype or "").startswith(t) for t in NUMERIC_TYPES):
                continue
            mapping[fkey] = resolved
        if quick.get("source") == "bigquery" and schema is not None:
            ai_time = (prof.get("time") or "").strip()
            node = schema.node(ai_time) if ai_time and "." not in ai_time else None
            if node is not None and node.type == "TIMESTAMP":
                mapping["time"] = node.name
        quick["mapping"] = mapping
        for fkey in ("operation", "status", "tokens"):
            if mapping.get(fkey):
                lbl = _clean(prof.get("%sLabel" % fkey), 40)
                lbl_ko = _clean(prof.get("%sLabelKo" % fkey), 40)
                if lbl:
                    lbl = profile.apply_renames(lbl, settings)[:40]
                card["%sLabel" % fkey] = lbl or card.get("%sLabel" % fkey) or profile.GENERIC_LABELS[fkey]
                if lbl_ko:
                    card["%sLabelKo" % fkey] = profile.apply_renames(lbl_ko, settings)[:40]
            else:
                card["%sLabel" % fkey] = None
                card.pop("%sLabelKo" % fkey, None)
        if card.get("title"):
            quick["name"] = profile.title_text(card)[:80]
        quick["meta"] = _card_meta(card)
        src["quickAdd"] = quick
        if quick.get("source") == "bigquery":
            src["suggestedMapping"] = dict(mapping)
    src["catalog"] = card


def _ai_profile(ctx, sources, schemas):
    """Profiles sources with Gemini (only new or changed ones are sent), merges group names and applies profiles."""
    ai_cfg = _tcfg(ctx.settings, "ai")
    app = getattr(ctx, "app", None)
    engine = getattr(app, "ai", None) if app is not None else None
    if engine is None or not ai_cfg["enabled"] or ai_cfg["max_sources"] <= 0:
        return {"enabled": False, "model": None}
    if not _live(ctx) and type(engine).__name__ == "AiEngine":
        return {"enabled": False, "model": None}
    preferred = ai_cfg["model"] or (ctx.settings.get("ai_engine") or {}).get("default_model") or ""
    if not preferred:
        return {"enabled": False, "model": None}
    doc = _profiles_doc(ctx)
    stored = doc["profiles"]
    todo, hits = [], {}
    ranked = sorted(sources, key=lambda s: -(s.get("entries7d") or s.get("rows") or 0))[:ai_cfg["max_sources"]]
    for src in ranked:
        entry = stored.get(src["key"])
        if _fresh(entry, src["_fp"], ai_cfg["profile_ttl_hours"]):
            hits[src["key"]] = entry
        else:
            todo.append(src)
    used_model = engine.effective_model(ctx.settings, preferred) if hasattr(engine, "effective_model") else preferred
    new_profiles = {}
    if todo:
        batches = [todo[i:i + ai_cfg["batch_size"]] for i in range(0, len(todo), ai_cfg["batch_size"])]
        known_groups = sorted({(e.get("profile") or {}).get("group") for e in stored.values()
                               if (e.get("profile") or {}).get("group")})[:40]

        def call(batch):
            items = [{"key": s["key"], "sourceType": s["kind"], "identifier": s["_ident"],
                      "product": (s.get("catalog") or {}).get("product"),
                      "resources": (s.get("catalog") or {}).get("resources"),
                      "entriesPerDay": round((s.get("entries7d") or 0) / float(max(1, s.get("_days") or 7)), 1),
                      "fields": s["_digest"]} for s in batch]
            prompt = ("Project: %s\nExisting group names to reuse when they fit: %s\nSources (JSON):\n%s" % (
                ctx.project, json.dumps(known_groups, ensure_ascii=False), json.dumps(items, ensure_ascii=False)))
            meta = {}
            resp = engine.generate(ctx.settings, preferred, prompt, system=_ai_system(ctx.settings), schema=AI_SCHEMA,
                                   temperature=0.1, max_output_tokens=16384, thinking_level="LOW", meta_out=meta,
                                   module="telemetry_ai")
            return resp, meta.get("model") or used_model

        results = ctx.run({str(i): (lambda b=b: call(b)) for i, b in enumerate(batches)})
        now = now_iso()
        for i, batch in enumerate(batches):
            ok, val = results[str(i)]
            if not ok:
                LOG.warning("telemetry AI batch failed: %s", type(val).__name__)
                continue
            resp, model = val
            used_model = model or used_model
            by_key = {s["key"]: s for s in (resp or {}).get("sources") or [] if isinstance(s, dict) and s.get("key")}
            for src in batch:
                prof = by_key.get(src["key"])
                if prof:
                    new_profiles[src["key"]] = {"fp": src["_fp"], "at": now, "model": used_model, "profile": prof}
    group_map = dict(doc.get("groups") or {})
    if new_profiles:
        names = sorted({(e["profile"] or {}).get("group") for e in list(hits.values()) + list(new_profiles.values())
                        if (e.get("profile") or {}).get("group")})
        unknown = [n for n in names if n not in group_map]
        if len(names) > 1 and unknown:
            try:
                resp = engine.generate(ctx.settings, preferred, "Merge these telemetry group names into a consistent, "
                                       "small set (keep distinct purposes separate). Return one canonical English name "
                                       "and its Korean name for every input name.\n%s" % json.dumps(names, ensure_ascii=False),
                                       system=_ai_system(ctx.settings), schema=GROUP_SCHEMA, temperature=0.0,
                                       max_output_tokens=4096, thinking_level="LOW", module="telemetry_ai")
                for item in (resp or {}).get("groups") or []:
                    name, canon, canon_ko = _clean(item.get("name"), 60), _clean(item.get("canonical"), 60), \
                        _clean(item.get("canonicalKo"), 60)
                    if name and canon:
                        canon = profile.apply_renames(canon, ctx.settings)[:60]
                        canon_ko = profile.apply_renames(canon_ko or canon, ctx.settings)[:60]
                        group_map[name] = {"name": canon, "nameKo": canon_ko}
            except Exception as err:  # pylint: disable=broad-except
                LOG.info("telemetry group merge skipped: %s", type(err).__name__)
        _save_profiles(ctx, new_profiles, group_map)
    applied = 0
    combined = dict(hits)
    combined.update(new_profiles)
    for src in sources:
        entry = combined.get(src["key"])
        if not entry:
            continue
        prof = dict(entry.get("profile") or {})
        canon = group_map.get(prof.get("group") or "")
        if canon:
            prof["group"], prof["groupKo"] = canon["name"], canon.get("nameKo") or canon["name"]
        _apply_profile(src, prof, entry.get("model") or used_model, schemas.get(src.get("_skey")), ctx.settings)
        applied += 1
    return {"enabled": applied > 0, "model": used_model if applied else None, "profiled": applied,
            "newlyProfiled": len(new_profiles), "candidates": len(ranked)}


# ---------------------------------------------------------------------------
# Administrator overrides
# ---------------------------------------------------------------------------

OVERRIDE_TEXT = (("title", 80), ("titleKo", 80), ("what", 320), ("whatKo", 320), ("group", 60), ("groupKo", 60))


def _overrides(ctx):
    doc, _ = aux_docs(getattr(ctx, "app", None)).load(OVERRIDES_DOC)
    return doc.get("sources") or {}


def _apply_override(src, ov):
    card = dict(src.get("catalog") or {})
    for key, _limit in OVERRIDE_TEXT:
        if ov.get(key):
            card[key] = ov[key]
            if key == "title":
                card["titleParams"] = None
            if key == "what":
                card["whatParams"] = None
    if ov.get("category") in profile.CATEGORIES:
        card["category"] = ov["category"]
    if isinstance(ov.get("hidden"), bool):
        card["hidden"] = ov["hidden"]
    card["overridden"] = True
    # The administrator's own values, so the edit dialog shows exactly what was overridden (the rest stays automatic).
    card["override"] = {k: ov[k] for k in ("title", "titleKo", "what", "whatKo", "category", "group", "groupKo", "hidden")
                        if k in ov and ov[k] not in (None, "")}
    src["catalog"] = card
    if isinstance(src.get("quickAdd"), dict):
        quick = dict(src["quickAdd"])
        if card.get("title"):
            quick["name"] = profile.title_text(card)[:80]
        quick["meta"] = _card_meta(card)
        src["quickAdd"] = quick


def save_override(ctx):
    """POST /api/v1/telemetry/sources/override {key, title, titleKo, what, whatKo, category, group, groupKo, hidden,
    reset}: an administrator's correction of a source card, kept across rediscovery."""
    require_identity_admin(ctx)
    body = ctx.body or {}
    key = body.get("key")
    if not isinstance(key, str) or not SOURCE_KEY_RE.match(key):
        raise ApiError("bad_request", "Field 'key' is invalid.")
    clean = {}
    if not body.get("reset"):
        for field, limit in OVERRIDE_TEXT:
            if body.get(field) is not None:
                val = _clean(body.get(field), limit)
                if body.get(field) and not val:
                    raise ApiError("bad_request", "Field '%s' is invalid." % field)
                clean[field] = val or ""
        if body.get("category") is not None:
            if body["category"] not in profile.CATEGORIES:
                raise ApiError("bad_request", "Field 'category' must be one of: %s." % ", ".join(profile.CATEGORIES))
            clean["category"] = body["category"]
        if body.get("hidden") is not None:
            clean["hidden"] = bool(body["hidden"])
        clean.update({"updatedAt": now_iso(), "updatedBy": ctx.identity.email})

    def apply(doc):
        sources = doc.setdefault("sources", {})
        if body.get("reset"):
            sources.pop(key, None)
        else:
            sources[key] = clean
        return True
    aux_docs(getattr(ctx, "app", None)).mutate(OVERRIDES_DOC, apply)
    ctx.app.cache.clear()
    return {"key": key, "override": None if body.get("reset") else clean}


def refresh_profiles(ctx):
    """POST /api/v1/telemetry/profiles/refresh {keys?}: forgets Gemini profiles (all or some) so the next discovery
    analyzes those sources again."""
    require_identity_admin(ctx)
    keys = (ctx.body or {}).get("keys")
    if keys is not None and (not isinstance(keys, list) or not all(isinstance(k, str) and SOURCE_KEY_RE.match(k)
                                                                    for k in keys)):
        raise ApiError("bad_request", "Field 'keys' must be a list of source keys.")

    def apply(doc):
        profiles = doc.setdefault("profiles", {})
        if keys is None:
            removed = len(profiles)
            doc["profiles"], doc["groups"] = {}, {}
            return removed
        return sum(1 for k in keys if profiles.pop(k, None) is not None)
    removed = aux_docs(getattr(ctx, "app", None)).mutate(PROFILES_DOC, apply)
    ctx.app.cache.clear()
    return {"removed": removed}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _compute_discover(ctx):
    disc = _tcfg(ctx.settings, "discovery")
    days = disc["activity_days"]
    by_table, by_log, by_filter = {}, {}, {}
    for conn in all_connectors(ctx):
        bq = conn.get("bigquery") or {}
        if conn.get("source") == "bigquery" and bq.get("table"):
            key = (bq.get("project"), bq.get("dataset"), bq["table"][:-2] if bq["table"].endswith("_*") else bq["table"])
            by_table.setdefault(key, conn.get("id"))
        elif conn.get("source") == "logging":
            sid = profile.connector_source_id(conn)
            if sid:
                by_log.setdefault(sid, conn.get("id"))
            flt = ((conn.get("logging") or {}).get("filter") or "").strip()
            if flt:
                by_filter.setdefault(flt, conn.get("id"))
    schemas = {}
    res = ctx.run_composite({
        "tables": lambda: _discover_tables(ctx, by_table, schemas),
        "logs": lambda: _list_logs(ctx),
        "activity": lambda: _log_metric(ctx, LOG_ENTRY_METRIC, days),
        "volume": lambda: routing.log_volumes(ctx, days),
        "titles": lambda: profile.service_titles(ctx),
        "descriptors": lambda: profile.resource_descriptors(ctx),
    })
    tables = _task_result(ctx, res["tables"], "telemetry:tables", "Table discovery")
    logs = _task_result(ctx, res["logs"], "api:logging.logs", "The Cloud Logging log list")
    activity = _task_result(ctx, res["activity"], "api:monitoring.log_entry_count", "Log activity from Cloud Monitoring")
    ok_v, volumes = res["volume"]
    volumes = volumes if ok_v else {}
    titles = res["titles"][1] if res["titles"][0] else {}
    descriptors = res["descriptors"][1] if res["descriptors"][0] else {}
    if activity is not None:
        ctx.add_source("api:monitoring.log_entry_count")
    entries, samples = _sample_entries(ctx, logs or [], activity)
    builtin = _builtin_logs(ctx)
    state = routing.load_state(ctx)
    routed = routing.routed_index(state)
    pol = routing.policy(ctx.settings)
    prices = routing.storage_prices(ctx)
    logical = {tb["id"] for tb in tables or []}
    log_stats, by_id, sources = {}, {}, []
    for log in logs or []:
        short = log["shortName"]
        stats = _stats_for(samples.get(short))
        log_stats[short] = stats
        card, quick, mapping = _describe_log(ctx, log, stats, titles, descriptors, builtin)
        counts = (activity.get(short) or [0] * days) if activity is not None else None
        sid = sanitize_log_name(short)
        bpd = volumes.get(short)
        log.update({
            "id": sid, "key": "log:%s:%s" % (log["project"], short), "kind": "logging",
            "routedToBigQuery": sid in logical, "routedBy": routed.get(log["logName"]),
            "core": sid in CORE_TABLES or short in builtin, "registered": sid in by_log, "connectorId": by_log.get(sid),
            "entries7d": sum(counts) if counts is not None else None, "daily": counts,
            "volume": {"bytesPerDay": round(bpd, 1) if bpd is not None else None},
            "estimate": routing.estimate(bpd, pol["samplePercent"], pol["retentionDays"], prices),
            "columns": list((stats or {}).get("fields", {}).keys()) or None, "catalog": card, "quickAdd": quick,
            "_fields": list((stats or {}).get("fields", {}).keys()), "_types": profile.field_types(stats),
            "_ident": short, "_days": days, "_skey": None,
        })
        log["_digest"] = _field_digest(stats, ctx.settings, _tcfg(ctx.settings, "ai")["share_value_examples"])
        log["_fp"] = _fingerprint(short, _field_list(stats))
        by_id.setdefault(sid, log)
        sources.append(log)
    for stream in _workload_streams(ctx, entries, descriptors, days):
        stats = profile.entry_stats(stream["entries"])
        mapping = profile.infer_mapping(stats, ctx.settings, kind="logging")
        sid = "workload_%s_%s" % (sanitize_log_name(stream["rtype"]), sanitize_log_name(stream["svc"]))
        rname = profile.apply_renames((descriptors.get(stream["rtype"]) or {}).get("name") or stream["rtype"], ctx.settings)
        card = profile.describe(sid, short=stream["svc"], stats=stats, mapping=mapping, settings=ctx.settings,
                                titles=titles, descriptors=descriptors)
        card.update({"title": "{name} · {resource}", "titleParams": {"name": stream["svc"], "resource": rname},
                     "what": "Structured JSON entries that the workload {name} ({resource}) writes to {logs}.",
                     "whatParams": {"name": stream["svc"], "resource": rname, "logs": ", ".join(stream["shorts"])}})
        quick = {"name": profile.title_text(card), "enabled": True, "source": "logging", "bigquery": None,
                 "logging": {"filter": stream["filter"]}, "mapping": dict(mapping, time="timestamp")}
        conn_id = by_filter.get(stream["filter"])
        item = {"id": sid, "key": "wl:%s:%s:%s" % (stream["proj"], stream["rtype"], stream["svc"]), "kind": "logging",
                "logName": None, "shortName": "%s (%s · %s)" % (stream["svc"], stream["rtype"], stream["proj"]),
                "sinkFilter": stream["filter"], "project": stream["proj"], "routedToBigQuery": False, "routedBy": None,
                "core": False, "registered": conn_id is not None, "connectorId": conn_id,
                "entries7d": sum(stream["daily"]) or len(stream["entries"]), "daily": stream["daily"],
                "volume": {"bytesPerDay": None}, "estimate": routing.estimate(None, 100, 0, prices),
                "columns": list(stats["fields"].keys()), "catalog": card, "quickAdd": quick,
                "_fields": list(stats["fields"].keys()), "_types": profile.field_types(stats),
                "_ident": stream["filter"], "_days": days, "_skey": None}
        item["_digest"] = _field_digest(stats, ctx.settings, _tcfg(ctx.settings, "ai")["share_value_examples"])
        item["_fp"] = _fingerprint(stream["filter"], _field_list(stats))
        if logs is not None:
            logs.insert(0, item)
        sources.append(item)
    for tb in tables or []:
        skey = (tb["project"], tb["dataset"], tb["logical"])
        schema = schemas.get(skey)
        log = by_id.get(tb["id"])
        stats = log_stats.get(log["shortName"]) if log else None
        card, quick, mapping = _describe_table(ctx, tb, schema, stats, titles, descriptors)
        bpd = volumes.get(log["shortName"]) if log else None
        tb.update({"key": "bq:%s.%s.%s" % skey, "kind": "bigquery", "catalog": card, "quickAdd": quick,
                   "suggestedMapping": mapping, "routedBy": log.get("routedBy") if log else None,
                   "volume": {"bytesPerDay": round(bpd, 1) if bpd is not None else None},
                   "estimate": routing.estimate(bpd, pol["samplePercent"], pol["retentionDays"], prices),
                   "_fields": [p for p, _t in (schema.leaf_types(200) if schema is not None else [])],
                   "_types": {p: t.split(" ")[0] for p, t in (schema.leaf_types(200) if schema is not None else [])},
                   "_ident": "%s.%s.%s" % skey, "_days": days, "_skey": skey})
        if log is not None:
            tb["entries7d"], tb["daily"] = log["entries7d"], log["daily"]
        tstats = profile.schema_stats(schema)
        tb["_digest"] = _field_digest(stats or tstats, ctx.settings, _tcfg(ctx.settings, "ai")["share_value_examples"]) \
            if stats else [{"path": p, "type": t} for p, t in list(tb["_types"].items())[:60]]
        tb["_fp"] = _fingerprint(tb["_ident"], ["%s:%s" % kv for kv in tb["_types"].items()])
        sources.append(tb)
    ai_info = _ai_profile(ctx, sources, schemas)
    overrides = _overrides(ctx)
    for src in sources:
        if src["key"] in overrides:
            _apply_override(src, overrides[src["key"]])
        for private in ("_fields", "_types", "_ident", "_days", "_skey", "_digest", "_fp"):
            src.pop(private, None)
    groups = {}
    for src in sources:
        card = src.get("catalog") or {}
        if card.get("group") and not src.get("core") and not card.get("hidden"):
            g = groups.setdefault(card["group"], {"name": card["group"], "nameKo": card.get("groupKo") or card["group"],
                                                  "keys": [], "logs": [], "projects": set()})
            g["keys"].append(src["key"])
            if src.get("logName"):
                g["logs"].append(src["logName"])
                g["projects"].add(src.get("project"))
    group_list = [dict(g, projects=sorted(p for p in g["projects"] if p)) for g in groups.values()]
    return {"tables": tables, "logs": logs, "activityDays": days, "aiGrouping": ai_info, "groups": group_list,
            "maxLogEntries": disc["max_log_entries"],
            "routing": {"dataset": pol["dataset"], "routes": len(state.get("routes") or []),
                        "samplePercent": pol["samplePercent"], "retentionDays": pol["retentionDays"],
                        "priced": prices is not None, "currency": (prices or {}).get("currency")}}


def discover(ctx):
    """Every telemetry source of the scanned projects, described and grouped dynamically."""
    if type(getattr(ctx, "gcp", None)).__name__ == "Gcp" and getattr(ctx, "app", None):
        key = ("telemetry_discover", ctx.project, tuple(getattr(ctx, "projects", None) or [ctx.project]),
               getattr(ctx, "version", 0))
        val, _ = ctx.app.cache.get_or_compute(key, HEALTH_TTL, lambda: _compute_discover(ctx))
        return copy.deepcopy(val)
    return _compute_discover(ctx)


# ---------------------------------------------------------------------------
# Connectors
# ---------------------------------------------------------------------------

META_LIMITS = {"title": 80, "titleKo": 80, "what": 320, "whatKo": 320, "group": 60, "groupKo": 60,
               "operationLabel": 40, "operationLabelKo": 40, "statusLabel": 40, "statusLabelKo": 40,
               "tokensLabel": 40, "tokensLabelKo": 40, "aiModel": 60}


def _sanitize_meta(meta):
    if not isinstance(meta, dict):
        return None
    out = {}
    for key, limit in META_LIMITS.items():
        val = _clean(meta.get(key), limit)
        if val:
            out[key] = val
    if meta.get("category") in profile.CATEGORIES:
        out["category"] = meta["category"]
    for bkey in ("sensitive", "aiProfiled", "aiGrouped"):
        if isinstance(meta.get(bkey), bool):
            out[bkey] = meta[bkey]
    return out or None


def _card_meta(card):
    if not isinstance(card, dict):
        return None
    raw = {k: card.get(k) for k in META_LIMITS}
    if not raw.get("title"):
        raw["title"] = profile.title_text(card)
    raw.update({"category": card.get("category"), "sensitive": bool(card.get("sensitive")),
                "aiProfiled": bool(card.get("aiProfiled")), "aiGrouped": bool(card.get("aiGrouped"))})
    return _sanitize_meta(raw)


def _card_with_meta(base_card, meta, conn):
    """The card of a connector: its saved meta (Gemini or administrator text at creation) over the dynamic card."""
    clean = _sanitize_meta(meta)
    if not clean:
        return base_card
    mapping = (conn or {}).get("mapping") or {}
    card = dict(base_card) if base_card is not None else {
        "id": profile.connector_source_id(conn or {}) or "custom", "known": False, "titleParams": None,
        "whatParams": None, "category": profile.OTHER, "sensitive": False, "builtIn": [],
        "title": (conn or {}).get("name") or "Telemetry", "what": "",
        "operationLabel": profile.GENERIC_LABELS["operation"] if mapping.get("operation") else None,
        "statusLabel": profile.GENERIC_LABELS["status"] if mapping.get("status") else None,
        "tokensLabel": profile.GENERIC_LABELS["tokens"] if mapping.get("tokens") else None}
    for key in ("title", "titleKo", "what", "whatKo", "group", "groupKo", "aiModel"):
        if clean.get(key):
            card[key] = clean[key]
            if key == "title":
                card["titleParams"] = None
            if key == "what":
                card["whatParams"] = None
    if clean.get("category"):
        card["category"] = clean["category"]
    for bkey in ("sensitive", "aiProfiled", "aiGrouped"):
        if bkey in clean:
            card[bkey] = clean[bkey]
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


def _enrich_connector_meta(ctx, item):
    item.setdefault("bigquery", None)
    item.setdefault("logging", None)
    if item.get("source") == "bigquery" and isinstance(item.get("bigquery"), dict):
        bq = item["bigquery"]
        proj = bq.get("project") or ctx.project
        item["project"] = proj
        item["projectScope"] = "single"
        item["bigquerySink"] = "%s.%s" % (proj, bq.get("dataset") or ctx.audit_dataset)
        return
    flt = ((item.get("logging") or {}).get("filter") or "").strip()
    extracted = re.findall(r"projects/([a-z][a-z0-9\-]{4,29})/logs/", flt)
    extracted += re.findall(r'project_id\s*=\s*"([a-z][a-z0-9\-]{4,29})"', flt)
    projects = list(dict.fromkeys(extracted))
    item["project"] = projects[0] if projects else (item.get("project") or ctx.project)
    item["projectScope"] = "multi" if len(projects) > 1 else "single"


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
                "filter": "(%s) AND timestamp>=\"%s\"" % (flt, since), "orderBy": "timestamp desc", "pageSize": 1}
        data = ctx.rest("POST", "%s/entries:list" % LOGGING_V2, json_body=body, timeout=6)
        entries = data.get("entries") or []
        return "ok", iso(parse_rfc3339(entries[0].get("timestamp"))) if entries else None
    except (GcpError, ValueError, ApiError) as err:
        LOG.info("connector health check failed id=%s kind=%s", conn.get("id"), getattr(err, "kind", type(err).__name__))
        return "error", None


def _connector_card(ctx, item):
    return _card_with_meta(profile.for_connector(item, ctx.settings), item.get("meta"), item)


def with_health(ctx, conn):
    item = copy.deepcopy(conn)
    _enrich_connector_meta(ctx, item)
    key = ("connector_health", json.dumps(item, sort_keys=True, default=str))
    (health, last), _ = ctx.app.cache.get_or_compute(key, HEALTH_TTL, lambda: _connector_health(ctx, item))
    item["health"] = health
    item["lastEvent"] = last
    item["catalog"] = _connector_card(ctx, item)
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
            item.update({"health": "error", "lastEvent": None, "catalog": _connector_card(ctx, conns[i])})
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
        if not _valid_json_path(value) and not (source == "logging" and value == "logName"):
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
        if not isinstance(flt, str) or not flt.strip() or len(flt) > 4000 or re.search(r"[\x00-\x08\x0b-\x1f\x7f]", flt):
            raise ApiError("bad_request", "Field 'logging.filter' must be 1 to 4000 characters.")
        out["logging"] = {"filter": flt.strip()}
        if not out["mapping"]["time"]:
            out["mapping"]["time"] = "timestamp"
    return out


def _store_connector(ctx, clean, conn_id=None):
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
    return saved


def save_connector(ctx):
    clean = validate_connector(ctx, ctx.body)
    conn_id = ctx.body.get("id")
    if conn_id is not None and (not isinstance(conn_id, str) or not CONNECTOR_ID_RE.match(conn_id)):
        raise ApiError("bad_request", "Field 'id' is invalid.")
    if conn_id and conn_id in ctx.app.settings.config_connector_ids():
        raise ApiError("conflict", "This connector is defined in config.yaml and cannot be edited from the UI.")
    saved = _store_connector(ctx, clean, conn_id)
    return {"connector": with_health(ctx, saved), "connectors": list_connectors(ctx)["connectors"]}


def ensure_connector(ctx, quick_add, bigquery_sink=None):
    """Creates a connector from a ready body unless one with the same filter or name exists. Returns it or None."""
    try:
        clean = validate_connector(ctx, quick_add)
    except ApiError:
        return None
    flt = ((clean.get("logging") or {}).get("filter") or "").strip()
    existing = next((c for c in all_connectors(ctx) if (flt and ((c.get("logging") or {}).get("filter") or "").strip() == flt)
                     or c.get("name") == clean.get("name")), None)
    saved = existing or _store_connector(ctx, clean)
    item = with_health(ctx, saved)
    if bigquery_sink and item.get("source") == "logging":
        item["bigquerySink"] = bigquery_sink
    return item


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


def group_dashboard(ctx):
    """POST /api/v1/telemetry/groups/dashboard {name, nameKo?, logs: [log names]}: one dashboard over a whole group of
    logs (one Cloud Logging filter), broken down by source log and severity."""
    require_identity_admin(ctx)
    body = ctx.body or {}
    name = _clean(body.get("name"), 80)
    logs = [str(n).strip() for n in body.get("logs") or [] if isinstance(n, str)]
    if not name:
        raise ApiError("bad_request", "Field 'name' must be 1 to 80 characters.")
    logs = [n for n in dict.fromkeys(logs) if profile.LOG_NAME_RE.match(n)][:routing.MAX_LOGS_PER_ROUTE]
    if not logs:
        raise ApiError("bad_request", "Field 'logs' must contain at least one full log name.")
    flt = "logName=%s" % _quote_log(logs[0]) if len(logs) == 1 else \
        "logName=(%s)" % " OR ".join(_quote_log(n) for n in logs)
    meta = {"title": name, "titleKo": _clean(body.get("nameKo"), 80), "group": name,
            "groupKo": _clean(body.get("nameKo"), 60), "operationLabel": "Source log", "operationLabelKo": "원본 로그",
            "statusLabel": "Severity", "statusLabelKo": "심각도", "aiGrouped": True,
            "what": "One dashboard over %d logs of this group, broken down by source log and severity." % len(logs)}
    quick = {"name": name, "enabled": True, "source": "logging", "logging": {"filter": flt}, "bigquery": None,
             "mapping": {"time": "timestamp", "operation": "logName", "status": "severity", "user": None,
                         "model": None, "tokens": None}, "meta": meta}
    existing = next((c for c in all_connectors(ctx) if c.get("source") == "logging" and
                     ((c.get("meta") or {}).get("group") == name or c.get("name") == name) and
                     ((c.get("logging") or {}).get("filter") or "").lstrip().startswith("logName=")), None)
    if existing is not None and existing.get("id") not in ctx.app.settings.config_connector_ids():
        # The group grew or shrank since its dashboard was created: the same dashboard follows the current members.
        clean = validate_connector(ctx, quick)
        if ((existing.get("logging") or {}).get("filter") or "").strip() != flt or existing.get("meta") != clean.get("meta"):
            existing = _store_connector(ctx, clean, existing["id"])
        connector = with_health(ctx, existing)
    else:
        connector = ensure_connector(ctx, quick)
    if connector is None:
        raise ApiError("bad_request", "The group dashboard could not be created.")
    return {"connector": connector, "connectors": list_connectors(ctx)["connectors"]}


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


def _sensitive_keys(settings=None):
    return set(profile._settings_hints(settings, "sensitive_hints")) | {"input", "output"}


def _summarize_structured_payload(payload, settings=None):
    """Compact scalar key=value summary for structured telemetry dicts without exposing prompt/content fields."""
    if not isinstance(payload, dict) or len(payload) < 2:
        return None
    hidden = _sensitive_keys(settings)
    pairs = []

    def collect(prefix, obj, depth):
        if len(pairs) >= 6 or depth > 2 or not isinstance(obj, dict):
            return
        for k, val in obj.items():
            if len(pairs) >= 6 or not isinstance(k, str):
                break
            if k.lower() in hidden:
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
    """The human-readable line of a LogEntry: textPayload, a JSON message, audit status, HTTP request or summary."""
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
                (entry.get("logName") or "").split("/logs/", 1)[-1])
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
    candidates.extend(["protopayload_auditlog.status.message", "schemaErrorDetail",
                       "jsonPayload.gen_ai_output_messages[0].parts[0].content", "jsonPayload.content.parts[0].text"])
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


def _max_entries(ctx):
    settings = getattr(ctx, "settings", None)
    if not isinstance(settings, dict):
        return MAX_LOG_ENTRIES
    return int(_tcfg(settings, "discovery")["max_log_entries"])


def _logging_data(ctx, conn, rng, out):
    flt = (conn.get("logging") or {}).get("filter") or ""
    is_live = type(getattr(ctx, "gcp", None)).__name__ == "Gcp"
    mapping = conn.get("mapping") or {}
    max_entries = _max_entries(ctx)
    prev = rng.previous()
    label = "logging:%s" % conn.get("name")
    if is_live:
        earliest = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
        query_start = max(min(prev.start_ts(), rng.end_ts() - dt.timedelta(days=30)), earliest)
    else:
        query_start = prev.start_ts()
    body_filter = '(%s) AND timestamp>="%s" AND timestamp<"%s"' % (flt, iso(query_start), iso(rng.end_ts()))
    entries, token, truncated = [], None, False
    resource_names = _logging_resource_names(ctx, flt)
    try:
        pages = 0
        page_cap = min(1000, max_entries)
        max_pages = max(1, -(-max_entries // page_cap))
        while pages < max_pages:
            pages += 1
            body = {"resourceNames": resource_names, "filter": body_filter, "orderBy": "timestamp desc",
                    "pageSize": min(page_cap, max_entries - len(entries))}
            if token:
                body["pageToken"] = token
            data = ctx.rest("POST", "%s/entries:list" % LOGGING_V2, json_body=body, timeout=10 if is_live else 45)
            entries.extend(e for e in data.get("entries") or [] if isinstance(e, dict))
            token = data.get("nextPageToken")
            if not token:
                break
            if len(entries) >= max_entries:
                truncated = True
                break
        if token:
            truncated = True
    except GcpError as err:
        ctx.warn_error(label, err, "Cloud Logging entries for this connector")
        return out
    ctx.add_source(label)
    if truncated:
        ctx.warn(label, "truncated", "Log entries were truncated at %d." % max_entries)
    tz = timeutil.get_zone(ctx.tz_name)
    time_path = mapping.get("time") or "timestamp"
    cur_events = prev_events = 0
    tokens_total = 0
    users = set()
    last = None
    ev_pairs, tok_pairs = [], []
    ops, statuses, top_users, models = {}, {}, {}, {}
    recent = []
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
        if ts < rng.start_ts():
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
            entry_proj = ((entry.get("resource") or {}).get("labels") or {}).get("project_id") or conn.get("project") \
                or ctx.project
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
    out["sample"] = {"limit": max_entries, "truncated": truncated, "partial": partial,
                     "since": iso(oldest) if partial else None}
    out["kpis"] = {"events": {"value": cur_events, "previous": None if truncated else prev_events},
                   "users": len(users) if mapping.get("user") else None,
                   "tokens": tokens_total if mapping.get("tokens") else None, "lastEvent": iso(last)}
    ev_values = timeutil.fold(rng, ev_pairs, int)
    tok_values = timeutil.fold(rng, tok_pairs, int) if mapping.get("tokens") else None
    if partial:
        cut = rng.bucket_index(oldest)
        for i in range((cut if cut is not None else -1) + 1):
            ev_values[i] = None
            if tok_values is not None:
                tok_values[i] = None
    series = [timeutil.series("events", "Events", ev_values)]
    if tok_values is not None:
        series.append(timeutil.series("tokens", "Tokens", tok_values))
    out["trend"] = timeutil.timeline(rng, series)
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
    """{Agent Engine ID: display name} of the configured regions; {} when they cannot be listed."""
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
    """Display names for operation values that are Agent Engine ids (any source; matched by value, not by log name)."""
    values = {o["operation"] for o in out.get("topOperations") or [] if o.get("operation")}
    values |= {r["operation"] for r in out.get("recent") or [] if r.get("operation")}
    ids = {str(v): ENGINE_ID_RE.search(str(v)) for v in values}
    ids = {v: m.group(1) for v, m in ids.items() if m}
    if not ids:
        return None
    names = _engine_names(ctx)
    found = {v: names[i] for v, i in ids.items() if names.get(i)}
    return {"operation": found} if found else None


def _compute_connector_data(ctx, conn_id, rng):
    conn = next((c for c in all_connectors(ctx) if c.get("id") == conn_id), None)
    if conn is None:
        raise ApiError("not_found", "Connector not found.")
    item = copy.deepcopy(conn)
    _enrich_connector_meta(ctx, item)
    item["health"] = "ok"
    item["lastEvent"] = None
    item["catalog"] = _connector_card(ctx, item)
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
        key = ("telemetry_connector_data", conn_id, str(rng.start), str(rng.end), rng.granularity, ctx.project,
               getattr(ctx, "version", 0))
        val, _ = ctx.app.cache.get_or_compute(key, HEALTH_TTL, lambda: _compute_connector_data(ctx, conn_id, rng))
        return copy.deepcopy(val)
    return _compute_connector_data(ctx, conn_id, rng)
