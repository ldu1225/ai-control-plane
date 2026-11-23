"""Users: per-user activity across surfaces with IAM state."""

from __future__ import annotations

import logging

from .. import config, timeutil
from ..gcp import GcpError
from ..httpkit import ApiError
from ..schema import quote_ident
from ..security import is_admin_email, uid_for
from .base import (AGY_SINK, ARMOR_OPS, AUDIT_DATA_ACCESS, bucket_sql, human_sql, iso, range_params, to_int)
from .gemini_enterprise import (ACTIVITY_UPLOAD_NOTE, AGENT_NAMES_CTE, act_ctes, activity_upload_sql, agent_label,
                                directory_for, ev_cte, file_items_sql, file_rows_to_items, msg_ctes, prompt_caps)
from .model_armor import armor_ctes, attribution_ctes, events_query
from .notebooklm import audit_fields, nb_ctes
from . import governance

LOG = logging.getLogger("controlplane.users")

METRICS = ("gePrompts", "agentCalls", "notebooklmPrompts", "antigravityRequests", "antigravityTokens",
           "agentPlatformCalls", "armorBlocks", "fileUploads")
ACTIVITY_METRICS = ("geActivity", "notebooklmActivity")
# Long-format metric for scheduled agent runs: summed into agentCalls, ignored for last activity.
AUTO_METRIC = "agentCallsScheduled"
MAX_USERS = 2000
MODEL_CALL_METHODS = r"(GenerateContent|StreamGenerateContent|Predict|RawPredict|StreamRawPredict|ChatCompletions)"


def long_ctes(ctx, start_param="start_ts"):
    """Builds CTEs and the long-format union.

    Returns (ctes, parts, labels, agy_in_sql, available) where `available` is the set of metric
    keys that have a source in this workspace."""
    ctes = []
    parts = []
    labels = []
    available = set()
    from .gemini_enterprise import GE_ACTIVITY, GE_MESSAGE
    msrc = ctx.audit_source(GE_MESSAGE)
    asrc = ctx.audit_source(GE_ACTIVITY)
    dsrc = ctx.audit_source(AUDIT_DATA_ACCESS)
    osrc = ctx.audit_source(ARMOR_OPS)
    agy_src = None
    if ctx.settings["governance"]["antigravity"].get("usage_source") in ("auto", "bigquery"):
        agy_src = ctx.audit_source(AGY_SINK, optional=True)
    act_ok = False
    caps_m = caps_a = None
    if msrc:
        c, caps_m = msg_ctes(msrc, start_param)
        ctes += c
        labels.append(msrc.label)
        parts.append("SELECT email AS email, 'geActivity' AS metric, 1 AS value, ts AS ts FROM msg WHERE NOT is_auto")
        available.add("geActivity")
    if asrc:
        c, caps_a = act_ctes(asrc, start_param)
        ctes += c
        labels.append(asrc.label)
        act_ok = caps_a["user"]
        parts.append("SELECT email AS email, 'geActivity' AS metric, 1 AS value, ts AS ts FROM act WHERE NOT is_auto")
        available.add("geActivity")
        if caps_a["agent"]:
            # Scheduled runs are the owner's agent calls but not activity of the person (AUTO_METRIC, see pivot_sql).
            parts.append("SELECT email AS email, IF(is_auto, '%s', 'agentCalls') AS metric, 1 AS value, ts AS ts "
                         "FROM act WHERE agent_id IS NOT NULL" % AUTO_METRIC)
        if caps_a["method"]:
            # Without the agent id column (no agent call logged yet, see act_ctes) agent calls are 0, not unknown.
            available.add("agentCalls")
    has_prompts, has_uploads = prompt_caps(caps_m, caps_a)
    if has_prompts or has_uploads:
        # Prompts and uploads: message log when populated in the window, otherwise activity log calls.
        ctes.append(ev_cte(bool(msrc), bool(asrc)))
        if has_prompts:
            parts.append("SELECT email AS email, 'gePrompts' AS metric, 1 AS value, ts AS ts FROM ev WHERE prompt = 1")
            available.add("gePrompts")
        if has_uploads:
            parts.append("SELECT email AS email, 'fileUploads' AS metric, 1 AS value, ts AS ts FROM ev WHERE upload = 1")
            available.add("fileUploads")
    if dsrc:
        nb = nb_ctes(dsrc, start_param)
        f = audit_fields(dsrc)
        if nb is not None:
            ctes += nb
            labels.append(dsrc.label)
            parts.append("SELECT email AS email, 'notebooklmActivity' AS metric, 1 AS value, ts AS ts FROM nb")
            parts.append("SELECT email AS email, 'notebooklmPrompts' AS metric, 1 AS value, MIN(ts) AS ts FROM nb "
                         "WHERE prompt_key IS NOT NULL GROUP BY email, prompt_key")
            ctes.append("apc AS (SELECT %s AS ts, LOWER(%s) AS email FROM da_src t WHERE %s = 'aiplatform.googleapis.com' "
                        "AND REGEXP_CONTAINS(%s, r'%s'))" % (f["ts"], f["principal"], f["service"], f["method"],
                                                             MODEL_CALL_METHODS))
            parts.append("SELECT email AS email, 'agentPlatformCalls' AS metric, 1 AS value, ts AS ts FROM apc")
            available.update({"notebooklmActivity", "notebooklmPrompts", "agentPlatformCalls"})
    if osrc and act_ok:
        c, caps = armor_ctes(osrc, start_param)
        if caps["verdict"] and caps["corr"]:
            if caps_a["token"]:
                ctes += c
                ctes.append("act_tok AS (SELECT token, ANY_VALUE(email) AS email FROM act WHERE token IS NOT NULL "
                            "AND email IS NOT NULL GROUP BY token)")
                labels.append(osrc.label)
                parts.append("SELECT t.email AS email, 'armorBlocks' AS metric, 1 AS value, o.ts AS ts "
                             "FROM ops o JOIN act_tok t ON t.token = o.token WHERE o.blocked")
            # Without the assist token column (no assist response logged yet) no block can be attributed: 0 per user.
            available.add("armorBlocks")
    agy_in_sql = False
    if agy_src is not None:
        f = governance.agy_fields(agy_src)
        if f["user"]:
            ctes.append(agy_src.cte("agy_src", start=start_param))
            ctes.append("agy AS (SELECT %s AS ts, LOWER(%s) AS email, IFNULL(%s, 0) AS total FROM agy_src t)"
                        % (f["ts"], f["user"], f["total"] or "0"))
            parts.append("SELECT email AS email, 'antigravityRequests' AS metric, 1 AS value, ts AS ts FROM agy")
            parts.append("SELECT email AS email, 'antigravityTokens' AS metric, total AS value, ts AS ts FROM agy")
            labels.append(agy_src.label)
            agy_in_sql = True
            available.update({"antigravityRequests", "antigravityTokens"})
    return ctes, parts, labels, agy_in_sql, available


def pivot_sql(ctes, parts, email_filter=False, per_bucket=False):
    metrics = METRICS + ACTIVITY_METRICS
    cols = ", ".join(("SUM(IF(metric IN ('%s', '%s'), value, 0)) AS %s" % (m, AUTO_METRIC, m)) if m == "agentCalls"
                     else ("SUM(IF(metric = '%s', value, 0)) AS %s" % (m, m)) for m in metrics)
    last = "MAX(IF(metric != '%s', ts, NULL)) AS last_active" % AUTO_METRIC
    where = "email IS NOT NULL AND %s" % human_sql("email")
    if email_filter:
        where += " AND email = @email"
    long_cte = "long AS (%s)" % "\nUNION ALL ".join(parts)
    if per_bucket:
        return ("WITH %s,\n%s\nSELECT %s AS bucket, %s, %s FROM long WHERE %s GROUP BY bucket"
                % (",\n".join(ctes), long_cte, bucket_sql("ts"), cols, last, where))
    order = " + ".join(m for m in METRICS if m != "antigravityTokens")
    return ("WITH %s,\n%s\nSELECT * FROM (SELECT email, %s, %s FROM long WHERE %s GROUP BY email)\n"
            "ORDER BY (%s + geActivity + notebooklmActivity) DESC, email LIMIT %d"
            % (",\n".join(ctes), long_cte, cols, last, where, order, MAX_USERS))


def surfaces_of(m):
    out = []
    if m.get("gePrompts") or m.get("agentCalls") or m.get("fileUploads") or m.get("geActivity"):
        out.append("gemini_enterprise")
    if m.get("notebooklmPrompts") or m.get("notebooklmActivity"):
        out.append("notebooklm")
    if m.get("antigravityRequests"):
        out.append("antigravity")
    if m.get("agentPlatformCalls"):
        out.append("agent_platform")
    return out


def user_table(ctx, rng):
    """Per-user metrics for a range.

    Returns {"users": {email: metrics}, "available": [metric keys with a readable source],
    "sourced": bool}. `sourced` is False when no contributing source could be read; the
    user list is then unknown (null in responses), never an empty list."""
    ctes, parts, labels, agy_in_sql, available = long_ctes(ctx)
    users = {}
    sourced = False
    if parts:
        try:
            rows = ctx.query(pivot_sql(ctes, parts), range_params(rng, with_prev=False), "users")
        except GcpError as err:
            ctx.warn_error(labels[0] if labels else "bigquery:%s" % ctx.audit_dataset, err, "User activity")
            available = set()
            agy_in_sql = False
        else:
            sourced = True
            for lbl in labels:
                ctx.add_source(lbl)
            for row in rows:
                email = row.get("email")
                if not email:
                    continue
                m = {k: to_int(row.get(k)) for k in METRICS + ACTIVITY_METRICS}
                m["lastActive"] = row.get("last_active")
                users[email] = m
            if len(rows) >= MAX_USERS:
                ctx.warn(labels[0] if labels else "bigquery", "truncated",
                         "Only the %d most active users are listed." % MAX_USERS)
    else:
        available = set()
    if not agy_in_sql and ctx.settings["modules"].get("antigravity", True):
        usage = governance.antigravity_usage(ctx, rng.start_ts(), rng.end_ts())
        if usage.ok:
            sourced = True
            available |= {"antigravityRequests", "antigravityTokens"}
            for email, u in usage.users.items():
                if not email or "@" not in email:
                    continue
                m = users.setdefault(email, {k: 0 for k in METRICS + ACTIVITY_METRICS})
                m.setdefault("lastActive", None)
                m["antigravityRequests"] += u["requests"]
                m["antigravityTokens"] += u["tokens"]
                last = u.get("last")
                if last is not None and (m.get("lastActive") is None or last > m["lastActive"]):
                    m["lastActive"] = last
    if not sourced and not parts and ctx.audit_dataset_ready() and not ctx.has_transient_warning():
        sourced = True
        ctx.add_source("bigquery:%s" % ctx.audit_dataset)
    return {"users": users, "available": sorted(available), "sourced": sourced}


def _metric(m, key, available):
    return m.get(key) if key in available else None


def iam_context(ctx):
    """Governance status (default policy) for IAM states. Returns (payload, internal) or (None, None)."""
    if not ctx.settings["modules"].get("antigravity", True):
        return None, None
    try:
        return ctx.memo("gov_default_status", "default", lambda c: governance.default_status(c), ttl=60)
    except GcpError as err:
        ctx.warn_error("api:cloudresourcemanager.projects.getIamPolicy", err, "The project IAM policy")
        return None, None


CAP_FIELDS = ("exception", "suspension", "restoreAt", "overCap", "overCapReasons", "capUsage", "estimatedCost",
              "caps", "credit")


def _blocked_map(ctx):
    mgr = getattr(ctx.app, "settings", None)
    return mgr.blocked_accounts() if mgr is not None else {}


def _ai_block_status(ctx, email, internal, blocked_map, custom_cache):
    rec = (blocked_map or {}).get(email)
    if not rec:
        return None
    remaining = []
    if internal and internal.get("readable"):
        cfg = internal["cfg"]
        remaining = [role for role, _ in internal["idx"].user_bindings(email)
                     if governance.is_ai_service_role(ctx, role, cfg, custom_cache)]
    return {
        "active": not remaining,
        "dashboardBlocked": True,
        "at": rec.get("at") or None,
        "by": rec.get("by") or None,
        "note": rec.get("note") or None,
        "removedRoles": list(rec.get("removedRoles") or []),
        "restoreRoles": list(rec.get("restoreRoles") or []),
        "remainingRoles": remaining,
    }


def iam_for(email, status_payload, internal, ctx=None, blocked_map=None, custom_cache=None):
    ai_block = _ai_block_status(ctx, email, internal, blocked_map, custom_cache if custom_cache is not None else {}) \
        if ctx is not None else None
    if status_payload is None or internal is None:
        out = {"state": "UNKNOWN", "reason": None}
        if ai_block is not None:
            out["aiBlocked"] = ai_block
        return out
    for u in status_payload["users"] or []:
        if u["email"] == email:
            out = {"state": u["state"], "reason": u["reason"]}
            out.update({k: u.get(k) for k in CAP_FIELDS})
            if ai_block is not None:
                out["aiBlocked"] = ai_block
            return out
    state, reason = governance.classify(email, False, internal["idx"], internal["cfg"])
    out = {"state": state, "reason": reason}
    for row in status_payload.get("exceptions") or []:
        if row.get("email") == email:
            out["exception"] = {k: v for k, v in row.items() if k not in ("email", "uid", "state")}
    if ai_block is not None:
        out["aiBlocked"] = ai_block
    return out


def _user_pricing(ctx):
    """Best-effort Pricing for per-model cost estimates in Users views. Never raises."""
    ag = (ctx.settings.get("governance") or {}).get("antigravity") or {}
    cur = "USD" if governance.credit_mode(ag) else (ag.get("currency") or "")
    table = None
    if (ag.get("pricing_source") or "auto") == "auto":
        try:
            table = governance.price_table(ctx, cur)
        except GcpError:
            table = None
    return governance.pricing_from(ag, table, currency=cur or None)


def _agent_platform_models(ctx, rng, email=None):
    """Per-user Agent Platform model call counts from Cloud Audit Data Access logs."""
    dsrc = ctx.audit_source(AUDIT_DATA_ACCESS)
    if dsrc is None:
        return []
    f = audit_fields(dsrc)
    if not (f["service"] and f["method"] and f["resource"] and f["principal"]):
        return []
    where = "model IS NOT NULL AND email IS NOT NULL AND %s" % human_sql("email")
    params = range_params(rng, with_prev=False)
    if email:
        where += " AND email = @email"
        params["email"] = email
    sql = ("WITH %s,\nm AS (SELECT LOWER(%s) AS email, REGEXP_EXTRACT(%s, r'models/([^/:@]+)') AS model, %s AS ts "
           "FROM da_src t WHERE %s = 'aiplatform.googleapis.com' AND REGEXP_CONTAINS(%s, r'%s'))\n"
           "SELECT email, model, COUNT(*) AS requests, MAX(ts) AS last_used FROM m WHERE %s "
           "GROUP BY email, model ORDER BY requests DESC, email, model LIMIT 500"
           % (dsrc.cte("da_src", start="start_ts"), f["principal"], f["resource"], f["ts"],
              f["service"], f["method"], MODEL_CALL_METHODS, where))
    rows = ctx.query(sql, params, "users")
    ctx.add_source(dsrc.label)
    return [{"email": r.get("email"), "model": r.get("model"), "requests": to_int(r.get("requests")),
             "lastUsed": iso(r.get("last_used"))} for r in rows if r.get("email") and r.get("model")]


def _agy_model_entries(by_model_map, pricing, prefixes, email=None):
    """Formats Antigravity byModel map into sorted public rows with cached/output tokens, cost, and cost share."""
    out = []
    ccy = pricing.currency if pricing is not None else "USD"
    for model, agg in (by_model_map or {}).items():
        if not model:
            continue
        prompt = to_int(agg.get("promptTokens"))
        cand = to_int(agg.get("candidateTokens"))
        cached = to_int(agg.get("cachedTokens"))
        tokens = to_int(agg.get("tokens"))
        output = max(tokens - prompt, cand, 0)
        cost, partial = governance.estimate_cost({"tokens": tokens, "byModel": {model: agg}}, pricing) \
            if pricing is not None else (None, False)
        row = {"model": model, "surface": "antigravity", "requests": to_int(agg.get("requests")),
               "tokens": tokens, "promptTokens": prompt, "candidateTokens": cand,
               "cachedTokens": cached, "outputTokens": output,
               "cost": cost, "costPartial": partial, "currency": ccy if cost is not None else None,
               "credit": governance.credit_ledger.credit_eligible(model, prefixes), "lastUsed": None}
        if email is not None:
            row["uid"] = uid_for(email)
            row["email"] = email
        out.append(row)
    total_cost = sum(r["cost"] for r in out if r["cost"] is not None)
    for r in out:
        r["costShare"] = round(r["cost"] / total_cost, 4) if (r["cost"] is not None and total_cost > 0) else None
    out.sort(key=lambda r: (-(r["cost"] if r["cost"] is not None else -1.0), -r["tokens"], -r["requests"], r["model"]))
    return out


def _ap_model_entries(ap_rows, include_user=False):
    out = []
    for r in ap_rows or ():
        row = {"model": r["model"], "surface": "agent_platform", "requests": r["requests"],
               "tokens": None, "promptTokens": None, "candidateTokens": None,
               "cachedTokens": None, "outputTokens": None,
               "cost": None, "costPartial": False, "costShare": None, "currency": None, "credit": None,
               "lastUsed": r.get("lastUsed")}
        if include_user:
            row["uid"] = uid_for(r["email"])
            row["email"] = r["email"]
        out.append(row)
    return out


def _model_sort_key(row):
    return (-(row["cost"] if row.get("cost") is not None else -1.0),
            -(row.get("tokens") or 0), -(row.get("requests") or 0),
            row.get("email") or "", row.get("model") or "")


def list_users(ctx):
    rng = ctx.range
    out = {"kpis": {"activeUsers": None, "antigravityUsers": None, "overCap": None, "manualActionRequired": None,
                    "enforceableCoverage": None, "estimatedCost": None},
           "currency": None, "users": None, "userModels": [], "iamAvailable": False}
    tasks = {"rows": lambda: ctx.memo("user_table", rng, lambda c: user_table(c, rng)),
             "iam": lambda: iam_context(ctx),
             "ap_models": lambda: _agent_platform_models(ctx, rng)}
    if ctx.settings["modules"].get("antigravity", True):
        tasks["agy_models"] = lambda: governance.antigravity_usage(ctx, rng.start_ts(), rng.end_ts())
        tasks["pricing"] = lambda: _user_pricing(ctx)
    res = ctx.run_composite(tasks)
    ok, table = res["rows"]
    if not ok:
        ctx.warn_error("bigquery:%s" % ctx.audit_dataset, table, "User activity")
        table = {"users": {}, "available": [], "sourced": False}
    ok, iam = res["iam"]
    status_payload, internal = iam if ok else (None, None)
    readable = bool(internal and internal.get("readable"))
    usage_ok = bool(status_payload is not None and status_payload.get("usageSource") != "unavailable")
    out["iamAvailable"] = readable
    if status_payload is not None:
        if usage_ok:
            out["kpis"]["overCap"] = sum(1 for u in status_payload["users"] or [] if u["overCap"])
        if usage_ok and readable:
            out["kpis"]["manualActionRequired"] = len(status_payload["manualActions"] or [])
        out["kpis"]["enforceableCoverage"] = status_payload["population"]["enforceableCoverage"]
    if not table["sourced"]:
        return out
    available = set(table["available"])
    blocked_map = _blocked_map(ctx)
    custom_cache = {}
    ag = (ctx.settings.get("governance") or {}).get("antigravity") or {}
    prefixes = governance.credit_prefixes(ag)
    pricing = res["pricing"][1] if ("pricing" in res and res["pricing"][0]) else None
    ccy = pricing.currency if pricing is not None else None
    out["currency"] = ccy
    user_models = []
    models_by_email = {}
    entries_by_email = {}
    if "agy_models" in res and res["agy_models"][0] and res["agy_models"][1].ok:
        for email, urec in res["agy_models"][1].users.items():
            if not email or "@" not in email:
                continue
            entries = _agy_model_entries(urec.get("byModel"), pricing, prefixes, email=email)
            user_models.extend(entries)
            models_by_email.setdefault(email, set()).update(e["model"] for e in entries)
            entries_by_email.setdefault(email, []).extend(entries)
    if "ap_models" in res and res["ap_models"][0]:
        ap_entries = _ap_model_entries(res["ap_models"][1], include_user=True)
        user_models.extend(ap_entries)
        for e in ap_entries:
            models_by_email.setdefault(e["email"], set()).add(e["model"])
            entries_by_email.setdefault(e["email"], []).append(e)
    user_models.sort(key=_model_sort_key)
    rows = []
    total_cost = 0.0
    any_cost = False
    for email, m in table["users"].items():
        u_models = sorted(entries_by_email.get(email) or (), key=_model_sort_key)
        priced = [e["cost"] for e in u_models if e.get("cost") is not None]
        est_cost = round(sum(priced), 6) if priced else None
        cost_partial = any(bool(e.get("costPartial")) for e in u_models)
        if est_cost is not None:
            total_cost += est_cost
            any_cost = True
        rows.append({"uid": uid_for(email), "email": email, "surfaces": surfaces_of(m),
                     "models": sorted(models_by_email.get(email) or ()),
                     "byModel": u_models,
                     "estimatedCost": est_cost, "costPartial": cost_partial,
                     "currency": ccy if est_cost is not None else None,
                     "gePrompts": _metric(m, "gePrompts", available),
                     "agentCalls": _metric(m, "agentCalls", available),
                     "notebooklmPrompts": _metric(m, "notebooklmPrompts", available),
                     "antigravityRequests": _metric(m, "antigravityRequests", available),
                     "antigravityTokens": _metric(m, "antigravityTokens", available),
                     "agentPlatformCalls": _metric(m, "agentPlatformCalls", available),
                     "armorBlocks": _metric(m, "armorBlocks", available),
                     "fileUploads": _metric(m, "fileUploads", available),
                     "lastActive": iso(m.get("lastActive")),
                     "iam": iam_for(email, status_payload, internal, ctx=ctx, blocked_map=blocked_map,
                                    custom_cache=custom_cache)})
    rows.sort(key=lambda r: (-(r["estimatedCost"] if r["estimatedCost"] is not None else -1.0),
                             -sum(r[k] or 0 for k in ("gePrompts", "agentCalls", "notebooklmPrompts",
                                                      "antigravityRequests", "agentPlatformCalls")), r["email"]))
    out["users"] = rows
    out["userModels"] = user_models
    out["kpis"]["activeUsers"] = len(rows)
    out["kpis"]["estimatedCost"] = round(total_cost, 6) if any_cost else None
    if "antigravityRequests" in available:
        out["kpis"]["antigravityUsers"] = sum(1 for r in rows if (r["antigravityRequests"] or 0) > 0)
    return out


def find_email(ctx, uid, rng):
    table = ctx.memo("user_table", rng, lambda c: user_table(c, rng))
    for email in table["users"]:
        if uid_for(email) == uid:
            return email
    for email in _blocked_map(ctx):
        if uid_for(email) == uid:
            return email
    status_payload, internal = iam_context(ctx)
    if status_payload is not None:
        for u in status_payload["users"] or []:
            if u["uid"] == uid:
                return u["email"]
    if internal and internal.get("readable"):
        for binding in internal["idx"].bindings:
            for member in binding.get("members") or []:
                low = str(member).lower()
                if low.startswith("user:") and uid_for(low[5:]) == uid:
                    return low[5:]
    return None


def _hinted_email(ctx, uid):
    """The email the page sent with the uid (the row that was opened) when it is a valid address of exactly that uid.
    It saves resolving the uid against every user's activity of the range (find_email), which used to run before any
    of this person's queries; detail() still answers 404 when the address has no trace at all."""
    raw = ctx.params.get("email")
    if not isinstance(raw, str):
        return None
    email = raw.strip().lower()
    if not email or len(email) > 320 or not config.EMAIL_RE.match(email) or uid_for(email) != uid:
        return None
    return email


DETAIL_PARTS = ("core", "activity")


def detail(ctx):
    """GET /api/v1/users/detail?uid=&email=&part=. part=core: Antigravity usage, models and costs, Agent Platform models
    and the access state. part=activity: Gemini Enterprise, NotebookLM, Agent Platform and Model Armor activity over
    time, agents and files (the slowest query). Without part, both: the drawer asks for the two parts at once and shows
    each as soon as it arrives."""
    rng = ctx.range
    uid = (ctx.params.get("uid") or "").strip().lower()
    if len(uid) != 16 or any(c not in "0123456789abcdef" for c in uid):
        raise ApiError("bad_request", "Parameter 'uid' is invalid.")
    part = ctx.params.get("part")
    part = part if part in DETAIL_PARTS else None
    want_core, want_activity = part in (None, "core"), part in (None, "activity")
    hinted = _hinted_email(ctx, uid)
    email = hinted or find_email(ctx, uid, rng)
    if not email:
        raise ApiError("not_found", "User not found.")
    params = dict(range_params(rng, with_prev=False), email=email)
    tasks = {}
    no_agent_column = False
    msrc = asrc = osrc = None
    if want_activity:
        ctes, parts, labels, agy_in_sql, available = long_ctes(ctx)
        from .gemini_enterprise import GE_ACTIVITY, GE_INFERENCE, GE_MESSAGE
        msrc = ctx.audit_source(GE_MESSAGE)
        asrc = ctx.audit_source(GE_ACTIVITY)
        isrc = ctx.audit_source(GE_INFERENCE, optional=True)
        osrc = ctx.audit_source(ARMOR_OPS)
        if parts:
            tasks["buckets"] = lambda: ctx.query(pivot_sql(ctes, parts, email_filter=True, per_bucket=True), params,
                                                 "users")
        if asrc:
            a_ctes, caps = act_ctes(asrc, "start_ts")
            if caps["agent"]:
                sql = ("WITH %s,\n%s\nSELECT c.agent_id, ANY_VALUE(n.agent_name) AS agent_name, COUNT(*) AS calls, "
                       "MAX(c.ts) AS last_used FROM act c LEFT JOIN act_names n ON n.agent_ref = c.agent_id "
                       "WHERE c.email = @email AND c.agent_id IS NOT NULL GROUP BY c.agent_id ORDER BY calls DESC "
                       "LIMIT 50" % (",\n".join(a_ctes), AGENT_NAMES_CTE))
                tasks["agents"] = lambda: ctx.query(sql, params, "users")
            no_agent_column = caps["method"] and not caps["agent"]
        if msrc:
            fsql = file_items_sql(msrc, asrc, email_filter=True, limit=200)
            if fsql:
                tasks["files"] = lambda: ctx.query(fsql, params, "users")
        if asrc:
            asql = activity_upload_sql(asrc, email_filter=True, limit=200, isrc=isrc)
            if asql:
                tasks["files_act"] = lambda: ctx.query(asql, params, "users")
        if osrc and attribution_ctes(asrc):
            tasks["armor"] = lambda: events_query(ctx, osrc, asrc, rng, verdict="blocked", limit=50, email=email)
    else:
        parts, labels, agy_in_sql, available = [], [], False, set()
    if want_core or not agy_in_sql:
        # Without the inference log table in the activity SQL, the activity part needs the Antigravity series too.
        tasks["agy"] = lambda: governance.antigravity_usage(ctx, rng.start_ts(), rng.end_ts(), email=email, rng=rng)
    if want_core:
        tasks["ap_models"] = lambda: _agent_platform_models(ctx, rng, email=email)
        tasks["pricing"] = lambda: _user_pricing(ctx)
        tasks["iam"] = lambda: iam_context(ctx)
    if hinted and part is not None:
        # One part sees only some of a person's traces; the uid lookup (alongside, mostly from cached tables) sees the
        # rest, so a part request cannot be used to probe an arbitrary address (see the trace check below).
        tasks["known"] = lambda: find_email(ctx, uid, rng)
    res = ctx.run_composite(tasks)
    metrics = {k: 0 for k in METRICS}
    activity_totals = {k: 0 for k in ACTIVITY_METRICS}
    series = {k: [0] * len(rng.buckets()) for k in ("gePrompts", "agentCalls", "notebooklmPrompts",
                                                     "antigravityRequests")}
    last_active = None
    sql_ok = False
    if "buckets" in res:
        ok, rows = res["buckets"]
        if ok:
            sql_ok = True
            for lbl in labels:
                ctx.add_source(lbl)
            for row in rows:
                idx = rng.bucket_index(row.get("bucket"))
                for k in METRICS:
                    metrics[k] += to_int(row.get(k))
                for k in ACTIVITY_METRICS:
                    activity_totals[k] += to_int(row.get(k))
                if idx is not None:
                    for k in series:
                        series[k][idx] += to_int(row.get(k))
                lr = row.get("last_active")
                if lr is not None and (last_active is None or lr > last_active):
                    last_active = lr
        else:
            ctx.warn_error(labels[0] if labels else "bigquery", rows, "User activity")
    if not sql_ok:
        available = set()
    ag = (ctx.settings.get("governance") or {}).get("antigravity") or {}
    prefixes = governance.credit_prefixes(ag)
    pricing = res["pricing"][1] if ("pricing" in res and res["pricing"][0]) else None
    ok, usage = res["agy"] if "agy" in res else (False, None)
    by_model = by_client = None
    models_all = []
    if ok and usage.ok:
        if not agy_in_sql or not sql_ok:
            u = usage.users.get(email) or {"requests": 0, "tokens": 0}
            metrics["antigravityRequests"] = u["requests"]
            metrics["antigravityTokens"] = u["tokens"]
            series["antigravityRequests"] = [0] * len(rng.buckets())
            for (mail, bucket), (req_count, _tok) in usage.daily.items():
                idx = rng.bucket_index(bucket)
                if mail == email and idx is not None:
                    series["antigravityRequests"][idx] += req_count
            last = u.get("last")
            if last is not None and (last_active is None or last > last_active):
                last_active = last
        available |= {"antigravityRequests", "antigravityTokens"}
        if want_core:
            by_model = _agy_model_entries(usage.by_model, pricing, prefixes)
            models_all.extend(by_model)
            by_client = sorted(({"client": k, "requests": v["requests"], "tokens": v["tokens"]}
                                for k, v in usage.by_client.items()), key=lambda x: -x["tokens"])
    if "ap_models" in res and res["ap_models"][0]:
        models_all.extend(_ap_model_entries(res["ap_models"][1], include_user=False))
    models_all.sort(key=_model_sort_key)
    model_totals = None
    if want_core:
        priced_models = [r["cost"] for r in models_all if r.get("cost") is not None]
        model_totals = {
            "models": len(models_all),
            "cost": round(sum(priced_models), 6) if priced_models else None,
            "costPartial": any(bool(r.get("costPartial")) for r in models_all),
            "currency": pricing.currency if (pricing is not None and priced_models) else None,
        }
    # Lists stay null unless their source query succeeded.
    agents = None
    if "agents" in res:
        ok, rows = res["agents"]
        if ok:
            directory = directory_for(ctx, rows)
            agents = [{"agent": agent_label(r.get("agent_id"), r.get("agent_name"), directory),
                       "calls": to_int(r.get("calls")), "lastUsed": iso(r.get("last_used"))} for r in rows]
        else:
            ctx.warn_error(asrc.label, rows, "Gemini Enterprise activity logs")
    elif no_agent_column and sql_ok:
        agents = []  # no agent call logged yet: the agent id column appears with the first one
    files = None
    if "files" in res:
        ok, rows = res["files"]
        if ok:
            files = file_rows_to_items(rows)
        else:
            ctx.warn_error(msrc.label, rows, "Gemini Enterprise file uploads")
    # Activity-log upload calls are listed only when the upload count is based on them (the message log recorded
    # no uploads in the window), so the list and the count shown next to it always agree.
    act_counted = not sql_ok or "fileUploads" not in available or metrics["fileUploads"] > 0
    if not files and "files_act" in res:
        ok, rows = res["files_act"]
        if ok:
            if rows and act_counted:
                files = file_rows_to_items(rows)
            elif files is None:
                files = []
        else:
            ctx.warn_error(asrc.label, rows, "Gemini Enterprise activity logs")
    armor = None
    if "armor" in res:
        ok, value = res["armor"]
        if ok:
            armor = value[0]
        else:
            ctx.warn_error(osrc.label, value, "Model Armor logs")
    iam_out = status_payload = blocked_map = bindings = None
    if want_core:
        iam_out, status_payload, blocked_map, bindings = _detail_iam(ctx, email, res.get("iam"))
    combined = dict(metrics, **activity_totals)
    out_metrics = {k: (metrics[k] if k in available else None) for k in METRICS}
    series_names = (("gePrompts", "Gemini Enterprise prompts"), ("agentCalls", "Agent calls"),
                    ("notebooklmPrompts", "NotebookLM prompts"), ("antigravityRequests", "Antigravity requests"))
    activity_series = ([timeutil.series(k, name, series[k]) for k, name in series_names if k in available]
                       if want_activity else [])
    if hinted:
        # Same answer as find_email: an address with no activity, usage, rule, IAM binding or block record is unknown.
        # A single part sees only some traces, so it also accepts the uid lookup that ran alongside ("known").
        listed = status_payload is not None and (
            any(u.get("email") == email for u in status_payload.get("users") or []) or
            any(r.get("email") == email for r in status_payload.get("exceptions") or []))
        traced = (listed or email in (blocked_map or {}) or bool(bindings) or last_active is not None or
                  any(metrics.get(k) for k in METRICS) or any(activity_totals.values()) or bool(models_all) or
                  bool(agents) or bool(files) or bool(armor))
        if not traced and part is not None:
            ok, found = res.get("known") or (False, None)
            traced = bool(ok and found == email)
        if not traced:
            raise ApiError("not_found", "User not found.")
    out = {"user": {"uid": uid, "email": email, "lastActive": iso(last_active), "surfaces": surfaces_of(combined)},
           "metrics": out_metrics,
           "activity": timeutil.timeline(rng, activity_series) if activity_series else None,
           "antigravity": {"byModel": by_model, "byClient": by_client} if want_core else None,
           "models": models_all if want_core else None,
           "modelTotals": model_totals,
           "agents": agents, "files": files, "armorEvents": armor,
           "iam": iam_out}
    if part:
        out["part"] = part
    return out


def detail_viewer_view(data):
    """GET /api/v1/users/detail for viewers and guests (the route's viewer_view, applied after the cache, which does not
    know the caller): the access actions are dropped. Only administrators can act on them, and they would tell who is
    an administrator (blockAll is never offered for one)."""
    iam = data.get("iam") if isinstance(data, dict) else None
    if not isinstance(iam, dict) or "actions" not in iam:
        return data
    return dict(data, iam=dict(iam, actions=None))


def _detail_iam(ctx, email, iam_result):
    """Access state and actions of one person for the drawer. Returns (iam, status_payload, blocked_map, bindings)."""
    ok, iam = iam_result if iam_result is not None else (False, None)
    status_payload, internal = iam if ok else (None, None)
    blocked_map = _blocked_map(ctx)
    iam_state = iam_for(email, status_payload, internal, ctx=ctx, blocked_map=blocked_map, custom_cache={})
    readable = bool(internal and internal.get("readable"))
    bindings = None
    direct_roles = set()
    if readable:
        raw_bindings = internal["idx"].user_bindings(email)
        direct_roles = {role for role, _ in raw_bindings}
        bindings = [{"role": role, "member": "user:%s" % email, "conditional": cond}
                    for role, cond in raw_bindings]
    state = iam_state["state"]
    repairable = (readable and state == "NOT_ENFORCEABLE" and iam_state["reason"] == "CONFLICTING_BINDINGS" and
                  governance._unconditional_marker_conflict(internal["idx"], email, internal["cfg"]))
    enforceable = (state not in ("NOT_ENFORCEABLE", "UNKNOWN") or repairable) and readable
    ai_block = iam_state.get("aiBlocked")
    is_ai_blocked = bool(ai_block and ai_block.get("active"))
    self_or_admin = (email == ctx.identity.email or
                     is_admin_email(email, (ctx.settings.get("security") or {}).get("admins")))
    project_priv = readable and bool(direct_roles & set(governance.PROJECT_WIDE_PRIVILEGED_ROLES))
    iam_out = {"state": state, "reason": iam_state["reason"], "bindings": bindings,
               "aiBlocked": ai_block,
               "actions": {
                   "allow": enforceable and (state in ("BLOCKED_BY_ADMIN", "CAPPED_BY_TOOL", "NO_ACCESS") or repairable),
                   "disallow": enforceable and (state in ("ENFORCEABLE_ALLOWED", "CAPPED_BY_TOOL") or repairable),
                   "blockAll": readable and not self_or_admin and not project_priv and not is_ai_blocked,
                   "unblockAll": readable and not self_or_admin and bool(ai_block),
               },
               "remediation": governance.REMEDIATION.get(iam_state["reason"]) if (not enforceable and state != "UNKNOWN")
               else None}
    iam_out.update({k: iam_state.get(k) for k in CAP_FIELDS})
    policy = (status_payload or {}).get("policy") or {}
    iam_out["currency"] = policy.get("currency")
    # The current cap period with explicit dates (an "allow until" rule ends at `end`) and what happens to someone over
    # the cap once a rule is revoked (manual review queue or automatic suspension).
    iam_out["capPeriod"] = {"start": policy.get("windowStart"), "end": policy.get("periodEnd"),
                            "nextReset": policy.get("nextReset"), "period": policy.get("period"),
                            "window": policy.get("window"), "timezone": policy.get("resetTimezone"),
                            "mode": policy.get("enforcementMode")} if policy else None
    return iam_out, status_payload, blocked_map, bindings


def access_action(ctx):
    """POST /api/v1/users/access-action {uid, email?, action: block_all | unblock_all, note?}."""
    uid = ctx.body.get("uid")
    email = ctx.body.get("email")
    action = ctx.body.get("action")
    if action not in ("block_all", "unblock_all"):
        raise ApiError("bad_request", "Field 'action' must be 'block_all' or 'unblock_all'.")
    if uid is not None and (not isinstance(uid, str) or len(uid) != 16 or any(c not in "0123456789abcdef" for c in uid)):
        raise ApiError("bad_request", "Field 'uid' is invalid.")
    if uid is None and not isinstance(email, str):
        raise ApiError("bad_request", "Field 'uid' or 'email' is required.")
    return governance.set_user_ai_access(ctx, action, uid=uid, email=email, note=ctx.body.get("note"))
