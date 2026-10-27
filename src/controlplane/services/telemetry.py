"""Telemetry extensions: table/log auto-discovery, one-click connectors and their standard dashboards.

Discovery describes every source with the plain-language catalog in telemetry_catalog.py.
"""

from __future__ import annotations

import copy
import datetime as dt
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
LOG_ENTRY_METRIC = "logging.googleapis.com/log_entry_count"
ACTIVITY_DAYS = 7
ENGINE_ID_RE = re.compile(r"(?:reasoningEngines/)?([0-9]{6,})$")
NAMES_TTL = 600
HEURISTICS = {
    "user": ("principalemail", "useriamprincipal", "user_id", "userid", "user_email", "email", "user"),
    "model": ("model_id", "modelid", "model", "model_version"),
    "tokens": ("totaltokencount", "total_tokens", "token_count", "tokens"),
    "operation": ("methodname", "operationtype", "operation", "action", "event_type", "eventtype"),
    "status": ("severity", "status.code", "verdict"),
}


def suggested_mapping(leaf_types):
    """Case-insensitive leaf-name heuristics."""
    lower = [(path, path.lower(), ftype) for path, ftype in leaf_types]
    mapping = {}
    time = None
    for wanted in ("timestamp", "receivetimestamp"):
        time = next((p for p, low, _ in lower if low == wanted), None)
        if time:
            break
    if time is None:
        time = next((p for p, _low, t in lower if t.startswith("TIMESTAMP") and "." not in p), None)
    mapping["time"] = time
    for key, endings in HEURISTICS.items():
        found = None
        for ending in endings:
            for path, low, _t in lower:
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
    out = [(ctx.project, ctx.audit_dataset)]
    for ref in ctx.ws.get("telemetry_datasets") or []:
        parsed = config.parse_dataset_ref(ref, ctx.project)
        if parsed and parsed not in out:
            out.append(parsed)
    return out


def _table_stats(ctx, project, dataset):
    sql = "SELECT table_id, row_count, size_bytes, last_modified_time FROM `%s.%s.__TABLES__`" % (project, dataset)
    rows = ctx.query(sql, {}, "telemetry")
    return {r["table_id"]: r for r in rows if r.get("table_id")}


def _discover_tables(ctx, connectors_by_table):
    """Tables of the audit and telemetry datasets, or None when no dataset could be scanned."""
    tables = []
    scanned = 0
    for project, dataset in discover_datasets(ctx):
        label = "bigquery:%s" % dataset
        try:
            groups = ctx.registry.tables(project, dataset, ctx.quota_project, ctx.max_bytes)
        except GcpError as err:
            ctx.warn_error(label, err, "BigQuery dataset %s" % dataset)
            continue
        ctx.add_source("bigquery:%s.INFORMATION_SCHEMA.TABLES" % dataset)
        scanned += 1
        try:
            stats = _table_stats(ctx, project, dataset)
        except GcpError as err:
            ctx.warn_error(label, err, "Table statistics for %s" % dataset)
            stats = {}
        names = sorted(groups)
        if len(names) > MAX_TABLES:
            ctx.warn(label, "truncated", "Only the first %d tables of %s are inspected." % (MAX_TABLES, dataset))
            names = names[:MAX_TABLES]
        schema_tasks = {n: (lambda n=n: ctx.registry.schema(project, dataset, groups[n]["latest"], ctx.quota_project))
                        for n in names}
        schemas = ctx.run(schema_tasks)
        failed_schemas = sum(1 for ok, _ in schemas.values() if not ok)
        if failed_schemas:
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
            columns = schema.leaf_paths(200) if schema is not None else None
            mapping = suggested_mapping(schema.leaf_types(200)) if schema is not None else None
            sid = name.lower()
            core = name in CORE_TABLES and (project, dataset) == (ctx.project, ctx.audit_dataset)
            card = catalog.describe(sid, dataset=dataset, built_in=None if core else [])
            quick, card = catalog.bigquery_connector(card, project, dataset, name, schema, mapping)
            conn_id = connectors_by_table.get((project, dataset, name))
            tables.append({"id": sid, "project": project, "dataset": dataset, "table": name + ("_*" if sharded else ""),
                           "logical": name, "sharded": sharded, "rows": rows, "sizeBytes": size,
                           "lastModified": iso(dt.datetime.fromtimestamp(modified / 1000.0, tz=dt.timezone.utc))
                           if modified else None,
                           "core": core, "registered": conn_id is not None, "connectorId": conn_id,
                           "suggestedMapping": mapping, "columns": columns, "catalog": card, "quickAdd": quick,
                           "entries7d": None, "daily": None})
    return tables if scanned else None


def _list_logs(ctx):
    """[{logName, shortName}] of the project, or None when the log list failed."""
    try:
        names, truncated = ctx.gcp.paginate("%s/projects/%s/logs" % (LOGGING_V2, ctx.project),
                                            quota_project=ctx.quota_project, items_key="logNames", page_size=1000,
                                            max_pages=5)
    except GcpError as err:
        ctx.warn_error("api:logging.logs", err, "The Cloud Logging log list")
        return None
    ctx.add_source("api:logging.logs")
    if truncated:
        ctx.warn("api:logging.logs", "truncated", "Only the first 5000 log names are listed.")
    out = []
    for log_name in names:
        if isinstance(log_name, str) and "/logs/" in log_name:
            out.append({"logName": log_name, "shortName": urllib.parse.unquote(log_name.split("/logs/", 1)[1])})
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


def discover(ctx):
    """Every telemetry source of the project, described in plain language.

    tables is null when no dataset could be scanned and logs is null when the log list failed. Each source has
    `catalog` (what the data is and what its dashboard shows), `quickAdd` (a ready body for
    POST /api/v1/telemetry/connectors, null when the source cannot be added) and `connectorId` (the connector
    that already reads it). entries7d/daily are the entries written per day over the last 7 days according to
    Cloud Monitoring; they are null when that metric could not be read."""
    by_table, by_log = {}, {}
    for conn in all_connectors(ctx):
        bq = conn.get("bigquery") or {}
        if conn.get("source") == "bigquery" and bq.get("table"):
            key = (bq.get("project"), bq.get("dataset"), bq["table"][:-2] if bq["table"].endswith("_*") else bq["table"])
            by_table.setdefault(key, conn.get("id"))
        elif conn.get("source") == "logging":
            sid = catalog.connector_source_id(conn)
            if sid:
                by_log.setdefault(sid, conn.get("id"))
    res = ctx.run_composite({"tables": lambda: _discover_tables(ctx, by_table), "logs": lambda: _list_logs(ctx),
                             "activity": lambda: _log_activity(ctx)})
    tables = _task_result(ctx, res["tables"], "telemetry:tables", "Table discovery")
    logs = _task_result(ctx, res["logs"], "api:logging.logs", "The Cloud Logging log list")
    activity = _task_result(ctx, res["activity"], "api:monitoring.log_entry_count", "Log activity from Cloud Monitoring")
    logical = {tb["id"] for tb in tables or []}
    builtin = _builtin_logs(ctx)
    by_id = {}
    for log in logs or []:
        short = log["shortName"]
        sid = catalog.sanitize_log_name(short)
        counts = (activity.get(short) or [0] * ACTIVITY_DAYS) if activity is not None else None
        core = sid in CORE_TABLES or short in builtin
        card = catalog.describe(sid, short=short, built_in=builtin.get(short) if short in builtin else
                                (None if core else []))
        log.update({"id": sid, "routedToBigQuery": sid in logical, "core": core,
                    "registered": sid in by_log, "connectorId": by_log.get(sid),
                    "entries7d": sum(counts) if counts is not None else None, "daily": counts,
                    "catalog": card, "quickAdd": catalog.logging_connector(card, log["logName"])})
        by_id.setdefault(sid, log)
    for tb in tables or []:
        log = by_id.get(tb["id"])
        if log is not None:
            tb["entries7d"], tb["daily"] = log["entries7d"], log["daily"]
    return {"tables": tables, "logs": logs, "activityDays": ACTIVITY_DAYS}


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
        body = {"resourceNames": ["projects/%s" % ctx.project], "filter": "(%s) AND timestamp>=\"%s\"" % (flt, since),
                "orderBy": "timestamp desc", "pageSize": 1}
        data = ctx.rest("POST", "%s/entries:list" % LOGGING_V2, json_body=body)
        entries = data.get("entries") or []
        return "ok", iso(parse_rfc3339(entries[0].get("timestamp"))) if entries else None
    except (GcpError, ValueError, ApiError) as err:
        LOG.info("connector health check failed id=%s kind=%s", conn.get("id"), getattr(err, "kind", type(err).__name__))
        return "error", None


def with_health(ctx, conn):
    item = copy.deepcopy(conn)
    item.setdefault("bigquery", None)
    item.setdefault("logging", None)
    key = ("connector_health", json.dumps(conn, sort_keys=True, default=str))
    (health, last), _ = ctx.app.cache.get_or_compute(key, HEALTH_TTL, lambda: _connector_health(ctx, conn))
    item["health"] = health
    item["lastEvent"] = last
    item["catalog"] = catalog.for_connector(conn)
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
            item.update({"health": "error", "lastEvent": None, "catalog": catalog.for_connector(conns[i])})
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


# ---------------------------------------------------------------------------
# GET /api/v1/telemetry/connectors/data
# ---------------------------------------------------------------------------

def _get_path(obj, path):
    """Resolves a dotted path (with optional [n] indexes) on a LogEntry; returns scalars only."""
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


def _log_message(entry):
    """The human-readable line of a LogEntry: textPayload, a JSON message or an audit status message."""
    text = entry.get("textPayload")
    if not isinstance(text, str):
        payload = entry.get("jsonPayload") if isinstance(entry.get("jsonPayload"), dict) else {}
        text = next((payload[k] for k in ("message", "msg", "error") if isinstance(payload.get(k), str)), None)
    if not isinstance(text, str):
        proto = entry.get("protoPayload") if isinstance(entry.get("protoPayload"), dict) else {}
        status = proto.get("status") if isinstance(proto.get("status"), dict) else {}
        text = status.get("message") if isinstance(status.get("message"), str) else None
    text = text.strip() if isinstance(text, str) else ""
    return text[:MESSAGE_CHARS] or None


def _message_expr(schema):
    """SQL for the same line in an exported table (typed payload columns are named jsonpayload_<type>)."""
    candidates = ["textPayload", "jsonPayload.message"]
    candidates += ["%s.message" % node.name for name, node in schema.tree.items()
                   if name.startswith("jsonpayload") and name != "jsonpayload"]
    candidates.append("protopayload_auditlog.status.message")
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
        out["recent"] = [{"time": iso(r.get("ts")), "user": r.get("user"), "operation": r.get("operation"),
                          "model": r.get("model"), "tokens": to_int(r.get("tokens")) if r.get("tokens") is not None
                          else None, "status": r.get("status"), "message": r.get("message") or None,
                          "payload": r.get("payload")} for r in rows]
    else:
        ctx.warn_error(label, rows, "The connector table")
    return out


def _logging_data(ctx, conn, rng, out):
    flt = (conn.get("logging") or {}).get("filter") or ""
    mapping = conn.get("mapping") or {}
    prev = rng.previous()
    label = "logging:%s" % conn.get("name")
    body_filter = '(%s) AND timestamp>="%s" AND timestamp<"%s"' % (flt, iso(prev.start_ts()), iso(rng.end_ts()))
    entries = []
    token = None
    truncated = False
    try:
        while True:
            body = {"resourceNames": ["projects/%s" % ctx.project], "filter": body_filter,
                    "orderBy": "timestamp desc", "pageSize": min(1000, MAX_LOG_ENTRIES - len(entries))}
            if token:
                body["pageToken"] = token
            data = ctx.rest("POST", "%s/entries:list" % LOGGING_V2, json_body=body, timeout=45)
            entries.extend(e for e in data.get("entries") or [] if isinstance(e, dict))
            token = data.get("nextPageToken")
            if not token:
                break
            if len(entries) >= MAX_LOG_ENTRIES:
                truncated = True
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
            recent.append({"time": iso(ts), "user": str(user) if user is not None else None,
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


def connector_data(ctx):
    rng = ctx.range
    conn_id = ctx.params.get("id") or ""
    conn = next((c for c in all_connectors(ctx) if c.get("id") == conn_id), None)
    if conn is None:
        raise ApiError("not_found", "Connector not found.")
    item = with_health(ctx, conn)
    out = _empty_data(item, rng)
    if not conn.get("enabled", True):
        ctx.warn("connector:%s" % conn_id, "disabled", "This connector is disabled.")
        return out
    try:
        if conn.get("source") == "bigquery":
            _bq_data(ctx, conn, rng, out)
        else:
            _logging_data(ctx, conn, rng, out)
    except GcpError as err:
        ctx.warn_error("connector:%s" % conn_id, err, "The connector source")
        return out
    out["valueNames"] = _value_names(ctx, conn, out)
    return out
