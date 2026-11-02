"""Shared helpers for the unit tests (standard library only).

Importing this module puts src/ on sys.path so that `controlplane` can be imported.
"""

from __future__ import annotations

import copy
import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from controlplane import config  # noqa: E402  pylint: disable=wrong-import-position
from controlplane.cache import TTLCache  # noqa: E402  pylint: disable=wrong-import-position
from controlplane.security import Identity  # noqa: E402  pylint: disable=wrong-import-position

PROJECT = "demo-project"
ACCESS_ROLE = "roles/discoveryengine.agentspaceUser"
CAP_MARKER = "projects/%s/roles/CustomAntigravityCapBlocked" % PROJECT
ADMIN_MARKER = "projects/%s/roles/CustomAntigravityAdminBlocked" % PROJECT
LEGACY_MARKER = "projects/%s/roles/CustomAntigravityRestrictedUser" % PROJECT


def make_settings(**sections):
    """Effective settings for a configured demo workspace, with optional section overrides."""
    settings = copy.deepcopy(config.DEFAULTS)
    settings["workspace"]["project_id"] = PROJECT
    settings["workspace"]["timezone"] = "UTC"
    for section, values in sections.items():
        if section == "governance":
            settings["governance"]["antigravity"].update(values)
        else:
            settings[section].update(values)
    return config.finalize(settings)


class FakeApp:
    def __init__(self):
        self.cache = TTLCache(64)
        self.settings = None
        self.invalidations = 0

    def invalidate_governance(self):
        self.invalidations += 1

    def on_settings_changed(self):
        self.cache.clear()


class FakeCtx:
    """Minimal stand-in for services.base.Ctx: no network, synchronous task execution."""

    def __init__(self, settings=None, body=None, params=None, role="admin"):
        self.app = FakeApp()
        self.settings = settings or make_settings()
        self.body = body or {}
        self.params = params or {}
        self.version = 1
        self.range = None
        self.identity = Identity("admin@example.com", role, "iap", "iap:admin@example.com")
        self.warnings = []
        self.sources = set()

    @property
    def ws(self):
        return self.settings["workspace"]

    @property
    def project(self):
        return self.ws["project_id"]

    @property
    def projects(self):
        out = [self.project]
        for p in self.ws.get("projects") or []:
            if p and p not in out:
                out.append(p)
        return out

    @property
    def quota_project(self):
        return self.project

    @property
    def tz_name(self):
        return self.ws.get("timezone") or "UTC"

    @property
    def audit_dataset(self):
        return self.ws["audit_dataset"]

    @property
    def excluded_patterns(self):
        return list(self.settings["model_catalog"]["excluded_sku_patterns"])

    def warn(self, source, code, message):
        self.warnings.append({"source": source, "code": code, "message": message})

    def warn_error(self, source, err, what):
        self.warnings.append({"source": source, "code": getattr(err, "kind", "error"), "message": what})

    def add_source(self, label):
        self.sources.add(label)

    def run(self, tasks):
        results = {}
        for name, fn in tasks.items():
            try:
                results[name] = (True, fn())
            except Exception as err:  # pylint: disable=broad-except
                results[name] = (False, err)
        return results

    run_composite = run

    def memo(self, name, key, fn, ttl=None):  # pylint: disable=unused-argument
        return fn(self)

    def audit_source(self, logical, optional=False):  # pylint: disable=unused-argument
        return None

    def billing_source(self):
        return None

    def rest(self, method, url, **kwargs):  # pragma: no cover - tests patch the callers
        raise AssertionError("unexpected REST call %s %s" % (method, url))

    def query(self, sql, params, module, **kwargs):  # pragma: no cover - tests patch the callers
        raise AssertionError("unexpected query for module %s" % module)
