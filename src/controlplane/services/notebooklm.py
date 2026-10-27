"""NotebookLM usage from Cloud Audit data access logs."""

from __future__ import annotations

from .. import timeutil
from ..gcp import GcpError
from ..schema import expr, quote_ident
from ..security import uid_for
from .base import (AUDIT_DATA_ACCESS, bucket_or_sentinel, bucket_values, human_sql, iso, period_sql,
                   range_params, split_rollup, to_int)

PROMPT_METHOD = "google.cloud.notebooklm.v1main.NotebookService.GenerateFreeFormStreamed"


def audit_fields(src, alias="t"):
    s = src.schema
    return {
        "service": expr(alias, s, "protopayload_auditlog.serviceName", "protoPayload.serviceName", cast="STRING"),
        "method": expr(alias, s, "protopayload_auditlog.methodName", "protoPayload.methodName", cast="STRING"),
        "resource": expr(alias, s, "protopayload_auditlog.resourceName", "protoPayload.resourceName", cast="STRING"),
        "principal": expr(alias, s, "protopayload_auditlog.authenticationInfo.principalEmail",
                          "protoPayload.authenticationInfo.principalEmail", cast="STRING"),
        "code": expr(alias, s, "protopayload_auditlog.status.code", "protoPayload.status.code", cast="INT64"),
        "message": expr(alias, s, "protopayload_auditlog.status.message", "protoPayload.status.message",
                        cast="STRING"),
        "ip": expr(alias, s, "protopayload_auditlog.requestMetadata.callerIp",
                   "protoPayload.requestMetadata.callerIp", cast="STRING"),
        "agent": expr(alias, s, "protopayload_auditlog.requestMetadata.callerSuppliedUserAgent",
                      "protoPayload.requestMetadata.callerSuppliedUserAgent", cast="STRING"),
        "response": expr(alias, s, "protopayload_auditlog.responseJson", cast="STRING"),
        "ts": "%s.%s" % (alias, quote_ident(src.time_column())),
    }


def nb_ctes(src, start_param="prev_start_ts"):
    f = audit_fields(src)
    if not (f["service"] and f["method"] and f["principal"]):
        return None
    resource = f["resource"] or "CAST(NULL AS STRING)"
    ctes = [
        src.cte("da_src", start=start_param),
        ("nb AS (SELECT %s AS ts, LOWER(%s) AS email, %s AS method, "
         "REGEXP_EXTRACT(%s, r'notebooks/([^/]+)') AS notebook, "
         "IF(%s = '%s', CONCAT(IFNULL(%s, ''), '|', IFNULL(%s, ''), '|', CAST(%s AS STRING)), NULL) AS prompt_key "
         "FROM da_src t WHERE %s = 'discoveryengine.googleapis.com' AND STARTS_WITH(%s, 'google.cloud.notebooklm.') "
         "AND %s)" % (f["ts"], f["principal"], f["method"], resource, f["method"], PROMPT_METHOD, resource,
                      f["principal"], f["ts"], f["service"], f["method"], human_sql("LOWER(%s)" % f["principal"]))),
    ]
    return ctes


def nb_totals(ctx, src, rng):
    ctes = nb_ctes(src)
    if ctes is None:
        return None
    sql = ("WITH %s\nSELECT period, bucket, COUNT(DISTINCT prompt_key) AS prompts, COUNT(DISTINCT email) AS users, "
           "COUNT(DISTINCT notebook) AS notebooks\nFROM (SELECT nb.*, %s AS period, %s AS bucket FROM nb)\n"
           "GROUP BY ROLLUP(period, bucket)" % (",\n".join(ctes), period_sql("ts"), bucket_or_sentinel("ts")))
    return split_rollup(ctx.query(sql, range_params(rng), "notebooklm"))


def summary(ctx):
    rng = ctx.range
    out = {"kpis": {"notebooks": {"value": None, "previous": None}, "activeUsers": {"value": None, "previous": None},
                    "prompts": {"value": None, "previous": None}},
           "activity": None, "topUsers": None}
    src = ctx.audit_source(AUDIT_DATA_ACCESS)
    if src is None:
        return out
    if nb_ctes(src) is None:
        ctx.warn(src.label, "unsupported_schema", "Audit log fields were not found in the table schema.")
        return out

    def top():
        ctes = nb_ctes(src, start_param="start_ts")
        sql = ("WITH %s\nSELECT email, COUNT(DISTINCT prompt_key) AS prompts, COUNT(DISTINCT notebook) AS notebooks, "
               "MAX(ts) AS last_active FROM nb GROUP BY email ORDER BY prompts DESC, email LIMIT 25"
               % ",\n".join(ctes))
        return ctx.query(sql, range_params(rng, with_prev=False), "notebooklm")

    res = ctx.run({"kpi": lambda: nb_totals(ctx, src, rng), "top": top})
    ok, value = res["kpi"]
    if ok:
        totals, buckets = value
        cur, prev = totals.get("cur", {}), totals.get("prev", {})
        out["kpis"] = {"notebooks": {"value": to_int(cur.get("notebooks")), "previous": to_int(prev.get("notebooks"))},
                       "activeUsers": {"value": to_int(cur.get("users")), "previous": to_int(prev.get("users"))},
                       "prompts": {"value": to_int(cur.get("prompts")), "previous": to_int(prev.get("prompts"))}}
        out["activity"] = timeutil.timeline(rng, [
            timeutil.series("prompts", "Prompts", bucket_values(rng, buckets, "prompts")),
            timeutil.series("activeUsers", "Active users", bucket_values(rng, buckets, "users"))])
        ctx.add_source(src.label)
    else:
        ctx.warn_error(src.label, value, "NotebookLM audit logs")
    ok, rows = res["top"]
    if ok:
        out["topUsers"] = [{"uid": uid_for(r["email"]), "email": r["email"], "prompts": to_int(r.get("prompts")),
                            "notebooks": to_int(r.get("notebooks")), "lastActive": iso(r.get("last_active"))}
                           for r in rows if r.get("email")]
    else:
        ctx.warn_error(src.label, rows, "NotebookLM audit logs")
    return out
