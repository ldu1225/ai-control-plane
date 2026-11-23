"""Workspace scope: the projects a folder or organization scope covers, their labels and GET /api/v1/scope/projects.

workspace.scope_type selects how the workspace finds its projects (services/base.py Ctx.all_projects):
- projects (default): the primary project, workspace.projects and, with scan_all_projects, up to 25 projects that
  projects:search returns.
- folder / organization: the primary project, workspace.projects and every active project under workspace.scope_ids
  (nested folders included), found here with a breadth-first walk of Cloud Resource Manager v3 (projects.list and
  folders.list by parent), up to workspace.scope_max_projects. With scope_filter gemini_enterprise only projects that
  have a Gemini Enterprise subscription distributed to them (license configurations in a discovery_engine_locations
  location) are kept; a project whose subscription state cannot be read is kept as unknown, never silently dropped.

Discovery never raises: an unreadable scope id or folder becomes an error code and the walk goes on.
"""

from __future__ import annotations

import collections
import logging
import os
import re
import threading
import time

from .. import config
from ..gcp import GcpError
from ..httpkit import ApiError

LOG = logging.getLogger("controlplane.scope")

CRM = "https://cloudresourcemanager.googleapis.com/v3"
SOURCE = "api:cloudresourcemanager.projects"
SCOPE_TYPES = ("folder", "organization")
DEFAULT_MAX_PROJECTS = 300
MAX_PROJECTS = 5000
MAX_DEPTH = 10            # folder levels walked below a scope id
MAX_FOLDERS = 2000        # folders listed per discovery
PAGE_SIZE = 300
# Pages read per listing. The server may return fewer items than PAGE_SIZE per page; paginate stops at the last page.
MAX_PROJECT_PAGES = 50
MAX_FOLDER_PAGES = 20
WALK_BATCH = 10           # folders / organizations listed concurrently
GE_BATCH = 64             # license checks handed to the worker pool at a time
DISCOVERY_TTL = 1800
RETRY_TTL = 120           # a discovery that met a transient failure is repeated sooner
LABELS_TTL = 1800
LABELS_RETRY_TTL = 300    # labels are read again sooner when a read failed
WAIT_SECONDS = 60         # longest wait for another thread's computation of the same result
MAX_LABELS = 64
PLACEHOLDER_PROJECTS = ("demo-project", "my-project")
AUTH_PROJECT_PREFIX = "ai-control-plane-auth-"
ERROR_CODES = {"not_found": "not_found", "denied": "access_denied", "credentials": "credentials_unavailable",
               "timeout": "timeout", "invalid": "invalid_request"}
TRANSIENT_ERRORS = frozenset({"credentials_unavailable", "timeout", "upstream_unavailable", "unexpected_error"})
DISABLED_HINTS = ("has not been used", "SERVICE_DISABLED", "is disabled")

_SCOPE_ID_RE = re.compile(r"^[0-9]{1,30}$")
_FOLDER_RE = re.compile(r"^folders/[0-9]{1,30}$")
_NUMBER_RE = re.compile(r"^projects/([0-9]{1,30})$")
_FLIGHTS = {}
_FLIGHTS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Settings and preconditions
# ---------------------------------------------------------------------------

def scope_ids(ctx):
    """Valid workspace.scope_ids (numeric folder or organization ids, as strings), in order and without duplicates."""
    raw = ctx.ws.get("scope_ids") or ()
    if isinstance(raw, str):
        raw = raw.split(",")
    ids = (str(value).strip() for value in raw)
    return tuple(dict.fromkeys(value for value in ids if _SCOPE_ID_RE.match(value)))


def max_projects(ctx):
    """workspace.scope_max_projects within 1..5000 (default 300)."""
    try:
        value = int(ctx.ws.get("scope_max_projects") or DEFAULT_MAX_PROJECTS)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_PROJECTS
    return max(1, min(MAX_PROJECTS, value))


def scope_filter(ctx):
    """workspace.scope_filter: gemini_enterprise (default) or none."""
    return "none" if ctx.ws.get("scope_filter") == "none" else "gemini_enterprise"


def scope_spec(ctx):
    """(scope_type, scope_ids, max_projects, scope_filter) of a folder or organization scope with at least one valid
    scope id, else None (projects scope)."""
    scope_type = ctx.ws.get("scope_type")
    if scope_type not in SCOPE_TYPES:
        return None
    ids = scope_ids(ctx)
    if not ids:
        return None
    return scope_type, ids, max_projects(ctx), scope_filter(ctx)


def _configured(ctx):
    """The preconditions of Ctx._crm_discovered: a configured workspace with a real project (or Cloud Run) and an
    application cache, so that unit tests with fake applications never reach Resource Manager."""
    project = getattr(ctx, "project", None)
    return bool(isinstance(project, str) and config.valid_project(project) and project not in PLACEHOLDER_PROJECTS
                and (config.workspace_configured(ctx.settings) or os.environ.get("K_SERVICE"))
                and hasattr(getattr(ctx, "app", None), "cache"))


def _live(ctx):
    """_configured and the real Google Cloud client (the fakes of unit tests are never called)."""
    app = getattr(ctx, "app", None)
    return _configured(ctx) and hasattr(app, "gcp") and type(app.gcp).__name__ == "Gcp"


def _central_project(ctx):
    value = getattr(ctx, "central_audit_project", None)
    if value is None:  # a context without the property (test doubles)
        value = ctx.ws.get("audit_project") or ""
        value = value if config.valid_project(value) else ""
    return value


def _shared(cache, key, compute, wait=WAIT_SECONDS):
    """Cached value of `key`, else compute() -> (value, ttl) run by one thread at a time. Returns None to a caller
    that finds another thread's computation still running `wait` seconds after it started.

    TTLCache.get_or_compute would make concurrent callers wait without a limit. These computations run their calls on
    the shared worker pools, and callers can be pool workers themselves (any task that reads Ctx.projects), so an
    unlimited wait could leave no worker to run them."""
    hit, value = cache.get(key)
    if hit:
        return value
    flight_key = (id(cache), key)
    with _FLIGHTS_LOCK:
        flight = _FLIGHTS.get(flight_key)
        owner = flight is None
        if owner:
            flight = _FLIGHTS[flight_key] = (threading.Event(), time.monotonic() + wait)
    if not owner:
        flight[0].wait(max(0.0, flight[1] - time.monotonic()))
        hit, value = cache.get(key)
        return value if hit else None
    try:
        hit, value = cache.get(key)
        if hit:
            return value
        value, ttl = compute()
        cache.set(key, value, ttl)
        return value
    finally:
        with _FLIGHTS_LOCK:
            _FLIGHTS.pop(flight_key, None)
        flight[0].set()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _cache_key(ctx, spec):
    from . import gemini_enterprise  # imported here: gemini_enterprise imports base, which imports this module
    return ("scope_discovery",) + tuple(spec) + (tuple(gemini_enterprise.discovery_locations(ctx)),)


def discover(ctx, blocking=True):
    """Projects under the folder or organization scope, or None.

    Returns {"projects": [{"id", "name", "parent", "number", "labels", "ge"}], "truncated": bool, "folders": int,
    "errors": [code]}: projects in breadth-first order (at most scope_max_projects); ge True when Gemini Enterprise
    license configurations were found, None when unknown or with scope_filter none (projects without any are left out
    with scope_filter gemini_enterprise); "folders" counts the folders listed; "errors" holds short codes
    (access_denied, not_found, ...) of folders or organizations that could not be read.

    None for a projects scope or without scope ids, outside a configured workspace with the real Google Cloud client
    (the guards of Ctx._crm_discovered), and with blocking=False when nothing is cached: blocking=False only reads the
    application cache and makes no calls. Results are cached for 30 minutes (2 minutes after a transient failure).
    Never raises."""
    spec = scope_spec(ctx)
    if spec is None or not _configured(ctx):
        return None
    key = _cache_key(ctx, spec)
    if not blocking:
        hit, value = ctx.app.cache.get(key)
        return value if hit else None
    if not _live(ctx):
        return None
    return _shared(ctx.app.cache, key, lambda: _discover(ctx, spec))


def _discover(ctx, spec):
    """Walks the scope and applies the Gemini Enterprise filter. Returns (result, ttl); never raises."""
    scope_type, ids, limit, flt = spec
    prefix = "folders/" if scope_type == "folder" else "organizations/"
    errors = []
    try:
        projects, truncated, folders = _walk(ctx, [prefix + sid for sid in ids], limit, errors)
    except Exception:  # pylint: disable=broad-except
        LOG.exception("scope walk failed type=%s", scope_type)
        projects, truncated, folders = [], False, 0
        _note(errors, "unexpected_error")
    if flt == "gemini_enterprise" and projects:
        try:
            projects = _ge_filter(ctx, projects)
        except Exception:  # pylint: disable=broad-except
            LOG.exception("scope Gemini Enterprise check failed")  # every project stays, with ge unknown
    LOG.info("scope discovery type=%s ids=%d projects=%d folders=%d truncated=%s errors=%s", scope_type, len(ids),
             len(projects), folders, truncated, ",".join(errors) or "none")
    ttl = RETRY_TTL if TRANSIENT_ERRORS.intersection(errors) else DISCOVERY_TTL
    return {"projects": projects, "truncated": truncated, "folders": folders, "errors": errors}, ttl


def _walk(ctx, roots, limit, errors):
    """Breadth-first walk below `roots` (folders/<id> or organizations/<id>). Returns (projects, truncated, folders).

    Parents are listed WALK_BATCH at a time and their results are taken in queue order, so the outcome is the one of a
    sequential walk: projects in breadth-first order without duplicates, at most `limit` of them, at most MAX_FOLDERS
    folders and MAX_DEPTH folder levels. truncated is True when the walk stopped at one of these limits with parts of
    the scope unvisited, or a listing had more pages than were read."""
    found, seen, queued = [], set(), set()
    truncated = False
    folders = 0
    queue = collections.deque()
    for root in roots:
        if root not in queued:
            queued.add(root)
            queue.append((root, 0))
    while queue:
        if len(found) >= limit:
            truncated = True
            break
        batch = []
        while queue and len(batch) < WALK_BATCH:
            if queue[0][0].startswith("folders/"):
                if folders >= MAX_FOLDERS:
                    break
                folders += 1
            batch.append(queue.popleft())
        if not batch:  # the folder budget is spent and folders remain
            truncated = True
            break
        res = ctx.run({parent: (lambda p=parent: _list_children(ctx, p)) for parent, _ in batch})
        for parent, depth in batch:
            ok, value = res.get(parent, (False, None))
            if not ok:
                value = (([], False, value), ([], False, value))
            (items, more_projects, project_err), (subs, more_folders, folder_err) = value
            for err in (project_err, folder_err):
                if err is not None:
                    _note(errors, _error_code(err), parent)
            truncated = truncated or more_projects or more_folders
            for item in items:
                entry = _project_entry(item)
                if entry is None or entry["id"] in seen:
                    continue
                if len(found) >= limit:
                    truncated = True
                    break
                seen.add(entry["id"])
                found.append(entry)
            for sub in subs:
                if sub in queued:
                    continue
                if depth >= MAX_DEPTH:
                    truncated = True
                    break
                queued.add(sub)
                queue.append((sub, depth + 1))
    return found, truncated, folders


def _list_children(ctx, parent):
    """((projects, more pages, error), (child folder names, more pages, error)) of one parent. Never raises."""
    projects = _list(ctx, "projects", parent, MAX_PROJECT_PAGES)
    items, more, err = _list(ctx, "folders", parent, MAX_FOLDER_PAGES)
    names = [item["name"] for item in items if item.get("state") == "ACTIVE"
             and isinstance(item.get("name"), str) and _FOLDER_RE.match(item["name"])]
    return projects, (names, more, err)


def _list(ctx, kind, parent, max_pages):
    """(items, more pages, error) of a Resource Manager list call by parent (kind: projects or folders)."""
    try:
        items, more = ctx.gcp.paginate("%s/%s" % (CRM, kind), quota_project=ctx.quota_project, items_key=kind,
                                       params={"parent": parent}, page_size=PAGE_SIZE, max_pages=max_pages)
    except Exception as err:  # pylint: disable=broad-except
        if not isinstance(err, GcpError):
            LOG.warning("scope listing failed kind=%s parent=%s error=%s", kind, parent, type(err).__name__)
        return [], False, err
    return [item for item in items or () if isinstance(item, dict)], bool(more), None


def _project_entry(item):
    """{"id", "name", "parent", "number", "labels", "ge"} of an active project from projects.list, or None."""
    if item.get("state") != "ACTIVE":
        return None
    pid = str(item.get("projectId") or "").strip().lower()
    if not config.valid_project(pid) or pid.startswith(AUTH_PROJECT_PREFIX):
        return None
    number = _NUMBER_RE.match(str(item.get("name") or ""))
    return {"id": pid, "name": str(item.get("displayName") or pid)[:120],
            "parent": str(item.get("parent") or "")[:120] or None, "number": number.group(1) if number else None,
            "labels": _labels(item.get("labels")), "ge": None}


def _labels(raw):
    """Project labels as {str: str} (at most MAX_LABELS entries; keys and values are at most 63 characters)."""
    out = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if len(out) >= MAX_LABELS:
                break
            if isinstance(key, str) and isinstance(value, str):
                out[key[:63]] = value[:63]
    return out


def _note(errors, code, parent=None):
    if parent:
        LOG.info("scope listing failed parent=%s code=%s", parent, code)
    if code not in errors:
        errors.append(code)


def _error_code(err):
    if isinstance(err, GcpError):
        return ERROR_CODES.get(err.kind, "upstream_unavailable")
    return "unexpected_error"


# ---------------------------------------------------------------------------
# Gemini Enterprise filter
# ---------------------------------------------------------------------------

def _ge_filter(ctx, projects):
    """Projects with ge set from their Gemini Enterprise license configurations: True when a location has any, None when
    unknown; projects whose every location has none (or no Discovery Engine API) are left out."""
    from . import gemini_enterprise  # see _cache_key
    locations = list(gemini_enterprise.discovery_locations(ctx))
    if not locations:
        return projects
    keys = [(entry["id"], loc) for entry in projects for loc in locations]
    # A child context keeps whatever license_configs records out of the request that runs this shared discovery.
    sub = ctx.child()
    outcomes = {}
    for start in range(0, len(keys), GE_BATCH):
        outcomes.update(ctx.run({key: (lambda k=key: gemini_enterprise.license_configs(sub, k[1], k[0]))
                                 for key in keys[start:start + GE_BATCH]}))
    kept = []
    for entry in projects:
        states = [_ge_state(outcomes.get((entry["id"], loc), (False, None))) for loc in locations]
        ge = True if True in states else (False if all(state is False for state in states) else None)
        if ge is not False:
            kept.append(dict(entry, ge=ge))
    return kept


def _ge_state(outcome):
    """True: license configurations found; False: none, or no Discovery Engine API there; None: unknown."""
    ok, value = outcome
    if ok:
        configs = value[0] if isinstance(value, tuple) else value
        return bool(configs)
    return False if _api_absent(value) else None


def _api_absent(err):
    """The Discovery Engine API is disabled in the project, or the location / resource does not exist."""
    if not isinstance(err, GcpError):
        return False
    if err.kind == "not_found":
        return True
    text = "%s %s" % (err.message or "", err.reason or "")
    return err.kind in ("denied", "invalid") and any(hint in text for hint in DISABLED_HINTS)


# ---------------------------------------------------------------------------
# Project labels
# ---------------------------------------------------------------------------

def project_labels(ctx):
    """{project id: {label: value}} for every project of ctx.all_projects ({} when a project's labels cannot be read).

    A folder or organization scope takes discovered projects' labels from the discovery; other projects are read with
    projects.get in parallel (cached for 30 minutes). Returns {} without any call outside a configured workspace with
    the real Google Cloud client (the guards of Ctx._crm_discovered)."""
    if not _live(ctx):
        return {}
    projects = list(dict.fromkeys(pid for pid in ctx.all_projects or ()
                                  if isinstance(pid, str) and config.valid_project(pid)))
    labels = {}
    if scope_spec(ctx) is not None:
        by_id = {item.get("id"): item for item in (discover(ctx) or {}).get("projects") or ()
                 if isinstance(item, dict)}
        for pid in projects:
            if pid in by_id:
                labels[pid] = dict(by_id[pid].get("labels") or {})
    missing = [pid for pid in projects if pid not in labels]
    if missing:
        labels.update(_read_labels(ctx, missing))
    return {pid: dict(labels.get(pid) or {}) for pid in projects}


def _read_labels(ctx, projects):
    """{project id: labels} read with projects.get; a failed read gives {} and is retried after 5 minutes."""
    def compute():
        res = ctx.run({pid: (lambda p=pid: _get_labels(ctx, p)) for pid in projects})
        out, failed = {}, False
        for pid in projects:
            ok, value = res.get(pid, (False, None))
            out[pid] = value if ok and isinstance(value, dict) else {}
            failed = failed or not ok
        return out, LABELS_RETRY_TTL if failed else LABELS_TTL
    return _shared(ctx.app.cache, ("scope_project_labels", tuple(projects)), compute) or {}


def _get_labels(ctx, project):
    payload, _ = ctx.gcp.request("GET", "%s/projects/%s" % (CRM, project), quota_project=ctx.quota_project,
                                 timeout=10)
    return _labels(payload.get("labels") if isinstance(payload, dict) else None)


# ---------------------------------------------------------------------------
# GET /api/v1/scope/projects
# ---------------------------------------------------------------------------

def scope_projects(ctx):
    """The projects the workspace covers: how each was linked (source: primary, configured, auto_discovered or scope),
    its labels and whether a Gemini Enterprise subscription is distributed to it (ge: true, or null when unknown)."""
    ws = ctx.ws
    spec = scope_spec(ctx)
    found = discover(ctx) if spec is not None else None
    discovered = {item.get("id"): item for item in (found or {}).get("projects") or () if isinstance(item, dict)}
    labels = project_labels(ctx)
    projects = []
    for detail in ctx.project_details or ():
        pid = detail.get("id")
        projects.append({"id": pid, "name": detail.get("name") or pid, "parent": detail.get("parent"),
                         "labels": labels.get(pid) or {}, "ge": (discovered.get(pid) or {}).get("ge"),
                         "source": detail.get("source")})
    if _live(ctx):
        ctx.add_source(SOURCE)
    if spec is not None:
        _scope_notices(ctx, found, spec)
    scope_type = ws.get("scope_type")
    return {
        "scopeType": scope_type if scope_type in SCOPE_TYPES else "projects",
        "scopeIds": list(scope_ids(ctx)),
        "filter": scope_filter(ctx),
        "maxProjects": max_projects(ctx),
        "truncated": bool((found or {}).get("truncated")),
        "auditProject": _central_project(ctx) or None,
        "projects": projects,
        "counts": {"total": len(projects), "ge": sum(1 for p in projects if p["ge"] is True),
                   "geUnknown": sum(1 for p in projects if p["ge"] is None)},
    }


def _scope_notices(ctx, found, spec):
    if found is None:
        if _live(ctx):  # another request's discovery is still running
            ctx.warn(SOURCE, "timeout", "Project discovery for the scope is still running; only the primary and "
                                        "listed projects are shown for now.")
        return
    if found.get("truncated"):
        ctx.warn(SOURCE, "truncated", "Not every project in the scope is included: discovery stops at %d projects "
                                      "(workspace.scope_max_projects), %d folders and %d folder levels."
                 % (spec[2], MAX_FOLDERS, MAX_DEPTH))
    if found.get("errors"):
        ctx.warn(SOURCE, "partial_data", "Some folders or organizations in the scope could not be read (%s)."
                 % ", ".join(found["errors"]))


# ---------------------------------------------------------------------------
# GET /api/v1/scope/candidates (scope picker in Settings, administrators)
# ---------------------------------------------------------------------------

CANDIDATE_KINDS = ("project", "folder", "organization")
MAX_CANDIDATES = 500
CANDIDATE_PAGE_SIZE = 100
CANDIDATE_PAGES = 6
_QUERY_TOKEN_RE = re.compile(r"[A-Za-z0-9._-]{1,60}")


def candidates(ctx):
    """GET /api/v1/scope/candidates?kind=project|folder|organization&query=TEXT: what the service identity can see,
    for the scope picker. Returns {"kind", "items": [{"id", "name", "parent"}], "truncated"}; ids are project ids or
    numeric folder and organization ids.

    Projects and folders are searched with Resource Manager v3 (projects:search with "name:WORD* id:WORD*", which
    matches either field, and folders:search with "displayName:WORD*", where WORD is the first word of the query);
    every word of the query must then appear in the id or the name. Organizations are all listed (organizations:search)
    and filtered the same way. Nothing is called outside a configured workspace with the real Google Cloud client."""
    kind = str(ctx.params.get("kind") or "project").strip()
    if kind not in CANDIDATE_KINDS:
        raise ApiError("bad_request", "Parameter 'kind' must be project, folder or organization.")
    query = str(ctx.params.get("query") or "").strip()
    if len(query) > 100 or config.CONTROL_RE.search(query):
        raise ApiError("bad_request", "Parameter 'query' must be text of at most 100 characters.")
    out = {"kind": kind, "items": [], "truncated": False}
    if not _live(ctx):
        return out
    words = [w.lower() for w in query.split()]
    token = next(iter(_QUERY_TOKEN_RE.findall(words[0])), "") if words else ""
    params = {}
    if kind == "project":
        url, key = CRM + "/projects:search", "projects"
        if token:
            params["query"] = "name:%s* id:%s*" % (token, token)
    elif kind == "folder":
        url, key = CRM + "/folders:search", "folders"
        if token:
            params["query"] = "displayName:%s*" % token
    else:
        url, key = CRM + "/organizations:search", "organizations"
    try:
        raw, more = ctx.gcp.paginate(url, quota_project=ctx.quota_project, items_key=key, params=params or None,
                                     page_size=CANDIDATE_PAGE_SIZE, max_pages=CANDIDATE_PAGES)
    except GcpError as err:
        ctx.warn_error("api:cloudresourcemanager.%s.search" % key, err, "The %s list" % kind)
        return out
    ctx.add_source("api:cloudresourcemanager.%s.search" % key)
    items, seen = [], set()
    for entry in raw or ():
        item = _candidate(kind, entry)
        if item is None or item["id"] in seen:
            continue
        text = ("%s %s" % (item["id"], item["name"])).lower()
        if all(word in text for word in words):
            seen.add(item["id"])
            items.append(item)
    items.sort(key=lambda item: (item["name"].lower(), item["id"]))
    out["truncated"] = bool(more) or len(items) > MAX_CANDIDATES
    out["items"] = items[:MAX_CANDIDATES]
    return out


def _candidate(kind, entry):
    """{"id", "name", "parent"} of an active project, folder or organization from a search result, or None."""
    if not isinstance(entry, dict) or entry.get("state") not in (None, "ACTIVE"):
        return None
    parent = str(entry.get("parent") or "")[:120] or None
    if kind == "project":
        pid = str(entry.get("projectId") or "").strip().lower()
        if not config.valid_project(pid) or pid.startswith(AUTH_PROJECT_PREFIX):
            return None
        return {"id": pid, "name": str(entry.get("displayName") or pid)[:120], "parent": parent}
    prefix = "folders/" if kind == "folder" else "organizations/"
    name = str(entry.get("name") or "")
    number = name[len(prefix):] if name.startswith(prefix) else ""
    if not (number.isdigit() and len(number) <= 30):
        return None
    return {"id": number, "name": str(entry.get("displayName") or number)[:120], "parent": parent}
