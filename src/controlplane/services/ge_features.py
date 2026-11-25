"""Gemini Enterprise feature management: the feature switches of every Gemini Enterprise app in one view, and
changes applied to many apps at once.

Each Gemini Enterprise app is a Discovery Engine engine. Its `features` map (key -> FeatureState) opts the app in or
out of product features (agent gallery, model selector, NotebookLM, image generation, ...):

    GET   https://{host}/v1/projects/{p}/locations/{l}/collections/default_collection/engines
    PATCH https://{host}/v1/projects/{p}/locations/{l}/collections/{c}/engines/{id}?updateMask=features

With updateMask=features the map in the request replaces the whole map, so a change is never sent as a partial map:
the engine is read again right before the update and only the switches an administrator changed are merged into the
map Google returned. A switch someone else changed in the console meanwhile is kept.

Keys are passed through as Google defines them (reference: Engine.features). Keys starting with "disable-" are
restrictions (FEATURE_STATE_ON blocks the capability) and the key "*" overrides every other switch; the dashboard
shows them as they are and never inverts a value. A key absent from the map follows the product default.
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
COLLECTION = "default_collection"
API_VERSION = "v1"
ON, OFF = "FEATURE_STATE_ON", "FEATURE_STATE_OFF"
STATE_OUT = {ON: "on", OFF: "off"}
STATE_IN = {"on": ON, "off": OFF}
DEFAULT = "default"
ALL_FEATURES = "*"

# Feature keys: "*" or lowercase words joined by hyphens (agent-gallery, disable-image-generation). \Z, not $:
# $ also matches before a trailing newline.
FEATURE_KEY_RE = re.compile(r"^(\*|[a-z][a-z0-9]*(?:-[a-z0-9]+){0,15})\Z")
ENGINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,62}\Z")
ENGINE_NAME_RE = re.compile(r"^projects/([A-Za-z0-9\-]+)/locations/([a-z0-9\-]+)/collections/([A-Za-z0-9_\-]+)/"
                            r"engines/([A-Za-z0-9][A-Za-z0-9_\-]{0,62})\Z")
MAX_KEY_LEN = 80
MAX_APPS_PER_APPLY = 100
MAX_KEYS_PER_APP = 64
MAX_ENGINE_PAGES = 5
INTRANET = "APP_TYPE_INTRANET"


def _engines_url(project, location):
    return "%s/%s/projects/%s/locations/%s/collections/%s/engines" % (
        discovery_host(location), API_VERSION, project, location, COLLECTION)


def _engine_url(location, name):
    return "%s/%s/%s" % (discovery_host(location), API_VERSION, name)


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


def is_ge_app(engine):
    """A Gemini Enterprise app: the API marks it as an intranet app, or it already carries feature switches."""
    if not isinstance(engine, dict):
        return False
    return engine.get("appType") == INTRANET or bool(_clean_features(engine.get("features")))


def _app(engine, project, location):
    match = ENGINE_NAME_RE.match(engine.get("name") or "")
    if not match:
        return None
    _, _, collection, engine_id = match.groups()
    features = _clean_features(engine.get("features"))
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
    }


# ---------------------------------------------------------------------------
# GET /api/v1/gemini-enterprise/features
# ---------------------------------------------------------------------------

def inventory(ctx):
    """Every Gemini Enterprise app of the selected projects in every configured location, with its switches.

    apps: [{key, project, location, collection, engineId, displayName, createTime, updateTime, subscriptionTier,
    features: {key: "on"|"off"}, allFeatures}] (None when no list could be read); keys: every key set on any app;
    otherEngines: engines that are not Gemini Enterprise apps (search-only apps); complete: every list was read."""
    projects = list(getattr(ctx, "projects", None) or [ctx.project])
    locations = discovery_locations(ctx)
    tasks = {}
    for project in projects:
        for location in locations:
            tasks[(project, location)] = (lambda u=_engines_url(project, location): ctx.gcp.paginate(
                u, quota_project=ctx.quota_project, items_key="engines", page_size=100,
                max_pages=MAX_ENGINE_PAGES))
    apps, keys, other, read, complete = [], set(), 0, 0, True
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
    if unreadable:
        ctx.warn(SOURCE, "partial_data", "Gemini Enterprise apps in %d linked project(s) could not be read: %s. Grant "
                                         "the runtime service account Discovery Engine Viewer there." % (
                                             len(unreadable), ", ".join(sorted(unreadable))))
    apps.sort(key=lambda a: (a["project"] != ctx.project, a["project"], a["location"], a["displayName"].lower()))
    return {"apps": apps if read else None, "keys": sorted(keys), "otherEngines": other, "complete": complete,
            "projects": projects, "locations": locations, "apiVersion": API_VERSION,
            "unreadableProjects": sorted(unreadable)}


# ---------------------------------------------------------------------------
# POST /api/v1/gemini-enterprise/features/apply
# ---------------------------------------------------------------------------

def _parse_changes(ctx, body):
    """[(project, location, engine_id, {key: "on"|"off"|"default"})] from the request, validated against the
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
        if not isinstance(updates, dict) or not updates:
            raise ApiError("bad_request", "Field 'set' must name at least one feature.")
        if len(updates) > MAX_KEYS_PER_APP:
            raise ApiError("bad_request", "At most %d features can be changed per app." % MAX_KEYS_PER_APP)
        clean = {}
        for key, value in updates.items():
            if not isinstance(key, str) or len(key) > MAX_KEY_LEN or not FEATURE_KEY_RE.match(key):
                raise ApiError("bad_request", "A feature key is invalid.")
            if value not in ("on", "off", DEFAULT):
                raise ApiError("bad_request", "A feature state must be on, off or default.")
            clean[key] = value
        out.append((project, location, engine_id, clean))
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


def _apply_one(ctx, project, location, engine_id, updates):
    """Reads the engine, merges the changed switches and patches the features map. Never raises."""
    name = "projects/%s/locations/%s/collections/%s/engines/%s" % (project, location, COLLECTION, engine_id)
    result = {"key": "%s/%s/%s" % (project, location, engine_id), "project": project, "location": location,
              "engineId": engine_id, "requested": dict(updates)}
    try:
        engine = ctx.rest("GET", _engine_url(location, name), timeout=30) or {}
    except GcpError as err:
        LOG.warning("feature read failed engine=%s kind=%s", engine_id, err.kind)
        return dict(result, status="failed", error=err.kind, stage="read")
    # Patch the resource name Google returned (it may carry the project number); it must still be this engine.
    match = ENGINE_NAME_RE.match(engine.get("name") or "")
    if not match or match.group(2) != location or match.group(4) != engine_id:
        return dict(result, status="failed", error="not_found", stage="read")
    before = engine.get("features") if isinstance(engine.get("features"), dict) else {}
    after = merge(before, updates)
    clean_before = {k: v for k, v in before.items() if v in STATE_OUT}
    if after == clean_before:
        return dict(result, status="unchanged", features=_clean_features(after))
    try:
        updated = ctx.rest("PATCH", _engine_url(location, engine["name"]), params={"updateMask": "features"},
                           json_body={"features": after}, timeout=45) or {}
    except GcpError as err:
        LOG.warning("feature update failed engine=%s kind=%s", engine_id, err.kind)
        return dict(result, status="failed", error=err.kind, stage="update", before=_clean_features(before))
    features = updated.get("features") if isinstance(updated.get("features"), dict) else after
    return dict(result, status="updated", before=_clean_features(before), features=_clean_features(features),
                updateTime=updated.get("updateTime"))


def apply(ctx):
    """POST /api/v1/gemini-enterprise/features/apply
    {changes: [{project, location, engineId, set: {featureKey: "on"|"off"|"default"}}]}

    Applies the switches to every listed app in parallel. Each app is read again and only the listed keys change
    ("default" removes the key so the product default applies). One governance audit entry records every app's
    outcome. Returns {results: [{key, status: updated|unchanged|failed, error?, features?}], updated, unchanged,
    failed, auditLogged}."""
    require_identity_admin(ctx)
    targets = _parse_changes(ctx, ctx.body or {})
    tasks = {(p, l, e): (lambda p=p, l=l, e=e, u=u: _apply_one(ctx, p, l, e, u)) for p, l, e, u in targets}
    outcomes = ctx.run(tasks)
    results = []
    for project, location, engine_id, updates in targets:
        ok, value = outcomes.get((project, location, engine_id), (False, None))
        if not ok or not isinstance(value, dict):
            value = {"key": "%s/%s/%s" % (project, location, engine_id), "project": project, "location": location,
                     "engineId": engine_id, "requested": dict(updates), "status": "failed", "error": "internal"}
        results.append(value)
    counts = {s: sum(1 for r in results if r["status"] == s) for s in ("updated", "unchanged", "failed")}
    logged = governance.write_audit(ctx, "ge_features_update", {
        "result": "success" if not counts["failed"] else ("partial" if counts["updated"] else "failed"),
        "apps": [{"target": r["key"], "status": r["status"], "error": r.get("error"), "changes": r["requested"]}
                 for r in results],
        **counts,
    })
    cache = getattr(getattr(ctx, "app", None), "cache", None)
    if cache is not None:
        cache.clear()
    return dict(results=results, auditLogged=logged, **counts)
