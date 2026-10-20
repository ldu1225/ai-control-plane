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


def time_series(ctx, metric, group_by, start, end, max_pages=20):
    """Daily ALIGN_SUM/REDUCE_SUM series. Returns list of (labels dict, [(day, value)])."""
    url = MONITORING_URL % ctx.project
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
    tz = None
    from .. import timeutil
    tz = timeutil.get_zone(ctx.tz_name)
    for _ in range(max_pages):
        page_params = list(params) + ([("pageToken", token)] if token else [])
        payload, _ = ctx.gcp.request("GET", url, quota_project=ctx.quota_project, params=page_params, timeout=45)
        for series in payload.get("timeSeries") or []:
            labels = {}
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
