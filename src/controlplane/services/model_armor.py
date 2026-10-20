"""Model Armor sanitize operations."""

from __future__ import annotations

import datetime as dt

from .. import timeutil
from ..gcp import GcpError
from ..httpkit import ApiError
from ..schema import expr, quote_ident
from ..security import uid_for
from .base import (ARMOR_OPS, GE_ACTIVITY, bucket_or_sentinel, bucket_values, iso, period_sql, range_params,
                   ratio, split_rollup, to_int)
from .gemini_enterprise import act_ctes

E = "jsonpayload_v1_sanitizeoperationlogentry"
J = "jsonPayload"
FILTERS = [
    ("pi_jailbreak", "Prompt injection and jailbreak",
     ("%s.sanitizationresult.filterresults.pi_and_jailbreak.piandjailbreakfilterresult.matchstate" % E,
      "%s.sanitizationResult.filterResults.pi_and_jailbreak.piAndJailbreakFilterResult.matchState" % J)),
    ("sdp", "Sensitive data protection",
     ("%s.sanitizationresult.filterresults.sdp.sdpfilterresult.inspectresult.matchstate" % E,
      "%s.sanitizationResult.filterResults.sdp.sdpFilterResult.inspectResult.matchState" % J)),
    ("rai", "Responsible AI",
     ("%s.sanitizationresult.filterresults.rai.raifilterresult.matchstate" % E,
      "%s.sanitizationResult.filterResults.rai.raiFilterResult.matchState" % J)),
    ("malicious_uri", "Malicious URLs",
     ("%s.sanitizationresult.filterresults.malicious_uris.maliciousurifilterresult.matchstate" % E,
      "%s.sanitizationResult.filterResults.malicious_uris.maliciousUriFilterResult.matchState" % J)),
    ("csam", "CSAM",
     ("%s.sanitizationresult.filterresults.csam.csamfilterfilterresult.matchstate" % E,
      "%s.sanitizationResult.filterResults.csam.csamFilterFilterResult.matchState" % J)),
]
FILTER_KEYS = [f[0] for f in FILTERS]


def armor_ctes(src, start_param="prev_start_ts"):
    """CTEs producing `ops` rows. Returns (ctes, caps) or (None, caps) when unusable."""
    s = src.schema
    verdict = expr("m", s, "%s.sanitizationresult.sanitizationverdict" % E,
                   "%s.sanitizationResult.sanitizationVerdict" % J, cast="STRING")
    reason = expr("m", s, "%s.sanitizationresult.sanitizationverdictreason" % E,
                  "%s.sanitizationResult.sanitizationVerdictReason" % J, cast="STRING")
    op = expr("m", s, "%s.operationtype" % E, "%s.operationType" % J, cast="STRING")
    text = expr("m", s, "%s.sanitizationinput.text" % E, "%s.sanitizationInput.text" % J, cast="STRING")
    corr = expr("m", s, "labels.modelarmor_googleapis_com_client_correlation_id", cast="STRING")
    template = expr("m", s, "resource.labels.template_id", "labels.modelarmor_googleapis_com_template_id",
                    cast="STRING")
    insert_id = expr("m", s, "insertId", cast="STRING")
    null = "CAST(NULL AS STRING)"
    flags = []
    for key, _name, paths in FILTERS:
        e = expr("m", s, *paths, cast="STRING")
        flags.append("IFNULL(%s = 'MATCH_FOUND', FALSE) AS f_%s" % (e, key) if e else "FALSE AS f_%s" % key)
    caps = {"verdict": bool(verdict), "corr": bool(corr)}
    ts = "m.%s" % quote_ident(src.time_column())
    ctes = [
        src.cte("ma_src", start=start_param),
        ("ma AS (SELECT %s AS ts, %s AS verdict, %s AS op, %s AS reason, %s AS text, %s AS corr, %s AS template, "
         "%s AS insert_id, %s FROM ma_src m)" % (ts, verdict or null, op or null, reason or null, text or null,
                                                corr or null, template or null, insert_id or null, ", ".join(flags))),
        ("ops AS (SELECT *, IFNULL(verdict LIKE '%BLOCK%', FALSE) AS blocked, "
         "CASE WHEN search_result THEN 'search_result' "
         "WHEN UPPER(IFNULL(op, '')) LIKE '%PROMPT%' THEN 'prompt' "
         "WHEN UPPER(IFNULL(op, '')) LIKE '%RESPONSE%' THEN 'response' ELSE 'other' END AS operation, "
         "SPLIT(SPLIT(corr, '|')[SAFE_OFFSET(1)], '/')[SAFE_OFFSET(7)] AS app, "
         "REGEXP_EXTRACT(corr, r'\\|([^\\|]+)$') AS token, "
         "SUBSTR(TO_HEX(SHA1(CONCAT(CAST(ts AS STRING), '|', IFNULL(insert_id, ''), '|', IFNULL(corr, '')))), 1, 20) "
         "AS op_id FROM (SELECT *, IFNULL(REGEXP_CONTAINS(text, r\"" + SEARCH_RESULT_RE + "\"), FALSE) "
         "AS search_result FROM ma))"),
    ]
    return ctes, caps


def attribution_ctes(asrc):
    """Activity CTEs used to attribute operations to users via the assist token.

    act_tok: one row per assist token (email, and whether the call was automatic: session summaries
    and scheduled runs). act_auto: automatic runs, used for operations whose token is not in the
    activity log (Gemini Enterprise screens grounding results of automatic runs under tokens of
    inner calls); such operations are attributed to the run's owner only when exactly one owner had an
    automatic run within AUTO_WINDOW_MINUTES."""
    if asrc is None:
        return None
    ctes, caps = act_ctes(asrc, start_param="act_start_ts")
    if not (caps["token"] and caps["user"]):
        return None
    # Activity rows are written around the sanitize call; pad the window by one day on both sides.
    ctes[0] = asrc.cte("a_src", start="act_start_ts", end="act_end_ts")
    ctes.append("act_tok AS (SELECT token, ANY_VALUE(email) AS email, LOGICAL_OR(is_auto) AS auto FROM act "
                "WHERE token IS NOT NULL AND email IS NOT NULL GROUP BY token)")
    ctes.append("act_auto AS (SELECT ts, email FROM act WHERE is_auto AND email IS NOT NULL)")
    return ctes


AUTO_WINDOW_MINUTES = 15
# Grounding results (web or data store search results) screened as part of an assistant call.
SEARCH_RESULT_RE = r"^\{'uri': 'https://vertexaisearch\.cloud\.google\.com/grounding-api-redirect/"


def attributed_ops_cte():
    """ops_attr: ops plus origin ('user' | 'automatic' | 'unknown'), email, inferred flag and kind."""
    return (
        "ops_attr AS (SELECT o.*, "
        "CASE WHEN t.email IS NOT NULL THEN IF(IFNULL(t.auto, FALSE), 'automatic', 'user') "
        "WHEN w.owners = 1 OR (w.owners > 1 AND o.search_result) THEN 'automatic' ELSE 'unknown' END AS origin, "
        "COALESCE(t.email, IF(w.owners = 1, w.owner, NULL)) AS email, "
        "t.email IS NULL AND w.owners = 1 AS inferred "
        "FROM ops o LEFT JOIN act_tok t ON t.token = o.token "
        "LEFT JOIN (SELECT o2.op_id, COUNT(DISTINCT x.email) AS owners, ANY_VALUE(x.email) AS owner "
        "FROM ops o2 JOIN act_auto x ON x.ts BETWEEN TIMESTAMP_SUB(o2.ts, INTERVAL %d MINUTE) "
        "AND TIMESTAMP_ADD(o2.ts, INTERVAL %d MINUTE) GROUP BY o2.op_id) w ON w.op_id = o.op_id "
        "AND t.email IS NULL)" % (AUTO_WINDOW_MINUTES, AUTO_WINDOW_MINUTES))


def armor_params(rng, with_prev=True):
    params = range_params(rng, with_prev=with_prev)
    first = params["prev_start_ts"] if with_prev else params["start_ts"]
    params["act_start_ts"] = first - dt.timedelta(days=1)
    params["act_end_ts"] = params["end_ts"] + dt.timedelta(days=1)
    return params


def armor_totals(ctx, src, rng):
    ctes, caps = armor_ctes(src)
    flag_sums = ", ".join("COUNTIF(f_%s) AS f_%s" % (k, k) for k in FILTER_KEYS)
    sql = ("WITH %s\nSELECT period, bucket, COUNT(*) AS ops, COUNTIF(blocked) AS blocked, "
           "COUNTIF(blocked AND operation = 'prompt') AS prompt_blocks, "
           "COUNTIF(blocked AND operation = 'response') AS response_blocks, "
           "COUNTIF(blocked AND operation = 'search_result') AS search_result_blocks, %s\n"
           "FROM (SELECT ops.*, %s AS period, %s AS bucket FROM ops)\nGROUP BY ROLLUP(period, bucket)"
           % (",\n".join(ctes), flag_sums, period_sql("ts"), bucket_or_sentinel("ts")))
    totals, buckets = split_rollup(ctx.query(sql, range_params(rng), "model_armor"))
    return {"totals": totals, "buckets": buckets, "caps": caps}


def summary(ctx):
    rng = ctx.range
    out = {"kpis": {"operations": {"value": None, "previous": None}, "blocked": {"value": None, "previous": None},
                    "blockRate": None, "promptBlocks": None, "responseBlocks": None, "searchResultBlocks": None,
                    "blockedByOrigin": None},
           "trend": None,
           "filters": [{"key": k, "name": n, "matches": None} for k, n, _ in FILTERS],
           "byApp": None, "byUser": None}
    src = ctx.audit_source(ARMOR_OPS)
    if src is None:
        return out
    asrc = ctx.audit_source(GE_ACTIVITY)
    attr = attribution_ctes(asrc)

    def by_app():
        ctes, _ = armor_ctes(src, start_param="start_ts")
        sql = ("WITH %s\nSELECT app, COUNT(*) AS operations, COUNTIF(blocked) AS blocked FROM ops "
               "GROUP BY app ORDER BY operations DESC LIMIT 10" % ",\n".join(ctes))
        return ctx.query(sql, range_params(rng, with_prev=False), "model_armor")

    def by_user():
        # People only: blocks in automatic runs (session summaries, scheduled runs) are reported separately.
        ctes, _ = armor_ctes(src, start_param="start_ts")
        sql = ("WITH %s\nSELECT email, COUNT(*) AS blocked, MAX(ts) AS last_blocked FROM ops_attr "
               "WHERE blocked AND origin = 'user' AND email IS NOT NULL GROUP BY email "
               "ORDER BY blocked DESC, email LIMIT 15" % ",\n".join(ctes + attr + [attributed_ops_cte()]))
        return ctx.query(sql, armor_params(rng, with_prev=False), "model_armor")

    def by_origin():
        ctes, _ = armor_ctes(src, start_param="start_ts")
        sql = ("WITH %s\nSELECT origin, COUNT(*) AS operations, COUNTIF(blocked) AS blocked FROM ops_attr "
               "GROUP BY origin" % ",\n".join(ctes + attr + [attributed_ops_cte()]))
        return ctx.query(sql, armor_params(rng, with_prev=False), "model_armor")

    tasks = {"kpi": lambda: armor_totals(ctx, src, rng), "apps": by_app}
    if attr:
        tasks["users"] = by_user
        tasks["origins"] = by_origin
    res = ctx.run(tasks)
    ok, kpi = res["kpi"]
    if ok:
        ctx.add_source(src.label)
        cur, prev = kpi["totals"].get("cur", {}), kpi["totals"].get("prev", {})
        if not kpi["caps"]["verdict"]:
            ctx.warn(src.label, "unsupported_schema", "The sanitization verdict field was not found.")
        ops_cur = to_int(cur.get("ops"))
        blocked = to_int(cur.get("blocked")) if kpi["caps"]["verdict"] else None
        out["kpis"] = {
            "operations": {"value": ops_cur, "previous": to_int(prev.get("ops"))},
            "blocked": {"value": blocked, "previous": to_int(prev.get("blocked")) if kpi["caps"]["verdict"] else None},
            "blockRate": ratio(blocked, ops_cur) if blocked is not None else None,
            "promptBlocks": to_int(cur.get("prompt_blocks")) if kpi["caps"]["verdict"] else None,
            "responseBlocks": to_int(cur.get("response_blocks")) if kpi["caps"]["verdict"] else None,
            "searchResultBlocks": to_int(cur.get("search_result_blocks")) if kpi["caps"]["verdict"] else None,
            "blockedByOrigin": None,
        }
        allowed = [o - b for o, b in zip(bucket_values(rng, kpi["buckets"], "ops"),
                                         bucket_values(rng, kpi["buckets"], "blocked"))]
        out["trend"] = timeutil.timeline(rng, [
            timeutil.series("allowed", "Allowed", allowed),
            timeutil.series("blocked", "Blocked", bucket_values(rng, kpi["buckets"], "blocked"))])
        out["filters"] = [{"key": k, "name": n, "matches": to_int(cur.get("f_%s" % k))} for k, n, _ in FILTERS]
    else:
        ctx.warn_error(src.label, kpi, "Model Armor logs")
    ok, rows = res["apps"]
    if ok:
        out["byApp"] = [{"app": r.get("app"), "operations": to_int(r.get("operations")),
                         "blocked": to_int(r.get("blocked"))} for r in rows]
    else:
        ctx.warn_error(src.label, rows, "Model Armor logs")
    if "users" in res:
        ok, rows = res["users"]
        if ok:
            ctx.add_source(asrc.label)
            out["byUser"] = [{"uid": uid_for(r["email"]), "email": r["email"], "blocked": to_int(r.get("blocked")),
                              "lastBlocked": iso(r.get("last_blocked"))} for r in rows if r.get("email")]
        else:
            ctx.warn_error(asrc.label, rows, "Gemini Enterprise activity logs")
    elif asrc is not None:
        ctx.warn(asrc.label, "unsupported_schema", "Assist tokens are not available for user attribution.")
    if "origins" in res:
        ok, rows = res["origins"]
        if ok and kpi_ok(res) and out["kpis"]["blocked"]["value"] is not None:
            counts = {r.get("origin"): to_int(r.get("blocked")) or 0 for r in rows}
            out["kpis"]["blockedByOrigin"] = {"user": counts.get("user", 0), "automatic": counts.get("automatic", 0),
                                              "unknown": counts.get("unknown", 0)}
        elif not ok:
            ctx.warn_error(asrc.label, rows, "Gemini Enterprise activity logs")
    return out


def kpi_ok(res):
    return bool(res.get("kpi") and res["kpi"][0])


def events_query(ctx, src, asrc, rng, verdict="blocked", flt=None, limit=100, email=None):
    attr = attribution_ctes(asrc)
    ctes, _ = armor_ctes(src, start_param="start_ts")
    where = ["TRUE"]
    if verdict == "blocked":
        where.append("o.blocked")
    if flt:
        if flt not in FILTER_KEYS:
            raise ApiError("bad_request", "Unknown filter.")
        where.append("o.f_%s" % flt)
    if attr:
        ctes = ctes + attr + [attributed_ops_cte()]
        table = "ops_attr"
        cols = "o.email, o.origin, o.inferred"
    else:
        table = "ops"
        cols = "CAST(NULL AS STRING) AS email, 'unknown' AS origin, FALSE AS inferred"
    if email:
        if not attr:
            return [], 0
        where.append("o.email = @email AND NOT o.inferred")
    flags = ", ".join("o.f_%s" % k for k in FILTER_KEYS)
    sql = ("WITH %s\nSELECT o.ts, %s, o.app, o.operation, o.blocked, o.reason, o.text, o.template, %s, "
           "o.op_id AS id, COUNT(*) OVER () AS total\n"
           "FROM %s o\nWHERE %s\nORDER BY o.ts DESC\nLIMIT %d"
           % (",\n".join(ctes), cols, flags, table, " AND ".join(where), int(limit)))
    params = armor_params(rng, with_prev=False)
    if email:
        params["email"] = email
    rows = ctx.query(sql, params, "model_armor")
    items = []
    for r in rows:
        mail = r.get("email")
        items.append({"id": r.get("id"), "time": iso(r.get("ts")), "uid": uid_for(mail) if mail else None,
                      "email": mail, "origin": r.get("origin") or "unknown", "inferred": bool(r.get("inferred")),
                      "app": r.get("app"), "operation": r.get("operation") or "other",
                      "verdict": "blocked" if r.get("blocked") else "allowed", "reason": r.get("reason"),
                      "filters": [k for k in FILTER_KEYS if r.get("f_%s" % k)], "text": r.get("text"),
                      "restricted": False, "template": r.get("template")})
    total = to_int(rows[0].get("total")) if rows else 0
    return items, total


def events(ctx):
    rng = ctx.range
    verdict = (ctx.params.get("verdict") or "blocked").lower()
    if verdict not in ("blocked", "all"):
        raise ApiError("bad_request", "Parameter 'verdict' must be 'blocked' or 'all'.")
    flt = ctx.params.get("filter") or None
    if flt and flt not in FILTER_KEYS:
        raise ApiError("bad_request", "Parameter 'filter' must be one of: %s." % ", ".join(FILTER_KEYS))
    try:
        limit = int(ctx.params.get("limit") or 100)
    except ValueError:
        raise ApiError("bad_request", "Parameter 'limit' must be an integer.")
    limit = max(1, min(limit, 500))
    src = ctx.audit_source(ARMOR_OPS)
    if src is None:
        return {"items": None, "total": None}
    asrc = ctx.audit_source(GE_ACTIVITY)
    try:
        items, total = events_query(ctx, src, asrc, rng, verdict, flt, limit)
    except GcpError as err:
        ctx.warn_error(src.label, err, "Model Armor logs")
        return {"items": None, "total": None}
    ctx.add_source(src.label)
    if asrc is not None and attribution_ctes(asrc):
        ctx.add_source(asrc.label)
    return {"items": items, "total": total}
