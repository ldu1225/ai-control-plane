"""Agent Gateway: AI API traffic from Cloud Audit Logs."""

from __future__ import annotations

from .. import timeutil
from ..gcp import GcpError
from ..security import uid_for
from .base import (AUDIT_ACTIVITY, AUDIT_DATA_ACCESS, bucket_or_sentinel, human_sql, iso, period_sql,
                   range_params, ratio, to_int)
from .models import normalize_model_id
from .notebooklm import audit_fields
from . import monitoring

OUTCOMES = (("success", "Success"), ("client_error", "Client error"), ("denied", "Denied"),
            ("throttled", "Throttled"), ("server_error", "Server error"))
OUTCOME_SQL = ("CASE WHEN code IS NULL OR code = 0 THEN 'success' WHEN code IN (7, 16) THEN 'denied' "
               "WHEN code = 8 THEN 'throttled' WHEN code IN (3, 5, 6, 9, 10, 11) THEN 'client_error' "
               "WHEN code IN (2, 4, 13, 14, 15) THEN 'server_error' ELSE 'client_error' END")
NULL_STR = "CAST(NULL AS STRING)"


def gateway_ctes(sources, start_param):
    ctes = []
    parts = []
    labels = []
    for name, src in sources:
        if src is None:
            continue
        f = audit_fields(src)
        if not (f["service"] and f["method"]):
            continue
        ctes.append(src.cte("%s_src" % name, start=start_param))
        parts.append("SELECT %s AS ts, %s AS service, %s AS method, LOWER(%s) AS principal, %s AS code, "
                     "SUBSTR(%s, 1, 300) AS message, %s AS ip FROM %s_src t WHERE %s IN UNNEST(@services)"
                     % (f["ts"], f["service"], f["method"], f["principal"] or NULL_STR,
                        f["code"] or "CAST(NULL AS INT64)", f["message"] or NULL_STR, f["ip"] or NULL_STR, name,
                        f["service"]))
        labels.append(src.label)
    if not parts:
        return None, labels
    ctes.append("gw AS (%s)" % " UNION ALL ".join(parts))
    ctes.append("cl AS (SELECT *, %s AS outcome FROM gw)" % OUTCOME_SQL)
    return ctes, labels


def summary(ctx):
    rng = ctx.range
    # Lists and timelines stay null unless their source query succeeded.
    out = {"kpis": {"requests": {"value": None, "previous": None}, "principals": {"value": None, "previous": None},
                    "humanPrincipals": None, "servicePrincipals": None, "errorRate": None, "denied": None,
                    "throttled": None},
           "trend": None, "byService": None, "byMethod": None, "topPrincipals": None, "modelInvocations": None,
           "recentFailures": None}
    da = ctx.audit_source(AUDIT_DATA_ACCESS)
    ac = ctx.audit_source(AUDIT_ACTIVITY)
    sources = [("da", da), ("ac", ac)]
    services = list(ctx.settings["gateway"]["services"] or [])
    tasks = {"invocations": lambda: monitoring.invocation_counts(ctx, rng.start_ts(), rng.end_ts())}
    ctes_prev, labels = gateway_ctes(sources, "prev_start_ts")
    ctes_cur, _ = gateway_ctes(sources, "start_ts")
    label = labels[0] if labels else "bigquery:%s" % ctx.audit_dataset
    if ctes_prev and services:
        params_prev = dict(range_params(rng), services=services)
        params_cur = dict(range_params(rng, with_prev=False), services=services)
        human = human_sql("principal")
        kpi_sql = ("WITH %s\nSELECT period, bucket, outcome, COUNT(*) AS n, COUNT(DISTINCT principal) AS principals, "
                   "COUNT(DISTINCT IF(%s, principal, NULL)) AS humans, "
                   "COUNT(DISTINCT IF(principal IS NOT NULL AND NOT IFNULL(%s, FALSE), principal, NULL)) AS services\n"
                   "FROM (SELECT cl.*, %s AS period, %s AS bucket FROM cl)\nGROUP BY ROLLUP(period, bucket, outcome)"
                   % (",\n".join(ctes_prev), human, human, period_sql("ts"), bucket_or_sentinel("ts")))
        methods_sql = ("WITH %s\nSELECT service, method, COUNT(*) AS n, COUNTIF(outcome != 'success') AS errors "
                       "FROM cl GROUP BY service, method" % ",\n".join(ctes_cur))
        principals_sql = ("WITH %s\nSELECT principal, COUNT(*) AS n, COUNTIF(outcome != 'success') AS errors, "
                          "MAX(ts) AS last_seen FROM cl WHERE principal IS NOT NULL GROUP BY principal "
                          "ORDER BY n DESC, principal LIMIT 15" % ",\n".join(ctes_cur))
        failures_sql = ("WITH %s\nSELECT ts, principal, service, method, code, outcome, message, ip FROM cl "
                        "WHERE outcome != 'success' ORDER BY ts DESC LIMIT 50" % ",\n".join(ctes_cur))
        tasks["kpi"] = lambda: ctx.query(kpi_sql, params_prev, "agent_gateway")
        tasks["methods"] = lambda: ctx.query(methods_sql, params_cur, "agent_gateway")
        tasks["principals"] = lambda: ctx.query(principals_sql, params_cur, "agent_gateway")
        tasks["failures"] = lambda: ctx.query(failures_sql, params_cur, "agent_gateway")
    res = ctx.run(tasks)
    if "kpi" in res:
        ok, rows = res["kpi"]
        if ok:
            for lbl in labels:
                ctx.add_source(lbl)
            period_tot = {}
            bucket_out = {}
            for row in rows:
                period, bucket, outcome = row.get("period"), row.get("bucket"), row.get("outcome")
                if period is None:
                    continue
                if bucket is None and outcome is None:
                    period_tot[period] = row
                elif period == "cur" and bucket is not None and outcome is not None:
                    bucket_out[(bucket, outcome)] = to_int(row.get("n"))
            per_outcome = {}
            for row in rows:
                if row.get("period") == "cur" and row.get("bucket") is not None and row.get("outcome") is not None:
                    per_outcome[row["outcome"]] = per_outcome.get(row["outcome"], 0) + to_int(row.get("n"))
            cur, prev = period_tot.get("cur", {}), period_tot.get("prev", {})
            total = to_int(cur.get("n"))
            errors = total - per_outcome.get("success", 0)
            out["kpis"] = {"requests": {"value": total, "previous": to_int(prev.get("n"))},
                           "principals": {"value": to_int(cur.get("principals")),
                                          "previous": to_int(prev.get("principals"))},
                           "humanPrincipals": to_int(cur.get("humans")),
                           "servicePrincipals": to_int(cur.get("services")),
                           "errorRate": ratio(errors, total) if total else None,
                           "denied": per_outcome.get("denied", 0), "throttled": per_outcome.get("throttled", 0)}
            series = []
            for key, name in OUTCOMES:
                values = [bucket_out.get((day, key), 0) for day in rng.buckets()]
                series.append(timeutil.series(key, name, values))
            out["trend"] = timeutil.timeline(rng, series)
        else:
            ctx.warn_error(label, rows, "Cloud Audit Logs")
        ok, rows = res["methods"]
        if ok:
            by_service = {}
            methods = []
            for row in rows:
                svc = row.get("service")
                entry = by_service.setdefault(svc, {"service": svc, "requests": 0, "errors": 0})
                entry["requests"] += to_int(row.get("n"))
                entry["errors"] += to_int(row.get("errors"))
                methods.append({"method": row.get("method"), "service": svc, "requests": to_int(row.get("n")),
                                "errors": to_int(row.get("errors"))})
            out["byService"] = sorted(by_service.values(), key=lambda s: (-s["requests"], s["service"] or ""))
            out["byMethod"] = sorted(methods, key=lambda m: (-m["requests"], m["method"] or ""))[:12]
        else:
            ctx.warn_error(label, rows, "Cloud Audit Logs")
        ok, rows = res["principals"]
        if ok:
            out["topPrincipals"] = []
            for row in rows:
                principal = row.get("principal")
                human = "@" in principal and not principal.endswith(".gserviceaccount.com") and \
                    not principal.startswith("principal://")
                out["topPrincipals"].append({"uid": uid_for(principal), "principal": principal,
                                             "type": "user" if human else "service_account",
                                             "requests": to_int(row.get("n")), "errors": to_int(row.get("errors")),
                                             "lastSeen": iso(row.get("last_seen"))})
        else:
            ctx.warn_error(label, rows, "Cloud Audit Logs")
        ok, rows = res["failures"]
        if ok:
            out["recentFailures"] = [{"time": iso(r.get("ts")), "principal": r.get("principal"),
                                      "service": r.get("service"), "method": r.get("method"),
                                      "code": to_int(r.get("code")) if r.get("code") is not None else None,
                                      "outcome": r.get("outcome"), "message": r.get("message"),
                                      "callerIp": r.get("ip")} for r in rows]
        else:
            ctx.warn_error(label, rows, "Cloud Audit Logs")
    elif not services:
        ctx.warn("settings:gateway.services", "not_configured", "No gateway services are configured.")
    elif labels or da or ac:
        ctx.warn(label, "unsupported_schema", "Audit log fields were not found in the table schema.")
    ok, inv = res["invocations"]
    inv_label = "monitoring:%s" % monitoring.INVOCATION_METRIC
    if ok:
        if inv:
            ctx.add_source(inv_label)
        merged = {}
        for raw_id, entry in inv.items():
            key = normalize_model_id(raw_id)
            agg = merged.setdefault(key, {"invocations": 0, "errors": 0})
            agg["invocations"] += entry["invocations"]
            agg["errors"] += entry["errors"]
        out["modelInvocations"] = sorted(({"model": k, "invocations": int(v["invocations"]),
                                           "errors": int(v["errors"]), "errorRate": ratio(v["errors"], v["invocations"])}
                                          for k, v in merged.items() if v["invocations"]),
                                         key=lambda m: (-m["invocations"], m["model"]))
    else:
        ctx.warn_error(inv_label, inv, "Cloud Monitoring invocation metrics")
    return out
