"""Gemini Enterprise: prompts, active users, agent usage, licenses and file uploads."""

from __future__ import annotations

import datetime as dt
import logging
import re

from .. import timeutil
from ..gcp import GcpError
from ..schema import expr, quote_ident
from ..security import uid_for
from .base import (GE_ACTIVITY, GE_INFERENCE, GE_MESSAGE, bucket_or_sentinel, bucket_sql, bucket_values, discovery_host,
                   discovery_locations, human_sql, iso, num, parse_rfc3339, period_sql, range_params, split_rollup,
                   to_int)

LOG = logging.getLogger("controlplane.ge")

NULL_STR = "CAST(NULL AS STRING)"
IDLE_LICENSE_DAYS = 30
UPLOAD_LIKE = "'%<start_of_user_uploaded_file%'"
# Activity log methods that represent a user prompt to the assistant, and a file upload into a session.
ASSIST_METHODS = r"(?i)^(StreamAssist|Assist|AnswerQuery|ConverseConversation)$"
UPLOAD_METHODS = r"(?i)^UploadSessionFile$"


# ---------------------------------------------------------------------------
# Shared CTE builders (also used by users and overview)
# ---------------------------------------------------------------------------

def _dedupe(alias, src, insert_id):
    """Log sinks deliver at least once: entries with the same insertId and timestamp are one entry."""
    if not insert_id:
        return ""
    return " WHERE TRUE QUALIFY ROW_NUMBER() OVER (PARTITION BY %s, %s.%s) = 1" % (
        insert_id, alias, quote_ident(src.time_column()))


def _system_text(col):
    """Assistant calls the product makes on the user's behalf (session summaries, injected context)."""
    return "IFNULL(%s LIKE '%%session_to_summarize%%' OR %s LIKE '%%For context:%%', FALSE)" % (col, col)


def msg_ctes(src, start_param="prev_start_ts"):
    """CTEs over gen_ai_user_message. Returns (ctes, caps)."""
    s = src.schema
    user = expr("m", s, "jsonPayload.user", cast="STRING")
    role = expr("m", s, "jsonPayload.content.role", cast="STRING")
    t0 = expr("m", s, "jsonPayload.content.parts[0].text", cast="STRING")
    t1 = expr("m", s, "jsonPayload.content.parts[1].text", cast="STRING")
    t2 = expr("m", s, "jsonPayload.content.parts[2].text", cast="STRING")
    trace = expr("m", s, "trace", cast="STRING")
    insert_id = expr("m", s, "insertId", cast="STRING")
    ts = "m.%s" % quote_ident(src.time_column())
    if role and t0:
        prompt = "IFNULL((%s) = 'user' AND (%s) IS NOT NULL AND NOT %s, FALSE)" % (role, t0, _system_text("(%s)" % t0))
    else:
        prompt = "FALSE"
    upload = "IFNULL((%s) LIKE %s, FALSE)" % (t1, UPLOAD_LIKE) if t1 else "FALSE"
    auto = _system_text("(%s)" % t0) if t0 else "FALSE"
    ctes = [
        src.cte("m_src", start=start_param),
        ("msg AS (SELECT %s AS ts, LOWER(%s) AS email, %s AS is_prompt, %s AS is_upload, %s AS is_auto, %s AS trace, "
         "%s AS file_tag, %s AS file_info FROM m_src m%s)"
         % (ts, user or NULL_STR, prompt, upload, auto, trace or NULL_STR, t1 or NULL_STR, t2 or NULL_STR,
            _dedupe("m", src, insert_id))),
    ]
    caps = {"user": bool(user), "prompt": bool(role and t0), "upload": bool(t1), "trace": bool(trace)}
    return ctes, caps


def act_ctes(src, start_param="prev_start_ts"):
    """CTEs over gemini_enterprise_user_activity. Returns (ctes, caps).

    One row per log entry. Semantics (verified against real activity logs):
    - is_assist (a prompt): an assistant call carrying user text; calls the product makes itself (session
      summaries, injected context) and scheduled runs are not prompts.
    - agent_id (an agent call): the agent an assistant call was addressed to, scheduled runs included and
      product-internal calls excluded. Agent page views and agent edits are not calls; they only contribute
      agent_ref/agent_name, which name agents (see AGENT_NAMES_CTE).
    - is_upload: an upload is logged twice, as a bare request and as the response carrying the new file id;
      only the response is counted.
    """
    s = src.schema
    user = expr("a", s, "jsonPayload.useriamprincipal", "jsonPayload.userIamPrincipal", cast="STRING")
    ue_agent = expr("a", s, "jsonPayload.request.userevent.agentspaceinfo.agentinfo.agentid",
                    "jsonPayload.request.userEvent.agentspaceInfo.agentInfo.agentId", cast="STRING")
    ue_name = expr("a", s, "jsonPayload.request.userevent.agentspaceinfo.agentinfo.name",
                   "jsonPayload.request.userEvent.agentspaceInfo.agentInfo.name", cast="STRING")
    resp_name = expr("a", s, "jsonPayload.response.name", cast="STRING")
    resp_display = expr("a", s, "jsonPayload.response.displayname", "jsonPayload.response.displayName",
                        cast="STRING")
    spec_agent = expr("a", s, "jsonPayload.request.agentsspec.agentspecs[0].agentid",
                      "jsonPayload.request.agentsSpec.agentSpecs[0].agentId", cast="STRING")
    schedule = expr("a", s, "jsonPayload.request.agentsspec.agentspecs[0].scheduleid",
                    "jsonPayload.request.agentsSpec.agentSpecs[0].scheduleId", cast="STRING")
    texts = [t for t in [expr("a", s, "jsonPayload.request.query.text", cast="STRING")]
             + [expr("a", s, "jsonPayload.request.query.parts[%d].text" % i, cast="STRING") for i in range(3)] if t]
    token = expr("a", s, "jsonPayload.response.assistToken", "jsonPayload.response.assisttoken", cast="STRING")
    trace = expr("a", s, "trace", cast="STRING")
    method = expr("a", s, "jsonPayload.logmetadata.methodname", "jsonPayload.logMetadata.methodName",
                  "resource.labels.method", cast="STRING")
    file_id = expr("a", s, "jsonPayload.response.fileid", "jsonPayload.response.fileId", cast="STRING")
    insert_id = expr("a", s, "insertId", cast="STRING")
    refs = [p for p in (spec_agent, ue_agent, "REGEXP_EXTRACT(%s, r'agents/([^/]+)')" % resp_name if resp_name else None)
            if p]
    names = [p for p in (ue_name, resp_display) if p]

    def coalesce(parts):
        return ("COALESCE(%s)" % ", ".join(parts)) if len(parts) > 1 else (parts[0] if parts else NULL_STR)

    text_sql = coalesce(["NULLIF(TRIM(%s), '')" % t for t in texts])
    ts = "a.%s" % quote_ident(src.time_column())
    assist = "IFNULL(REGEXP_CONTAINS(method, r'%s'), FALSE)" % ASSIST_METHODS if method else "FALSE"
    own_call = "%s AND NOT %s" % (assist, _system_text("q_text"))
    prompt = "%s AND q_text IS NOT NULL AND schedule_id IS NULL" % own_call if texts else "FALSE"
    upload = ("IFNULL(REGEXP_CONTAINS(method, r'%s'), FALSE) AND file_id IS NOT NULL" % UPLOAD_METHODS
              if method and file_id else "FALSE")
    ctes = [
        src.cte("a_src", start=start_param),
        ("act_raw AS (SELECT %s AS ts, LOWER(%s) AS email, %s AS method, %s AS q_text, %s AS spec_agent, "
         "%s AS schedule_id, %s AS agent_ref, %s AS agent_name, %s AS token, %s AS trace, %s AS file_id FROM a_src a%s)"
         % (ts, user or NULL_STR, method or NULL_STR, text_sql, spec_agent or NULL_STR, schedule or NULL_STR,
            coalesce(refs), coalesce(names), token or NULL_STR, trace or NULL_STR, file_id or NULL_STR,
            _dedupe("a", src, insert_id))),
        ("act AS (SELECT ts, email, agent_ref, agent_name, token, trace, file_id, %s AS is_assist, "
         "IF(%s, spec_agent, NULL) AS agent_id, %s AS is_upload, "
         "(%s AND %s) OR schedule_id IS NOT NULL AS is_auto FROM act_raw)"
         % (prompt, own_call, upload, assist, _system_text("q_text"))),
    ]
    caps = {"user": bool(user), "agent": bool(method and spec_agent), "token": bool(token), "trace": bool(trace),
            "prompt": bool(method and texts), "upload": bool(method and file_id)}
    return ctes, caps


# Latest known display name per agent id, from any activity entry that names the agent (page views, agent edits).
AGENT_NAMES_CTE = ("act_names AS (SELECT agent_ref, ARRAY_AGG(agent_name IGNORE NULLS ORDER BY ts DESC LIMIT 1)"
                   "[SAFE_OFFSET(0)] AS agent_name FROM act WHERE agent_ref IS NOT NULL GROUP BY agent_ref)")


def agent_label(agent_id, name=None, directory=None):
    """Display name for an agent: logged name, then the agent directory, then a readable form of built-in ids."""
    if name:
        return name
    if directory and directory.get(agent_id):
        return directory[agent_id]
    if agent_id and re.match(r"^[a-z][a-z0-9_]*$", agent_id):
        return agent_id.replace("_", " ").strip().capitalize()
    return agent_id


def ev_cte(has_msg, has_act):
    """Unified event rows (one CTE string, possibly preceded by helper CTEs). Prompts and uploads come from the
    message log when it recorded them in the selected window, otherwise from assist and upload calls in the
    activity log, so that one interaction is never counted twice and an empty message log does not hide usage.
    `human` is 0 for rows the product generated on its own (session summaries, scheduled runs): they still count as
    agent calls but do not make a person active."""
    ctes = []
    use_prompt = use_upload = "FALSE"
    if has_msg:
        ctes.append("msg_used AS (SELECT COUNTIF(is_prompt AND ts >= @start_ts) > 0 AS prompts, "
                    "COUNTIF(is_upload AND ts >= @start_ts) > 0 AS uploads FROM msg)")
        use_prompt = "(SELECT prompts FROM msg_used)"
        use_upload = "(SELECT uploads FROM msg_used)"
    parts = []
    if has_msg:
        parts.append("SELECT ts, email, IF(is_prompt AND %s, 1, 0) AS prompt, IF(is_upload AND %s, 1, 0) AS upload, "
                     "0 AS agent_call, IF(is_auto, 0, 1) AS human FROM msg" % (use_prompt, use_upload))
    if has_act:
        parts.append("SELECT ts, email, IF(is_assist AND NOT %s, 1, 0) AS prompt, "
                     "IF(is_upload AND NOT %s, 1, 0) AS upload, IF(agent_id IS NOT NULL, 1, 0) AS agent_call, "
                     "IF(is_auto, 0, 1) AS human FROM act" % (use_prompt, use_upload))
    ctes.append("ev AS (%s)" % " UNION ALL ".join(parts))
    return ",\n".join(ctes)


def prompt_caps(caps_m, caps_a):
    """(prompts available, uploads available) given the message and activity CTE capabilities."""
    caps_m = caps_m or {}
    caps_a = caps_a or {}
    return (bool(caps_m.get("prompt") or caps_a.get("prompt")), bool(caps_m.get("upload") or caps_a.get("upload")))


def ge_sources(ctx):
    return ctx.audit_source(GE_MESSAGE), ctx.audit_source(GE_ACTIVITY)


def ge_kpis(ctx, msrc, asrc, rng, with_series=True):
    """Runs the KPI/trend query. Returns dict with cur/prev totals and bucket series, or None."""
    ctes = []
    caps_m = caps_a = None
    if msrc:
        c, caps_m = msg_ctes(msrc)
        ctes += c
    if asrc:
        c, caps_a = act_ctes(asrc)
        ctes += c
    if not ctes:
        return None
    ctes.append(ev_cte(bool(msrc), bool(asrc)))
    sql = ("WITH %s\nSELECT period, bucket, SUM(prompt) AS prompts, SUM(upload) AS uploads, "
           "SUM(agent_call) AS agent_calls, COUNT(DISTINCT IF(human = 1 AND %s, email, NULL)) AS users\n"
           "FROM (SELECT ev.*, %s AS period, %s AS bucket FROM ev)\nGROUP BY ROLLUP(period, bucket)"
           % (",\n".join(ctes), human_sql("email"), period_sql("ts"), bucket_or_sentinel("ts")))
    rows = ctx.query(sql, range_params(rng), "gemini_enterprise")
    totals, buckets = split_rollup(rows)
    return {"totals": totals, "buckets": buckets, "caps_m": caps_m, "caps_a": caps_a}


# ---------------------------------------------------------------------------
# GET /api/v1/gemini-enterprise/summary
# ---------------------------------------------------------------------------

def summary(ctx):
    rng = ctx.range
    msrc, asrc = ge_sources(ctx)
    label = (msrc or asrc).label if (msrc or asrc) else "bigquery:%s" % ctx.audit_dataset
    # Timelines and lists stay null unless their source query succeeded.
    result = {
        "kpis": {"activeUsers": {"value": None, "previous": None}, "prompts": {"value": None, "previous": None},
                 "promptsPerUser": None, "fileUploads": {"value": None, "previous": None},
                 "agentCalls": {"value": None, "previous": None}},
        "activity": None, "agentUsage": None, "topUsers": None,
    }
    if not msrc and not asrc:
        return result
    tasks = {"kpi": lambda: ge_kpis(ctx, msrc, asrc, rng)}
    if asrc:
        tasks["agents"] = lambda: _agent_usage(ctx, asrc, rng)
    tasks["top"] = lambda: _top_users(ctx, msrc, asrc, rng)
    res = ctx.run(tasks)

    ok, kpi = res["kpi"]
    if ok and kpi:
        cur = kpi["totals"].get("cur", {})
        prev = kpi["totals"].get("prev", {})
        caps_m = kpi["caps_m"] or {}
        if msrc:
            ctx.add_source(msrc.label)
        if asrc:
            ctx.add_source(asrc.label)
        users_cur = to_int(cur.get("users"))
        users_prev = to_int(prev.get("users"))
        result["kpis"]["activeUsers"] = {"value": users_cur, "previous": users_prev}
        if not msrc or not asrc:
            ctx.warn(label, "partial_data", "Active users are counted from the available Gemini Enterprise "
                                            "log tables only.")
        has_prompts, has_uploads = prompt_caps(caps_m, kpi["caps_a"])
        if has_prompts:
            prompts = to_int(cur.get("prompts"))
            result["kpis"]["prompts"] = {"value": prompts, "previous": to_int(prev.get("prompts"))}
            result["kpis"]["promptsPerUser"] = round(prompts / users_cur, 1) if users_cur else None
        elif msrc:
            ctx.warn(msrc.label, "unsupported_schema", "Prompt fields were not found in the message log schema.")
        if has_uploads:
            result["kpis"]["fileUploads"] = {"value": to_int(cur.get("uploads")),
                                             "previous": to_int(prev.get("uploads"))}
        if asrc and (kpi["caps_a"] or {}).get("agent"):
            result["kpis"]["agentCalls"] = {"value": to_int(cur.get("agent_calls")),
                                            "previous": to_int(prev.get("agent_calls"))}
        series = []
        if has_prompts:
            series.append(timeutil.series("prompts", "Prompts", bucket_values(rng, kpi["buckets"], "prompts")))
        series.append(timeutil.series("activeUsers", "Active users", bucket_values(rng, kpi["buckets"], "users")))
        result["activity"] = timeutil.timeline(rng, series)
    elif not ok:
        ctx.warn_error(label, kpi, "Gemini Enterprise activity")

    if "agents" in res:
        ok, rows = res["agents"]
        if ok:
            if rows is not None:
                result["agentUsage"] = agent_series(rng, rows, directory_for(ctx, rows))
        else:
            ctx.warn_error(asrc.label, rows, "Gemini Enterprise agent activity")
    ok, top = res["top"]
    if ok:
        result["topUsers"] = top
    else:
        ctx.warn_error(label, top, "Gemini Enterprise user activity")
    return result


def _agent_usage(ctx, asrc, rng):
    """Agent calls per bucket and agent (rows), or None when the activity log has no agent fields."""
    ctes, caps = act_ctes(asrc, start_param="start_ts")
    if not caps["agent"]:
        ctx.warn(asrc.label, "unsupported_schema", "Agent fields were not found in the activity log schema.")
        return None
    sql = ("WITH %s,\n%s\nSELECT c.bucket, c.agent_id, ANY_VALUE(n.agent_name) AS agent_name, COUNT(*) AS calls\n"
           "FROM (SELECT agent_id, %s AS bucket FROM act WHERE agent_id IS NOT NULL) c\n"
           "LEFT JOIN act_names n ON n.agent_ref = c.agent_id\nGROUP BY c.bucket, c.agent_id"
           % (",\n".join(ctes), AGENT_NAMES_CTE, bucket_sql("ts")))
    return ctx.query(sql, range_params(rng, with_prev=False), "gemini_enterprise")


def directory_for(ctx, rows):
    """The agent directory, fetched only when an agent in rows has no logged name. Call outside ctx.run tasks."""
    if not any(r.get("agent_id") and not r.get("agent_name") and agent_label(r["agent_id"]) == r["agent_id"]
               for r in rows or []):
        return None
    from .agents import agent_directory  # pylint: disable=import-outside-toplevel (agents imports this module)
    return agent_directory(ctx)


def agent_series(rng, rows, directory=None):
    per_agent = {}
    for row in rows:
        agent = row.get("agent_id")
        if not agent:
            continue
        item = per_agent.setdefault(agent, {"name": None, "values": [0] * len(rng.buckets()), "total": 0})
        if row.get("agent_name") and not item["name"]:
            item["name"] = row["agent_name"]
        idx = rng.bucket_index(row.get("bucket"))
        calls = to_int(row.get("calls"))
        if idx is not None:
            item["values"][idx] += calls
        item["total"] += calls
    ranked = sorted(per_agent.items(), key=lambda kv: (-kv[1]["total"], kv[0]))
    series = [timeutil.series(agent, agent_label(agent, info["name"], directory), info["values"], total=info["total"])
              for agent, info in ranked[:8]]
    rest = ranked[8:]
    if rest:
        values = [sum(info["values"][i] for _, info in rest) for i in range(len(rng.buckets()))]
        series.append(timeutil.series("other", "Other", values, total=sum(values)))
    return timeutil.timeline(rng, series)


def _top_users(ctx, msrc, asrc, rng):
    ctes = []
    if msrc:
        ctes += msg_ctes(msrc, start_param="start_ts")[0]
    if asrc:
        ctes += act_ctes(asrc, start_param="start_ts")[0]
    ctes.append(ev_cte(bool(msrc), bool(asrc)))
    sql = ("WITH %s\nSELECT email, SUM(prompt) AS prompts, SUM(agent_call) AS agent_calls, SUM(upload) AS uploads, "
           "MAX(IF(human = 1, ts, NULL)) AS last_active\nFROM ev WHERE %s\nGROUP BY email\n"
           "ORDER BY prompts DESC, agent_calls DESC, email\nLIMIT 25" % (",\n".join(ctes), human_sql("email")))
    rows = ctx.query(sql, range_params(rng, with_prev=False), "gemini_enterprise")
    return [{"uid": uid_for(r["email"]), "email": r["email"], "prompts": to_int(r.get("prompts")),
             "agentCalls": to_int(r.get("agent_calls")), "fileUploads": to_int(r.get("uploads")),
             "lastActive": iso(r.get("last_active"))} for r in rows if r.get("email")]


# ---------------------------------------------------------------------------
# GET /api/v1/gemini-enterprise/licenses
# ---------------------------------------------------------------------------

def _date_str(value):
    if isinstance(value, dict):
        try:
            return dt.date(int(value.get("year")), int(value.get("month") or 1), int(value.get("day") or 1)).isoformat()
        except (TypeError, ValueError):
            return None
    if isinstance(value, str) and value:
        return value[:10]
    return None


def license_configs(ctx, location="global", project=None):
    """License configurations distributed to this project in one location: (configs, active seats or None)."""
    project = project or ctx.project
    url = "%s/v1alpha/projects/%s/locations/%s/licenseConfigs" % (discovery_host(location), project, location)
    items, _ = ctx.gcp.paginate(url, quota_project=ctx.quota_project, items_key="licenseConfigs", max_pages=5)
    configs = []
    active = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        count = to_int(item.get("licenseCount"))
        cfg = {"name": item.get("name"), "displayName": item.get("displayName"), "state": item.get("state"),
               "licenseCount": count, "subscriptionTier": item.get("subscriptionTier"),
               "startDate": _date_str(item.get("startDate")), "endDate": _date_str(item.get("endDate")),
               "location": location, "project": project}
        configs.append(cfg)
        if item.get("state") == "ACTIVE":
            key = item.get("name") or "config-%d" % len(active)
            active[key] = max(active.get(key, 0), count)
    purchased = sum(active.values()) if active else (0 if configs else None)
    return configs, purchased


def user_licenses(ctx, location="global", project=None):
    project = project or ctx.project
    url = ("%s/v1alpha/projects/%s/locations/%s/userStores/default_user_store/userLicenses"
           % (discovery_host(location), project, location))
    return ctx.gcp.paginate(url, quota_project=ctx.quota_project, items_key="userLicenses", page_size=500,
                            max_pages=20)


def licenses(ctx):
    """Seats in every configured location across scanned projects. Subscriptions are distributed to a project per
    location (global, us, eu), so purchased and assigned seats are summed over DISCOVERY_ENGINE_LOCATIONS."""
    today = timeutil.today_in(ctx.tz_name)
    tz = timeutil.get_zone(ctx.tz_name)
    locations = discovery_locations(ctx)
    projects = getattr(ctx, "projects", None) or [ctx.project]
    out = {"configs": None, "purchased": None, "assigned": None, "unassigned": None, "users": None,
           "idleThresholdDays": IDLE_LICENSE_DAYS, "idleAssigned": None, "userLicensesAvailable": False,
           "locations": locations}
    tasks = {}
    for proj in projects:
        for loc in locations:
            tasks[("configs", proj, loc)] = (lambda where=loc, p=proj: license_configs(ctx, where, p))
            tasks[("users", proj, loc)] = (lambda where=loc, p=proj: user_licenses(ctx, where, p))
    res = ctx.run(tasks)
    configs, purchased, configs_ok = [], None, False
    seen_configs = set()
    users, users_ok = [], False
    seen_users = set()
    for proj in projects:
        is_primary = proj == ctx.project
        for loc in locations:
            ok, value = res[("configs", proj, loc)]
            if ok:
                configs_ok = True
                loc_configs, _loc_purchased = value
                for cfg in loc_configs:
                    ckey = (cfg.get("name") or "", loc)
                    if ckey in seen_configs:
                        continue
                    seen_configs.add(ckey)
                    configs.append(cfg)
                    if cfg.get("state") == "ACTIVE":
                        purchased = (purchased or 0) + to_int(cfg.get("licenseCount"))
                if loc_configs and purchased is None:
                    purchased = 0
                ctx.add_source("api:discoveryengine.licenseConfigs")
            elif is_primary:
                ctx.warn_error("api:discoveryengine.licenseConfigs", value, "The Discovery Engine license API (%s)" % loc)
            ok, value = res[("users", proj, loc)]
            if not ok:
                if is_primary:
                    ctx.warn_error("api:discoveryengine.userLicenses", value,
                                   "The Discovery Engine user license API (%s)" % loc)
                continue
            users_ok = True
            items, truncated = value
            ctx.add_source("api:discoveryengine.userLicenses")
            if truncated and is_primary:
                ctx.warn("api:discoveryengine.userLicenses", "truncated",
                         "Only the first 10000 user licenses in %s are shown." % loc)
            for item in items:
                if not isinstance(item, dict):
                    continue
                email = (item.get("userPrincipal") or "").strip().lower()
                if not email:
                    continue
                ukey = (email, loc)
                if ukey in seen_users:
                    continue
                seen_users.add(ukey)
                last = parse_rfc3339(item.get("lastLoginTime"))
                idle = (today - last.astimezone(tz).date()).days if last else None
                users.append({"uid": uid_for(email), "email": email, "state": item.get("licenseAssignmentState"),
                              "lastLogin": iso(last), "idleDays": idle, "location": loc, "project": proj})
    if configs_ok:
        out["configs"], out["purchased"] = configs, purchased
    if users_ok:
        users.sort(key=lambda u: (-(u["idleDays"] if u["idleDays"] is not None else 10 ** 6), u["email"],
                                  u["location"]))
        out["users"] = users
        out["userLicensesAvailable"] = True
        assigned = [u for u in users if u["state"] == "ASSIGNED"]
        out["assigned"] = len(assigned)
        out["idleAssigned"] = sum(1 for u in assigned if u["idleDays"] is None or u["idleDays"] >= IDLE_LICENSE_DAYS)
        if out["purchased"] is not None:
            out["unassigned"] = max(0, out["purchased"] - out["assigned"])
    return out


# ---------------------------------------------------------------------------
# GET /api/v1/gemini-enterprise/file-uploads
# ---------------------------------------------------------------------------

_DOC_EXT = {".pdf", ".doc", ".docx", ".rtf", ".odt", ".hwp", ".hwpx", ".pages"}
_SHEET_EXT = {".xls", ".xlsx", ".xlsm", ".csv", ".tsv", ".ods", ".numbers"}
_SLIDE_EXT = {".ppt", ".pptx", ".odp", ".key"}
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".svg"}
_TEXT_EXT = {".txt", ".md", ".json", ".xml", ".html", ".htm", ".py", ".js", ".ts", ".java", ".go", ".sql", ".yaml",
             ".yml", ".log"}


def file_category(file_name, mime):
    mime = (mime or "").lower()
    ext = ""
    if file_name and "." in file_name:
        ext = "." + file_name.rsplit(".", 1)[-1].lower()
    if ("pdf" in mime or "msword" in mime or "wordprocessingml" in mime or "opendocument.text" in mime
            or "hwp" in mime or mime == "application/rtf" or ext in _DOC_EXT):
        return "document"
    if "spreadsheet" in mime or "ms-excel" in mime or mime in ("text/csv", "text/tab-separated-values") \
            or ext in _SHEET_EXT:
        return "spreadsheet"
    if "presentation" in mime or "powerpoint" in mime or ext in _SLIDE_EXT:
        return "presentation"
    if mime.startswith("image/") or ext in _IMAGE_EXT:
        return "image"
    if mime.startswith("text/") or mime in ("application/json", "application/xml") or ext in _TEXT_EXT:
        return "text"
    return "other"


def file_items_sql(msrc, asrc, email_filter=False, limit=200):
    ctes, caps = msg_ctes(msrc, start_param="start_ts")
    if not caps["upload"]:
        return None
    join = ""
    user_expr = "up.email"
    if asrc is not None:
        a_ctes, a_caps = act_ctes(asrc, start_param="start_ts")
        if caps["trace"] and a_caps["trace"] and a_caps["user"]:
            ctes += a_ctes
            ctes.append("act_users AS (SELECT trace, ANY_VALUE(email) AS a_email FROM act "
                        "WHERE trace IS NOT NULL AND email IS NOT NULL GROUP BY trace)")
            join = "LEFT JOIN act_users au ON au.trace = up.trace"
            user_expr = "COALESCE(au.a_email, up.email)"
    ctes.append("up AS (SELECT * FROM msg WHERE is_upload)")
    ctes.append(("files AS (SELECT up.ts, %s AS email, TRIM(REGEXP_EXTRACT(up.file_tag, "
                 "r'<start_of_user_uploaded_file:\\s*([^>]+)>')) AS file_name, "
                 "REGEXP_EXTRACT(up.file_info, r'mime type:\\s*(\\S+)') AS mime FROM up %s)") % (user_expr, join))
    where = "WHERE email = @email" if email_filter else "WHERE TRUE"
    sql = ("WITH %s\nSELECT ts, email, file_name, mime, COUNT(*) OVER () AS total FROM (\n"
           "  SELECT * FROM files %s\n  QUALIFY ROW_NUMBER() OVER (PARTITION BY IFNULL(email, ''), "
           "IFNULL(file_name, '') ORDER BY ts DESC) = 1\n)\nORDER BY ts DESC\nLIMIT %d"
           % (",\n".join(ctes), where, int(limit)))
    return sql


_EXT_MIMES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".json": "application/json",
}


def _inf_msgs_json(isrc, alias="i"):
    if isrc is None or isrc.schema is None:
        return None
    jp = isrc.schema.node("jsonPayload")
    msgs = isrc.schema.node("jsonPayload.gen_ai_input_messages")
    if jp is None or msgs is None:
        return None
    return "TO_JSON_STRING(%s.%s.%s)" % (alias, quote_ident(jp.name), quote_ident(msgs.name))


def activity_upload_sql(asrc, email_filter=False, limit=200, isrc=None):
    """Upload events from the activity log, enriched with filenames from gen_ai_client_inference_operation_details when present."""
    ctes, caps = act_ctes(asrc, start_param="start_ts")
    if not caps["upload"]:
        return None
    where = "is_upload AND email = @email" if email_filter else "is_upload"
    msgs_json = _inf_msgs_json(isrc) if (isrc and caps.get("trace") and caps.get("user")) else None
    if msgs_json:
        i_ts = "i.%s" % quote_ident(isrc.time_column())
        i_trace = expr("i", isrc.schema, "trace", cast="STRING")
        i_user = expr("i", isrc.schema, "jsonPayload.user_id", "jsonPayload.user", cast="STRING")
        user_sql = "COALESCE(au.a_email, LOWER(%s))" % i_user if i_user else "au.a_email"
        join_au = ("LEFT JOIN act_users au ON au.trace = %s" % i_trace) if i_trace else ""
        ctes.append("act_users AS (SELECT trace, ANY_VALUE(email) AS a_email FROM act "
                    "WHERE trace IS NOT NULL AND email IS NOT NULL GROUP BY trace)")
        ctes.append(isrc.cte("i_src", start="start_ts"))
        ctes.append(
            "inf_files AS (SELECT %s AS ts, %s AS email, "
            "TRIM(COALESCE(REGEXP_EXTRACT(%s, r'original_filename:\\s*([^,>\"\\\\]+)'), "
            "REGEXP_EXTRACT(%s, r'<start_of_user_uploaded_file:\\s*([^,>\"\\\\]+)'))) AS file_name, "
            "REGEXP_EXTRACT(%s, r'mime type:\\s*([A-Za-z0-9_./+-]+)') AS mime "
            "FROM i_src i %s WHERE %s LIKE %s)"
            % (i_ts, user_sql, msgs_json, msgs_json, msgs_json, join_au, msgs_json, UPLOAD_LIKE)
        )
        ctes.append(
            "act_up AS (SELECT u.ts, u.email, f.file_name, f.mime, u.file_id "
            "FROM (SELECT * FROM act WHERE %s) u "
            "LEFT JOIN inf_files f ON f.email = u.email AND f.file_name IS NOT NULL "
            "AND f.ts BETWEEN TIMESTAMP_SUB(u.ts, INTERVAL 1 MINUTE) AND TIMESTAMP_ADD(u.ts, INTERVAL 30 MINUTE) "
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY u.file_id, u.ts "
            "ORDER BY ABS(TIMESTAMP_DIFF(f.ts, u.ts, SECOND)) ASC NULLS LAST) = 1)" % where
        )
        return ("WITH %s\nSELECT ts, email, file_name, mime, file_id, COUNT(*) OVER () AS total FROM act_up\n"
                "ORDER BY ts DESC\nLIMIT %d" % (",\n".join(ctes), int(limit)))
    return ("WITH %s\nSELECT ts, email, CAST(NULL AS STRING) AS file_name, CAST(NULL AS STRING) AS mime, file_id, "
            "COUNT(*) OVER () AS total FROM act WHERE %s\nORDER BY ts DESC\nLIMIT %d"
            % (",\n".join(ctes), where, int(limit)))


ACTIVITY_UPLOAD_NOTE = ("File names are not recorded in the activity log. Enable the Gemini Enterprise message log "
                        "to see file names.")


def file_rows_to_items(rows):
    items = []
    for row in rows:
        email = row.get("email")
        name = row.get("file_name")
        mime = row.get("mime")
        if name and "." in name:
            ext = "." + name.rsplit(".", 1)[-1].lower()
            if not mime or (ext in (".xlsx", ".xls") and mime == "text/csv"):
                mime = _EXT_MIMES.get(ext, mime)
        category = file_category(name, mime)
        items.append({"time": iso(row.get("ts")), "uid": uid_for(email) if email else None, "email": email,
                      "fileName": name, "fileId": row.get("file_id"), "mimeType": mime, "category": category,
                      "reviewRecommended": category in ("document", "spreadsheet", "presentation"),
                      "restricted": False})
    return items


def file_uploads(ctx):
    rng = ctx.range
    msrc, asrc = ge_sources(ctx)
    isrc = ctx.audit_source(GE_INFERENCE, optional=True)
    params = range_params(rng, with_prev=False)
    rows = None
    if msrc:
        sql = file_items_sql(msrc, asrc)
        if sql is None:
            if not asrc:
                ctx.warn(msrc.label, "unsupported_schema", "File upload fields were not found in the message log schema.")
        else:
            try:
                rows = ctx.query(sql, params, "gemini_enterprise")
                ctx.add_source(msrc.label)
            except GcpError as err:
                ctx.warn_error(msrc.label, err, "Gemini Enterprise file uploads")
    if not rows and asrc:
        # The message log is optional: fall back to upload calls in the activity log (enriched with inference details).
        a_sql = activity_upload_sql(asrc, isrc=isrc)
        if a_sql:
            try:
                a_rows = ctx.query(a_sql, params, "gemini_enterprise")
            except GcpError as err:
                ctx.warn_error(asrc.label, err, "Gemini Enterprise activity logs")
            else:
                ctx.add_source(asrc.label)
                if isrc:
                    ctx.add_source(isrc.label)
                if a_rows:
                    rows = a_rows
                elif rows is None:
                    rows = []
    if rows is None:
        return {"items": None, "total": None}
    total = to_int(rows[0].get("total")) if rows else 0
    return {"items": file_rows_to_items(rows), "total": total}
