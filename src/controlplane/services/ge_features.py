"""Gemini Enterprise feature management, app model selection, and AI Developer Tools policy inventory.

1. App feature switches & model selection (Discovery Engine v1 — read & bulk write):
   Each Gemini Enterprise app is a Discovery Engine engine with two admin override maps:
   - `features` (key -> FeatureState): opts the app in or out of web-app features.
   - `modelConfigs` (modelId -> ModelState): overrides individual Gemini, image, and partner models.

       GET   https://{host}/v1/projects/{p}/locations/{l}/collections/default_collection/engines
       PATCH https://{host}/v1/projects/{p}/locations/{l}/collections/{c}/engines/{id}?updateMask=...

   With `updateMask=features` or `updateMask=modelConfigs`, the map in the request replaces the whole map, so a
   change is never sent as a partial map: each engine is read again right before the update and only the keys an
   administrator changed are merged into the map Google returned. Concurrent console changes are preserved.
   When readable, `widgetConfigs/default_search_widget_config` (v1alpha) supplies the live end-user resolved
   model list (`uiSettings.modelConfigInfo.resolvedModels`) alongside the admin overrides.

   Feature keys starting with "disable-" are restrictions (FEATURE_STATE_ON blocks the capability) and "*" overrides
   every other switch; the dashboard shows them as they are and never inverts a value. An absent key follows the
   product default.

2. AI Developer Tools & Antigravity authorized models (Discovery Engine v1alpha — read-only inventory):
       GET https://{host}/v1alpha/projects/{p}/locations/{l}/devToolsConfig

   Reads project/location-level AI developer tools settings across every workspace project and location (global, us,
   eu, plus any configured Discovery Engine locations): enablement, compliance logging, security policies, browser
   access, MCP allowlist, plugin restrictions, and Antigravity authorized Google & third-party models.
   Google Cloud currently restricts `UpdateDevToolsConfig` writes to Google Cloud Console (external REST calls return
   404 Method not found), so the dashboard audits all projects live and links directly to Cloud Console for edits.
"""

from __future__ import annotations

import logging
import re

from .. import config
from ..gcp import GcpError
from ..httpkit import ApiError
from . import governance
from .base import discovery_host, discovery_locations, require_identity_admin

LOG = logging.getLogger("controlplane.ge_features")

SOURCE = "api:discoveryengine.engines"
WIDGET_SOURCE = "api:discoveryengine.widgetConfigs"
DEV_TOOLS_SOURCE = "api:discoveryengine.devToolsConfig"
COLLECTION = "default_collection"
API_VERSION = "v1"
ALPHA_VERSION = "v1alpha"
WIDGET_CONFIG_ID = "default_search_widget_config"

ON, OFF = "FEATURE_STATE_ON", "FEATURE_STATE_OFF"
STATE_OUT = {ON: "on", OFF: "off"}
STATE_IN = {"on": ON, "off": OFF}

MODEL_ON, MODEL_OFF = "MODEL_ENABLED", "MODEL_DISABLED"
MODEL_STATE_OUT = {MODEL_ON: "on", MODEL_OFF: "off"}
MODEL_STATE_IN = {"on": MODEL_ON, "off": MODEL_OFF}

DEFAULT = "default"
ALL_FEATURES = "*"

# Feature keys: "*" or lowercase words joined by hyphens (agent-gallery, disable-image-generation). \Z, not $:
# $ also matches before a trailing newline.
FEATURE_KEY_RE = re.compile(r"^(\*|[a-z][a-z0-9]*(?:-[a-z0-9]+){0,15})\Z")
# Model IDs: lowercase alphanumeric identifiers with hyphens, dots or underscores (gemini-4-argon,
# gemini-3.1-pro-preview, gemini-3.1-flash-image-preview, claude-opus-5-5).
MODEL_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}\Z")
ENGINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,62}\Z")
ENGINE_NAME_RE = re.compile(r"^projects/([A-Za-z0-9\-]+)/locations/([a-z0-9\-]+)/collections/([A-Za-z0-9_\-]+)/"
                            r"engines/([A-Za-z0-9][A-Za-z0-9_\-]{0,62})\Z")
MAX_KEY_LEN = 80
MAX_APPS_PER_APPLY = 100
MAX_KEYS_PER_APP = 64
MAX_MODELS_PER_APP = 64
MAX_ENGINE_PAGES = 5
MAX_WIDGET_PROBES = 50
INTRANET = "APP_TYPE_INTRANET"

DEV_TOOLS_DEFAULT_LOCATIONS = ("global", "us", "eu")

# Verified DevToolsConfig (v1alpha) protobuf enum mappings.
FILE_ACCESS_OUT = {
    "FILE_ACCESS_POLICY_DENY": "deny",
    "FILE_ACCESS_POLICY_ALWAYS_ASK": "always_ask",
    "FILE_ACCESS_POLICY_NOT_ENFORCED": "allow",
}
TERMINAL_AUTO_EXEC_OUT = {
    "TERMINAL_COMMAND_AUTO_EXECUTION_POLICY_REQUIRE_REVIEW": "require_review",
    "TERMINAL_COMMAND_AUTO_EXECUTION_POLICY_PROCEED_IN_SANDBOX": "proceed_in_sandbox",
    "TERMINAL_COMMAND_AUTO_EXECUTION_POLICY_NOT_ENFORCED": "always_proceed",
}
JS_EXEC_OUT = {
    "JAVASCRIPT_EXECUTION_POLICY_DISABLED": "disabled",
    "JAVASCRIPT_EXECUTION_POLICY_REQUEST_REVIEW": "request_review",
    "JAVASCRIPT_EXECUTION_POLICY_NOT_ENFORCED": "allowed",
}


def _engines_url(project, location):
    return "%s/%s/projects/%s/locations/%s/collections/%s/engines" % (
        discovery_host(location), API_VERSION, project, location, COLLECTION)


def _engine_url(location, name):
    return "%s/%s/%s" % (discovery_host(location), API_VERSION, name)


def _widget_config_url(location, name):
    return "%s/%s/%s/widgetConfigs/%s" % (discovery_host(location), ALPHA_VERSION, name, WIDGET_CONFIG_ID)


def _dev_tools_url(project, location):
    return "%s/%s/projects/%s/locations/%s/devToolsConfig" % (
        discovery_host(location), ALPHA_VERSION, project, location)


def _workspace_projects(ctx):
    out = list(getattr(ctx, "all_projects", None) or [])
    if ctx.project not in out:
        out.append(ctx.project)
    return out


def _api_disabled(err):
    """The Discovery Engine API is not enabled in the project (it then has no Gemini Enterprise apps)."""
    text = getattr(err, "message", "") or ""
    return getattr(err, "kind", "") == "denied" and (
        "SERVICE_DISABLED" in text or "has not been used in project" in text or "it is disabled" in text)


def _clean_features(raw):
    """{key: "on"|"off"} of an engine's features map; unspecified states and malformed keys are dropped."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        if isinstance(key, str) and len(key) <= MAX_KEY_LEN and FEATURE_KEY_RE.match(key) and value in STATE_OUT:
            out[key] = STATE_OUT[value]
    return out


def _clean_models(raw):
    """{modelId: "on"|"off"} of an engine's modelConfigs map; unspecified states and malformed IDs are dropped."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        if isinstance(key, str) and len(key) <= MAX_KEY_LEN and MODEL_KEY_RE.match(key) and value in MODEL_STATE_OUT:
            out[key] = MODEL_STATE_OUT[value]
    return out


def is_ge_app(engine):
    """A Gemini Enterprise app: marked as an intranet app, or already carries feature switches or model overrides."""
    if not isinstance(engine, dict):
        return False
    return (engine.get("appType") == INTRANET
            or bool(_clean_features(engine.get("features")))
            or bool(_clean_models(engine.get("modelConfigs"))))


def _app(engine, project, location):
    match = ENGINE_NAME_RE.match(engine.get("name") or "")
    if not match:
        return None
    _, _, collection, engine_id = match.groups()
    features = _clean_features(engine.get("features"))
    models = _clean_models(engine.get("modelConfigs"))
    search = engine.get("searchEngineConfig") if isinstance(engine.get("searchEngineConfig"), dict) else {}
    return {
        "key": "%s/%s/%s" % (project, location, engine_id),
        "project": project,
        "location": location,
        "collection": collection,
        "engineId": engine_id,
        "displayName": str(engine.get("displayName") or engine_id)[:200],
        "createTime": engine.get("createTime"),
        "updateTime": engine.get("updateTime"),
        "subscriptionTier": search.get("requiredSubscriptionTier") or None,
        "features": features,
        "allFeatures": features.get(ALL_FEATURES),
        "models": models,
        "resolvedModels": [],
        "defaultModelId": None,
        "_name": engine.get("name"),
    }


def _extract_widget_models(raw):
    """Parse WidgetConfig.uiSettings.modelConfigInfo into cleaned resolved model descriptors."""
    if not isinstance(raw, dict):
        return [], None
    ui = raw.get("uiSettings") if isinstance(raw.get("uiSettings"), dict) else {}
    info = ui.get("modelConfigInfo") if isinstance(ui.get("modelConfigInfo"), dict) else {}
    default_id = info.get("defaultModelId")
    if not isinstance(default_id, str) or not MODEL_KEY_RE.match(default_id):
        default_id = None
    items = info.get("resolvedModels") if isinstance(info.get("resolvedModels"), list) else []
    resolved = []
    seen = set()
    for entry in items[:64]:
        if not isinstance(entry, dict):
            continue
        mid = entry.get("modelId")
        if not isinstance(mid, str) or not mid or not MODEL_KEY_RE.match(mid) or mid in seen:
            continue
        seen.add(mid)
        resolved.append({
            "modelId": mid,
            "displayName": str(entry.get("displayName") or mid)[:120],
            "label": str(entry.get("label") or entry.get("displayName") or mid)[:80],
            "description": str(entry.get("description") or "")[:240] or None,
            "isPreview": bool(entry.get("isPreview")),
            "family": str(entry.get("family") or "")[:60] or None,
        })
    return resolved, default_id


def _enrich_widget_models(ctx, apps):
    """Best-effort fetch of WidgetConfig.uiSettings.modelConfigInfo for discovered GE apps."""
    targets = [a for a in apps if a.get("_name")][:MAX_WIDGET_PROBES]
    if not targets:
        return {}
    tasks = {}
    for app in targets:
        url = _widget_config_url(app["location"], app["_name"])
        tasks[app["key"]] = (lambda u=url: ctx.rest("GET", u, timeout=10))
    meta = {}
    by_key = {a["key"]: a for a in targets}
    for key, (ok, value) in ctx.run(tasks).items():
        if not ok or not isinstance(value, dict):
            continue
        resolved, default_id = _extract_widget_models(value)
        app = by_key.get(key)
        if not app:
            continue
        app["resolvedModels"] = [m["modelId"] for m in resolved]
        app["defaultModelId"] = default_id
        for item in resolved:
            if item["modelId"] not in meta:
                meta[item["modelId"]] = {
                    "displayName": item["displayName"],
                    "label": item["label"],
                    "description": item["description"],
                    "isPreview": item["isPreview"],
                    "family": item["family"],
                }
    if meta:
        ctx.add_source(WIDGET_SOURCE)
    return meta


# ---------------------------------------------------------------------------
# GET /api/v1/gemini-enterprise/features
# ---------------------------------------------------------------------------

def inventory(ctx):
    """Every Gemini Enterprise app of the selected projects in every configured location, with its switches and models.

    apps: [{key, project, location, collection, engineId, displayName, createTime, updateTime, subscriptionTier,
    features: {key: "on"|"off"}, allFeatures, models: {modelId: "on"|"off"}, resolvedModels: [modelId],
    defaultModelId}] (None when no list could be read); keys: every feature key set on any app; modelKeys: every
    model ID set or resolved on any app; resolvedModelMeta: live display metadata from WidgetConfig; otherEngines:
    engines that are not Gemini Enterprise apps (search-only apps); complete: every list was read."""
    projects = list(getattr(ctx, "projects", None) or [ctx.project])
    locations = discovery_locations(ctx)
    tasks = {}
    for project in projects:
        for location in locations:
            tasks[(project, location)] = (lambda u=_engines_url(project, location): ctx.gcp.paginate(
                u, quota_project=ctx.quota_project, items_key="engines", page_size=100,
                max_pages=MAX_ENGINE_PAGES))
    apps, keys, model_keys, other, read, complete = [], set(), set(), 0, 0, True
    unreadable = {}  # linked project -> locations whose list failed
    for (project, location), (ok, value) in sorted(ctx.run(tasks).items()):
        if not ok:
            # A project without Gemini Enterprise in this location, or without the Discovery Engine API, has no apps.
            if isinstance(value, GcpError) and (value.kind == "not_found" or _api_disabled(value)):
                read += 1
                continue
            complete = False
            if project == ctx.project:
                ctx.warn_error(SOURCE, value, "Discovery Engine apps in %s" % location)
            else:
                unreadable.setdefault(project, []).append(location)
            LOG.warning("engine list failed project=%s location=%s kind=%s", project, location,
                        getattr(value, "kind", type(value).__name__))
            continue
        read += 1
        ctx.add_source(SOURCE)
        items, truncated = value
        if truncated:
            complete = False
            ctx.warn(SOURCE, "truncated", "Only the first %d engines per location are listed." % (
                100 * MAX_ENGINE_PAGES))
        for engine in items or []:
            if not is_ge_app(engine):
                other += 1
                continue
            app = _app(engine, project, location)
            if app:
                apps.append(app)
                keys.update(app["features"])
                model_keys.update(app["models"])
    if unreadable:
        ctx.warn(SOURCE, "partial_data", "Gemini Enterprise apps in %d linked project(s) could not be read: %s. Grant "
                                         "the runtime service account Discovery Engine Viewer there." % (
                                             len(unreadable), ", ".join(sorted(unreadable))))
    apps.sort(key=lambda a: (a["project"] != ctx.project, a["project"], a["location"], a["displayName"].lower()))
    resolved_meta = _enrich_widget_models(ctx, apps) if apps else {}
    for app in apps:
        model_keys.update(app.get("resolvedModels") or [])
        app.pop("_name", None)
    return {
        "apps": apps if read else None,
        "keys": sorted(keys),
        "modelKeys": sorted(model_keys),
        "resolvedModelMeta": resolved_meta,
        "otherEngines": other,
        "complete": complete,
        "projects": projects,
        "locations": locations,
        "apiVersion": API_VERSION,
        "unreadableProjects": sorted(unreadable),
    }


# ---------------------------------------------------------------------------
# POST /api/v1/gemini-enterprise/features/apply
# ---------------------------------------------------------------------------

def _parse_changes(ctx, body):
    """[(project, location, engine_id, {featureKey: state}, {modelId: state})] from the request, validated against the
    workspace projects and configured locations. Raises ApiError(bad_request)."""
    changes = body.get("changes") if isinstance(body, dict) else None
    if not isinstance(changes, list) or not changes:
        raise ApiError("bad_request", "Field 'changes' must be a non-empty list.")
    if len(changes) > MAX_APPS_PER_APPLY:
        raise ApiError("bad_request", "At most %d apps can be changed at once." % MAX_APPS_PER_APPLY)
    projects = set(_workspace_projects(ctx))
    locations = set(discovery_locations(ctx))
    out, seen = [], set()
    for item in changes:
        if not isinstance(item, dict):
            raise ApiError("bad_request", "Each change must be an object.")
        project = str(item.get("project") or "").strip().lower()
        location = str(item.get("location") or "").strip().lower()
        engine_id = str(item.get("engineId") or "").strip()
        if not config.valid_project(project) or project not in projects:
            raise ApiError("bad_request", "The project is not part of this workspace.")
        if not config.LOCATION_RE.match(location) or location not in locations:
            raise ApiError("bad_request", "The location is not a configured Gemini Enterprise location.")
        if not ENGINE_ID_RE.match(engine_id):
            raise ApiError("bad_request", "The app ID is invalid.")
        target = (project, location, engine_id)
        if target in seen:
            raise ApiError("bad_request", "Each app can appear only once.")
        seen.add(target)

        updates = item.get("set")
        model_updates = item.get("setModels")
        if updates is not None and not isinstance(updates, dict):
            raise ApiError("bad_request", "Field 'set' must be an object.")
        if model_updates is not None and not isinstance(model_updates, dict):
            raise ApiError("bad_request", "Field 'setModels' must be an object.")
        if not updates and not model_updates:
            raise ApiError("bad_request", "Field 'set' or 'setModels' must name at least one change.")
        if updates and len(updates) > MAX_KEYS_PER_APP:
            raise ApiError("bad_request", "At most %d features can be changed per app." % MAX_KEYS_PER_APP)
        if model_updates and len(model_updates) > MAX_MODELS_PER_APP:
            raise ApiError("bad_request", "At most %d models can be changed per app." % MAX_MODELS_PER_APP)

        clean_features = {}
        for key, value in (updates or {}).items():
            if not isinstance(key, str) or len(key) > MAX_KEY_LEN or not FEATURE_KEY_RE.match(key):
                raise ApiError("bad_request", "A feature key is invalid.")
            if value not in ("on", "off", DEFAULT):
                raise ApiError("bad_request", "A feature state must be on, off or default.")
            clean_features[key] = value

        clean_models = {}
        for key, value in (model_updates or {}).items():
            if not isinstance(key, str) or len(key) > MAX_KEY_LEN or not MODEL_KEY_RE.match(key):
                raise ApiError("bad_request", "A model ID is invalid.")
            if value not in ("on", "off", DEFAULT):
                raise ApiError("bad_request", "A model state must be on, off or default.")
            clean_models[key] = value

        out.append((project, location, engine_id, clean_features, clean_models))
    return out


def merge(current, updates):
    """The FeatureState map after applying {key: "on"|"off"|"default"} to `current` (a FeatureState map)."""
    merged = {k: v for k, v in (current or {}).items() if isinstance(k, str) and v in STATE_OUT}
    for key, value in updates.items():
        if value == DEFAULT:
            merged.pop(key, None)
        else:
            merged[key] = STATE_IN[value]
    return merged


def merge_models(current, updates):
    """The ModelState map after applying {modelId: "on"|"off"|"default"} to `current` (a ModelState map)."""
    merged = {k: v for k, v in (current or {}).items() if isinstance(k, str) and v in MODEL_STATE_OUT}
    for key, value in updates.items():
        if value == DEFAULT:
            merged.pop(key, None)
        else:
            merged[key] = MODEL_STATE_IN[value]
    return merged


def _apply_one(ctx, project, location, engine_id, feature_updates, model_updates):
    """Reads the engine, merges changed switches and/or model overrides, and patches in one call. Never raises."""
    name = "projects/%s/locations/%s/collections/%s/engines/%s" % (project, location, COLLECTION, engine_id)
    result = {
        "key": "%s/%s/%s" % (project, location, engine_id),
        "project": project,
        "location": location,
        "engineId": engine_id,
        "requested": dict(feature_updates),
        "requestedModels": dict(model_updates),
    }
    try:
        engine = ctx.rest("GET", _engine_url(location, name), timeout=30) or {}
    except GcpError as err:
        LOG.warning("feature read failed engine=%s kind=%s", engine_id, err.kind)
        return dict(result, status="failed", error=err.kind, stage="read")
    # Patch the resource name Google returned (it may carry the project number); it must still be this engine.
    match = ENGINE_NAME_RE.match(engine.get("name") or "")
    if not match or match.group(2) != location or match.group(4) != engine_id:
        return dict(result, status="failed", error="not_found", stage="read")

    before_f = engine.get("features") if isinstance(engine.get("features"), dict) else {}
    before_m = engine.get("modelConfigs") if isinstance(engine.get("modelConfigs"), dict) else {}
    clean_before_f = {k: v for k, v in before_f.items() if v in STATE_OUT}
    clean_before_m = {k: v for k, v in before_m.items() if v in MODEL_STATE_OUT}

    after_f = merge(before_f, feature_updates) if feature_updates else clean_before_f
    after_m = merge_models(before_m, model_updates) if model_updates else clean_before_m

    masks, body = [], {}
    if feature_updates and after_f != clean_before_f:
        masks.append("features")
        body["features"] = after_f
    if model_updates and after_m != clean_before_m:
        masks.append("modelConfigs")
        body["modelConfigs"] = after_m

    if not masks:
        return dict(result, status="unchanged", features=_clean_features(after_f), models=_clean_models(after_m))

    try:
        updated = ctx.rest("PATCH", _engine_url(location, engine["name"]),
                           params={"updateMask": ",".join(masks)}, json_body=body, timeout=45) or {}
    except GcpError as err:
        LOG.warning("feature/model update failed engine=%s mask=%s kind=%s", engine_id, ",".join(masks), err.kind)
        return dict(result, status="failed", error=err.kind, stage="update",
                    before=_clean_features(before_f), beforeModels=_clean_models(before_m))

    features = updated.get("features") if isinstance(updated.get("features"), dict) else after_f
    models = updated.get("modelConfigs") if isinstance(updated.get("modelConfigs"), dict) else after_m
    return dict(
        result,
        status="updated",
        before=_clean_features(before_f),
        features=_clean_features(features),
        beforeModels=_clean_models(before_m),
        models=_clean_models(models),
        updateTime=updated.get("updateTime"),
    )


def apply(ctx):
    """POST /api/v1/gemini-enterprise/features/apply
    {changes: [{project, location, engineId, set?: {featureKey: state}, setModels?: {modelId: state}}]}

    Applies feature switch and/or model selection changes to every listed app in parallel. Each app is read again
    and only the listed keys change ("default" removes the key so the product default applies). One governance audit
    entry records every app's outcome. Returns {results, updated, unchanged, failed, auditLogged}."""
    require_identity_admin(ctx)
    targets = _parse_changes(ctx, ctx.body or {})
    tasks = {
        (p, l, e): (lambda p=p, l=l, e=e, f=f, m=m: _apply_one(ctx, p, l, e, f, m))
        for p, l, e, f, m in targets
    }
    outcomes = ctx.run(tasks)
    results = []
    for project, location, engine_id, f_updates, m_updates in targets:
        ok, value = outcomes.get((project, location, engine_id), (False, None))
        if not ok or not isinstance(value, dict):
            value = {
                "key": "%s/%s/%s" % (project, location, engine_id),
                "project": project,
                "location": location,
                "engineId": engine_id,
                "requested": dict(f_updates),
                "requestedModels": dict(m_updates),
                "status": "failed",
                "error": "internal",
            }
        results.append(value)
    counts = {s: sum(1 for r in results if r["status"] == s) for s in ("updated", "unchanged", "failed")}
    audit_apps = []
    for r in results:
        entry = {"target": r["key"], "status": r["status"], "error": r.get("error"), "changes": r["requested"]}
        if r.get("requestedModels"):
            entry["modelChanges"] = r["requestedModels"]
        audit_apps.append(entry)
    logged = governance.write_audit(ctx, "ge_features_update", {
        "result": "success" if not counts["failed"] else ("partial" if counts["updated"] else "failed"),
        "apps": audit_apps,
        **counts,
    })
    cache = getattr(getattr(ctx, "app", None), "cache", None)
    if cache is not None:
        cache.clear()
    return dict(results=results, auditLogged=logged, **counts)


# ---------------------------------------------------------------------------
# GET /api/v1/gemini-enterprise/dev-tools
# ---------------------------------------------------------------------------

def _dev_tools_locations(ctx):
    """Locations to probe for DevToolsConfig: global, us, eu plus any configured Discovery Engine locations."""
    locs = list(DEV_TOOLS_DEFAULT_LOCATIONS)
    for loc in discovery_locations(ctx):
        if loc not in locs:
            locs.append(loc)
    return locs


def _clean_model_list(raw):
    """Sanitized, deduplicated list of model IDs from DevToolsConfig.modelConfig."""
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for item in raw[:64]:
        if isinstance(item, str) and len(item) <= MAX_KEY_LEN and MODEL_KEY_RE.match(item) and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _sanitize_json(value, depth=0, max_depth=5, max_items=64, max_str=300):
    """Bounded deep copy for nested protobuf Struct / dynamic fields returned by Discovery Engine."""
    if depth >= max_depth:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:max_str]
    if isinstance(value, list):
        return [_sanitize_json(v, depth + 1, max_depth, max_items, max_str) for v in value[:max_items]]
    if isinstance(value, dict):
        out = {}
        for idx, (k, v) in enumerate(value.items()):
            if idx >= max_items:
                break
            if isinstance(k, str) and len(k) <= 100:
                out[k] = _sanitize_json(v, depth + 1, max_depth, max_items, max_str)
        return out
    return str(value)[:max_str]


def _extract_mcp_servers(allowlist):
    """Extract local & remote server IDs from DevToolsConfig.mcpConfig.mcpAllowlist."""
    if not isinstance(allowlist, dict):
        return [], []
    root = allowlist.get("mcpServers") if isinstance(allowlist.get("mcpServers"), dict) else allowlist
    local_raw = root.get("local_servers") if isinstance(root.get("local_servers"), list) else []
    remote_raw = root.get("remote_servers") if isinstance(root.get("remote_servers"), list) else []
    local_ids, remote_ids = [], []
    for entry in local_raw[:50]:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"].strip():
            local_ids.append(entry["id"].strip()[:100])
    for entry in remote_raw[:50]:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"].strip():
            remote_ids.append(entry["id"].strip()[:100])
    return local_ids, remote_ids


def _parse_dev_tools_config(raw, project, location):
    """Normalize a Discovery Engine v1alpha DevToolsConfig payload into a stable dashboard schema."""
    raw = raw if isinstance(raw, dict) else {}
    log_cfg = raw.get("loggingConfig") if isinstance(raw.get("loggingConfig"), dict) else {}
    agent_cfg = raw.get("agentConfig") if isinstance(raw.get("agentConfig"), dict) else {}
    browser_cfg = raw.get("browserConfig") if isinstance(raw.get("browserConfig"), dict) else {}
    mcp_cfg = raw.get("mcpConfig") if isinstance(raw.get("mcpConfig"), dict) else {}
    plugin_cfg = raw.get("pluginConfig") if isinstance(raw.get("pluginConfig"), dict) else {}
    model_cfg = raw.get("modelConfig") if isinstance(raw.get("modelConfig"), dict) else {}
    overage_cfg = raw.get("overageConfig") if isinstance(raw.get("overageConfig"), dict) else {}
    feature_cfg = raw.get("featureConfig") if isinstance(raw.get("featureConfig"), dict) else {}

    file_raw = agent_cfg.get("outsideWorkspaceFileAccessPolicy")
    term_raw = agent_cfg.get("terminalCommandAutoExecutionPolicy")
    js_raw = browser_cfg.get("javascriptExecutionPolicy")

    allowlist = _sanitize_json(mcp_cfg.get("mcpAllowlist")) if isinstance(mcp_cfg.get("mcpAllowlist"), dict) else None
    local_ids, remote_ids = _extract_mcp_servers(allowlist)

    allowed_models = _clean_model_list(model_cfg.get("allowedModels"))
    allowed_3p = _clean_model_list(model_cfg.get("allowedThirdPartyModels"))

    browser_extra = {
        k: _sanitize_json(v) for k, v in browser_cfg.items()
        if k not in ("browserEnabled", "javascriptExecutionPolicy") and v not in (None, "", [], {})
    }
    feature_extra = {
        k: _sanitize_json(v) for k, v in feature_cfg.items()
        if v not in (None, "", [], {})
    }

    has_any = any(
        k != "name" and raw.get(k) not in (None, False, {}, [])
        for k in raw
    )
    return {
        "key": "%s/%s" % (project, location),
        "project": project,
        "location": location,
        "resourceName": str(raw.get("name") or "")[:240] or None,
        "devToolsEnabled": bool(raw.get("devToolsEnabled")),
        "hasAnyConfig": has_any,
        "logging": {
            "metadataLoggingEnabled": bool(log_cfg.get("metadataLoggingEnabled")),
            "promptAndResponseLoggingEnabled": bool(log_cfg.get("promptAndResponseLoggingEnabled")),
        },
        "agent": {
            "outsideWorkspaceFileAccessPolicy": FILE_ACCESS_OUT.get(file_raw, DEFAULT),
            "outsideWorkspaceFileAccessPolicyRaw": file_raw if isinstance(file_raw, str) else None,
            "terminalCommandAutoExecutionPolicy": TERMINAL_AUTO_EXEC_OUT.get(term_raw, DEFAULT),
            "terminalCommandAutoExecutionPolicyRaw": term_raw if isinstance(term_raw, str) else None,
            "sandboxModeEnabled": bool(agent_cfg.get("sandboxModeEnabled")),
        },
        "browser": {
            "browserEnabled": bool(browser_cfg.get("browserEnabled")),
            "javascriptExecutionPolicy": JS_EXEC_OUT.get(js_raw, DEFAULT),
            "javascriptExecutionPolicyRaw": js_raw if isinstance(js_raw, str) else None,
            "extra": browser_extra,
        },
        "mcp": {
            "mcpEnabled": bool(mcp_cfg.get("mcpEnabled")),
            "hasAllowlist": bool(allowlist),
            "localServerCount": len(local_ids),
            "remoteServerCount": len(remote_ids),
            "localServerIds": local_ids,
            "remoteServerIds": remote_ids,
            "mcpAllowlist": allowlist,
        },
        "plugins": {
            "pluginsDisabled": bool(plugin_cfg.get("pluginsDisabled")),
            "marketplaceDisabled": bool(plugin_cfg.get("marketplaceDisabled")),
        },
        "models": {
            "mode": "allowlist" if allowed_models else "all_gemini",
            "allowedModels": allowed_models,
            "allowedThirdPartyModels": allowed_3p,
        },
        "overage": {
            "overageEnabled": bool(overage_cfg["overageEnabled"]) if "overageEnabled" in overage_cfg else None,
        },
        "features": feature_extra,
    }


def dev_tools_inventory(ctx):
    """GET /api/v1/gemini-enterprise/dev-tools

    Reads Discovery Engine v1alpha `devToolsConfig` for every selected project and location (global, us, eu, and any
    configured Discovery Engine locations). Returns live AI Developer Tools settings and Antigravity authorized
    models per (project, location)."""
    projects = list(getattr(ctx, "projects", None) or [ctx.project])
    locations = _dev_tools_locations(ctx)
    tasks = {}
    for project in projects:
        for location in locations:
            url = _dev_tools_url(project, location)
            tasks[(project, location)] = (lambda u=url: ctx.rest("GET", u, timeout=15))

    configs, observed_models, observed_3p, read, complete = [], set(), set(), 0, True
    unreadable = {}
    for (project, location), (ok, value) in sorted(ctx.run(tasks).items()):
        if not ok:
            if isinstance(value, GcpError) and (value.kind == "not_found" or _api_disabled(value)):
                read += 1
                continue
            complete = False
            if project == ctx.project:
                ctx.warn_error(DEV_TOOLS_SOURCE, value, "AI developer tools settings in %s" % location)
            else:
                unreadable.setdefault(project, []).append(location)
            LOG.warning("devToolsConfig read failed project=%s location=%s kind=%s", project, location,
                        getattr(value, "kind", type(value).__name__))
            continue
        read += 1
        ctx.add_source(DEV_TOOLS_SOURCE)
        parsed = _parse_dev_tools_config(value, project, location)
        configs.append(parsed)
        observed_models.update(parsed["models"]["allowedModels"])
        observed_3p.update(parsed["models"]["allowedThirdPartyModels"])

    if unreadable:
        ctx.warn(
            DEV_TOOLS_SOURCE,
            "partial_data",
            "AI developer tools settings in %d linked project(s) could not be read: %s. Grant the runtime service "
            "account Discovery Engine Viewer there." % (len(unreadable), ", ".join(sorted(unreadable))),
        )
    loc_order = {loc: idx for idx, loc in enumerate(locations)}
    configs.sort(key=lambda c: (c["project"] != ctx.project, c["project"], loc_order.get(c["location"], 99)))
    return {
        "configs": configs if read else None,
        "projects": projects,
        "locations": locations,
        "observedModels": sorted(observed_models),
        "observedThirdPartyModels": sorted(observed_3p),
        "complete": complete,
        "unreadableProjects": sorted(unreadable),
        "apiVersion": ALPHA_VERSION,
        "writeSupported": False,
        "writeSupportReason": "console_only",
    }
