"""Folder / organization scope discovery, project labels and GET /api/v1/scope/projects (services/scope.py).

Resource Manager and Discovery Engine are served by an in-memory fake behind a real Gcp instance (real pagination
and worker pools), so no test reaches Google Cloud.
"""

import copy
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane.cache import TTLCache
from controlplane.gcp import Gcp, GcpError
from controlplane.security import Identity
from controlplane.services import base, scope

CONFIG = {"name": "projects/1/locations/global/licenseConfigs/c1", "state": "ACTIVE", "licenseCount": "10"}


def project(pid, state="ACTIVE", parent="folders/100", number=None, name=None, labels=None):
    item = {"projectId": pid, "state": state, "parent": parent, "displayName": name or pid.title()}
    if number:
        item["name"] = "projects/%s" % number
    if labels is not None:
        item["labels"] = labels
    return item


class FakeCloud:
    """Resource Manager v3 (projects.list / folders.list by parent, projects.get) and Discovery Engine licenseConfigs.

    Lists are served `page` items at a time with numeric page tokens, whatever pageSize asks for."""

    def __init__(self, projects=None, folders=None, licenses=None, labels=None, fail=None, page=2):
        self.projects = projects or {}   # parent -> [project items]
        self.folders = folders or {}     # parent -> [child folder ids, or folder items]
        self.licenses = licenses or {}   # (project, location) -> [configs] or GcpError
        self.labels = labels or {}       # project -> {label: value} or GcpError
        self.fail = fail or {}           # (kind, parent) -> exception
        self.page = page
        self.calls = []
        self.lock = threading.Lock()

    def request(self, method, url, *, quota_project=None, params=None, json_body=None, **_kwargs):
        with self.lock:
            self.calls.append((method, url, dict(params or {})))
        if url.startswith(scope.CRM + "/projects/"):
            pid = url.rsplit("/", 1)[-1]
            value = self.labels.get(pid, {})
            if isinstance(value, Exception):
                raise value
            return {"projectId": pid, "labels": value}, {}
        if url in (scope.CRM + "/projects", scope.CRM + "/folders"):
            kind = url.rsplit("/", 1)[-1]
            parent = params["parent"]
            if (kind, parent) in self.fail:
                raise self.fail[(kind, parent)]
            if kind == "projects":
                items = self.projects.get(parent, [])
            else:
                items = [sub if isinstance(sub, dict) else {"name": "folders/%s" % sub, "state": "ACTIVE",
                                                            "parent": parent}
                         for sub in self.folders.get(parent, [])]
            start = int(params.get("pageToken") or 0)
            payload = {kind: items[start:start + self.page]}
            if start + self.page < len(items):
                payload["nextPageToken"] = str(start + self.page)
            return payload, {}
        if url.endswith("/licenseConfigs"):
            parts = url.split("/")
            key = (parts[parts.index("projects") + 1], parts[parts.index("locations") + 1])
            value = self.licenses.get(key, [])
            if isinstance(value, Exception):
                raise value
            return {"licenseConfigs": value}, {}
        raise AssertionError("unexpected call %s %s" % (method, url))

    def count(self, fragment):
        return sum(1 for _, url, _ in self.calls if fragment in url)

    def listed(self, kind):
        return [params["parent"] for _, url, params in self.calls
                if url == "%s/%s" % (scope.CRM, kind) and not params.get("pageToken")]


class ScopeTestCase(unittest.TestCase):
    def real_gcp(self, cloud):
        gcp = Gcp()
        gcp.request = cloud.request
        self.addCleanup(lambda: [pool.shutdown(wait=False) for pool in (gcp.pool, gcp.subpool, gcp.orchestrator)])
        return gcp

    def make_ctx(self, cloud, app=None, params=None, **workspace):
        ws = {"project_id": "acme-prod", "projects": [], "scan_all_projects": False, "scope_type": "folder",
              "scope_ids": ["100"], "scope_filter": "none", "discovery_engine_locations": ["global"]}
        ws.update(workspace)
        if app is None:
            app = support.FakeApp()
            app.gcp = self.real_gcp(cloud)
        settings = support.make_settings(workspace=ws)
        return base.Ctx(app, settings, 1, Identity("admin@example.com", "admin", "google", "u:admin"),
                        params=params or {})


def nested_cloud():
    """folders/100 (6 projects on 3 pages, 4 kept) > folders/200 > folders/400, and folders/100 > folders/300."""
    return FakeCloud(
        projects={
            "folders/100": [project("p-alpha-001", number="111", name="Alpha", labels={"team": "ai", "env": "prod"}),
                            project("p-beta-002"), project("p-gamma-003", state="DELETE_REQUESTED"),
                            project("Bad_Id"), project("ai-control-plane-auth-xyz1"), project("p-delta-004")],
            "folders/200": [project("p-eps-005", parent="folders/200")],
            "folders/300": [project("p-alpha-001", parent="folders/300")],
            "folders/400": [project("p-zeta-006", parent="folders/400")],
        },
        folders={"folders/100": ["200", "300", {"name": "folders/900", "state": "DELETE_REQUESTED"},
                                 {"name": "organizations/1", "state": "ACTIVE"}],
                 "folders/200": ["400"]})


class DiscoveryWalkTest(ScopeTestCase):
    def test_nested_folders_pages_and_filters(self):
        cloud = nested_cloud()
        ctx = self.make_ctx(cloud)
        found = scope.discover(ctx)
        self.assertEqual([p["id"] for p in found["projects"]],
                         ["p-alpha-001", "p-beta-002", "p-delta-004", "p-eps-005", "p-zeta-006"])
        self.assertEqual(found["projects"][0], {"id": "p-alpha-001", "name": "Alpha", "parent": "folders/100",
                                                "number": "111", "labels": {"team": "ai", "env": "prod"},
                                                "ge": None})
        self.assertIsNone(found["projects"][1]["number"])
        self.assertEqual(found["projects"][3]["parent"], "folders/200")
        self.assertEqual((found["truncated"], found["folders"], found["errors"]), (False, 4, []))
        # Deleted folders and names that are not folders are never listed.
        self.assertEqual(sorted(cloud.listed("projects")), ["folders/100", "folders/200", "folders/300",
                                                            "folders/400"])
        tokens = [params.get("pageToken") for _, url, params in cloud.calls
                  if url == scope.CRM + "/projects" and params["parent"] == "folders/100"]
        self.assertEqual(sorted(tokens, key=str), ["2", "4", None])
        self.assertTrue(all(params.get("pageSize") == scope.PAGE_SIZE for _, url, params in cloud.calls))
        self.assertEqual(ctx.warnings, [])

    def test_organization_root(self):
        cloud = FakeCloud(projects={"organizations/1234": [project("org-proj-01", parent="organizations/1234")],
                                    "folders/55": [project("org-proj-02", parent="folders/55")]},
                          folders={"organizations/1234": ["55"]})
        found = scope.discover(self.make_ctx(cloud, scope_type="organization", scope_ids=["1234"]))
        self.assertEqual([p["id"] for p in found["projects"]], ["org-proj-01", "org-proj-02"])
        self.assertEqual(found["folders"], 1)  # organizations are not counted as folders
        self.assertEqual(cloud.listed("folders"), ["organizations/1234", "folders/55"])

    def test_depth_limit(self):
        chain = {"folders/%d" % (1000 + i): ["%d" % (1001 + i)] for i in range(12)}
        projects = {"folders/%d" % (1000 + i): [project("chain-proj-%02d" % i, parent="folders/%d" % (1000 + i))]
                    for i in range(13)}
        cloud = FakeCloud(projects=projects, folders=chain)
        found = scope.discover(self.make_ctx(cloud, scope_ids=["1000"]))
        # The scope folder and 10 levels below it.
        self.assertEqual(len(found["projects"]), scope.MAX_DEPTH + 1)
        self.assertEqual(found["projects"][-1]["id"], "chain-proj-10")
        self.assertEqual(found["folders"], scope.MAX_DEPTH + 1)
        self.assertTrue(found["truncated"])
        self.assertNotIn("folders/1011", cloud.listed("projects"))

    def test_folder_budget(self):
        cloud = FakeCloud(folders={"folders/100": ["201", "202", "203", "204"]},
                          projects={"folders/%d" % i: [project("budget-p%d" % i, parent="folders/%d" % i)]
                                    for i in (201, 202, 203, 204)})
        with mock.patch.object(scope, "MAX_FOLDERS", 3):
            found = scope.discover(self.make_ctx(cloud))
        self.assertEqual([p["id"] for p in found["projects"]], ["budget-p201", "budget-p202"])
        self.assertEqual(found["folders"], 3)
        self.assertTrue(found["truncated"])

    def test_max_projects_truncation(self):
        items = [project("many-proj-%02d" % i) for i in range(5)]
        found = scope.discover(self.make_ctx(FakeCloud(projects={"folders/100": items}), scope_max_projects=3))
        self.assertEqual([p["id"] for p in found["projects"]], ["many-proj-00", "many-proj-01", "many-proj-02"])
        self.assertTrue(found["truncated"])
        exact = scope.discover(self.make_ctx(FakeCloud(projects={"folders/100": items[:3]}), scope_max_projects=3))
        self.assertEqual(len(exact["projects"]), 3)
        self.assertFalse(exact["truncated"])
        # A limit reached with folders left to visit is truncated as well.
        nested = FakeCloud(projects={"folders/100": items[:3]}, folders={"folders/100": ["200"]})
        self.assertTrue(scope.discover(self.make_ctx(nested, scope_max_projects=3))["truncated"])

    def test_unreadable_scope_ids_are_reported_and_the_walk_goes_on(self):
        cloud = FakeCloud(projects={"folders/100": [project("ok-project-1")]},
                          fail={("projects", "folders/999"): GcpError("denied", 403, "denied"),
                                ("folders", "folders/999"): GcpError("denied", 403, "denied"),
                                ("folders", "folders/100"): ValueError("bad payload")})
        ctx = self.make_ctx(cloud, scope_ids=["999", "100"])
        found = scope.discover(ctx)
        self.assertEqual([p["id"] for p in found["projects"]], ["ok-project-1"])
        self.assertEqual(found["errors"], ["access_denied", "unexpected_error"])
        self.assertEqual(ctx.warnings, [])
        _, ttl = scope._discover(ctx, scope.scope_spec(ctx))  # pylint: disable=protected-access
        self.assertEqual(ttl, scope.RETRY_TTL)  # an unexpected failure is retried sooner
        denied = FakeCloud(fail={("projects", "folders/999"): GcpError("denied", 403)})
        dctx = self.make_ctx(denied, scope_ids=["999"])
        _, ttl = scope._discover(dctx, scope.scope_spec(dctx))  # pylint: disable=protected-access
        self.assertEqual(ttl, scope.DISCOVERY_TTL)

    def test_blocking_false_reads_the_cache_only(self):
        cloud = nested_cloud()
        ctx = self.make_ctx(cloud, projects=["acme-dev-01"])
        self.assertIsNone(scope.discover(ctx, blocking=False))
        self.assertEqual(ctx.known_projects, ["acme-prod", "acme-dev-01"])
        self.assertEqual(cloud.calls, [])
        first = scope.discover(ctx)
        calls = len(cloud.calls)
        self.assertIs(scope.discover(ctx, blocking=False), first)
        self.assertIs(scope.discover(ctx), first)
        self.assertEqual(ctx.known_projects, ["acme-prod", "acme-dev-01", "p-alpha-001", "p-beta-002",
                                              "p-delta-004", "p-eps-005", "p-zeta-006"])
        self.assertEqual(len(cloud.calls), calls)

    def test_cache_key_follows_the_scope_settings(self):
        cloud = nested_cloud()
        ctx = self.make_ctx(cloud)
        scope.discover(ctx)
        calls = len(cloud.calls)
        smaller = self.make_ctx(cloud, app=ctx.app, scope_max_projects=2)
        self.assertEqual(len(scope.discover(smaller)["projects"]), 2)
        self.assertGreater(len(cloud.calls), calls)
        calls = len(cloud.calls)
        scope.discover(self.make_ctx(cloud, app=ctx.app, discovery_engine_locations=["global", "us"]))
        self.assertGreater(len(cloud.calls), calls)

    def test_no_discovery_without_scope_or_real_client(self):
        cloud = nested_cloud()
        self.assertIsNone(scope.discover(self.make_ctx(cloud, scope_type="projects")))
        self.assertIsNone(scope.discover(self.make_ctx(cloud, scope_ids=[])))
        self.assertIsNone(scope.discover(self.make_ctx(cloud, scope_ids=["abc"])))
        self.assertIsNone(scope.discover(self.make_ctx(cloud, project_id="demo-project")))
        self.assertIsNone(scope.discover(self.make_ctx(cloud, project_id="your-gcp-project-id")))
        fake_app = support.FakeApp()
        fake_app.gcp = mock.Mock()
        ctx = self.make_ctx(cloud, app=fake_app)
        self.assertIsNone(scope.discover(ctx))
        self.assertEqual(ctx.all_projects, ["acme-prod"])
        self.assertEqual(fake_app.gcp.mock_calls, [])
        no_gcp = self.make_ctx(cloud, app=support.FakeApp())
        self.assertIsNone(scope.discover(no_gcp))
        self.assertEqual(no_gcp.all_projects, ["acme-prod"])
        self.assertEqual(cloud.calls, [])

    def test_ctx_lists_discovered_projects_after_primary_and_configured(self):
        cloud = nested_cloud()
        ctx = self.make_ctx(cloud, projects=["acme-dev-01", "p-beta-002"])
        self.assertEqual(ctx.all_projects, ["acme-prod", "acme-dev-01", "p-beta-002", "p-alpha-001", "p-delta-004",
                                            "p-eps-005", "p-zeta-006"])
        details = {d["id"]: d for d in ctx.project_details}
        self.assertEqual(details["acme-prod"]["source"], "primary")
        self.assertEqual(details["p-beta-002"]["source"], "configured")
        self.assertEqual(details["p-beta-002"]["parent"], "folders/100")
        self.assertEqual(details["p-alpha-001"], {"id": "p-alpha-001", "name": "Alpha", "parent": "folders/100",
                                                  "sameOrg": None, "source": "scope"})
        self.assertEqual(scope.discover(ctx)["projects"][0]["id"], "p-alpha-001")
        self.assertEqual(ctx.projects, ctx.all_projects)
        self.assertEqual(self.make_ctx(cloud, app=ctx.app, params={"project": "p-eps-005"}).projects, ["p-eps-005"])

    def test_scope_settings_normalization(self):
        ctx = self.make_ctx(FakeCloud(), scope_ids=["100", " 200 ", "abc", "100"], scope_max_projects=99999,
                            scope_filter="other")
        ctx.ws["scope_ids"].append(300)
        self.assertEqual(scope.scope_ids(ctx), ("100", "200", "300"))
        self.assertEqual(scope.scope_spec(ctx), ("folder", ("100", "200", "300"), scope.MAX_PROJECTS,
                                                 "gemini_enterprise"))
        ctx.ws["scope_max_projects"] = "x"
        self.assertEqual(scope.max_projects(ctx), scope.DEFAULT_MAX_PROJECTS)
        ctx.ws["scope_ids"] = "7,8"
        self.assertEqual(scope.scope_ids(ctx), ("7", "8"))

    def test_labels_are_bounded(self):
        raw = {"k%02d" % i: "v" for i in range(70)}
        raw.update({"num": 1, 2: "x", "long": "v" * 100})
        labels = scope._labels(raw)  # pylint: disable=protected-access
        self.assertEqual(len(labels), scope.MAX_LABELS)
        self.assertTrue(all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items()))
        self.assertEqual(scope._labels({"long": "v" * 100})["long"], "v" * 63)  # pylint: disable=protected-access
        self.assertEqual(scope._labels(["x"]), {})  # pylint: disable=protected-access


GE_PROJECTS = ["ge-yes-001", "ge-none-002", "ge-off-003", "ge-deny-004", "ge-us-005", "ge-svc-006", "ge-slow-007"]
GE_LICENSES = {
    ("ge-yes-001", "global"): [CONFIG],
    ("ge-off-003", "global"): GcpError("denied", 403, "Discovery Engine API has not been used in project 123 before "
                                                      "or it is disabled.", "PERMISSION_DENIED"),
    ("ge-off-003", "us"): GcpError("not_found", 404, "Not found"),
    ("ge-deny-004", "global"): GcpError("denied", 403, "Permission 'discoveryengine.licenseConfigs.list' denied",
                                        "PERMISSION_DENIED"),
    ("ge-us-005", "us"): [CONFIG],
    ("ge-svc-006", "global"): GcpError("invalid", 400, "SERVICE_DISABLED", "FAILED_PRECONDITION"),
    ("ge-slow-007", "global"): GcpError("timeout", message="request timed out"),
}


class GeminiEnterpriseFilterTest(ScopeTestCase):
    def cloud(self):
        return FakeCloud(projects={"folders/100": [project(pid) for pid in GE_PROJECTS]}, licenses=GE_LICENSES,
                         page=10)

    def test_classification(self):
        cloud = self.cloud()
        ctx = self.make_ctx(cloud, scope_filter="gemini_enterprise", discovery_engine_locations=["global", "us"])
        found = scope.discover(ctx)
        self.assertEqual([(p["id"], p["ge"]) for p in found["projects"]],
                         [("ge-yes-001", True), ("ge-deny-004", None), ("ge-us-005", True), ("ge-slow-007", None)])
        self.assertEqual(cloud.count("/licenseConfigs"), len(GE_PROJECTS) * 2)
        self.assertEqual(cloud.count("https://us-discoveryengine.googleapis.com/"), len(GE_PROJECTS))
        self.assertEqual(found["errors"], [])  # license failures are not scope errors
        self.assertEqual(ctx.warnings, [])

    def test_filter_none_keeps_every_project_unchecked(self):
        cloud = self.cloud()
        found = scope.discover(self.make_ctx(cloud, scope_filter="none"))
        self.assertEqual([p["id"] for p in found["projects"]], GE_PROJECTS)
        self.assertTrue(all(p["ge"] is None for p in found["projects"]))
        self.assertEqual(cloud.count("/licenseConfigs"), 0)

    def test_api_absent_detection(self):
        absent = scope._api_absent  # pylint: disable=protected-access
        self.assertTrue(absent(GcpError("not_found", 404)))
        self.assertTrue(absent(GcpError("denied", 403, "Discovery Engine API has not been used in project 1")))
        self.assertTrue(absent(GcpError("invalid", 400, "API discoveryengine.googleapis.com is disabled")))
        self.assertTrue(absent(GcpError("denied", 403, "", "SERVICE_DISABLED")))
        self.assertFalse(absent(GcpError("denied", 403, "Permission denied")))
        self.assertFalse(absent(GcpError("timeout")))
        self.assertFalse(absent(ValueError("SERVICE_DISABLED")))


class ProjectLabelsTest(ScopeTestCase):
    def test_projects_scope_reads_each_project_once(self):
        cloud = FakeCloud(labels={"acme-prod": {"env": "prod", "cost-center": "cc-1"},
                                  "acme-dev-01": GcpError("denied", 403)})
        ctx = self.make_ctx(cloud, scope_type="projects", projects=["acme-dev-01"])
        expected = {"acme-prod": {"env": "prod", "cost-center": "cc-1"}, "acme-dev-01": {}}
        self.assertEqual(scope.project_labels(ctx), expected)
        self.assertEqual(cloud.count(scope.CRM + "/projects/"), 2)
        self.assertEqual(scope.project_labels(ctx), expected)  # cached
        self.assertEqual(cloud.count(scope.CRM + "/projects/"), 2)
        self.assertEqual(ctx.warnings, [])

    def test_folder_scope_uses_discovered_labels(self):
        cloud = nested_cloud()
        cloud.labels = {"acme-prod": {"env": "host"}}
        ctx = self.make_ctx(cloud)
        labels = scope.project_labels(ctx)
        self.assertEqual(labels["acme-prod"], {"env": "host"})
        self.assertEqual(labels["p-alpha-001"], {"team": "ai", "env": "prod"})
        self.assertEqual(labels["p-beta-002"], {})
        self.assertEqual(set(labels), set(ctx.all_projects))
        self.assertEqual(cloud.count(scope.CRM + "/projects/"), 1)  # only the primary project is read

    def test_no_calls_without_real_client(self):
        cloud = FakeCloud(labels={"acme-prod": {"env": "prod"}})
        fake_app = support.FakeApp()
        fake_app.gcp = mock.Mock()
        self.assertEqual(scope.project_labels(self.make_ctx(cloud, app=fake_app, scope_type="projects")), {})
        self.assertEqual(fake_app.gcp.mock_calls, [])
        self.assertEqual(scope.project_labels(self.make_ctx(cloud, scope_type="projects",
                                                            project_id="demo-project")), {})
        self.assertEqual(cloud.calls, [])


class ScopeProjectsEndpointTest(ScopeTestCase):
    def test_projects_scope(self):
        cloud = FakeCloud(labels={"acme-prod": {"env": "prod"}})
        ctx = self.make_ctx(cloud, scope_type="projects", projects=["acme-dev-01"],
                            scope_filter="gemini_enterprise")
        data = scope.scope_projects(ctx)
        self.assertEqual(data, {
            "scopeType": "projects", "scopeIds": ["100"], "filter": "gemini_enterprise", "maxProjects": 300,
            "truncated": False, "auditProject": None,
            "projects": [{"id": "acme-prod", "name": "acme-prod", "parent": None, "labels": {"env": "prod"},
                          "ge": None, "source": "primary"},
                         {"id": "acme-dev-01", "name": "acme-dev-01", "parent": None, "labels": {}, "ge": None,
                          "source": "configured"}],
            "counts": {"total": 2, "ge": 0, "geUnknown": 2}})
        self.assertEqual(ctx.warnings, [])
        self.assertEqual(cloud.listed("projects"), [])

    def test_folder_scope_with_filter_and_notices(self):
        cloud = FakeCloud(projects={"folders/100": [project(pid) for pid in GE_PROJECTS]}, licenses=GE_LICENSES,
                          folders={"folders/100": ["200"]}, page=10,
                          fail={("projects", "folders/200"): GcpError("not_found", 404),
                                ("folders", "folders/200"): GcpError("not_found", 404)})
        ctx = self.make_ctx(cloud, scope_filter="gemini_enterprise", discovery_engine_locations=["global", "us"],
                            audit_project="central-logs", scope_max_projects=6)
        data = scope.scope_projects(ctx)
        self.assertEqual(data["scopeType"], "folder")
        self.assertEqual(data["auditProject"], "central-logs")
        self.assertTrue(data["truncated"])
        self.assertEqual([(p["id"], p["ge"], p["source"]) for p in data["projects"]],
                         [("acme-prod", None, "primary"), ("ge-yes-001", True, "scope"),
                          ("ge-deny-004", None, "scope"), ("ge-us-005", True, "scope")])
        self.assertEqual(data["counts"], {"total": 4, "ge": 2, "geUnknown": 2})
        codes = {w["code"] for w in ctx.warnings}
        self.assertEqual(codes, {"truncated"})  # the limit was reached before folders/200 was listed
        self.assertNotIn("internal_error", codes)

    def test_unreadable_folder_notice(self):
        cloud = FakeCloud(projects={"folders/100": [project("ok-project-1")]},
                          fail={("projects", "folders/999"): GcpError("denied", 403),
                                ("folders", "folders/999"): GcpError("denied", 403)})
        ctx = self.make_ctx(cloud, scope_ids=["100", "999"])
        data = scope.scope_projects(ctx)
        self.assertEqual([p["id"] for p in data["projects"]], ["acme-prod", "ok-project-1"])
        self.assertEqual([(w["code"], w["source"]) for w in ctx.warnings], [("partial_data", scope.SOURCE)])
        self.assertIn("access_denied", ctx.warnings[0]["message"])

    def test_discovery_still_running_elsewhere(self):
        cloud = nested_cloud()
        ctx = self.make_ctx(cloud)
        key = scope._cache_key(ctx, scope.scope_spec(ctx))  # pylint: disable=protected-access
        flight_key = (id(ctx.app.cache), key)
        scope._FLIGHTS[flight_key] = (threading.Event(), time.monotonic() - 1)  # pylint: disable=protected-access
        try:
            data = scope.scope_projects(ctx)
        finally:
            scope._FLIGHTS.pop(flight_key, None)  # pylint: disable=protected-access
        self.assertEqual([p["id"] for p in data["projects"]], ["acme-prod"])
        self.assertEqual([w["code"] for w in ctx.warnings], ["timeout"])
        self.assertEqual(cloud.count(scope.CRM + "/folders"), 0)

    def test_works_with_the_offline_application(self):
        """The real App with the offline endpoint fake (every unknown URL returns {}): no exception, no internal
        errors, in the projects scope and in a folder scope."""
        from controlplane import app as app_module
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = os.path.join(tmp.name, "config.yaml")
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write("workspace:\n  project_id: demo-project\nsecurity:\n  auth_mode: local\nstorage:\n"
                     "  backend: memory\n")
        app = app_module.App(environ={"CONFIG_PATH": cfg})
        self.addCleanup(lambda: [p.shutdown(wait=False) for p in (app.gcp.pool, app.gcp.subpool,
                                                                   app.gcp.orchestrator)])
        calls = []
        app.gcp.request = lambda method, url, **kwargs: (calls.append(url), ({}, {}))[1]
        settings, version = app.settings.snapshot()
        identity = Identity("admin@example.com", "admin", "local", "local:admin")
        data = scope.scope_projects(base.Ctx(app, settings, version, identity))
        self.assertEqual([p["id"] for p in data["projects"]], ["demo-project"])
        folder = copy.deepcopy(settings)
        folder["workspace"].update({"project_id": "acme-prod", "scope_type": "organization", "scope_ids": ["42"]})
        ctx = base.Ctx(app, folder, version, identity)
        data = scope.scope_projects(ctx)
        self.assertEqual(data["scopeType"], "organization")
        self.assertEqual([p["id"] for p in data["projects"]], ["acme-prod"])
        self.assertFalse({w["code"] for w in ctx.warnings} & {"internal_error", "query_failed"})
        self.assertIn(scope.CRM + "/projects", calls)


class SharedComputationTest(unittest.TestCase):
    def test_one_computation_and_bounded_waits(self):
        cache = TTLCache(8)
        started, release = threading.Event(), threading.Event()
        runs = []

        def slow():
            runs.append(1)
            started.set()
            release.wait(5)
            return "value", 60

        results = {}
        owner = threading.Thread(target=lambda: results.setdefault("owner", scope._shared(cache, "k", slow, 0.3)))
        owner.start()
        self.assertTrue(started.wait(5))
        began = time.monotonic()
        self.assertIsNone(scope._shared(cache, "k", slow))  # pylint: disable=protected-access
        self.assertLess(time.monotonic() - began, 3)
        release.set()
        owner.join(5)
        self.assertEqual(results["owner"], "value")
        self.assertEqual(scope._shared(cache, "k", slow), "value")  # pylint: disable=protected-access
        self.assertEqual(len(runs), 1)

    def test_waiter_gets_the_owner_result(self):
        cache = TTLCache(8)
        started, release = threading.Event(), threading.Event()

        def slow():
            started.set()
            release.wait(5)
            return "value", 60

        owner = threading.Thread(target=lambda: scope._shared(cache, "k", slow, 10))  # pylint: disable=protected-access
        owner.start()
        self.assertTrue(started.wait(5))
        threading.Timer(0.2, release.set).start()
        self.assertEqual(scope._shared(cache, "k", lambda: ("other", 60)), "value")  # pylint: disable=protected-access
        owner.join(5)


if __name__ == "__main__":
    unittest.main()
