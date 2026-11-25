"""Gemini Enterprise feature management: the app x feature inventory and bulk updates through engines.patch with
updateMask=features (merge on the freshly read map, validation, audit, viewers refused)."""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane import app as app_module
from controlplane.gcp import GcpError
from controlplane.security import Identity
from controlplane.services import ge_features

PROJECT = "demo-project"
LINKED = "linked-project"
NUMBER = "987654321098"
UPSTREAM_SECRET = "upstream detail that must stay in the server log"

CONFIG = """workspace:
  project_id: demo-project
  projects: [linked-project]
  scan_all_projects: false
  audit_dataset: ge_analytics
  timezone: UTC
  discovery_engine_locations: [global, us]
security:
  auth_mode: local
storage:
  backend: memory
"""

ADMIN = Identity("admin@example.com", "admin", "google", "user:admin@example.com")
VIEWER = Identity("viewer@example.com", "viewer", "google", "user:viewer@example.com")


def engine(engine_id, display, features=None, app_type="APP_TYPE_INTRANET", location="global", project=NUMBER):
    item = {"name": "projects/%s/locations/%s/collections/default_collection/engines/%s" % (project, location, engine_id),
            "displayName": display, "solutionType": "SOLUTION_TYPE_SEARCH",
            "updateTime": "2026-11-01T00:00:00Z"}
    if app_type:
        item["appType"] = app_type
    if features is not None:
        item["features"] = dict(features)
    return item


class Cloud:
    """Fake Discovery Engine: engines per (project, location); records every call."""

    def __init__(self):
        self.calls = []
        self.audit = []
        self.engines = {}   # (project, location) -> [Engine]
        self.fail = {}      # substring of "METHOD url" -> GcpError
        self.on_get = None  # hook run before an engine GET answers (simulates a concurrent change)

    def find(self, method, needle=""):
        return [c for c in self.calls if c[0] == method and needle in c[1]]

    def _engine(self, location, engine_id):
        for (_, loc), items in self.engines.items():
            if loc != location:
                continue
            for item in items:
                if item["name"].rsplit("/", 1)[1] == engine_id:
                    return item
        return None

    def request(self, method, url, **kwargs):
        params = dict(kwargs.get("params") or {})
        body = kwargs.get("json_body")
        self.calls.append((method, url, params, body))
        for needle, err in self.fail.items():
            if needle in "%s %s" % (method, url):
                raise err
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path
        if path.endswith("/entries:write"):
            self.audit.extend(e["jsonPayload"] for e in body["entries"])
            return {}, {}
        if "discoveryengine" not in parsed.hostname:
            return {}, {}
        parts = path.split("/")
        if path.endswith("/engines") and method == "GET":
            return {"engines": self.engines.get((parts[3], parts[5]), [])}, {}
        if "/engines/" in path:
            item = self._engine(parts[5], parts[-1])
            if item is None:
                raise GcpError("not_found", status=404, message=UPSTREAM_SECRET)
            if method == "GET":
                if self.on_get:
                    self.on_get(item)
                return json.loads(json.dumps(item)), {}
            if method == "PATCH":
                assert params == {"updateMask": "features"}, params
                item["features"] = dict(body["features"])
                return json.loads(json.dumps(item)), {}
        raise AssertionError("unexpected call %s %s" % (method, url))


class MergeTest(unittest.TestCase):
    def test_merge_changes_only_the_listed_keys(self):
        current = {"agent-gallery": "FEATURE_STATE_ON", "model-selector": "FEATURE_STATE_OFF",
                   "junk": "FEATURE_STATE_UNSPECIFIED"}
        merged = ge_features.merge(current, {"model-selector": "on", "agent-gallery": "default",
                                             "disable-image-generation": "on"})
        self.assertEqual(merged, {"model-selector": "FEATURE_STATE_ON", "disable-image-generation": "FEATURE_STATE_ON"})

    def test_feature_keys_follow_the_documented_shape(self):
        for key in ("*", "agent-gallery", "people-search-org-chart", "sobi", "skill-sharing-without-admin-approval"):
            self.assertRegex(key, ge_features.FEATURE_KEY_RE)
        for key in ("", "Agent-Gallery", "agent gallery", "agent--gallery", "-x", "a/b", "**", "x" * 2 + "\n"):
            self.assertNotRegex(key, ge_features.FEATURE_KEY_RE)

    def test_ge_apps_are_intranet_apps_or_apps_with_switches(self):
        self.assertTrue(ge_features.is_ge_app(engine("a", "A")))
        self.assertTrue(ge_features.is_ge_app(engine("b", "B", {"agent-gallery": "FEATURE_STATE_ON"}, app_type=None)))
        self.assertFalse(ge_features.is_ge_app(engine("c", "Site search", app_type=None)))


class FeatureManagementTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = os.path.join(cls.tmp.name, "config.yaml")
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write(CONFIG)
        with mock.patch.dict(os.environ, {"CONFIG_PATH": cfg}):
            cls.app = app_module.App(environ={"CONFIG_PATH": cfg})
        cls.server = app_module.make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.cloud = Cloud()
        self.app.gcp.request = self.cloud.request
        self.app.cache.clear()
        self.app.limiter = app_module.RateLimiter()
        self.cloud.engines[(PROJECT, "global")] = [
            engine("ge-main", "Company assistant", {"agent-gallery": "FEATURE_STATE_ON",
                                                     "disable-image-generation": "FEATURE_STATE_ON"}),
            engine("ge-pilot", "Pilot assistant"),
            engine("site-search", "Site search", app_type=None),
        ]
        self.cloud.engines[(LINKED, "us")] = [
            engine("ge-sales", "Sales assistant", {"*": "FEATURE_STATE_OFF"}, location="us", project="111"),
        ]

    def call(self, method, path, body=None, identity=None):
        headers, raw = {}, None
        if method == "POST":
            raw = json.dumps(body or {})
            token = "t" * 40
            headers.update({"Content-Type": "application/json", "X-CSRF-Token": token, "Cookie": "cp_csrf=%s" % token})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            with mock.patch.object(app_module, "authenticate", return_value=identity or ADMIN):
                conn.request(method, path, body=raw, headers=headers)
                resp = conn.getresponse()
                return resp.status, json.loads(resp.read() or b"{}")
        finally:
            conn.close()

    def apply(self, changes, identity=None):
        return self.call("POST", "/api/v1/gemini-enterprise/features/apply", {"changes": changes}, identity=identity)

    # -- inventory -----------------------------------------------------------
    def test_inventory_lists_every_ge_app_with_its_switches(self):
        status, payload = self.call("GET", "/api/v1/gemini-enterprise/features", identity=VIEWER)
        self.assertEqual(status, 200, payload)
        data = payload["data"]
        self.assertEqual([a["key"] for a in data["apps"]],
                         ["demo-project/global/ge-main", "demo-project/global/ge-pilot", "linked-project/us/ge-sales"])
        main = data["apps"][0]
        self.assertEqual(main["features"], {"agent-gallery": "on", "disable-image-generation": "on"})
        self.assertEqual(data["apps"][2]["allFeatures"], "off")
        self.assertEqual(data["keys"], ["*", "agent-gallery", "disable-image-generation"])
        self.assertEqual(data["otherEngines"], 1)
        self.assertTrue(data["complete"])
        # GA API and the default collection of every project and configured location.
        lists = self.cloud.find("GET", "/engines")
        self.assertEqual(len(lists), 4)
        self.assertTrue(all("/v1/projects/" in c[1] and "/collections/default_collection/engines" in c[1] for c in lists))
        self.assertTrue(any(c[1].startswith("https://us-discoveryengine.googleapis.com/") for c in lists))

    def test_inventory_is_null_when_no_list_can_be_read(self):
        self.cloud.fail["/engines"] = GcpError("denied", status=403, message=UPSTREAM_SECRET)
        status, payload = self.call("GET", "/api/v1/gemini-enterprise/features")
        self.assertEqual(status, 200)
        self.assertIsNone(payload["data"]["apps"])
        self.assertNotIn(UPSTREAM_SECRET, json.dumps(payload))

    def test_refresh_skips_the_cache_so_new_apps_appear(self):
        path = "/api/v1/gemini-enterprise/features"
        _, first = self.call("GET", path)
        self.assertEqual(len(first["data"]["apps"]), 3)
        self.cloud.engines[(LINKED, "global")] = [engine("ge-new", "Just created", location="global", project="111")]
        _, cached = self.call("GET", path)
        self.assertEqual(len(cached["data"]["apps"]), 3)
        _, fresh = self.call("GET", path + "?refresh=1")
        self.assertEqual(len(fresh["data"]["apps"]), 4)
        self.assertIn("linked-project/global/ge-new", [a["key"] for a in fresh["data"]["apps"]])
        # The fresh result replaces the cached one for later plain requests.
        _, after = self.call("GET", path)
        self.assertEqual(len(after["data"]["apps"]), 4)

    def test_unreadable_linked_projects_are_named_in_a_warning(self):
        self.cloud.fail["/projects/%s/locations/us/" % LINKED] = GcpError("denied", status=403, message=UPSTREAM_SECRET)
        _, payload = self.call("GET", "/api/v1/gemini-enterprise/features")
        data, warnings = payload["data"], payload["meta"]["warnings"]
        self.assertEqual(data["unreadableProjects"], [LINKED])
        self.assertFalse(data["complete"])
        self.assertNotIn("linked-project/us/ge-sales", [a["key"] for a in data["apps"]])
        partial = [w for w in warnings if w["code"] == "partial_data"]
        self.assertEqual(len(partial), 1)
        self.assertIn(LINKED, partial[0]["message"])
        self.assertNotIn(UPSTREAM_SECRET, json.dumps(payload))

    def test_linked_projects_without_the_api_stay_silent(self):
        self.cloud.fail["/projects/%s/" % LINKED] = GcpError(
            "denied", status=403, message="Discovery Engine API has not been used in project 111 before or it is disabled")
        _, payload = self.call("GET", "/api/v1/gemini-enterprise/features")
        self.assertEqual(payload["data"]["unreadableProjects"], [])
        self.assertEqual(payload["meta"]["warnings"], [])
        self.assertTrue(payload["data"]["complete"])

    # -- apply ---------------------------------------------------------------
    def test_apply_merges_into_the_freshly_read_map_of_each_app(self):
        def concurrent(item):
            # Someone turned on NotebookLM in the console after the matrix was loaded: it must survive.
            if item["name"].endswith("/ge-main"):
                item.setdefault("features", {})["notebook-lm"] = "FEATURE_STATE_ON"
        self.cloud.on_get = concurrent
        status, payload = self.apply([
            {"project": PROJECT, "location": "global", "engineId": "ge-main",
             "set": {"agent-gallery": "off", "disable-image-generation": "default", "model-selector": "on"}},
            {"project": PROJECT, "location": "global", "engineId": "ge-pilot", "set": {"model-selector": "on"}},
            {"project": LINKED, "location": "us", "engineId": "ge-sales", "set": {"*": "off"}},
        ])
        self.assertEqual(status, 200, payload)
        data = payload["data"]
        self.assertEqual((data["updated"], data["unchanged"], data["failed"]), (2, 1, 0))
        patches = self.cloud.find("PATCH")
        self.assertEqual(len(patches), 2)
        main = next(c for c in patches if c[1].endswith("/ge-main"))
        self.assertEqual(main[3], {"features": {"agent-gallery": "FEATURE_STATE_OFF", "model-selector": "FEATURE_STATE_ON",
                                                "notebook-lm": "FEATURE_STATE_ON"}})
        # The resource name Google returned (project number) is patched, on the GA endpoint.
        self.assertIn("/v1/projects/%s/locations/global/collections/default_collection/engines/ge-main" % NUMBER, main[1])
        results = {r["key"]: r for r in data["results"]}
        self.assertEqual(results["linked-project/us/ge-sales"]["status"], "unchanged")
        self.assertEqual(results["demo-project/global/ge-pilot"]["features"], {"model-selector": "on"})
        # One audit entry with every app's outcome.
        self.assertEqual(len(self.cloud.audit), 1)
        entry = self.cloud.audit[0]
        self.assertEqual((entry["action"], entry["actor"], entry["result"]), ("ge_features_update", ADMIN.email, "success"))
        self.assertEqual(len(entry["apps"]), 3)
        self.assertTrue(data["auditLogged"])

    def test_failures_are_reported_per_app_without_upstream_detail(self):
        self.cloud.fail["PATCH https://discoveryengine.googleapis.com/v1/projects/%s/locations/global/collections/" \
                        "default_collection/engines/ge-pilot" % NUMBER] = GcpError("denied", status=403,
                                                                                    message=UPSTREAM_SECRET)
        status, payload = self.apply([
            {"project": PROJECT, "location": "global", "engineId": "ge-main", "set": {"feedback": "off"}},
            {"project": PROJECT, "location": "global", "engineId": "ge-pilot", "set": {"feedback": "off"}},
            {"project": PROJECT, "location": "global", "engineId": "missing", "set": {"feedback": "off"}},
        ])
        self.assertEqual(status, 200, payload)
        data = payload["data"]
        self.assertEqual((data["updated"], data["failed"]), (1, 2))
        errors = {r["engineId"]: r.get("error") for r in data["results"]}
        self.assertEqual(errors, {"ge-main": None, "ge-pilot": "denied", "missing": "not_found"})
        self.assertNotIn(UPSTREAM_SECRET, json.dumps(payload))
        self.assertEqual(self.cloud.audit[0]["result"], "partial")

    def test_viewers_cannot_apply(self):
        status, payload = self.apply([{"project": PROJECT, "location": "global", "engineId": "ge-main",
                                       "set": {"feedback": "off"}}], identity=VIEWER)
        self.assertEqual(status, 403, payload)
        self.assertEqual(self.cloud.find("PATCH"), [])

    def test_requests_outside_the_workspace_or_malformed_are_refused(self):
        bad = [
            [],
            [{"project": "other-project", "location": "global", "engineId": "ge-main", "set": {"feedback": "on"}}],
            [{"project": PROJECT, "location": "eu", "engineId": "ge-main", "set": {"feedback": "on"}}],
            [{"project": PROJECT, "location": "global", "engineId": "../x", "set": {"feedback": "on"}}],
            [{"project": PROJECT, "location": "global", "engineId": "ge-main", "set": {"Bad Key": "on"}}],
            [{"project": PROJECT, "location": "global", "engineId": "ge-main", "set": {"feedback": "enabled"}}],
            [{"project": PROJECT, "location": "global", "engineId": "ge-main", "set": {}}],
            [{"project": PROJECT, "location": "global", "engineId": "ge-main", "set": {"feedback": "on"}}] * 2,
            [{"project": PROJECT, "location": "global", "engineId": "e%d" % i, "set": {"feedback": "on"}}
             for i in range(ge_features.MAX_APPS_PER_APPLY + 1)],
        ]
        for changes in bad:
            status, payload = self.apply(changes)
            self.assertEqual(status, 400, (changes[:1], payload))
        self.assertEqual(self.cloud.find("PATCH"), [])
        self.assertEqual(self.cloud.audit, [])


if __name__ == "__main__":
    unittest.main()
