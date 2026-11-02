"""Agent Registry and Agent Deployments."""

from __future__ import annotations

import datetime as dt
import logging
import re

from .. import config, timeutil
from ..gcp import GcpError
from ..security import uid_for
from .base import (AUDIT_ACTIVITY, AUDIT_DATA_ACCESS, GE_ACTIVITY, discovery_host, iso, money, parse_rfc3339,
                   range_params, to_int)
from .billing import billing_fields
from .gemini_enterprise import AGENT_NAMES_CTE, act_ctes, agent_label
from .notebooklm import audit_fields

LOG = logging.getLogger("controlplane.agents")

ENGINE_NAME_RE = re.compile(r"^projects/[A-Za-z0-9\-]+/locations/[a-z0-9\-]+/collections/[A-Za-z0-9_\-]+/engines/"
                            r"[A-Za-z0-9_\-]+$")
ASSISTANT_NAME_RE = re.compile(r"^projects/[A-Za-z0-9\-]+/locations/[a-z0-9\-]+/collections/[A-Za-z0-9_\-]+/engines/"
                               r"[A-Za-z0-9_\-]+/assistants/[A-Za-z0-9_\-]+$")
MAX_ENGINES = 25
ATTRIBUTION_NOTE = ("Model calls per identity are exact (Cloud Audit Logs). Tokens and cost are allocated "
                    "proportionally by call share of each model.")
MODEL_METHOD_RE = r"(GenerateContent|StreamGenerateContent|Predict|RawPredict|StreamRawPredict|ChatCompletions)"


de_host = discovery_host


def _short_after(name, marker):
    if not name or marker not in name:
        return None
    return name[name.index(marker):]


def _agent_type_de(agent):
    keys = " ".join(k.lower() for k in agent.keys())
    if "adkagent" in keys or "adk_agent" in keys:
        return "ADK"
    if "a2aagent" in keys or "a2a_agent" in keys:
        return "A2A"
    if "dialogflow" in keys:
        return "Dialogflow"
    if "managedagent" in keys or "managed_agent" in keys:
        return "Built-in"
    if "lowcode" in keys or "nocode" in keys or "low_code" in keys:
        return "No-code"
    return None


# ---------------------------------------------------------------------------
# API inventory
# ---------------------------------------------------------------------------

def reasoning_engines(ctx):
    regions = ctx.ws.get("agent_engine_regions") or []
    projects = getattr(ctx, "projects", None) or [ctx.project]
    tasks = {}
    for project in projects:
        for region in regions:
            if not config.LOCATION_RE.match(region or ""):
                continue
            url = "https://%s-aiplatform.googleapis.com/v1/projects/%s/locations/%s/reasoningEngines" % (
                region, project, region)
            tasks[(project, region)] = (lambda u=url: ctx.gcp.paginate(
                u, quota_project=ctx.quota_project, items_key="reasoningEngines", page_size=100, max_pages=10))
    engines = []
    ok_count = 0
    res = ctx.run(tasks)
    for (project, region), (ok, value) in res.items():
        if not ok:
            if project == ctx.project:
                ctx.warn_error("api:aiplatform.reasoningEngines", value, "Agent Engine (Agent Platform) in %s" % region)
            continue
        ok_count += 1
        ctx.add_source("api:aiplatform.reasoningEngines")
        items, truncated = value
        if truncated and project == ctx.project:
            ctx.warn("api:aiplatform.reasoningEngines", "truncated", "Only the first 1000 reasoning engines per "
                                                                     "region are listed.")
        for item in items:
            if isinstance(item, dict) and item.get("name"):
                enriched = dict(item, _project=project)
                engines.append((region, enriched))
    return engines, ok_count


def _engine_agents(ctx, location, engine_name, warn=True):
    host = de_host(location)
    out = []
    assistants, _ = ctx.gcp.paginate("%s/v1alpha/%s/assistants" % (host, engine_name),
                                     quota_project=ctx.quota_project, items_key="assistants", max_pages=3)
    for assistant in assistants:
        name = (assistant or {}).get("name") or ""
        if not ASSISTANT_NAME_RE.match(name):
            continue
        out.append(("assistant", assistant))
        try:
            agents, _ = ctx.gcp.paginate("%s/v1alpha/%s/agents" % (host, name), quota_project=ctx.quota_project,
                                         items_key="agents", page_size=100, max_pages=5)
        except GcpError as err:
            if warn:
                ctx.warn_error("api:discoveryengine.agents", err, "Discovery Engine agents")
            continue
        for agent in agents:
            if isinstance(agent, dict) and agent.get("name"):
                out.append(("agent", agent))
    return out


def discovery_agents(ctx):
    locations = ctx.ws.get("discovery_engine_locations") or []
    projects = getattr(ctx, "projects", None) or [ctx.project]
    found = []
    ok_count = 0
    loc_tasks = {}
    for project in projects:
        for location in locations:
            if not config.LOCATION_RE.match(location or ""):
                continue
            url = "%s/v1alpha/projects/%s/locations/%s/collections/default_collection/engines" % (
                de_host(location), project, location)
            loc_tasks[(project, location)] = (lambda u=url: ctx.gcp.paginate(
                u, quota_project=ctx.quota_project, items_key="engines", page_size=100, max_pages=3))
    loc_res = ctx.run(loc_tasks)
    engine_tasks = {}
    engine_meta = {}
    for (project, location), (ok, value) in loc_res.items():
        is_primary = project == ctx.project
        if not ok:
            if is_primary:
                ctx.warn_error("api:discoveryengine.engines", value, "Discovery Engine in %s" % location)
            continue
        ctx.add_source("api:discoveryengine.engines")
        ok_count += 1
        engines, _ = value
        engine_by_name = {e.get("name"): e for e in engines if isinstance(e, dict) and ENGINE_NAME_RE.match(e.get("name") or "")}
        names = list(engine_by_name.keys())
        if len(names) > MAX_ENGINES and is_primary:
            ctx.warn("api:discoveryengine.engines", "truncated", "Only the first %d engines are inspected." % MAX_ENGINES)
        for name in names[:MAX_ENGINES]:
            key = (project, location, name)
            engine_meta[key] = engine_by_name[name]
            engine_tasks[key] = (lambda n=name, loc=location, w=is_primary: _engine_agents(ctx, loc, n, warn=w))
    for (project, location, name), (ok, value) in ctx.run(engine_tasks).items():
        is_primary = project == ctx.project
        eng = engine_meta[(project, location, name)]
        if not ok:
            if eng.get("solutionType") == "SOLUTION_TYPE_SEARCH":
                found.append((location, "search_engine", dict(eng, _project=project)))
            elif is_primary:
                ctx.warn_error("api:discoveryengine.agents", value, "Discovery Engine assistants")
            continue
        ctx.add_source("api:discoveryengine.agents")
        if not value and eng.get("solutionType") == "SOLUTION_TYPE_SEARCH":
            found.append((location, "search_engine", dict(eng, _project=project)))
        for kind, item in value:
            found.append((location, kind, dict(item, _project=project)))
    return found, ok_count


# ---------------------------------------------------------------------------
# Activity enrichment
# ---------------------------------------------------------------------------

def _lookback_start(ctx, rng):
    dep = int(ctx.settings["agent_lifecycle"]["deprecation_days"])
    look = max(dep, 90) + 1
    start = min(rng.start_ts(), rng.end_ts() - dt.timedelta(days=look))
    return start


def activity_calls(ctx, rng, asrc):
    ctes, caps = act_ctes(asrc, start_param="look_start_ts")
    if not caps["agent"]:
        return {}
    sql = ("WITH %s,\n%s\nSELECT c.agent_id, ANY_VALUE(n.agent_name) AS agent_name, "
           "COUNTIF(c.ts >= @start_ts AND c.ts < @end_ts) AS calls, MAX(c.ts) AS last_call "
           "FROM act c LEFT JOIN act_names n ON n.agent_ref = c.agent_id WHERE c.agent_id IS NOT NULL "
           "GROUP BY c.agent_id" % (",\n".join(ctes), AGENT_NAMES_CTE))
    params = range_params(rng, with_prev=False)
    params["look_start_ts"] = _lookback_start(ctx, rng)
    params["sfx_start"] = (params["look_start_ts"] - dt.timedelta(days=1)).strftime("%Y%m%d")
    rows = ctx.query(sql, params, "agent_registry")
    return {r["agent_id"]: dict(r, agent_name=agent_label(r["agent_id"], r.get("agent_name")))
            for r in rows if r.get("agent_id")}


def agent_directory(ctx):
    """{agent id: display name} for Gemini Enterprise agents in the configured locations.

    Best effort and silent: it only names agents seen in activity logs; the registry page reports listing errors.
    Uses ctx.run internally, so call it from a request thread or a run_composite task, never from ctx.run tasks."""
    def load(child):
        names = {}
        try:
            found, _ = child.memo("discovery_agents", "all", discovery_agents, ttl=300)
        except Exception:  # pylint: disable=broad-except
            found = []
        for _location, kind, obj in found:
            if kind == "agent" and obj.get("displayName"):
                names[(obj.get("name") or "").rsplit("/", 1)[-1]] = obj["displayName"]
        del child.warnings[:]
        child.sources.clear()
        return names
    return ctx.memo("agent_directory", (), load)


def engine_calls(ctx, rng, dsrc):
    f = audit_fields(dsrc)
    if not (f["method"] and f["resource"]):
        return {}
    sql = ("WITH %s\nSELECT REGEXP_EXTRACT(%s, r'reasoningEngines/([^/:]+)') AS engine, "
           "COUNTIF(%s >= @start_ts) AS calls, COUNTIF(%s >= @prev_start_ts AND %s < @start_ts) AS prev_calls, "
           "COUNT(DISTINCT IF(%s >= @start_ts, %s, NULL)) AS callers, "
           "COUNTIF(%s >= @start_ts AND IFNULL(%s, 0) != 0) AS errors, MAX(%s) AS last_call\n"
           "FROM da_src t WHERE %s LIKE '%%ReasoningEngineExecutionService%%' GROUP BY engine"
           % (dsrc.cte("da_src", start="look_start_ts"), f["resource"], f["ts"], f["ts"], f["ts"], f["ts"],
              f["principal"] or "CAST(NULL AS STRING)", f["ts"], f["code"] or "0", f["ts"], f["method"]))
    params = range_params(rng)
    params["look_start_ts"] = min(_lookback_start(ctx, rng), params["prev_start_ts"])
    params["sfx_start"] = (params["look_start_ts"] - dt.timedelta(days=1)).strftime("%Y%m%d")
    rows = ctx.query(sql, params, "agent_registry")
    return {r["engine"]: r for r in rows if r.get("engine")}


def creation_events(ctx, rng, asrc):
    f = audit_fields(asrc)
    if not (f["method"] and f["resource"]):
        return []
    resource = "IFNULL(%s, '')" % f["resource"]
    response = "IFNULL(%s, '')" % f["response"] if f["response"] else "''"
    sql = ("WITH %s\nSELECT %s AS ts, LOWER(%s) AS principal, "
           "COALESCE(REGEXP_EXTRACT(%s, r'reasoningEngines/([^/:]+)'), REGEXP_EXTRACT(%s, r'reasoningEngines/([^/\"]+)')) "
           "AS engine, COALESCE(REGEXP_EXTRACT(%s, r'/agents/([^/:]+)'), REGEXP_EXTRACT(%s, r'/agents/([^/\"]+)')) AS agent\n"
           "FROM ac_src t WHERE %s LIKE '%%CreateReasoningEngine%%' OR %s LIKE '%%AgentService.CreateAgent%%' "
           "OR %s LIKE '%%CreateAgent%%'\nORDER BY ts DESC LIMIT 5000"
           % (asrc.cte("ac_src", start="create_start_ts"), f["ts"], f["principal"] or "CAST(NULL AS STRING)",
              resource, response, resource, response, f["method"], f["method"], f["method"]))
    params = range_params(rng, with_prev=False)
    params["create_start_ts"] = min(rng.start_ts(), rng.end_ts() - dt.timedelta(days=400))
    params["sfx_start"] = (params["create_start_ts"] - dt.timedelta(days=1)).strftime("%Y%m%d")
    return ctx.query(sql, params, "agent_registry")


# ---------------------------------------------------------------------------
# GET /api/v1/agent-platform/registry
# ---------------------------------------------------------------------------

def _status(days_idle, thresholds):
    if days_idle is None:
        return "idle"
    if days_idle >= thresholds["deprecationDays"]:
        return "deprecation_candidate"
    if days_idle >= thresholds["idleDays"]:
        return "idle"
    return "active"


def registry_data(ctx, rng):
    lifecycle = ctx.settings["agent_lifecycle"]
    thresholds = {"idleDays": int(lifecycle["idle_days"]), "deprecationDays": int(lifecycle["deprecation_days"])}
    asrc = ctx.audit_source(GE_ACTIVITY)
    dsrc = ctx.audit_source(AUDIT_DATA_ACCESS)
    acsrc = ctx.audit_source(AUDIT_ACTIVITY)
    tasks = {
        "engines": lambda: ctx.memo("reasoning_engines", "all", reasoning_engines, ttl=300),
        "de": lambda: ctx.memo("discovery_agents", "all", discovery_agents, ttl=300),
    }
    if asrc:
        tasks["activity"] = lambda: activity_calls(ctx, rng, asrc)
    if dsrc:
        tasks["engine_calls"] = lambda: engine_calls(ctx, rng, dsrc)
    if acsrc:
        tasks["created"] = lambda: creation_events(ctx, rng, acsrc)
    res = ctx.run(tasks)

    succeeded = set()

    def take(name, default, label, what):
        if name not in res:
            return default
        ok, value = res[name]
        if ok:
            succeeded.add(name)
            if label:
                ctx.add_source(label)
            return value
        ctx.warn_error(label or "api", value, what)
        return default

    engines, engine_ok = take("engines", ([], 0), None, "Agent Engine (Agent Platform)")
    de_items, de_ok = take("de", ([], 0), None, "Discovery Engine")
    activity = take("activity", {}, asrc.label if asrc else None, "Gemini Enterprise activity logs")
    ecalls = take("engine_calls", {}, dsrc.label if dsrc else None, "Cloud Audit data access logs")
    created = take("created", [], acsrc.label if acsrc else None, "Cloud Audit activity logs")
    available = bool(engine_ok or de_ok or "activity" in succeeded)
    creation_available = "created" in succeeded
    activity_ok = "activity" in succeeded
    engine_calls_ok = "engine_calls" in succeeded

    owners = {}
    for row in sorted(created, key=lambda r: r.get("ts") or dt.datetime.min.replace(tzinfo=dt.timezone.utc)):
        for key in (("engine", row.get("engine")), ("agent", row.get("agent"))):
            if key[1] and key not in owners:
                owners[key] = row.get("principal")
    today = timeutil.today_in(ctx.tz_name)
    tz = timeutil.get_zone(ctx.tz_name)
    agents = []
    matched_activity = set()

    def finish(item, last_call):
        update = parse_rfc3339(item.get("updateTime")) or parse_rfc3339(item.get("createTime"))
        candidates = [t for t in (update, last_call) if t is not None]
        last = max(candidates) if candidates else None
        item["lastActivity"] = iso(last)
        item["daysIdle"] = (today - last.astimezone(tz).date()).days if last else None
        item["status"] = _status(item["daysIdle"], thresholds)
        agents.append(item)

    for region, engine in engines:
        name = engine.get("name")
        engine_id = name.rsplit("/", 1)[-1]
        spec = engine.get("spec") or {}
        framework = (spec.get("agentFramework") or "").lower()
        atype = "ADK" if "adk" in framework else ("A2A" if "a2a" in framework else "Reasoning Engine")
        calls = ecalls.get(engine_id) or {}
        proj = engine.get("_project") or ctx.project
        item = {"id": "reasoningEngines/%s" % engine_id, "name": engine.get("displayName") or engine_id,
                "description": engine.get("description") or "", "platform": "agent_engine", "type": atype,
                "project": proj, "region": region, "owner": owners.get(("engine", engine_id)),
                "identity": spec.get("effectiveIdentity") or spec.get("serviceAccount") or None,
                "createTime": iso(parse_rfc3339(engine.get("createTime"))),
                "updateTime": iso(parse_rfc3339(engine.get("updateTime"))),
                "calls": to_int(calls.get("calls")) if engine_calls_ok else None, "resourceName": name}
        finish(item, calls.get("last_call"))
    for location, kind, obj in de_items:
        name = obj.get("name") or ""
        short = _short_after(name, "engines/") or name
        if kind == "assistant":
            atype = "Assistant"
            calls = {}
        else:
            atype = _agent_type_de(obj)
            agent_id = name.rsplit("/", 1)[-1]
            calls = activity.get(agent_id) or {}
            if calls:
                matched_activity.add(agent_id)
        proj = obj.get("_project") or ctx.project
        item = {"id": short, "name": obj.get("displayName") or name.rsplit("/", 1)[-1],
                "description": obj.get("description") or "", "platform": "gemini_enterprise", "type": atype,
                "project": proj, "region": location,
                "owner": owners.get(("agent", name.rsplit("/", 1)[-1])) if kind == "agent" else None,
                "identity": None, "createTime": iso(parse_rfc3339(obj.get("createTime"))),
                "updateTime": iso(parse_rfc3339(obj.get("updateTime"))),
                "calls": to_int(calls.get("calls")) if (kind != "assistant" and activity_ok) else None,
                "resourceName": name}
        finish(item, calls.get("last_call"))
    for agent_id, row in activity.items():
        if agent_id in matched_activity:
            continue
        item = {"id": "observed/%s" % agent_id, "name": row.get("agent_name") or agent_id, "description": "",
                "platform": "gemini_enterprise_observed", "type": None, "project": ctx.project, "region": None,
                "owner": owners.get(("agent", agent_id)), "identity": None, "createTime": None, "updateTime": None,
                "calls": to_int(row.get("calls")), "resourceName": None}
        finish(item, row.get("last_call"))
    agents.sort(key=lambda a: (-(a["calls"] or 0), a["name"] or ""))

    created_in_range = [r for r in created if r.get("ts") and rng.start_ts() <= r["ts"] < rng.end_ts()]
    trend_pairs = [(r["ts"].astimezone(tz).date(), 1) for r in created_in_range]
    creators = {}
    for row in created_in_range:
        principal = row.get("principal")
        if not principal:
            continue
        entry = creators.setdefault(principal, {"uid": uid_for(principal), "email": principal, "created": 0,
                                                "lastCreated": None, "agents": []})
        entry["created"] += 1
        when = iso(row.get("ts"))
        entry["lastCreated"] = max(entry["lastCreated"] or when, when)
        agent_ref = ("reasoningEngines/%s" % row["engine"]) if row.get("engine") else (
            ("agents/%s" % row["agent"]) if row.get("agent") else None)
        if agent_ref and agent_ref not in entry["agents"]:
            entry["agents"].append(agent_ref)
    return {"agents": agents, "thresholds": thresholds,
            "createdInRange": len(created_in_range) if creation_available else None,
            "trend": timeutil.fold(rng, trend_pairs, int) if creation_available else None,
            "creators": creators if creation_available else None,
            "engines": engines, "engineCalls": ecalls, "available": available, "enginesAvailable": bool(engine_ok),
            "engineCallsAvailable": "engine_calls" in succeeded}


def registry(ctx):
    rng = ctx.range
    data = ctx.memo("registry_data", rng, lambda c: registry_data(c, rng))
    agents = data["agents"]
    avail = data["available"]
    return {"kpis": {"total": len(agents) if avail else None,
                     "active": sum(1 for a in agents if a["status"] == "active") if avail else None,
                     "idle": sum(1 for a in agents if a["status"] == "idle") if avail else None,
                     "deprecationCandidates": sum(1 for a in agents if a["status"] == "deprecation_candidate")
                     if avail else None,
                     "createdInRange": data["createdInRange"]},
            "agents": agents if avail else None,
            "creationTrend": timeutil.timeline(rng, [timeutil.series("created", "Created", data["trend"])])
            if data["trend"] is not None else None,
            "creators": sorted(data["creators"].values(), key=lambda c: (-c["created"], c["email"]))
            if data["creators"] is not None else None,
            "thresholds": data["thresholds"]}


# ---------------------------------------------------------------------------
# GET /api/v1/agent-platform/deployments
# ---------------------------------------------------------------------------

def runtime_costs(ctx, rng):
    bsrc = ctx.billing_source()
    if bsrc is None:
        return None
    f = billing_fields(bsrc)
    parts = [p for p in (f["resource_global"], f["resource_name"]) if p]
    if not parts:
        ctx.warn(bsrc.label, "unsupported_schema", "Runtime cost requires the detailed (resource-level) billing export.")
        return None
    text = "CONCAT(%s)" % ", ' ', ".join("IFNULL(%s, '')" % p for p in parts)
    engine = "REGEXP_EXTRACT(%s, r'reasoningEngines/([^/\\s]+)')" % text
    sql = ("WITH %s\nSELECT %s AS engine, SUM(IFNULL(%s, 0)) AS cost, ANY_VALUE(%s) AS currency FROM bil b "
           "WHERE %s IS NOT NULL GROUP BY engine" % (bsrc.cte("bil", start="start_ts"), engine, f["cost"],
                                                       f["currency"] or "CAST(NULL AS STRING)", engine))
    rows = ctx.query(sql, range_params(rng, with_prev=False), "agent_deployments")
    ctx.add_source(bsrc.label)
    return {r["engine"]: r for r in rows if r.get("engine")}


def identity_usage(ctx, rng):
    dsrc = ctx.audit_source(AUDIT_DATA_ACCESS)
    if dsrc is None:
        return None
    f = audit_fields(dsrc)
    if not (f["service"] and f["method"] and f["resource"] and f["principal"]):
        ctx.warn(dsrc.label, "unsupported_schema", "Audit log fields were not found in the table schema.")
        return None
    sql = ("WITH %s,\nm AS (SELECT LOWER(%s) AS principal, REGEXP_EXTRACT(%s, r'models/([^/:@]+)') AS model "
           "FROM da_src t WHERE %s = 'aiplatform.googleapis.com' AND REGEXP_CONTAINS(%s, r'%s'))\n"
           "SELECT principal, model, COUNT(*) AS calls, SUM(COUNT(*)) OVER (PARTITION BY model) AS model_calls "
           "FROM m WHERE model IS NOT NULL AND principal IS NOT NULL GROUP BY principal, model "
           "ORDER BY calls DESC, principal LIMIT 100"
           % (dsrc.cte("da_src", start="start_ts"), f["principal"], f["resource"], f["service"], f["method"],
              MODEL_METHOD_RE))
    rows = ctx.query(sql, range_params(rng, with_prev=False), "agent_deployments")
    ctx.add_source(dsrc.label)
    return rows


def deployments(ctx):
    rng = ctx.range
    from . import models as models_service
    out = {"kpis": {"deployments": None, "regions": None, "identities": None,
                    "invocations": {"value": None, "previous": None}, "runtimeCost": None},
           "deployments": None, "identityUsage": None, "attributionNote": ATTRIBUTION_NOTE}
    res = ctx.run_composite({
        "registry": lambda: ctx.memo("registry_data", rng, lambda c: registry_data(c, rng)),
        "costs": lambda: runtime_costs(ctx, rng),
        "identity": lambda: identity_usage(ctx, rng),
        "models": lambda: ctx.memo("models_usage", rng, lambda c: models_service.usage_data(c, rng))})
    ok, reg = res["registry"]
    if not ok:
        ctx.warn_error("api:aiplatform.reasoningEngines", reg, "Agent Engine (Agent Platform)")
        reg = {"engines": [], "engineCalls": {}, "enginesAvailable": False, "engineCallsAvailable": False}
    ok, costs = res["costs"]
    if not ok:
        ctx.warn_error("bigquery:billing", costs, "The billing export table")
        costs = None
    rows = []
    total_inv = total_prev = 0
    cost_total = 0.0 if costs is not None else None
    for region, engine in reg["engines"]:
        engine_id = engine["name"].rsplit("/", 1)[-1]
        spec = engine.get("spec") or {}
        dep_spec = spec.get("deploymentSpec") or {}
        pkg = spec.get("packageSpec") or {}
        calls = reg["engineCalls"].get(engine_id) or {}
        inv = to_int(calls.get("calls"))
        total_inv += inv
        total_prev += to_int(calls.get("prev_calls"))
        cost_row = (costs or {}).get(engine_id)
        cost = money(cost_row.get("cost")) if cost_row else (0.0 if costs is not None else None)
        if cost is not None and cost_total is not None:
            cost_total += cost
        proj = engine.get("_project") or ctx.project
        rows.append({"id": "reasoningEngines/%s" % engine_id, "name": engine.get("displayName") or engine_id,
                     "project": proj, "region": region, "framework": spec.get("agentFramework") or None,
                     "pythonVersion": pkg.get("pythonVersion") or None,
                     "identity": spec.get("effectiveIdentity") or spec.get("serviceAccount") or None,
                     "minInstances": dep_spec.get("minInstances") if isinstance(dep_spec.get("minInstances"), int)
                     else None,
                     "maxInstances": dep_spec.get("maxInstances") if isinstance(dep_spec.get("maxInstances"), int)
                     else None,
                     "createTime": iso(parse_rfc3339(engine.get("createTime"))),
                     "updateTime": iso(parse_rfc3339(engine.get("updateTime"))),
                     "invocations": inv, "callers": to_int(calls.get("callers")), "errors": to_int(calls.get("errors")),
                     "runtimeCost": cost, "currency": (cost_row or {}).get("currency") or "USD"})
    rows.sort(key=lambda r: (-(r["invocations"] or 0), r["name"]))
    has_calls = bool(reg.get("engineCallsAvailable"))
    engines_ok = bool(reg.get("enginesAvailable"))
    if not has_calls:
        for row in rows:
            row["invocations"] = None
            row["callers"] = None
            row["errors"] = None
    out["deployments"] = rows if engines_ok else None
    out["kpis"] = {"deployments": len(rows) if engines_ok else None,
                   "regions": len({r["region"] for r in rows}) if engines_ok else None,
                   "identities": len({r["identity"] for r in rows if r["identity"]}) if engines_ok else None,
                   "invocations": {"value": total_inv if has_calls else None,
                                   "previous": total_prev if has_calls else None},
                   "runtimeCost": money(cost_total) if cost_total is not None else None}
    ok, id_rows = res["identity"]
    ok_m, mdata = res["models"]
    model_totals = {}
    if ok_m:
        for key, entry in mdata["models"].items():
            tokens, tsource = models_service._model_tokens(entry)
            # Unknown token or cost figures stay null (never allocated as zero).
            model_totals[key] = (tokens["total"] if tsource or entry["agy"] else None,
                                 entry["cost"] if mdata["status"]["billing"] else None)
    if ok and id_rows is not None:
        usage = []
        for row in id_rows:
            principal = row.get("principal")
            if not principal:
                continue
            raw_model = row.get("model")
            key = models_service.normalize_model_id(raw_model)
            calls = to_int(row.get("calls"))
            model_calls = to_int(row.get("model_calls")) or calls
            share = calls / model_calls if model_calls else 0.0
            tokens, cost = model_totals.get(key, (None, None))
            human = "@" in principal and not principal.endswith(".gserviceaccount.com") and \
                not principal.startswith("principal://")
            usage.append({"principal": principal, "type": "user" if human else "service_account", "model": key,
                          "calls": calls,
                          "allocatedTokens": int(round(tokens * share)) if tokens is not None else None,
                          "allocatedCost": round(cost * share, 4) if cost is not None else None,
                          "attribution": "proportional_by_calls"})
        out["identityUsage"] = usage
    elif not ok:
        ctx.warn_error("bigquery:%s.%s" % (ctx.audit_dataset, AUDIT_DATA_ACCESS), id_rows, "Cloud Audit data access logs")
    return out
