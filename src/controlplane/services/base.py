"""Request context, warnings and SQL helpers shared by all services."""

from __future__ import annotations

import datetime as dt
import decimal
import logging
import threading

from .. import config
from ..gcp import GcpError
from ..httpkit import ApiError

LOG = logging.getLogger("controlplane.services")

AUDIT_ACTIVITY = "cloudaudit_googleapis_com_activity"
AUDIT_DATA_ACCESS = "cloudaudit_googleapis_com_data_access"
GE_MESSAGE = "discoveryengine_googleapis_com_gen_ai_user_message"
GE_ACTIVITY = "discoveryengine_googleapis_com_gemini_enterprise_user_activity"
ARMOR_OPS = "modelarmor_googleapis_com_sanitize_operations"
AGY_SINK = "businessaicode_googleapis_com_inference_response"
CORE_TABLES = (AUDIT_ACTIVITY, AUDIT_DATA_ACCESS, GE_MESSAGE, GE_ACTIVITY, ARMOR_OPS, AGY_SINK)

TRANSIENT_CODES = {"timeout", "query_failed", "upstream_unavailable", "credentials_unavailable", "api_error"}


def num(value, default=0):
    if value is None:
        return default
    if isinstance(value, decimal.Decimal):
        return float(value)
    return value


def to_int(value):
    value = num(value, 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def to_float(value):
    value = num(value, None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def money(value):
    value = to_float(value)
    return None if value is None else round(value, 2)


def ratio(num_value, den_value, digits=4):
    if num_value is None or den_value in (None, 0):
        return None
    return round(float(num_value) / float(den_value), digits)


def iso(value):
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, str):
        return value
    return str(value)


def parse_rfc3339(value):
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    if "." in text:
        head, _, tail = text.partition(".")
        frac = ""
        rest = ""
        for i, ch in enumerate(tail):
            if not ch.isdigit():
                rest = tail[i:]
                break
            frac += ch
        text = head + ("." + frac[:6] if frac else "") + rest
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# SQL fragments (only fixed SQL text; values always go through parameters)
# ---------------------------------------------------------------------------

def human_sql(col):
    return ("(%s LIKE '%%@%%' AND NOT ENDS_WITH(%s, '.gserviceaccount.com') AND NOT STARTS_WITH(%s, 'principal://'))"
            % (col, col, col))


def bucket_sql(ts):
    return ("DATE_ADD(@start_date, INTERVAL DIV(DATE_DIFF(DATE(%s, @tz), @start_date, DAY), @bucket_days) "
            "* @bucket_days DAY)" % ts)


def period_sql(ts):
    return "IF(%s >= @start_ts, 'cur', 'prev')" % ts


def bucket_or_sentinel(ts):
    return "IF(%s >= @start_ts, %s, DATE '1970-01-01')" % (ts, bucket_sql(ts))


def range_params(rng, with_prev=True):
    prev = rng.previous()
    first = prev.start if with_prev else rng.start
    lo = (first - dt.timedelta(days=1)).strftime("%Y%m%d")
    hi = (rng.end + dt.timedelta(days=1)).strftime("%Y%m%d")
    return {"start_ts": rng.start_ts(), "end_ts": rng.end_ts(),
            "prev_start_ts": prev.start_ts() if with_prev else rng.start_ts(),
            "tz": rng.tz_name, "start_date": rng.start, "bucket_days": rng.bucket_days,
            "sfx_start": lo, "sfx_end": hi}


def split_rollup(rows):
    """Splits ROLLUP(period, bucket) rows into ({period: totals_row}, {bucket_date: row})."""
    totals = {}
    buckets = {}
    for row in rows:
        period = row.get("period")
        bucket = row.get("bucket")
        if period is None:
            continue
        if bucket is None:
            totals[period] = row
        elif period == "cur":
            if isinstance(bucket, dt.datetime):
                bucket = bucket.date()
            buckets[bucket] = row
    return totals, buckets


def bucket_values(rng, buckets, key, cast=int):
    out = []
    for day in rng.buckets():
        row = buckets.get(day)
        value = num(row.get(key), 0) if row else 0
        out.append(cast(value) if cast is int else round(float(value), 6))
    return out


# ---------------------------------------------------------------------------
# Request context
# ---------------------------------------------------------------------------

class Ctx:
    def __init__(self, app, settings, version, identity, params=None, body=None, rng=None):
        self.app = app
        self.settings = settings
        self.version = version
        self.identity = identity
        self.params = params or {}
        self.body = body or {}
        self.range = rng
        self.warnings = []
        self.sources = set()
        self._lock = threading.Lock()
        self._dataset_failed = {}

    # -- shortcuts ----------------------------------------------------------------
    @property
    def gcp(self):
        return self.app.gcp

    @property
    def registry(self):
        return self.app.registry

    @property
    def ws(self):
        return self.settings["workspace"]

    @property
    def project(self):
        return self.ws["project_id"]

    @property
    def quota_project(self):
        return config.quota_project(self.settings)

    @property
    def audit_dataset(self):
        return self.ws["audit_dataset"]

    @property
    def max_bytes(self):
        return int(self.settings["bigquery"]["max_bytes_billed"])

    @property
    def tz_name(self):
        return self.ws.get("timezone") or "UTC"

    @property
    def excluded_patterns(self):
        return list(self.settings["model_catalog"]["excluded_sku_patterns"])

    def child(self, rng=None):
        return Ctx(self.app, self.settings, self.version, self.identity, self.params, self.body,
                   rng if rng is not None else self.range)

    def absorb(self, other):
        for w in other.warnings:
            self.warn(w["source"], w["code"], w["message"])
        with self._lock:
            self.sources |= other.sources

    # -- warnings -----------------------------------------------------------------
    def warn(self, source, code, message):
        with self._lock:
            for w in self.warnings:
                if w["source"] == source and w["code"] == code:
                    return
            self.warnings.append({"source": source, "code": code, "message": message})

    def warn_error(self, source, err, what):
        if isinstance(err, GcpError):
            code, message = describe_error(err, what)
        elif isinstance(err, ApiError):
            raise err
        else:
            LOG.exception("unexpected service error source=%s", source)
            code, message = "internal_error", "%s could not be processed." % what
        self.warn(source, code, message)

    def add_source(self, label):
        with self._lock:
            self.sources.add(label)

    def has_transient_warning(self):
        return any(w["code"] in TRANSIENT_CODES for w in self.warnings)

    # -- data access --------------------------------------------------------------
    def query(self, sql, params, module, max_bytes=None, max_rows=None, timeout=60):
        return self.gcp.query(sql, params, quota_project=self.quota_project, module=module,
                              max_bytes=max_bytes or self.max_bytes, max_rows=max_rows, timeout=timeout).rows

    def rest(self, method, url, **kwargs):
        return self.gcp.request(method, url, quota_project=self.quota_project, **kwargs)[0]

    def dataset_source(self, project, dataset, logical, required_time=True):
        label = "bigquery:%s.%s" % (dataset, logical)
        key = (project, dataset)
        if key in self._dataset_failed:
            return None
        try:
            src = self.registry.source(project, dataset, logical, self.quota_project, self.max_bytes)
        except GcpError as err:
            if err.kind == "not_found":
                self.warn("bigquery:%s" % dataset, "dataset_not_found",
                          "Dataset %s was not found in project %s." % (dataset, project))
            else:
                self.warn_error("bigquery:%s" % dataset, err, "BigQuery dataset %s" % dataset)
            self._dataset_failed[key] = True
            return None
        except ValueError:
            self.warn("bigquery:%s" % dataset, "not_configured", "Dataset %s is not a valid identifier." % dataset)
            self._dataset_failed[key] = True
            return None
        if src is None:
            self.warn(label, "table_not_found", "Table not found in dataset %s" % dataset)
            return None
        if required_time and src.time_column() is None:
            self.warn(label, "unsupported_schema", "Table has no TIMESTAMP column to filter on.")
            return None
        return src

    def audit_source(self, logical, optional=False):
        if optional:
            # Optional sinks (for example the Antigravity inference sink) do not warn when absent.
            try:
                groups = self.registry.tables(self.project, self.audit_dataset, self.quota_project, self.max_bytes)
            except (GcpError, ValueError):
                return None
            if logical not in groups:
                return None
        return self.dataset_source(self.project, self.audit_dataset, logical)

    def billing_source(self):
        b = self.ws["billing"]
        label = "bigquery:%s.%s" % (b.get("dataset") or "billing", b.get("table") or "export")
        if not config.billing_configured(self.settings):
            self.warn(label, "not_configured", "The billing export table is not configured.")
            return None
        project = config.billing_project(self.settings)
        try:
            src = self.registry.table_source(project, b["dataset"], b["table"], self.quota_project)
        except GcpError as err:
            if err.kind == "not_found":
                self.warn(label, "table_not_found", "Billing export table not found in dataset %s" % b["dataset"])
            else:
                self.warn_error(label, err, "The billing export table")
            return None
        except ValueError:
            self.warn(label, "not_configured", "The billing export table identifier is invalid.")
            return None
        if src.schema.node("usage_start_time") is None or src.schema.node("cost") is None:
            self.warn(label, "unsupported_schema", "The billing table does not look like a billing export.")
            return None
        return src

    def run(self, tasks):
        return self.gcp.run_parallel(tasks)

    def run_composite(self, tasks):
        """Runs composite tasks (that themselves run queries) on the orchestrator pool."""
        return self.gcp.run_parallel(tasks, pool=self.gcp.orchestrator)

    def memo(self, name, key, fn, ttl=None):
        """Caches fn(child_ctx) per settings version and replays its warnings and sources."""
        def compute():
            child = self.child()
            value = fn(child)
            return value, list(child.warnings), set(child.sources)

        ttl = ttl if ttl is not None else int(self.settings["server"].get("cache_ttl_seconds") or 0)
        cache_key = ("memo", name, self.version, key)
        (value, warnings, sources), _ = self.app.cache.get_or_compute(
            cache_key, ttl, compute, cacheable=lambda v: not any(w["code"] in TRANSIENT_CODES for w in v[1]))
        for w in warnings:
            self.warn(w["source"], w["code"], w["message"])
        with self._lock:
            self.sources |= sources
        return value


def describe_error(err, what):
    kind = getattr(err, "kind", "http")
    inner = what[0].lower() + what[1:] if what.startswith("The ") else what
    if kind == "not_found":
        return "not_found", "%s was not found." % what
    if kind == "denied":
        return "access_denied", "Access to %s was denied for the service identity." % inner
    if kind == "credentials":
        return "credentials_unavailable", "Google Cloud credentials are unavailable."
    if kind == "timeout":
        return "timeout", "%s timed out." % what
    if kind == "cost_limit":
        return "cost_limit", "%s exceeded the configured bytes-billed limit." % what
    if kind == "invalid":
        return "query_failed", "%s could not be queried." % what
    if kind in ("conflict", "precondition"):
        return "api_error", "%s rejected the request." % what
    return "upstream_unavailable", "%s is temporarily unavailable." % what


def require_identity_admin(ctx):
    if not ctx.identity.can_write:
        raise ApiError("forbidden", "Administrator privileges are required.")
