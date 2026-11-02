"""Persistence of UI-managed settings overrides and telemetry connectors.

The store persists ONLY overrides:
{"version": 1, "overrides": {...nested...}, "connectors": [...], "updatedAt": "...", "updatedBy": "...",
 "auth": {"requests": [...], "lastLogin": {...}, "revoked": {...}},
 "governance": {"exceptions": [...], "suspensions": [...], "notified": {...}, "lastRun": {...}}}
The "auth" part (Google sign-in access requests, last sign-in times and per-account session
revocation epochs) and the "governance" part (Antigravity per-user exception rules, suspension
records and enforcement run summaries) are internal: they are never returned by the settings API
and changing them does not invalidate cached data.
Backends: file (atomic temp+rename, directory 0700, file 0600), gcs (JSON API with
ifGenerationMatch optimistic concurrency) and memory. Settings are reloaded from the
store every 30 seconds for multi-instance consistency.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.parse

from . import config
from .httpkit import ApiError, now_iso

LOG = logging.getLogger("controlplane.settings")

RELOAD_SECONDS = 30
ACCOUNT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._@\-]{1,127}$")
EMPTY_AUTH = {"requests": [], "lastLogin": {}, "revoked": {}, "accounts": {}}
EMPTY_GOVERNANCE = {"exceptions": [], "suspensions": [], "notified": {}, "lastRun": None, "lastScheduledRun": None}
EMPTY_DOC = {"version": 1, "overrides": {}, "connectors": [], "updatedAt": None, "updatedBy": None,
             "auth": EMPTY_AUTH, "governance": EMPTY_GOVERNANCE}
MAX_ACCESS_REQUESTS = 200
MAX_AUTH_ENTRIES = 5000
MAX_GOVERNANCE_ENTRIES = 5000
REVOCATION_RETENTION_MS = 8 * 24 * 3600 * 1000  # longer than the maximum session lifetime (7 days)
EXCEPTION_TYPES = ("allow", "block", "limit")
SUSPENSION_TRIGGERS = ("auto", "review", "admin", "bulk")
CAP_REASONS = ("tokens", "requests", "cost")


def _valid_account_key(key):
    return isinstance(key, str) and bool(config.EMAIL_RE.match(key) or ACCOUNT_ID_RE.match(key))


def sanitize_auth(auth):
    """Bounded, type-checked copy of the internal sign-in state."""
    auth = auth if isinstance(auth, dict) else {}
    requests = []
    seen = set()
    for item in auth.get("requests") or []:
        if not isinstance(item, dict):
            continue
        email = str(item.get("email") or "").strip().lower()
        if not config.EMAIL_RE.match(email) or email in seen:
            continue
        seen.add(email)
        requests.append({"email": email, "name": str(item.get("name") or "")[:120],
                         "requestedAt": str(item.get("requestedAt") or "")[:40]})
    requests = requests[-MAX_ACCESS_REQUESTS:]
    last_login = {}
    for email, when in list((auth.get("lastLogin") or {}).items())[-MAX_AUTH_ENTRIES:]:
        if _valid_account_key(email) and isinstance(when, str):
            last_login[email] = when[:40]
    revoked = {}
    floor = int(time.time() * 1000) - REVOCATION_RETENTION_MS
    for email, epoch in list((auth.get("revoked") or {}).items())[-MAX_AUTH_ENTRIES:]:
        if _valid_account_key(email) and isinstance(epoch, int) and epoch > floor:
            revoked[email] = epoch
    accounts = {}
    for acct, meta in list((auth.get("accounts") or {}).items())[-MAX_AUTH_ENTRIES:]:
        key = str(acct or "").strip().lower()
        if not _valid_account_key(key) or not isinstance(meta, dict):
            continue
        role = "admin" if meta.get("role") == "admin" or key == "admin" else "viewer"
        pw_hash = str(meta.get("passwordHash") or "")[:256]
        name = str(meta.get("name") or "")[:120]
        updated_at = str(meta.get("updatedAt") or "")[:40]
        accounts[key] = {"role": role, "passwordHash": pw_hash, "name": name, "updatedAt": updated_at}
    return {"requests": requests, "lastLogin": last_login, "revoked": revoked, "accounts": accounts}


def _opt_str(value, limit=40):
    return value[:limit] if isinstance(value, str) and value else None


def _opt_num(value, lo, hi, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    if value < lo or value > hi:
        return None
    return int(value) if integer else float(value)


def _run_summary(run):
    if not isinstance(run, dict):
        return None
    out = {"at": _opt_str(run.get("at")), "trigger": _opt_str(run.get("trigger"), 20),
           "actor": _opt_str(run.get("actor"), 200), "mode": _opt_str(run.get("mode"), 20),
           "result": _opt_str(run.get("result"), 20)}
    for key in ("blocked", "restored", "disallowed", "review", "failed"):
        out[key] = _opt_num(run.get(key), 0, 10 ** 7, integer=True) or 0
    return out if out["at"] else None


def sanitize_governance(state):
    """Bounded, type-checked copy of the internal Antigravity governance state:
    exceptions  per-user exception rules {email, type allow|block|limit, until, caps, note, createdBy, createdAt}
    suspensions users suspended by the control plane {email, since, until, trigger, by, reasons, usage}
    notified    {email: windowStart} users already announced to the webhook for the review queue
    lastRun / lastScheduledRun  summaries of the latest enforcement runs."""
    state = state if isinstance(state, dict) else {}
    exceptions = []
    seen = set()
    for item in state.get("exceptions") or []:
        if not isinstance(item, dict):
            continue
        email = str(item.get("email") or "").strip().lower()
        kind = item.get("type")
        if not config.EMAIL_RE.match(email) or kind not in EXCEPTION_TYPES or email in seen:
            continue
        seen.add(email)
        exceptions.append({
            "email": email, "type": kind, "until": _opt_str(item.get("until")),
            "tokenCap": _opt_num(item.get("tokenCap"), 0, 10 ** 12, integer=True) if kind == "limit" else None,
            "requestCap": _opt_num(item.get("requestCap"), 0, 10 ** 9, integer=True) if kind == "limit" else None,
            "costCap": _opt_num(item.get("costCap"), 0, 1e9) if kind == "limit" else None,
            "note": str(item.get("note") or "")[:200], "createdBy": str(item.get("createdBy") or "")[:200],
            "createdAt": _opt_str(item.get("createdAt"))})
    suspensions = []
    seen = set()
    for item in state.get("suspensions") or []:
        if not isinstance(item, dict):
            continue
        email = str(item.get("email") or "").strip().lower()
        if not config.EMAIL_RE.match(email) or email in seen:
            continue
        seen.add(email)
        trigger = item.get("trigger") if item.get("trigger") in SUSPENSION_TRIGGERS else "admin"
        suspensions.append({
            "email": email, "since": _opt_str(item.get("since")), "until": _opt_str(item.get("until")),
            "trigger": trigger, "by": str(item.get("by") or "")[:200],
            "reasons": [r for r in item.get("reasons") or [] if r in CAP_REASONS][:3],
            "tokens": _opt_num(item.get("tokens"), 0, 10 ** 15, integer=True),
            "requests": _opt_num(item.get("requests"), 0, 10 ** 12, integer=True),
            "cost": _opt_num(item.get("cost"), 0, 1e12)})
    notified = {}
    for email, when in list((state.get("notified") or {}).items())[-MAX_GOVERNANCE_ENTRIES:]:
        if isinstance(email, str) and config.EMAIL_RE.match(email) and isinstance(when, str):
            notified[email] = when[:40]
    return {"exceptions": exceptions[-MAX_GOVERNANCE_ENTRIES:], "suspensions": suspensions[-MAX_GOVERNANCE_ENTRIES:],
            "notified": notified, "lastRun": _run_summary(state.get("lastRun")),
            "lastScheduledRun": _run_summary(state.get("lastScheduledRun"))}


def _settings_part(doc):
    return doc.get("overrides"), doc.get("connectors")


class StoreConflict(Exception):
    pass


class StoreError(Exception):
    pass


class MemoryStore:
    backend = "memory"
    durable = False

    def __init__(self):
        self._doc = copy.deepcopy(EMPTY_DOC)
        self._generation = 0

    @property
    def location(self):
        return "memory"

    def load(self):
        return copy.deepcopy(self._doc), self._generation

    def save(self, doc, generation):
        if generation != self._generation:
            raise StoreConflict()
        self._doc = copy.deepcopy(doc)
        self._generation += 1
        return self._generation


class FileStore:
    backend = "file"

    def __init__(self, path, durable):
        self.path = path
        self.durable = durable

    @property
    def location(self):
        return os.path.relpath(self.path, config.repo_root()) if self.path.startswith(config.repo_root()) \
            else self.path

    def _generation(self):
        try:
            return os.stat(self.path).st_mtime_ns
        except FileNotFoundError:
            return 0

    def load(self):
        gen = self._generation()
        if not gen:
            return copy.deepcopy(EMPTY_DOC), 0
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as err:
            raise StoreError("settings file unreadable: %s" % type(err).__name__)
        return doc if isinstance(doc, dict) else copy.deepcopy(EMPTY_DOC), gen

    def save(self, doc, generation):
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        if self._generation() != generation:
            raise StoreConflict()
        fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return self._generation()


class GcsStore:
    backend = "gcs"
    durable = True

    def __init__(self, gcp, bucket, obj, quota_project_fn):
        self.gcp = gcp
        self.bucket = bucket
        self.obj = obj
        self.quota_project_fn = quota_project_fn

    @property
    def location(self):
        return "gs://%s/%s" % (self.bucket, self.obj)

    def load(self):
        from .gcp import GcpError
        url = "https://storage.googleapis.com/storage/v1/b/%s/o/%s" % (
            urllib.parse.quote(self.bucket, safe=""), urllib.parse.quote(self.obj, safe=""))
        try:
            content, headers = self.gcp.request("GET", url, quota_project=self.quota_project_fn(),
                                                params={"alt": "media"}, expect_json=False)
        except GcpError as err:
            if err.kind == "not_found":
                return copy.deepcopy(EMPTY_DOC), 0
            raise StoreError("settings object unreadable (%s)" % err.kind)
        generation = int(headers.get("x-goog-generation") or 0)
        try:
            doc = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise StoreError("settings object is not valid JSON")
        return doc if isinstance(doc, dict) else copy.deepcopy(EMPTY_DOC), generation

    def save(self, doc, generation):
        from .gcp import GcpError
        url = "https://storage.googleapis.com/upload/storage/v1/b/%s/o" % urllib.parse.quote(self.bucket, safe="")
        body = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        try:
            payload, _ = self.gcp.request("POST", url, quota_project=self.quota_project_fn(),
                                          params={"uploadType": "media", "name": self.obj,
                                                  "ifGenerationMatch": str(generation)},
                                          data=body, headers={"Content-Type": "application/json"})
        except GcpError as err:
            if err.kind == "precondition":
                raise StoreConflict()
            raise StoreError("settings object could not be written (%s)" % err.kind)
        return int(payload.get("generation") or 0)


def make_store(base_settings, gcp, environ=None):
    environ = os.environ if environ is None else environ
    storage = base_settings.get("storage", {})
    backend = storage.get("backend", "auto")
    on_cloud_run = bool(environ.get("K_SERVICE"))
    if backend == "auto":
        if storage.get("gcs_bucket"):
            backend = "gcs"
        else:
            backend = "memory" if on_cloud_run else "file"
    if backend == "gcs":
        if not storage.get("gcs_bucket"):
            raise ValueError("storage.backend is gcs but storage.gcs_bucket is empty")
        return GcsStore(gcp, storage["gcs_bucket"], storage.get("gcs_object") or "control-plane/settings.json",
                        lambda: config.quota_project(base_settings))
    if backend == "file":
        path = storage.get("file_path") or "data/settings.json"
        if not os.path.isabs(path):
            path = os.path.join(config.repo_root(), path)
        return FileStore(path, durable=not on_cloud_run)
    return MemoryStore()


class UserDocStore:
    """Small per-user JSON documents (personal widget layouts) next to the settings document.

    Uses the backend of the settings store: <settings folder>/users/<key>.json in the same bucket or
    directory, or process memory. Keys are user hashes (security.uid_for), never email addresses.
    Writes are optimistic: a concurrent change is retried on the fresh document."""

    KEY_LEN = 16

    def __init__(self, settings_store):
        self.base = settings_store
        self._memory = {}
        self._lock = threading.Lock()

    def _store(self, key):
        if not isinstance(key, str) or len(key) != self.KEY_LEN or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("invalid user document key")
        base = self.base
        if isinstance(base, GcsStore):
            folder = base.obj.rsplit("/", 1)[0] + "/" if "/" in base.obj else ""
            return GcsStore(base.gcp, base.bucket, "%susers/%s.json" % (folder, key), base.quota_project_fn)
        if isinstance(base, FileStore):
            return FileStore(os.path.join(os.path.dirname(base.path), "users", key + ".json"), base.durable)
        return None

    def load(self, key):
        """Returns (doc, generation); a missing document is ({}, 0)."""
        store = self._store(key)
        if store is None:
            with self._lock:
                doc, generation = self._memory.get(key, ({}, 0))
                return copy.deepcopy(doc), generation
        doc, generation = store.load()
        return (doc if generation and isinstance(doc, dict) else {}), generation

    def mutate(self, key, fn, attempts=3):
        """Applies fn(doc) to the current document and saves it; returns the saved document."""
        for _ in range(attempts):
            doc, generation = self.load(key)
            fn(doc)
            store = self._store(key)
            try:
                if store is None:
                    with self._lock:
                        if self._memory.get(key, ({}, 0))[1] != generation:
                            raise StoreConflict()
                        self._memory[key] = (copy.deepcopy(doc), generation + 1)
                else:
                    store.save(doc, generation)
                return doc
            except StoreConflict:
                continue
        raise StoreConflict()


class SettingsManager:
    """Effective settings = finalize(base (defaults+yaml+env) merged with persisted overrides)."""

    def __init__(self, base, store):
        self.base = base
        self.store = store
        self._lock = threading.RLock()
        self._doc = copy.deepcopy(EMPTY_DOC)
        self._generation = 0
        self._loaded_at = 0.0
        self._reloading = False
        self.version = 1
        self.load_error = None
        self._effective = self._compute()
        self.reload(initial=True)

    # -- loading ------------------------------------------------------------------
    def _sanitize(self, doc):
        overrides = doc.get("overrides") if isinstance(doc.get("overrides"), dict) else {}
        clean = config.validate_layer(overrides, "settings store", strict_security=False)
        # UI-locked keys are never honored from the store.
        for dotted in config.UI_LOCKED_KEYS:
            config.del_path(clean, dotted)
        connectors = [c for c in (doc.get("connectors") or []) if isinstance(c, dict)][:200]
        return {"version": 1, "overrides": clean, "connectors": connectors,
                "updatedAt": doc.get("updatedAt"), "updatedBy": doc.get("updatedBy"),
                "auth": sanitize_auth(doc.get("auth")), "governance": sanitize_governance(doc.get("governance"))}

    def _compute(self, doc=None):
        doc = self._doc if doc is None else doc
        overrides = doc.get("overrides") or {}
        eff = config.deep_merge(self.base, overrides)
        for dotted in config.MEMBER_LIST_KEYS:
            config.set_path(eff, dotted, config.merge_admins(config.get_path(self.base, dotted, []),
                                                             config.get_path(overrides, dotted)))
        eff["telemetry"]["connectors"] = [c for c in (self.base["telemetry"].get("connectors") or [])] + \
            [c for c in (doc.get("connectors") or [])]
        return config.finalize(eff)

    def reload(self, initial=False):
        try:
            doc, generation = self.store.load()
            self.load_error = None
        except StoreError as err:
            self.load_error = str(err)
            LOG.warning("settings store load failed: %s", err)
            self._loaded_at = time.monotonic()
            return
        with self._lock:
            self._loaded_at = time.monotonic()
            if generation == self._generation and not initial:
                return
            clean = self._sanitize(doc)
            changed = _settings_part(clean) != _settings_part(self._doc)
            self._doc = clean
            self._generation = generation
            if changed or initial:
                self._effective = self._compute()
                if not initial:
                    self.version += 1

    def maybe_reload(self):
        if time.monotonic() - self._loaded_at < RELOAD_SECONDS or self._reloading:
            return
        with self._lock:
            if self._reloading:
                return
            self._reloading = True
        try:
            self.reload()
        finally:
            self._reloading = False

    # -- reading ------------------------------------------------------------------
    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._effective), self.version

    def overrides(self):
        with self._lock:
            return copy.deepcopy(self._doc.get("overrides") or {})

    def ui_connectors(self):
        with self._lock:
            return copy.deepcopy(self._doc.get("connectors") or [])

    def config_connector_ids(self):
        return {c.get("id") for c in (self.base["telemetry"].get("connectors") or []) if isinstance(c, dict)}

    def persistence(self):
        with self._lock:
            return {"backend": self.store.backend, "durable": bool(self.store.durable),
                    "location": self.store.location, "updatedAt": self._doc.get("updatedAt"),
                    "updatedBy": self._doc.get("updatedBy")}

    # -- writing ------------------------------------------------------------------
    def _mutate(self, fn, actor, bump=True, check=None):
        """Applies fn(doc) with optimistic concurrency (one retry after reload). bump=False is used
        for internal sign-in state: the settings version (cache key) and audit fields stay unchanged.
        check(effective) may raise ApiError to refuse the change before anything is written."""
        for attempt in (1, 2):
            with self._lock:
                doc = copy.deepcopy(self._doc)
                generation = self._generation
            result = fn(doc)
            if check is not None:
                check(self._compute(self._sanitize(doc)))
            if bump:
                doc["updatedAt"] = now_iso()
                doc["updatedBy"] = actor
            try:
                new_gen = self.store.save(doc, generation)
            except StoreConflict:
                if attempt == 1:
                    self.reload()
                    continue
                raise ApiError("conflict", "Settings were changed concurrently. Reload and try again.")
            except (StoreError, OSError) as err:
                LOG.error("settings store write failed: %s", type(err).__name__)
                raise ApiError("upstream_error", "The settings could not be saved.")
            with self._lock:
                self._doc = self._sanitize(doc)
                self._generation = new_gen
                if bump:
                    self._effective = self._compute()
                    self.version += 1
                self._loaded_at = time.monotonic()
            return result
        raise ApiError("conflict", "Settings were changed concurrently. Reload and try again.")

    def update_section(self, section, values, actor, check=None):
        with self._lock:
            effective = copy.deepcopy(self._effective)
        changes = config.validate_section_update(section, values, effective, self.base)

        def apply(doc):
            overrides = doc.setdefault("overrides", {})
            for dotted, value in changes.items():
                base_value = config.get_path(self.base, dotted)
                if dotted in config.MEMBER_LIST_KEYS:
                    if value:
                        config.set_path(overrides, dotted, value)
                    else:
                        config.del_path(overrides, dotted)
                elif value == base_value:
                    config.del_path(overrides, dotted)
                else:
                    config.set_path(overrides, dotted, value)
        self._mutate(apply, actor, check=check)

    def reset(self, section, keys, actor, check=None):
        dotted_keys = config.reset_keys(section, keys)

        def apply(doc):
            overrides = doc.setdefault("overrides", {})
            for dotted in dotted_keys:
                if dotted == "model_catalog":
                    known = config.get_path(overrides, "model_catalog.known_models")
                    config.del_path(overrides, dotted)
                    if known:
                        config.set_path(overrides, "model_catalog.known_models", known)
                else:
                    config.del_path(overrides, dotted)
        self._mutate(apply, actor, check=check)

    # -- internal sign-in state ------------------------------------------------------
    def auth_state(self):
        with self._lock:
            return copy.deepcopy(self._doc.get("auth") or EMPTY_AUTH)

    def revoked_at(self, email):
        """Epoch (ms) before which sessions of this account are invalid; 0 when never revoked."""
        with self._lock:
            return int(((self._doc.get("auth") or {}).get("revoked") or {}).get(email, 0))

    def mutate_auth(self, fn, actor):
        def apply(doc):
            auth = sanitize_auth(doc.get("auth"))
            doc["auth"] = auth
            return fn(auth)
        return self._mutate(apply, actor, bump=False)

    # -- internal Antigravity governance state ------------------------------------------
    def governance_state(self):
        with self._lock:
            return copy.deepcopy(self._doc.get("governance") or EMPTY_GOVERNANCE)

    def governance_durable(self):
        return bool(getattr(self.store, "durable", False))

    def mutate_governance(self, fn, actor):
        """Applies fn(state) to the governance part with optimistic concurrency; returns fn's result."""
        def apply(doc):
            state = sanitize_governance(doc.get("governance"))
            doc["governance"] = state
            return fn(state)
        return self._mutate(apply, actor, bump=False)

    def save_connectors(self, fn, actor):
        def apply(doc):
            connectors = doc.setdefault("connectors", [])
            return fn(connectors)
        return self._mutate(apply, actor)

    def record_models(self, keys, today_iso, auto_enable, actor="system", recent=None):
        """Appends newly observed model keys to model_catalog.known_models. The very first
        observation seeds a baseline (not flagged as new). Later, only keys in `recent` (first usage
        within the new-model window) are flagged as new; other keys are old usage that a longer date
        range revealed and join the baseline. Returns the known list."""
        with self._lock:
            known = config.get_path(self._doc.get("overrides") or {}, "model_catalog.known_models") or []
        known_keys = {k.get("key") for k in known if isinstance(k, dict)}
        new = [k for k in sorted(set(keys)) if k not in known_keys]
        if not new:
            return known
        seeding = not known
        if seeding:
            fresh = []
        elif recent is None:
            fresh = list(new)
        else:
            fresh = [k for k in new if k in set(recent)]

        def apply(doc):
            overrides = doc.setdefault("overrides", {})
            current = config.get_path(overrides, "model_catalog.known_models") or []
            current_keys = {k.get("key") for k in current if isinstance(k, dict)}
            for key in new:
                if key not in current_keys:
                    current.append({"key": key, "detectedAt": today_iso, "baseline": key not in fresh})
            config.set_path(overrides, "model_catalog.known_models", current[-2000:])
            if not auto_enable and fresh:
                disabled = config.get_path(overrides, "model_catalog.disabled_models") or list(
                    config.get_path(self.base, "model_catalog.disabled_models") or [])
                for key in fresh:
                    if key not in disabled:
                        disabled.append(key)
                config.set_path(overrides, "model_catalog.disabled_models", disabled)
        try:
            self._mutate(apply, actor)
        except ApiError as err:
            LOG.warning("could not record model catalog snapshot: %s", err.code)
        with self._lock:
            return config.get_path(self._doc.get("overrides") or {}, "model_catalog.known_models") or []
