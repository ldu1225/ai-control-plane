"""Log Router -> BigQuery routing created from Telemetry Extensions, with cost controls.

Every sink this control plane creates is a managed route: its name starts with telemetry.routing.sink_prefix, its
destination is the dedicated dataset telemetry.routing.dataset (created on first use in the location of the audit
dataset, labelled managed-by=ai-control-plane, with a default partition expiration of the retention policy) and its
definition is kept in the telemetry_routing document. Routing everything without limits gets expensive, so every
route carries cost controls (defaults in Settings > Data collection and cost, overridable per route):

- sampling: sample(insertId, fraction) in the sink filter, and a minimum severity;
- exclusion filters: sink exclusions, for example health checks or debug output;
- retention: partition expiration of the routed tables (the dataset default plus each existing table);
- daily budgets in GiB per route and for all routes together, measured with the Cloud Monitoring metric
  logging.googleapis.com/exports/byte_count. Over budget, routes are paused (the sink is disabled) or only reported
  (telemetry.routing.over_budget_action) and resumed at the next local day (telemetry.routing.auto_resume);
- a maximum number of routes;
- group routes follow their Gemini group: logs that later join the group are added automatically while the estimated
  volume stays within the budget (telemetry.routing.follow_groups).

Estimates use logging.googleapis.com/byte_count per log and the BigQuery streaming-insert and storage list prices of
the Cloud Billing Catalog, synced daily with the token prices. Nothing about volumes or prices is built in.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import secrets
import urllib.parse
import urllib.request

from .. import config, timeutil
from ..gcp import GcpError
from ..httpkit import ApiError, now_iso
from ..settings_store import aux_docs
from .base import iso, parse_rfc3339, require_identity_admin, to_float, to_int
from . import telemetry_profile as profile

LOG = logging.getLogger("controlplane.telemetry_routing")

LOGGING_V2 = "https://logging.googleapis.com/v2"
BQ_V2 = "https://bigquery.googleapis.com/bigquery/v2"
MONITORING_V3 = "https://monitoring.googleapis.com/v3/projects/%s/timeSeries"
EXPORT_BYTES_METRIC = "logging.googleapis.com/exports/byte_count"
LOG_BYTES_METRIC = "logging.googleapis.com/byte_count"
STATE_DOC = "telemetry_routing"
ROUTE_ID_RE = re.compile(r"^rt_[0-9a-f]{8}$")
SINK_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,100}$")
GIB = float(2 ** 30)
MAX_LOGS_PER_ROUTE = 60
MAX_FILTER_CHARS = 18000
MAX_EVENTS = 200
DATASET_LABEL = ("managed-by", "ai-control-plane")
ACTIONS = ("pause", "resume", "delete", "apply")


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

def policy(settings):
    """Effective routing defaults (Settings > Data collection and cost)."""
    r = ((settings or {}).get("telemetry") or {}).get("routing") or {}
    d = config.DEFAULTS["telemetry"]["routing"]
    get = lambda k: r.get(k, d[k])  # noqa: E731
    return {"dataset": get("dataset"), "datasetLocation": get("dataset_location"), "sinkPrefix": get("sink_prefix"),
            "samplePercent": float(get("sample_percent")), "minSeverity": get("min_severity") or "",
            "exclusions": list(get("exclusion_filters") or []), "retentionDays": int(get("retention_days")),
            "dailyBudgetGb": float(get("daily_budget_gb")), "sinkDailyGb": float(get("sink_daily_gb")),
            "overBudgetAction": get("over_budget_action"), "autoResume": bool(get("auto_resume")),
            "maxSinks": int(get("max_sinks")), "followGroups": bool(get("follow_groups")),
            "webhook": get("notify_webhook_url") or ""}


def route_policy(route, defaults):
    """A route's own values where set, else the defaults."""
    own = (route or {}).get("policy") or {}
    pick = lambda k, dk: own[k] if own.get(k) is not None else defaults[dk]  # noqa: E731
    return {"samplePercent": float(pick("samplePercent", "samplePercent")),
            "minSeverity": pick("minSeverity", "minSeverity") or "",
            "exclusions": list(pick("exclusions", "exclusions") or []),
            "dailyGb": float(pick("dailyGb", "sinkDailyGb")),
            "retentionDays": int(pick("retentionDays", "retentionDays"))}


def _clean_policy(raw):
    """Validated per-route overrides; None keeps the default."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    try:
        if raw.get("samplePercent") not in (None, ""):
            out["samplePercent"] = config.VALIDATORS["telemetry.routing.sample_percent"](float(raw["samplePercent"]))
        if raw.get("minSeverity") is not None:
            out["minSeverity"] = config.VALIDATORS["telemetry.routing.min_severity"](str(raw["minSeverity"]).upper())
        if raw.get("exclusions") is not None:
            out["exclusions"] = config.VALIDATORS["telemetry.routing.exclusion_filters"](list(raw["exclusions"]))
        if raw.get("dailyGb") not in (None, ""):
            out["dailyGb"] = config.VALIDATORS["telemetry.routing.sink_daily_gb"](float(raw["dailyGb"]))
        if raw.get("retentionDays") not in (None, ""):
            out["retentionDays"] = config.VALIDATORS["telemetry.routing.retention_days"](int(raw["retentionDays"]))
    except (config.Invalid, TypeError, ValueError) as err:
        raise ApiError("bad_request", "Invalid routing policy: %s." % err)
    return out


def build_filter(logs, base_filter="", min_severity="", sample_percent=100.0):
    """The sink filter: the routed logs (or a custom filter), the severity floor and the sampling fraction."""
    parts = []
    quoted = ['"%s"' % ln.replace("\\", "\\\\").replace('"', '\\"') for ln in logs or []]
    if len(quoted) == 1:
        parts.append("logName=%s" % quoted[0])
    elif quoted:
        parts.append("logName=(%s)" % " OR ".join(quoted))
    if base_filter:
        parts.append("(%s)" % base_filter.strip())
    if min_severity:
        parts.append("severity>=%s" % min_severity)
    if sample_percent and float(sample_percent) < 100.0:
        frac = ("%.6f" % (float(sample_percent) / 100.0)).rstrip("0").rstrip(".")
        parts.append("sample(insertId, %s)" % frac)
    return " AND ".join(parts)


def _exclusions(filters):
    return [{"name": "acp-exclusion-%d" % (i + 1), "filter": f, "disabled": False} for i, f in enumerate(filters or [])]


# ---------------------------------------------------------------------------
# State document
# ---------------------------------------------------------------------------

def load_state(ctx):
    doc, _ = aux_docs(getattr(ctx, "app", None)).load(STATE_DOC)
    doc.setdefault("routes", [])
    doc.setdefault("events", [])
    return doc


def _mutate(ctx, fn):
    def apply(doc):
        doc.setdefault("routes", [])
        doc.setdefault("events", [])
        result = fn(doc)
        doc["events"] = doc["events"][-MAX_EVENTS:]
        return result
    return aux_docs(getattr(ctx, "app", None)).mutate(STATE_DOC, apply)


def _event(kind, route, detail, **extra):
    item = {"at": now_iso(), "kind": kind, "route": (route or {}).get("id"), "sink": (route or {}).get("sink"),
            "title": (route or {}).get("title"), "detail": str(detail)[:300]}
    item.update(extra)
    return item


def routed_index(state):
    """{log name: route id} of every log a managed route sends to BigQuery."""
    out = {}
    for route in (state or {}).get("routes") or []:
        for log_name in route.get("logs") or []:
            out.setdefault(log_name, route.get("id"))
    return out


# ---------------------------------------------------------------------------
# Volumes, prices and estimates
# ---------------------------------------------------------------------------

def storage_prices(ctx, currency=None):
    """{"ingestPerGiB", "storagePerGiBMonth", "currency", "source"} in the display currency, or None when the
    daily price sync has not found BigQuery list prices yet."""
    from . import pricing_sync
    settings_mgr = getattr(getattr(ctx, "app", None), "settings", None)
    storage = (settings_mgr.pricing_state().get("storage") if settings_mgr is not None else None) or {}
    if storage.get("ingestPerGiB") is None and storage.get("storagePerGiBMonth") is None:
        return None
    cur = pricing_sync.preferred_currency(ctx, currency)
    fx = pricing_sync.fx_rate(ctx, cur)
    if fx is None:
        cur, fx = "USD", {"rate": 1.0}
    rate = float(fx["rate"])
    conv = lambda v: round(v * rate, 6) if v is not None else None  # noqa: E731
    return {"ingestPerGiB": conv(storage.get("ingestPerGiB")), "storagePerGiBMonth": conv(storage.get("storagePerGiBMonth")),
            "currency": cur, "source": storage.get("source") or "cloud_billing_catalog", "service": storage.get("service")}


def estimate(bytes_per_day, sample_percent, retention_days, prices):
    """GiB per day after sampling and the monthly BigQuery cost (streaming ingestion + steady-state storage)."""
    if bytes_per_day is None:
        return {"gibPerDay": None, "monthlyCost": None, "currency": (prices or {}).get("currency"), "priced": False}
    gib_day = float(bytes_per_day) * float(sample_percent) / 100.0 / GIB
    out = {"gibPerDay": round(gib_day, 6), "monthlyCost": None, "currency": (prices or {}).get("currency"),
           "priced": False}
    if prices:
        ingest = prices.get("ingestPerGiB")
        store = prices.get("storagePerGiBMonth")
        if ingest is not None or store is not None:
            kept_days = retention_days if retention_days and retention_days > 0 else 30
            cost = gib_day * 30.0 * (ingest or 0.0) + gib_day * kept_days * (store or 0.0)
            out.update({"monthlyCost": round(cost, 4), "priced": True})
    return out


def log_volumes(ctx, days=None):
    """{log id: average bytes per day} over the discovery activity window (Cloud Monitoring byte_count)."""
    from . import monitoring
    days = int(days or ((ctx.settings.get("telemetry") or {}).get("discovery") or {}).get("activity_days") or 7)
    now = dt.datetime.now(dt.timezone.utc)
    try:
        series = monitoring.time_series(ctx, LOG_BYTES_METRIC, ["metric.label.log"], now - dt.timedelta(days=days), now)
    except GcpError as err:
        if err.kind in ("not_found", "invalid"):
            return {}
        raise
    out = {}
    for labels, points in series:
        name = urllib.parse.unquote(str(labels.get("metric.label.log") or "")).strip()
        if name:
            out[name] = out.get(name, 0) + sum(to_int(v) for _d, v in points)
    return {k: v / float(days) for k, v in out.items()}


def _local_midnight(tz_name, now=None):
    tz = timeutil.get_zone(tz_name)
    now = now or dt.datetime.now(dt.timezone.utc)
    local = now.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc), local.date()


def exported_bytes_today(ctx, project, tz_name, now=None):
    """{sink name: bytes exported since local midnight} for one project (logging_sink exports/byte_count)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    start, _ = _local_midnight(tz_name, now)
    period = max(60, int((now - start).total_seconds()) // 60 * 60)
    params = [("filter", 'metric.type="%s" AND resource.type="logging_sink"' % EXPORT_BYTES_METRIC),
              ("interval.startTime", iso(now - dt.timedelta(seconds=period))), ("interval.endTime", iso(now)),
              ("aggregation.alignmentPeriod", "%ds" % period), ("aggregation.perSeriesAligner", "ALIGN_SUM"),
              ("aggregation.crossSeriesReducer", "REDUCE_SUM"), ("aggregation.groupByFields", "resource.label.name"),
              ("pageSize", "1000")]
    payload, _ = ctx.gcp.request("GET", MONITORING_V3 % project, quota_project=ctx.quota_project, params=params,
                                 timeout=30)
    out = {}
    for series in payload.get("timeSeries") or []:
        name = ((series.get("resource") or {}).get("labels") or {}).get("name")
        total = 0
        for point in series.get("points") or []:
            value = point.get("value") or {}
            total += to_int(value.get("int64Value")) if "int64Value" in value else int(to_float(value.get("doubleValue")) or 0)
        if name:
            out[name] = out.get(name, 0) + total
    return out


# ---------------------------------------------------------------------------
# Google Cloud resources
# ---------------------------------------------------------------------------

def _dataset_location(ctx, pol):
    if pol["datasetLocation"]:
        return pol["datasetLocation"]
    try:
        loc = ctx.registry.dataset_location(ctx.project, ctx.audit_dataset, ctx.quota_project)
    except Exception:  # pylint: disable=broad-except
        loc = None
    return loc or "US"


def ensure_dataset(ctx, pol):
    """Creates the routing dataset when missing (labelled, with the retention as default partition expiration) or
    updates the default expiration of a dataset this control plane created. Returns dataset metadata."""
    project, dataset = ctx.project, pol["dataset"]
    if not config.valid_dataset(dataset):
        raise ApiError("bad_request", "The routing dataset name in Settings is invalid.")
    expiration = str(int(pol["retentionDays"]) * 86400000) if pol["retentionDays"] > 0 else None
    url = "%s/projects/%s/datasets/%s" % (BQ_V2, project, dataset)
    try:
        meta = ctx.rest("GET", url)
    except GcpError as err:
        if err.kind != "not_found":
            raise ApiError("upstream_error", "The routing dataset %s.%s could not be read." % (project, dataset))
        body = {"datasetReference": {"projectId": project, "datasetId": dataset}, "location": _dataset_location(ctx, pol),
                "description": "Logs routed by AI Control Plane telemetry routes (cost-controlled).",
                "labels": {DATASET_LABEL[0]: DATASET_LABEL[1]}}
        if expiration:
            body["defaultPartitionExpirationMs"] = expiration
        try:
            meta = ctx.rest("POST", "%s/projects/%s/datasets" % (BQ_V2, project), json_body=body)
        except GcpError as err2:
            raise ApiError("upstream_error", "The routing dataset %s.%s could not be created (%s)." % (project, dataset,
                                                                                                    err2.kind))
        return meta
    managed = (meta.get("labels") or {}).get(DATASET_LABEL[0]) == DATASET_LABEL[1]
    if managed and meta.get("defaultPartitionExpirationMs") != expiration:
        try:
            meta = ctx.rest("PATCH", url, json_body={"defaultPartitionExpirationMs": expiration})
        except GcpError as err:
            LOG.warning("routing dataset expiration update failed kind=%s", err.kind)
    return meta


def grant_writer(ctx, dataset, writer_identity):
    """Gives the sink writer WRITER on the routing dataset only. Returns True when granted or already present."""
    if not writer_identity.startswith("serviceAccount:"):
        return False
    email = writer_identity.split(":", 1)[1].strip()
    url = "%s/projects/%s/datasets/%s" % (BQ_V2, ctx.project, dataset)
    try:
        meta = ctx.rest("GET", url)
        access = list(meta.get("access") or [])
        if not any(isinstance(a, dict) and a.get("userByEmail") == email and
                   a.get("role") in ("WRITER", "OWNER", "roles/bigquery.dataEditor", "roles/bigquery.dataOwner")
                   for a in access):
            access.append({"role": "WRITER", "userByEmail": email})
            ctx.rest("PATCH", url, json_body={"access": access})
        return True
    except GcpError as err:
        LOG.warning("routing dataset grant failed kind=%s", err.kind)
        return False


def _sink_url(project, name=None):
    base = "%s/projects/%s/sinks" % (LOGGING_V2, urllib.parse.quote(project, safe=""))
    return base if name is None else "%s/%s" % (base, urllib.parse.quote(name, safe=""))


def apply_sink(ctx, route, pol, disabled=None):
    """Creates or updates the Log Router sink of a route. Returns the sink resource."""
    rp = route_policy(route, pol)
    flt = build_filter(route.get("logs"), route.get("filter") or "", rp["minSeverity"], rp["samplePercent"])
    if not flt or len(flt) > MAX_FILTER_CHARS:
        raise ApiError("bad_request", "The route filter is empty or too long; route fewer logs per route.")
    body = {"name": route["sink"], "destination": "bigquery.googleapis.com/projects/%s/datasets/%s" % (ctx.project,
                                                                                                       pol["dataset"]),
            "filter": flt, "description": ("AI Control Plane telemetry route %s: %s" % (route["id"], route.get("title")
                                                                                        or ""))[:900],
            "disabled": bool(route.get("status") in ("paused", "paused_budget")) if disabled is None else bool(disabled),
            "exclusions": _exclusions(rp["exclusions"]), "bigqueryOptions": {"usePartitionedTables": True}}
    try:
        return ctx.rest("POST", "%s?uniqueWriterIdentity=true" % _sink_url(route["project"]), json_body=body)
    except GcpError as err:
        if err.kind != "conflict" and err.status != 409:
            LOG.warning("sink create failed project=%s sink=%s kind=%s", route["project"], route["sink"], err.kind)
            raise ApiError("upstream_error", "The Log Router sink %s could not be created (%s)." % (route["sink"], err.kind))
    mask = "destination,filter,description,disabled,exclusions"
    body.pop("bigqueryOptions", None)
    try:
        return ctx.rest("PUT", "%s?uniqueWriterIdentity=true&updateMask=%s" % (_sink_url(route["project"], route["sink"]),
                                                                              mask), json_body=body)
    except GcpError as err:
        raise ApiError("upstream_error", "The Log Router sink %s could not be updated (%s)." % (route["sink"], err.kind))


def set_disabled(ctx, route, disabled):
    ctx.rest("PATCH", "%s?uniqueWriterIdentity=true&updateMask=disabled" % _sink_url(route["project"], route["sink"]),
             json_body={"name": route["sink"], "disabled": bool(disabled)})


def delete_sink(ctx, route):
    try:
        ctx.rest("DELETE", _sink_url(route["project"], route["sink"]))
    except GcpError as err:
        if err.kind != "not_found":
            raise ApiError("upstream_error", "The Log Router sink %s could not be deleted (%s)." % (route["sink"], err.kind))


def apply_retention(ctx, route, pol):
    """Sets the partition expiration of the route's existing tables to its retention. Returns tables updated."""
    days = route_policy(route, pol)["retentionDays"]
    want = str(days * 86400000) if days > 0 else None
    updated = 0
    for log_name in route.get("logs") or []:
        table = profile.sanitize_log_name(urllib.parse.unquote(log_name.split("/logs/", 1)[-1]))
        url = "%s/projects/%s/datasets/%s/tables/%s" % (BQ_V2, ctx.project, pol["dataset"], table)
        try:
            meta = ctx.rest("GET", url)
        except GcpError:
            continue  # the sink creates the table with the first entry
        part = dict(meta.get("timePartitioning") or {})
        if not part or part.get("expirationMs") == want:
            continue
        part["expirationMs"] = want
        try:
            ctx.rest("PATCH", url, json_body={"timePartitioning": part})
            updated += 1
        except GcpError as err:
            LOG.warning("table expiration update failed table=%s kind=%s", table, err.kind)
    return updated


def notify(pol, text):
    url = pol.get("webhook") or ""
    if not url or not config.valid_webhook(url):
        return
    try:
        req = urllib.request.Request(url, data=json.dumps({"text": text}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310 - allowlisted https webhook hosts only
            resp.read(256)
    except Exception as err:  # pylint: disable=broad-except
        LOG.info("routing webhook failed: %s", type(err).__name__)


# ---------------------------------------------------------------------------
# API: GET /api/v1/telemetry/routing
# ---------------------------------------------------------------------------

def _gb(value):
    return round(value / GIB, 4) if value is not None else None


def get_routing(ctx):
    pol = policy(ctx.settings)
    state = load_state(ctx)
    prices = storage_prices(ctx)
    routes = [dict(r) for r in state.get("routes") or []]
    projects = sorted({r.get("project") for r in routes if r.get("project")})
    need_volumes = any(r.get("logs") for r in routes)
    tasks = {}
    for proj in projects:
        tasks["bytes:%s" % proj] = (lambda p=proj: exported_bytes_today(ctx, p, ctx.tz_name))
    for route in routes:
        tasks["sink:%s" % route["id"]] = (lambda r=route: ctx.rest("GET", _sink_url(r["project"], r["sink"])))
    if need_volumes:
        tasks["volumes"] = (lambda: log_volumes(ctx))
    results = ctx.run(tasks) if tasks else {}
    bytes_today = {}
    for proj in projects:
        ok, val = results.get("bytes:%s" % proj, (False, None))
        if ok:
            bytes_today.update(val or {})
        else:
            ctx.warn_error("monitoring:%s" % EXPORT_BYTES_METRIC, val, "Exported log volume from Cloud Monitoring")
    volumes = {}
    if need_volumes:
        ok_vol, val_vol = results.get("volumes", (False, None))
        if ok_vol:
            volumes = val_vol or {}
        elif isinstance(val_vol, GcpError):
            ctx.warn_error("monitoring:%s" % LOG_BYTES_METRIC, val_vol, "Log volume from Cloud Monitoring")
    total_today = 0
    for route in routes:
        ok, sink = results.get("sink:%s" % route["id"], (False, None))
        rp = route_policy(route, pol)
        live = {"exists": bool(ok), "disabled": bool(sink.get("disabled")) if ok else None,
                "writerIdentity": sink.get("writerIdentity") if ok else None,
                "filter": sink.get("filter") if ok else None}
        today = bytes_today.get(route["sink"])
        total_today += today or 0
        bpd = sum(volumes.get(urllib.parse.unquote(ln.split("/logs/", 1)[-1]), 0) for ln in route.get("logs") or [])
        route.update({"effectivePolicy": rp, "live": live, "bytesToday": today, "gbToday": _gb(today),
                      "estimate": estimate(bpd if route.get("logs") else None, rp["samplePercent"], rp["retentionDays"],
                                           prices),
                      "overBudget": bool(rp["dailyGb"] and today and today > rp["dailyGb"] * GIB)})
    public_pol = dict(pol, webhook=bool(pol["webhook"]))  # the webhook URL is a secret; only say whether it is set
    return {"policy": public_pol, "dataset": {"project": ctx.project, "dataset": pol["dataset"]}, "prices": prices,
            "routes": routes, "totals": {"bytesToday": total_today, "gbToday": _gb(total_today),
                                         "dailyBudgetGb": pol["dailyBudgetGb"], "routes": len(routes),
                                         "maxSinks": pol["maxSinks"]},
            "events": list(reversed(state.get("events") or []))[:60], "lastEnforcement": state.get("lastEnforcement")}


# ---------------------------------------------------------------------------
# API: estimate, create/update, actions
# ---------------------------------------------------------------------------

def _clean_logs(ctx, logs, project):
    out = []
    for name in logs or []:
        name = str(name or "").strip()
        if not profile.LOG_NAME_RE.match(name):
            raise ApiError("bad_request", "Each routed log must be a full log name (projects/<id>/logs/<log id>).")
        if not name.startswith("projects/%s/logs/" % project):
            raise ApiError("bad_request", "All logs of one route must belong to the route's project.")
        if name not in out:
            out.append(name)
    if len(out) > MAX_LOGS_PER_ROUTE:
        raise ApiError("bad_request", "A route can send at most %d logs." % MAX_LOGS_PER_ROUTE)
    return out


def estimate_route(ctx):
    body = ctx.body or {}
    pol = policy(ctx.settings)
    rp = route_policy({"policy": _clean_policy(body.get("policy"))}, pol)
    logs = [str(n) for n in body.get("logs") or [] if isinstance(n, str)][:MAX_LOGS_PER_ROUTE]
    prices = storage_prices(ctx)
    try:
        volumes = log_volumes(ctx)
    except GcpError as err:
        ctx.warn_error("monitoring:%s" % LOG_BYTES_METRIC, err, "Log volume from Cloud Monitoring")
        volumes = None
    per_log = []
    # Volume is measured per log; a filter-only route (for example a workload stream) cannot be estimated.
    total = 0.0 if (volumes is not None and logs) else None
    for name in logs:
        short = urllib.parse.unquote(name.split("/logs/", 1)[-1])
        bpd = volumes.get(short) if volumes is not None else None
        if bpd is not None and total is not None:
            total += bpd
        per_log.append({"logName": name, "bytesPerDay": bpd,
                        "estimate": estimate(bpd, rp["samplePercent"], rp["retentionDays"], prices)})
    est = estimate(total, rp["samplePercent"], rp["retentionDays"], prices)
    state = load_state(ctx)
    others = 0.0
    for route in state.get("routes") or []:
        if route.get("id") == body.get("id") or volumes is None:
            continue
        orp = route_policy(route, pol)
        others += sum(volumes.get(urllib.parse.unquote(ln.split("/logs/", 1)[-1]), 0) for ln in route.get("logs") or []) \
            * orp["samplePercent"] / 100.0 / GIB
    checks = {"overRouteBudget": bool(rp["dailyGb"] and est["gibPerDay"] is not None and est["gibPerDay"] > rp["dailyGb"]),
              "overTotalBudget": bool(pol["dailyBudgetGb"] and est["gibPerDay"] is not None and
                                      est["gibPerDay"] + others > pol["dailyBudgetGb"]),
              "otherRoutesGibPerDay": round(others, 6)}
    return {"policy": rp, "logs": per_log, "estimate": est, "checks": checks, "prices": prices,
            "filter": build_filter(logs, body.get("filter") or "", rp["minSeverity"], rp["samplePercent"])}


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40] or "route"


def save_route(ctx):
    """POST /api/v1/telemetry/routing/routes: creates or updates a route (sink, dataset, writer grant, retention)."""
    require_identity_admin(ctx)
    body = ctx.body or {}
    pol = policy(ctx.settings)
    route_id = body.get("id")
    if route_id is not None and (not isinstance(route_id, str) or not ROUTE_ID_RE.match(route_id)):
        raise ApiError("bad_request", "Field 'id' is invalid.")
    state = load_state(ctx)
    existing = next((r for r in state["routes"] if r.get("id") == route_id), None) if route_id else None
    if route_id and existing is None:
        raise ApiError("not_found", "Route not found.")
    project = (body.get("project") or (existing or {}).get("project") or ctx.project or "").strip().lower()
    if not config.valid_project(project) or project not in (list(getattr(ctx, "all_projects", None) or []) + [ctx.project]):
        raise ApiError("bad_request", "Field 'project' must be a project of this workspace.")
    logs = _clean_logs(ctx, body.get("logs") if body.get("logs") is not None else (existing or {}).get("logs"), project)
    base_filter = str(body.get("filter") if body.get("filter") is not None else (existing or {}).get("filter") or "").strip()
    if base_filter and (len(base_filter) > 4000 or config.CONTROL_RE.search(base_filter.replace("\n", " "))):
        raise ApiError("bad_request", "Field 'filter' must be at most 4000 characters.")
    if not logs and not base_filter:
        raise ApiError("bad_request", "Select at least one log or enter a filter.")
    title = str(body.get("title") or (existing or {}).get("title") or "").strip()[:80]
    if not title or config.CONTROL_RE.search(title):
        raise ApiError("bad_request", "Field 'title' must be 1 to 80 characters.")
    if existing is None and len(state["routes"]) >= pol["maxSinks"]:
        raise ApiError("conflict", "The maximum number of routes (%d) is reached. Raise it in Settings or delete a "
                                   "route." % pol["maxSinks"])
    route = dict(existing or {})
    route.update({
        "id": route_id or "rt_%s" % secrets.token_hex(4), "project": project, "title": title, "logs": logs,
        "filter": base_filter, "group": str(body.get("group") or route.get("group") or "")[:60] or None,
        "groupKo": str(body.get("groupKo") or route.get("groupKo") or "")[:60] or None,
        "followGroup": bool(body.get("followGroup", route.get("followGroup", False))) and pol["followGroups"],
        "policy": _clean_policy(body.get("policy")) if body.get("policy") is not None else route.get("policy") or {},
    })
    if not route.get("sink"):
        sink = (body.get("sinkName") or "").strip() or ("%s%s-%s" % (pol["sinkPrefix"], _slug(title), route["id"][3:]))
        if not SINK_NAME_RE.match(sink):
            raise ApiError("bad_request", "The sink name may contain only letters, digits, '_' and '-'.")
        route["sink"] = sink[:100]
    route.setdefault("status", "active")
    ensure_dataset(ctx, pol)
    sink = apply_sink(ctx, route, pol)
    route["writerIdentity"] = sink.get("writerIdentity") or route.get("writerIdentity")
    granted = grant_writer(ctx, pol["dataset"], route.get("writerIdentity") or "")
    actor = ctx.identity.email
    now = now_iso()
    if existing is None:
        route.update({"createdAt": now, "createdBy": actor})
    route.update({"updatedAt": now, "updatedBy": actor})

    def mutate(doc):
        doc["routes"] = [r for r in doc["routes"] if r.get("id") != route["id"]] + [route]
        doc["events"].append(_event("updated" if existing else "created", route,
                                    "%d logs, sampling %s%%, retention %s days" % (
                                        len(logs), route_policy(route, pol)["samplePercent"],
                                        route_policy(route, pol)["retentionDays"])))
    _mutate(ctx, mutate)
    try:
        apply_retention(ctx, route, pol)
    except ApiError:
        pass
    connector = None
    if body.get("createConnector") and isinstance(body.get("quickAdd"), dict):
        from . import telemetry
        connector = telemetry.ensure_connector(ctx, body["quickAdd"], bigquery_sink="%s.%s" % (ctx.project, pol["dataset"]))
    if hasattr(ctx.app, "cache"):
        ctx.app.cache.clear()
    return {"route": route, "writerGranted": granted, "connector": connector, "routing": get_routing(ctx)}


def route_action(ctx):
    """POST /api/v1/telemetry/routing/routes/action {id, action: pause|resume|delete|apply}."""
    require_identity_admin(ctx)
    body = ctx.body or {}
    route_id, action = body.get("id"), body.get("action")
    if not isinstance(route_id, str) or not ROUTE_ID_RE.match(route_id) or action not in ACTIONS:
        raise ApiError("bad_request", "Specify a route 'id' and an 'action' (pause, resume, delete or apply).")
    pol = policy(ctx.settings)
    state = load_state(ctx)
    route = next((r for r in state["routes"] if r.get("id") == route_id), None)
    if route is None:
        raise ApiError("not_found", "Route not found.")
    if action == "delete":
        delete_sink(ctx, route)
    elif action in ("pause", "resume"):
        try:
            set_disabled(ctx, route, action == "pause")
        except GcpError as err:
            raise ApiError("upstream_error", "The Log Router sink could not be %s (%s)." % (
                "paused" if action == "pause" else "resumed", err.kind))
    else:
        ensure_dataset(ctx, pol)
        apply_sink(ctx, route, pol)
        apply_retention(ctx, route, pol)
    actor = ctx.identity.email

    def mutate(doc):
        if action == "delete":
            doc["routes"] = [r for r in doc["routes"] if r.get("id") != route_id]
        for r in doc["routes"]:
            if r.get("id") == route_id:
                if action == "pause":
                    r.update({"status": "paused", "pausedAt": now_iso(), "pausedReason": "admin"})
                elif action == "resume":
                    r.update({"status": "active", "pausedAt": None, "pausedReason": None})
                r["updatedAt"], r["updatedBy"] = now_iso(), actor
        doc["events"].append(_event({"pause": "paused", "resume": "resumed", "delete": "deleted",
                                     "apply": "applied"}[action], route, "by %s" % actor))
    _mutate(ctx, mutate)
    if hasattr(ctx.app, "cache"):
        ctx.app.cache.clear()
    return {"routing": get_routing(ctx)}


def legacy_create_sink(ctx):
    """POST /api/v1/telemetry/sinks (earlier one-click export): creates a managed route for the given logs."""
    body = ctx.body or {}
    log_names = [str(n).strip() for n in (body.get("logNames") or []) if isinstance(n, str) and n.strip()]
    project = (body.get("project") or "").strip().lower()
    if log_names and not project:
        project = log_names[0].split("/")[1]
    quick = body.get("quickAdd") if isinstance(body.get("quickAdd"), dict) else None
    title = (quick or {}).get("name") or body.get("sourceId") or "Telemetry"
    flt = "" if log_names else (body.get("filter") or "").strip()
    ctx.body = {"title": title, "project": project or ctx.project, "logs": log_names, "filter": flt,
                "sinkName": body.get("sinkName") or "", "createConnector": body.get("createConnector", True),
                "quickAdd": quick}
    return save_route(ctx)


# ---------------------------------------------------------------------------
# Enforcement: budgets, auto-resume, retention and group following
# ---------------------------------------------------------------------------

def run_enforcement(ctx, trigger="manual", follow=False):
    """Checks today's exported volume against the budgets, pauses or resumes routes, re-applies retention and,
    when `follow` is set, adds new logs of followed groups. Returns a summary."""
    pol = policy(ctx.settings)
    state = load_state(ctx)
    routes = state.get("routes") or []
    summary = {"at": now_iso(), "trigger": trigger, "routes": len(routes), "paused": 0, "resumed": 0, "followed": 0,
               "retention": 0, "errors": 0, "gbToday": 0.0}
    if not routes:
        return summary
    _, today = _local_midnight(ctx.tz_name)
    bytes_today = {}
    for proj in sorted({r.get("project") for r in routes}):
        try:
            bytes_today.update(exported_bytes_today(ctx, proj, ctx.tz_name))
        except GcpError as err:
            summary["errors"] += 1
            LOG.warning("exported volume unreadable project=%s kind=%s", proj, err.kind)
    events, changes = [], {}
    total = sum(bytes_today.get(r["sink"], 0) for r in routes)
    summary["gbToday"] = _gb(total)
    total_over = bool(pol["dailyBudgetGb"] and total > pol["dailyBudgetGb"] * GIB)
    for route in routes:
        rp = route_policy(route, pol)
        used = bytes_today.get(route["sink"], 0)
        status = route.get("status") or "active"
        paused_day = (parse_rfc3339(route.get("pausedAt")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)) \
            .astimezone(timeutil.get_zone(ctx.tz_name)).date()
        if status == "paused_budget" and pol["autoResume"] and paused_day < today:
            try:
                set_disabled(ctx, route, False)
                changes[route["id"]] = {"status": "active", "pausedAt": None, "pausedReason": None}
                events.append(_event("resumed", route, "budget period reset"))
                summary["resumed"] += 1
                status = "active"
            except GcpError as err:
                summary["errors"] += 1
                events.append(_event("error", route, "resume failed (%s)" % err.kind))
        over_route = bool(rp["dailyGb"] and used > rp["dailyGb"] * GIB)
        if status == "active" and (over_route or total_over):
            reason = "route budget %.3f GiB" % rp["dailyGb"] if over_route else "total budget %.3f GiB" % pol["dailyBudgetGb"]
            if pol["overBudgetAction"] == "pause":
                try:
                    set_disabled(ctx, route, True)
                    changes[route["id"]] = {"status": "paused_budget", "pausedAt": now_iso(), "pausedReason": reason}
                    events.append(_event("paused", route, "over %s" % reason, bytes=used))
                    summary["paused"] += 1
                except GcpError as err:
                    summary["errors"] += 1
                    events.append(_event("error", route, "pause failed (%s)" % err.kind))
            elif route.get("lastAlertDay") != today.isoformat():
                changes.setdefault(route["id"], {})["lastAlertDay"] = today.isoformat()
                events.append(_event("over_budget", route, "over %s" % reason, bytes=used))
        changes.setdefault(route["id"], {})["lastCheck"] = {"at": now_iso(), "bytesToday": used}
    if follow or trigger in ("daily", "manual"):
        if state.get("lastRetentionDay") != today.isoformat():
            for route in routes:
                try:
                    summary["retention"] += apply_retention(ctx, route, pol)
                except ApiError:
                    summary["errors"] += 1
    if follow and pol["followGroups"]:
        added = _follow_groups(ctx, routes, pol, events, changes)
        summary["followed"] = added

    def mutate(doc):
        for r in doc["routes"]:
            if r.get("id") in changes:
                r.update(changes[r["id"]])
        doc["events"].extend(events)
        doc["lastEnforcement"] = summary
        if follow or trigger in ("daily", "manual"):
            doc["lastRetentionDay"] = today.isoformat()
    _mutate(ctx, mutate)
    for ev in events:
        if ev["kind"] in ("paused", "resumed", "over_budget"):
            notify(pol, "[AI Control Plane] Telemetry route %s (%s): %s %s" % (ev.get("title"), ev.get("sink"),
                                                                             ev["kind"], ev.get("detail")))
    return summary


def _follow_groups(ctx, routes, pol, events, changes):
    """Adds logs that joined a followed Gemini group since the route was created, within the budgets."""
    from . import telemetry
    followed = [r for r in routes if r.get("followGroup") and r.get("group")]
    if not followed:
        return 0
    try:
        found = telemetry.discover(ctx)
    except Exception as err:  # pylint: disable=broad-except
        LOG.warning("group following skipped: %s", type(err).__name__)
        return 0
    routed = routed_index({"routes": routes})
    by_group = {}
    for lg in found.get("logs") or []:
        card = lg.get("catalog") or {}
        if lg.get("core") or not lg.get("logName") or lg["logName"] in routed or card.get("hidden"):
            continue
        by_group.setdefault((card.get("group") or "").lower(), []).append(lg)
    added_total = 0
    try:
        volumes = log_volumes(ctx)
    except GcpError:
        volumes = {}
    budget = pol["dailyBudgetGb"] * GIB if pol["dailyBudgetGb"] else None
    current = sum(sum(volumes.get(urllib.parse.unquote(ln.split("/logs/", 1)[-1]), 0) for ln in r.get("logs") or [])
                  * route_policy(r, pol)["samplePercent"] / 100.0 for r in routes)
    for route in followed:
        rp = route_policy(route, pol)
        new_logs = []
        for lg in by_group.get(route["group"].lower(), []):
            if lg["logName"].split("/logs/")[0] != "projects/%s" % route["project"]:
                continue
            extra = volumes.get(lg.get("shortName") or "", 0) * rp["samplePercent"] / 100.0
            if budget is not None and current + extra > budget:
                events.append(_event("skipped", route, "%s would exceed the total budget" % lg.get("shortName")))
                continue
            if len(route.get("logs") or []) + len(new_logs) >= MAX_LOGS_PER_ROUTE:
                break
            new_logs.append(lg["logName"])
            current += extra
        if not new_logs:
            continue
        updated = dict(route, logs=list(route.get("logs") or []) + new_logs)
        try:
            apply_sink(ctx, updated, pol)
            changes.setdefault(route["id"], {})["logs"] = updated["logs"]
            events.append(_event("followed", route, "added %d log(s): %s" % (len(new_logs), ", ".join(
                urllib.parse.unquote(n.split("/logs/", 1)[-1]) for n in new_logs)[:200])))
            added_total += len(new_logs)
        except ApiError as err:
            events.append(_event("error", route, "group update failed (%s)" % err.message))
    return added_total


def enforce(ctx):
    """POST /api/v1/telemetry/routing/enforce (administrators and the maintenance scheduler)."""
    if getattr(ctx.identity, "auth_mode", "") != "scheduler":
        require_identity_admin(ctx)
    follow = bool((ctx.body or {}).get("follow"))
    summary = run_enforcement(ctx, trigger="scheduled" if ctx.identity.auth_mode == "scheduler" else "manual",
                              follow=follow)
    if hasattr(ctx.app, "cache"):
        ctx.app.cache.clear()
    return {"summary": summary, "routing": get_routing(ctx)}


def alert_summary(app):
    """Unseen routing events for the bootstrap banner: paused routes and over-budget reports."""
    try:
        doc, _ = aux_docs(app).load(STATE_DOC)
    except Exception:  # pylint: disable=broad-except
        return {"paused": 0, "routes": 0}
    routes = doc.get("routes") or []
    return {"routes": len(routes), "paused": sum(1 for r in routes if r.get("status") == "paused_budget"),
            "lastEnforcement": (doc.get("lastEnforcement") or {}).get("at")}
