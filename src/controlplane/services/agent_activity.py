"""Coding agents in the cloud (Antigravity, Jetski and similar agents): Google Cloud API calls that
coding agents made with users' credentials, from Cloud Audit Logs.

Agents identify themselves in the caller-supplied user agent ("... agent-name/<agent>"), for example
the Google Cloud CLI run by an Antigravity or Jetski agent. Data access logs show reads (queries, gets,
lists); admin activity logs show changes. Model token usage of these agents is not part of the
project's logs; it is reported by the Antigravity inference log when that log exists.
"""

from __future__ import annotations

from .. import timeutil
from ..gcp import GcpError
from ..security import uid_for
from .base import (AUDIT_ACTIVITY, AUDIT_DATA_ACCESS, bucket_or_sentinel, bucket_values, iso, period_sql,
                   range_params, split_rollup, to_int)
from .notebooklm import audit_fields

AGENT_RE = r"agent-name/([A-Za-z0-9_.\-]+)"
NULL_STR = "CAST(NULL AS STRING)"


def activity_ctes(sources, start_param):
    """CTEs producing `aa` rows (one per audited call made by a coding agent). Returns (ctes, labels)."""
    ctes, parts, labels = [], [], []
    for name, kind, src in sources:
        if src is None:
            continue
        f = audit_fields(src)
        if not (f["service"] and f["method"] and f["agent"]):
            continue
        ctes.append(src.cte("%s_src" % name, start=start_param))
        parts.append(
            "SELECT %s AS ts, %s AS service, %s AS method, LOWER(%s) AS principal, %s AS code, "
            "LOWER(REGEXP_EXTRACT(%s, r'%s')) AS agent, '%s' AS kind, SUBSTR(%s, 1, 300) AS resource "
            "FROM %s_src t WHERE REGEXP_CONTAINS(IFNULL(%s, ''), r'agent-name/')"
            % (f["ts"], f["service"], f["method"], f["principal"] or NULL_STR, f["code"] or "CAST(NULL AS INT64)",
               f["agent"], AGENT_RE, kind, f["resource"] or NULL_STR, name, f["agent"]))
        labels.append(src.label)
    if not parts:
        return None, labels
    ctes.append("aa AS (%s)" % " UNION ALL ".join(parts))
    return ctes, labels


def summary(ctx):
    """GET /api/v1/governance/antigravity/agent-activity."""
    rng = ctx.range
    out = {"kpis": {"calls": {"value": None, "previous": None}, "users": None, "changes": None, "errors": None},
           "trend": None, "byAgent": None, "byApi": None, "byUser": None, "recent": None}
    sources = [("da", "read", ctx.audit_source(AUDIT_DATA_ACCESS)),
               ("ac", "change", ctx.audit_source(AUDIT_ACTIVITY))]
    prev_ctes, labels = activity_ctes(sources, "prev_start_ts")
    cur_ctes, _ = activity_ctes(sources, "start_ts")
    if not prev_ctes:
        return out
    label = labels[0]
    cur = ",\n".join(cur_ctes)
    kpi_sql = ("WITH %s\nSELECT period, bucket, COUNT(*) AS n, COUNT(DISTINCT principal) AS users, "
               "COUNTIF(kind = 'change') AS changes, COUNTIF(IFNULL(code, 0) != 0) AS errors\n"
               "FROM (SELECT aa.*, %s AS period, %s AS bucket FROM aa)\nGROUP BY ROLLUP(period, bucket)"
               % (",\n".join(prev_ctes), period_sql("ts"), bucket_or_sentinel("ts")))
    agents_sql = "WITH %s\nSELECT agent, COUNT(*) AS n FROM aa GROUP BY agent ORDER BY n DESC LIMIT 10" % cur
    api_sql = ("WITH %s\nSELECT service, method, kind, COUNT(*) AS n, COUNTIF(IFNULL(code, 0) != 0) AS errors "
               "FROM aa GROUP BY service, method, kind ORDER BY n DESC LIMIT 25" % cur)
    users_sql = ("WITH %s\nSELECT principal, COUNT(*) AS n, COUNTIF(kind = 'change') AS changes, MAX(ts) AS last_seen, "
                 "ARRAY_AGG(DISTINCT agent IGNORE NULLS LIMIT 5) AS agents FROM aa WHERE principal IS NOT NULL "
                 "GROUP BY principal ORDER BY n DESC, principal LIMIT 20" % cur)
    recent_sql = ("WITH %s\nSELECT ts, principal, agent, service, method, kind, code, resource FROM aa "
                  "ORDER BY ts DESC LIMIT 50" % cur)
    params_prev = range_params(rng)
    params_cur = range_params(rng, with_prev=False)
    res = ctx.run({
        "kpi": lambda: ctx.query(kpi_sql, params_prev, "antigravity"),
        "agents": lambda: ctx.query(agents_sql, params_cur, "antigravity"),
        "api": lambda: ctx.query(api_sql, params_cur, "antigravity"),
        "users": lambda: ctx.query(users_sql, params_cur, "antigravity"),
        "recent": lambda: ctx.query(recent_sql, params_cur, "antigravity"),
    })
    ok, rows = res["kpi"]
    if not ok:
        ctx.warn_error(label, rows, "Cloud Audit Logs")
        return out
    for lab in labels:
        ctx.add_source(lab)
    totals, buckets = split_rollup(rows)
    now, prev = totals.get("cur", {}), totals.get("prev", {})
    out["kpis"] = {"calls": {"value": to_int(now.get("n")) or 0, "previous": to_int(prev.get("n")) or 0},
                   "users": to_int(now.get("users")) or 0, "changes": to_int(now.get("changes")) or 0,
                   "errors": to_int(now.get("errors")) or 0}
    out["trend"] = timeutil.timeline(rng, [timeutil.series("calls", "API calls", bucket_values(rng, buckets, "n"))])
    ok, rows = res["agents"]
    if ok:
        out["byAgent"] = [{"agent": r.get("agent"), "calls": to_int(r.get("n"))} for r in rows]
    ok, rows = res["api"]
    if ok:
        out["byApi"] = [{"service": r.get("service"), "method": r.get("method"), "kind": r.get("kind"),
                         "calls": to_int(r.get("n")), "errors": to_int(r.get("errors"))} for r in rows]
    ok, rows = res["users"]
    if ok:
        out["byUser"] = [{"uid": uid_for(r.get("principal")), "email": r.get("principal"), "calls": to_int(r.get("n")),
                          "changes": to_int(r.get("changes")), "lastSeen": iso(r.get("last_seen")),
                          "agents": list(r.get("agents") or [])} for r in rows]
    ok, rows = res["recent"]
    if ok:
        out["recent"] = [{"time": iso(r.get("ts")), "email": r.get("principal"), "agent": r.get("agent"),
                          "service": r.get("service"), "method": r.get("method"), "kind": r.get("kind"),
                          "code": to_int(r.get("code")), "resource": r.get("resource")} for r in rows]
    for key in ("agents", "api", "users", "recent"):
        if not res[key][0] and isinstance(res[key][1], GcpError):
            ctx.warn_error(label, res[key][1], "Cloud Audit Logs")
            break
    return out
