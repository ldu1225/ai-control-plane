"""Cloud Monitoring time series helpers (per-model token counts and invocations)."""

from __future__ import annotations

import datetime as dt
import logging

from ..gcp import GcpError
from .base import iso, parse_rfc3339, to_int

LOG = logging.getLogger("controlplane.monitoring")

TOKEN_METRIC = "aiplatform.googleapis.com/publisher/online_serving/token_count"
INVOCATION_METRIC = "aiplatform.googleapis.com/publisher/online_serving/model_invocation_count"
MONITORING_URL = "https://monitoring.googleapis.com/v3/projects/%s/timeSeries"


def _point_value(point):
    value = point.get("value") or {}
    if "int64Value" in value:
        return to_int(value["int64Value"])
    if "doubleValue" in value:
        try:
            return float(value["doubleValue"])
        except (TypeError, ValueError):
            return 0
    return 0


def _project_time_series(ctx, project, metric, group_by, start, end, max_pages=20):
    url = MONITORING_URL % project
    params = [
        ("filter", 'metric.type="%s"' % metric),
        ("interval.startTime", iso(start)),
        ("interval.endTime", iso(end)),
        ("aggregation.alignmentPeriod", "86400s"),
        ("aggregation.perSeriesAligner", "ALIGN_SUM"),
        ("aggregation.crossSeriesReducer", "REDUCE_SUM"),
        ("pageSize", "1000"),
    ]
    params += [("aggregation.groupByFields", g) for g in group_by]
    out = []
    token = None
    from .. import timeutil
    tz = timeutil.get_zone(ctx.tz_name)
    for _ in range(max_pages):
        page_params = list(params) + ([("pageToken", token)] if token else [])
        payload, _ = ctx.gcp.request("GET", url, quota_project=ctx.quota_project, params=page_params, timeout=45)
        for series in payload.get("timeSeries") or []:
            labels = {"resource.label.project_id": project}
            labels.update({"resource.label." + k: v for k, v in ((series.get("resource") or {}).get("labels") or {}).items()})
            labels.update({"metric.label." + k: v for k, v in ((series.get("metric") or {}).get("labels") or {}).items()})
            points = []
            for point in series.get("points") or []:
                end_time = parse_rfc3339((point.get("interval") or {}).get("endTime"))
                if end_time is None:
                    continue
                day = (end_time - dt.timedelta(seconds=1)).astimezone(tz).date()
                points.append((day, _point_value(point)))
            out.append((labels, points))
        token = payload.get("nextPageToken")
        if not token:
            break
    return out


def time_series(ctx, metric, group_by, start, end, max_pages=20):
    """Daily ALIGN_SUM/REDUCE_SUM series across scanned projects. Returns list of (labels dict, [(day, value)])."""
    projects = list(getattr(ctx, "projects", None) or [ctx.project])

    def load():
        if len(projects) <= 1:
            return _project_time_series(ctx, projects[0] if projects else ctx.project, metric, group_by, start, end, max_pages=max_pages)
        tasks = {proj: (lambda p=proj: _project_time_series(ctx, p, metric, group_by, start, end, max_pages=max_pages))
                 for proj in projects}
        res = ctx.run(tasks)
        out = []
        for proj in projects:
            ok, value = res[proj]
            if not ok:
                if proj == ctx.project:
                    raise value
                continue
            out.extend(value)
        return out

    cache = getattr(getattr(ctx, "app", None), "cache", None)
    if cache is not None:
        key = ("mon_ts", ctx.version, tuple(projects), metric, tuple(group_by), iso(start), iso(end), max_pages)
        ttl = int(ctx.settings["server"].get("cache_ttl_seconds") or 120)
        val, _ = cache.get_or_compute(key, ttl, load)
        return val
    return load()


def token_counts(ctx, start, end):
    """Returns ({model_user_id: {"input": {day: v}, "output": {...}, "cached": {...}, "total": {...}}}, split)."""
    split = True
    try:
        series = time_series(ctx, TOKEN_METRIC, ["resource.label.model_user_id", "metric.label.type"], start, end)
    except GcpError as err:
        if err.kind != "invalid":
            raise
        split = False
        series = time_series(ctx, TOKEN_METRIC, ["resource.label.model_user_id"], start, end)
    out = {}
    for labels, points in series:
        model = labels.get("resource.label.model_user_id")
        if not model:
            continue
        kind = (labels.get("metric.label.type") or "").lower()
        bucket = "cached" if "cach" in kind else ("input" if "input" in kind else ("output" if "output" in kind
                                                                                   else "other"))
        entry = out.setdefault(model, {"input": {}, "output": {}, "cached": {}, "other": {}, "total": {}})
        for day, value in points:
            entry[bucket][day] = entry[bucket].get(day, 0) + value
            entry["total"][day] = entry["total"].get(day, 0) + value
    return out, split


def invocation_counts(ctx, start, end):
    """Returns {model_user_id: {"invocations": n, "errors": n, "daily": {day: n}}}."""
    series = time_series(ctx, INVOCATION_METRIC, ["resource.label.model_user_id", "metric.label.response_code"],
                         start, end)
    out = {}
    for labels, points in series:
        model = labels.get("resource.label.model_user_id")
        if not model:
            continue
        code = str(labels.get("metric.label.response_code") or "")
        entry = out.setdefault(model, {"invocations": 0, "errors": 0, "daily": {}})
        for day, value in points:
            entry["invocations"] += value
            entry["daily"][day] = entry["daily"].get(day, 0) + value
            if not code.startswith("2"):
                entry["errors"] += value
    return out


# ---------------------------------------------------------------------------
# Per-series reads and distributions (per-agent views)
# ---------------------------------------------------------------------------

DESCRIPTORS_URL = "https://monitoring.googleapis.com/v3/projects/%s/metricDescriptors"


def metric_types(ctx, project, prefix, max_pages=5):
    """{metric type: {"kind", "valueType"}} for the metric descriptors of `project` whose type starts with `prefix`."""
    params = [("filter", 'metric.type = starts_with("%s")' % prefix), ("pageSize", "1000")]
    out = {}
    token = None
    for _ in range(max_pages):
        page_params = list(params) + ([("pageToken", token)] if token else [])
        payload, _ = ctx.gcp.request("GET", DESCRIPTORS_URL % project, quota_project=ctx.quota_project,
                                     params=page_params, timeout=30)
        for desc in payload.get("metricDescriptors") or []:
            if isinstance(desc, dict) and desc.get("type"):
                out[desc["type"]] = {"kind": desc.get("metricKind"), "valueType": desc.get("valueType")}
        token = payload.get("nextPageToken")
        if not token:
            break
    return out


def _aligned_value(point):
    value = point.get("value") or {}
    if isinstance(value.get("distributionValue"), dict):
        return value["distributionValue"]
    return _point_value(point)


def aligned_series(ctx, project, flt, start, end, aligner, max_pages=10):
    """Daily aligned series of one project with no cross-series reduction, so that callers can group by any label.

    `flt` is a Monitoring filter built from validated identifiers only. Returns
    [{"metric": {label: value}, "resource": {label: value}, "points": [(local day, number | distribution dict)]}];
    distribution metrics yield the raw distributionValue of each day (see Distribution)."""
    from .. import timeutil
    tz = timeutil.get_zone(ctx.tz_name)
    params = [("filter", flt), ("interval.startTime", iso(start)), ("interval.endTime", iso(end)),
              ("aggregation.alignmentPeriod", "86400s"), ("aggregation.perSeriesAligner", aligner),
              ("pageSize", "1000")]
    out = []
    token = None
    for _ in range(max_pages):
        page_params = list(params) + ([("pageToken", token)] if token else [])
        payload, _ = ctx.gcp.request("GET", MONITORING_URL % project, quota_project=ctx.quota_project,
                                     params=page_params, timeout=45)
        for series in payload.get("timeSeries") or []:
            points = []
            for point in series.get("points") or []:
                end_time = parse_rfc3339((point.get("interval") or {}).get("endTime"))
                if end_time is None:
                    continue
                points.append(((end_time - dt.timedelta(seconds=1)).astimezone(tz).date(), _aligned_value(point)))
            out.append({"metric": dict((series.get("metric") or {}).get("labels") or {}),
                        "resource": dict((series.get("resource") or {}).get("labels") or {}), "points": points})
        token = payload.get("nextPageToken")
        if not token:
            break
    return out


def _bucket_bounds(options, index):
    """(lower, upper) bounds of histogram bucket `index`; None marks an open end (underflow or overflow bucket)."""
    if isinstance(options.get("explicitBuckets"), dict):
        bounds = [float(b) for b in options["explicitBuckets"].get("bounds") or []]
        lower = bounds[index - 1] if 0 < index <= len(bounds) else None
        upper = bounds[index] if index < len(bounds) else None
        return lower, upper
    if isinstance(options.get("exponentialBuckets"), dict):
        opt = options["exponentialBuckets"]
        finite = to_int(opt.get("numFiniteBuckets"))
        growth = float(opt.get("growthFactor") or 0)
        scale = float(opt.get("scale") or 0)
        if index == 0:
            return None, scale
        return scale * growth ** (index - 1), (scale * growth ** index if index <= finite else None)
    if isinstance(options.get("linearBuckets"), dict):
        opt = options["linearBuckets"]
        finite = to_int(opt.get("numFiniteBuckets"))
        width = float(opt.get("width") or 0)
        offset = float(opt.get("offset") or 0)
        if index == 0:
            return None, offset
        return offset + width * (index - 1), (offset + width * index if index <= finite else None)
    return None, None


class Distribution:
    """Sum of Cloud Monitoring distribution values: sample count, sum and merged histogram buckets.

    Percentiles are estimates: linear interpolation inside the bucket that holds the requested rank. All metrics
    read here are non-negative (durations, token counts, scores), so the underflow bucket starts at zero."""

    __slots__ = ("count", "total", "buckets")

    def __init__(self):
        self.count = 0
        self.total = 0.0
        self.buckets = {}

    def add(self, value):
        """Adds one distributionValue (dict). Other values are ignored."""
        if not isinstance(value, dict):
            return self
        count = to_int(value.get("count"))
        if count <= 0:
            return self
        try:
            mean = float(value.get("mean") or 0)
        except (TypeError, ValueError):
            mean = 0.0
        self.count += count
        self.total += mean * count
        options = value.get("bucketOptions") or {}
        for index, raw in enumerate(value.get("bucketCounts") or []):
            n = to_int(raw)
            if n > 0:
                key = _bucket_bounds(options, index)
                self.buckets[key] = self.buckets.get(key, 0) + n
        return self

    def merge(self, other):
        self.count += other.count
        self.total += other.total
        for key, n in other.buckets.items():
            self.buckets[key] = self.buckets.get(key, 0) + n
        return self

    def mean(self):
        return self.total / self.count if self.count else None

    def percentile(self, q):
        """Estimated q-quantile (0 < q <= 1), or None without samples."""
        if self.count <= 0:
            return None
        items = sorted(self.buckets.items(), key=lambda kv: float("-inf") if kv[0][0] is None else kv[0][0])
        total = sum(n for _, n in items)
        if total <= 0:
            return self.mean()
        target = q * total
        seen = 0
        for (lower, upper), n in items:
            if seen + n >= target:
                if lower is None and upper is None:
                    return self.mean()
                if upper is None:
                    return lower
                if lower is None:
                    lower = 0.0 if upper > 0 else upper
                return lower + (upper - lower) * max(0.0, min(1.0, (target - seen) / n))
            seen += n
        return self.mean()
