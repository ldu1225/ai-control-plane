"""Gemini Enterprise feature management, app model selection, and AI Developer Tools inventory:
- app x feature & app x model inventory (Engine.features, Engine.modelConfigs, WidgetConfig resolvedModels)
- bulk updates through engines.patch (updateMask=features / modelConfigs / features,modelConfigs) with fresh-read
  merge, validation, audit, and viewer refusal
- live multi-project/location DevToolsConfig (v1alpha) inventory for AI Developer Tools & Antigravity models."""

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


def engine(engine_id, display, features=None, models=None, app_type="APP_TYPE_INTRANET", location="global", project=NUMBER):
    item = {"name": "projects/%s/locations/%s/collections/default_collection/engines/%s" % (project, location, engine_id),
            "displayName": display, "solutionType": "SOLUTION_TYPE_SEARCH",
            "updateTime": "2026-11-01T00:00:00Z"}
    if app_type:
        item["appType"] = app_type
    if features is not None:
        item["features"] = dict(features)
    if models is not None:
        item["modelConfigs"] = dict(models)
    return item


class Cloud:
    """Fake Discovery Engine: engines, widgetConfigs, and devToolsConfig per (project, location); records every call."""

    def __init__(self):
        self.calls = []
        self.audit = []
        self.engines = {}    # (project, location) -> [Engine]
        self.widgets = {}    # (location, engine_id) -> WidgetConfig
        self.dev_tools = {}  # (project, location) -> DevToolsConfig
        self.publisher_models = {}  # (hostname, publisher) -> [PublisherModel]
        self.fail = {}       # substring of "METHOD url" -> GcpError
        self.on_get = None   # hook run before an engine GET answers (simulates a concurrent change)

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
        if parsed.hostname and "aiplatform" in parsed.hostname and "/publishers/" in path and method == "GET":
            segs = path.split("/")
            pub = segs[segs.index("publishers") + 1]
            return {"publisherModels": list(self.publisher_models.get((parsed.hostname, pub), []))}, {}
        if "discoveryengine" not in parsed.hostname:
            return {}, {}
        parts = path.split("/")
        if path.endswith("/devToolsConfig") and method == "GET":
            proj, loc = parts[3], parts[5]
            cfg = self.dev_tools.get((proj, loc))
            if cfg is None:
                return {"name": "projects/%s/locations/%s/devToolsConfig" % (proj, loc)}, {}
            return json.loads(json.dumps(cfg)), {}
        if "/widgetConfigs/" in path and method == "GET":
            loc, eid = parts[5], parts[9]
            widget = self.widgets.get((loc, eid))
            if widget is None:
                raise GcpError("not_found", status=404, message=UPSTREAM_SECRET)
            return json.loads(json.dumps(widget)), {}
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
                masks = [m.strip() for m in (params.get("updateMask") or "").split(",") if m.strip()]
                assert masks and set(masks) <= {"features", "modelConfigs"}, params
                if "features" in masks:
                    item["features"] = dict(body["features"])
                if "modelConfigs" in masks:
                    item["modelConfigs"] = dict(body["modelConfigs"])
                return json.loads(json.dumps(item)), {}
        raise AssertionError("unexpected call %s %s" % (method, url))


class MergeTest(unittest.TestCase):
    def test_merge_changes_only_the_listed_keys(self):
        current = {"agent-gallery": "FEATURE_STATE_ON", "model-selector": "FEATURE_STATE_OFF",
                   "junk": "FEATURE_STATE_UNSPECIFIED"}
        merged = ge_features.merge(current, {"model-selector": "on", "agent-gallery": "default",
                                             "disable-image-generation": "on"})
        self.assertEqual(merged, {"model-selector": "FEATURE_STATE_ON", "disable-image-generation": "FEATURE_STATE_ON"})

    def test_merge_models_changes_only_the_listed_ids(self):
        current = {"gemini-4-argon": "MODEL_ENABLED", "gemini-2.5-pro": "MODEL_DISABLED",
                   "junk": "MODEL_STATE_UNSPECIFIED"}
        merged = ge_features.merge_models(current, {"gemini-4-argon": "default", "gemini-2.5-pro": "on",
                                                    "claude-opus-5-5": "on"})
        self.assertEqual(merged, {"gemini-2.5-pro": "MODEL_ENABLED", "claude-opus-5-5": "MODEL_ENABLED"})

    def test_feature_keys_follow_the_documented_shape(self):
        for key in ("*", "agent-gallery", "people-search-org-chart", "sobi", "skill-sharing-without-admin-approval"):
            self.assertRegex(key, ge_features.FEATURE_KEY_RE)
        for key in ("", "Agent-Gallery", "agent gallery", "agent--gallery", "-x", "a/b", "**", "x" * 2 + "\n"):
            self.assertNotRegex(key, ge_features.FEATURE_KEY_RE)

    def test_model_keys_follow_the_documented_shape(self):
        for key in ("gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.1-pro-preview",
                    "gemini-3.1-flash-image-preview", "claude-opus-5-5", "model_v1"):
            self.assertRegex(key, ge_features.MODEL_KEY_RE)
        for key in ("", "Gemini-4", "gemini 4", "-gemini", "../etc", "a/b", "x" * 81, "gemini-4\n"):
            self.assertNotRegex(key, ge_features.MODEL_KEY_RE)

    def test_ge_apps_are_intranet_apps_or_apps_with_switches_or_models(self):
        self.assertTrue(ge_features.is_ge_app(engine("a", "A")))
        self.assertTrue(ge_features.is_ge_app(engine("b", "B", {"agent-gallery": "FEATURE_STATE_ON"}, app_type=None)))
        self.assertTrue(ge_features.is_ge_app(engine("m", "M", models={"gemini-4-argon": "MODEL_ENABLED"}, app_type=None)))
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
            engine("ge-main", "Company assistant",
                   features={"agent-gallery": "FEATURE_STATE_ON", "disable-image-generation": "FEATURE_STATE_ON"},
                   models={"gemini-4-argon": "MODEL_ENABLED", "gemini-2.5-flash-image": "MODEL_DISABLED"}),
            engine("ge-pilot", "Pilot assistant"),
            engine("site-search", "Site search", app_type=None),
        ]
        self.cloud.engines[(LINKED, "us")] = [
            engine("ge-sales", "Sales assistant", {"*": "FEATURE_STATE_OFF"}, location="us", project="111"),
        ]
        self.cloud.widgets[("global", "ge-main")] = {
            "uiSettings": {
                "modelConfigInfo": {
                    "defaultModelId": "gemini-3.8-flash",
                    "resolvedModels": [
                        {"displayName": "Auto"},  # virtual entry without modelId must be skipped
                        {"modelId": "gemini-4-argon", "displayName": "Gemini 4 Argon", "isPreview": False},
                        {"modelId": "gemini-3.8-flash", "displayName": "Gemini 3.8 Flash", "isPreview": False},
                        {"modelId": "gemini-3.1-pro-preview", "displayName": "Gemini 3.1 Pro", "isPreview": True},
                    ],
                },
            },
        }
        self.cloud.dev_tools[(PROJECT, "global")] = {
            "name": "projects/987654321098/locations/global/devToolsConfig",
            "devToolsEnabled": True,
            "loggingConfig": {"metadataLoggingEnabled": True, "promptAndResponseLoggingEnabled": False},
            "agentConfig": {
                "outsideWorkspaceFileAccessPolicy": "FILE_ACCESS_POLICY_ALWAYS_ASK",
                "terminalCommandAutoExecutionPolicy": "TERMINAL_COMMAND_AUTO_EXECUTION_POLICY_PROCEED_IN_SANDBOX",
                "sandboxModeEnabled": True,
            },
            "browserConfig": {
                "browserEnabled": True,
                "javascriptExecutionPolicy": "JAVASCRIPT_EXECUTION_POLICY_REQUEST_REVIEW",
                "allowedUrls": ["https://docs.example.com/*"],
            },
            "mcpConfig": {
                "mcpEnabled": True,
                "mcpAllowlist": {
                    "mcpServers": {
                        "local_servers": [{"id": "gopls-mcp", "command": "go"}],
                        "remote_servers": [{"id": "bigquery"}, {"id": "custom", "url": "https://mcp.example.com"}],
                    },
                },
            },
            "pluginConfig": {"pluginsDisabled": True, "marketplaceDisabled": False},
            "modelConfig": {
                "allowedModels": ["gemini-4-argon", "gemini-3.8-flash", "Bad Model!"],
                "allowedThirdPartyModels": ["claude-opus-5-5"],
            },
            "overageConfig": {"overageEnabled": True},
            "featureConfig": {"boostEnabled": False},
        }
        self.cloud.dev_tools[(LINKED, "us")] = {
            "name": "projects/111/locations/us/devToolsConfig",
            "devToolsEnabled": True,
            "loggingConfig": {"metadataLoggingEnabled": False, "promptAndResponseLoggingEnabled": False},
            "agentConfig": {
                "outsideWorkspaceFileAccessPolicy": "FILE_ACCESS_POLICY_NOT_ENFORCED",
                "terminalCommandAutoExecutionPolicy": "TERMINAL_COMMAND_AUTO_EXECUTION_POLICY_NOT_ENFORCED",
            },
            "browserConfig": {
                "javascriptExecutionPolicy": "JAVASCRIPT_EXECUTION_POLICY_NOT_ENFORCED",
            },
        }

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

    # -- features & models inventory -----------------------------------------
    def test_inventory_lists_every_ge_app_with_switches_models_and_widget_metadata(self):
        status, payload = self.call("GET", "/api/v1/gemini-enterprise/features", identity=VIEWER)
        self.assertEqual(status, 200, payload)
        data = payload["data"]
        self.assertEqual([a["key"] for a in data["apps"]],
                         ["demo-project/global/ge-main", "demo-project/global/ge-pilot", "linked-project/us/ge-sales"])
        main = data["apps"][0]
        self.assertEqual(main["features"], {"agent-gallery": "on", "disable-image-generation": "on"})
        self.assertEqual(main["models"], {"gemini-4-argon": "on", "gemini-2.5-flash-image": "off"})
        self.assertEqual(main["resolvedModels"], ["gemini-4-argon", "gemini-3.8-flash", "gemini-3.1-pro-preview"])
        self.assertEqual(main["defaultModelId"], "gemini-3.8-flash")
        self.assertEqual(data["apps"][1]["resolvedModels"], [])  # 404 widgetConfig degrades silently
        self.assertEqual(data["apps"][2]["allFeatures"], "off")
        self.assertEqual(data["keys"], ["*", "agent-gallery", "disable-image-generation"])
        self.assertEqual(data["modelKeys"], ["gemini-2.5-flash-image", "gemini-3.1-pro-preview", "gemini-3.8-flash",
                                             "gemini-4-argon"])
        self.assertTrue(data["resolvedModelMeta"]["gemini-3.1-pro-preview"]["isPreview"])
        self.assertEqual(data["otherEngines"], 1)
        self.assertTrue(data["complete"])
        # GA API engine lists for every project and configured location.
        lists = [c for c in self.cloud.calls if c[0] == "GET" and urllib.parse.urlsplit(c[1]).path.endswith("/engines")]
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

    # -- apply (features, models, and combined atomic patch) -----------------
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
        self.assertEqual(main[2], {"updateMask": "features"})
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

    def test_apply_model_selection_merges_and_patches_atomically_with_features(self):
        def concurrent(item):
            # Concurrent console edit enabling gemini-3.7-flash must survive modelConfigs merge.
            if item["name"].endswith("/ge-main"):
                item.setdefault("modelConfigs", {})["gemini-3.7-flash"] = "MODEL_ENABLED"
        self.cloud.on_get = concurrent
        status, payload = self.apply([
            # Combined feature + model patch -> single PATCH with updateMask=features,modelConfigs
            {"project": PROJECT, "location": "global", "engineId": "ge-main",
             "set": {"model-selector": "on"},
             "setModels": {"gemini-4-argon": "default", "gemini-2.5-flash-image": "on", "claude-opus-5-5": "on"}},
            # Model-only patch -> updateMask=modelConfigs
            {"project": PROJECT, "location": "global", "engineId": "ge-pilot",
             "setModels": {"gemini-4-hydrogen": "on", "gemini-2.5-pro": "off"}},
            # Unchanged model state -> no PATCH
            {"project": LINKED, "location": "us", "engineId": "ge-sales",
             "setModels": {"gemini-4-argon": "default"}},
        ])
        self.assertEqual(status, 200, payload)
        data = payload["data"]
        self.assertEqual((data["updated"], data["unchanged"], data["failed"]), (2, 1, 0))
        patches = self.cloud.find("PATCH")
        self.assertEqual(len(patches), 2)
        main = next(c for c in patches if c[1].endswith("/ge-main"))
        self.assertEqual(main[2], {"updateMask": "features,modelConfigs"})
        self.assertEqual(main[3]["modelConfigs"], {
            "gemini-2.5-flash-image": "MODEL_ENABLED",
            "claude-opus-5-5": "MODEL_ENABLED",
            "gemini-3.7-flash": "MODEL_ENABLED",
        })
        pilot = next(c for c in patches if c[1].endswith("/ge-pilot"))
        self.assertEqual(pilot[2], {"updateMask": "modelConfigs"})
        self.assertEqual(pilot[3], {"modelConfigs": {"gemini-4-hydrogen": "MODEL_ENABLED",
                                                     "gemini-2.5-pro": "MODEL_DISABLED"}})
        # Audit entry records modelChanges alongside feature changes.
        entry = self.cloud.audit[0]
        main_audit = next(a for a in entry["apps"] if a["target"] == "demo-project/global/ge-main")
        self.assertEqual(main_audit["changes"], {"model-selector": "on"})
        self.assertEqual(main_audit["modelChanges"], {"gemini-4-argon": "default", "gemini-2.5-flash-image": "on",
                                                      "claude-opus-5-5": "on"})

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
                                       "setModels": {"gemini-4-argon": "off"}}], identity=VIEWER)
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
            [{"project": PROJECT, "location": "global", "engineId": "ge-main", "setModels": {}}],
            [{"project": PROJECT, "location": "global", "engineId": "ge-main", "setModels": ["gemini-4-argon"]}],
            [{"project": PROJECT, "location": "global", "engineId": "ge-main", "setModels": {"Bad Model": "on"}}],
            [{"project": PROJECT, "location": "global", "engineId": "ge-main", "setModels": {"gemini-4-argon": "allow"}}],
            [{"project": PROJECT, "location": "global", "engineId": "ge-main",
              "setModels": {"m%d" % i: "on" for i in range(ge_features.MAX_MODELS_PER_APP + 1)}}],
            [{"project": PROJECT, "location": "global", "engineId": "ge-main", "set": {"feedback": "on"}}] * 2,
            [{"project": PROJECT, "location": "global", "engineId": "e%d" % i, "set": {"feedback": "on"}}
             for i in range(ge_features.MAX_APPS_PER_APPLY + 1)],
        ]
        for changes in bad:
            self.app.limiter = app_module.RateLimiter()
            status, payload = self.apply(changes)
            self.assertEqual(status, 400, (changes[:1], payload))
        self.assertEqual(self.cloud.find("PATCH"), [])
        self.assertEqual(self.cloud.audit, [])

    # -- AI developer tools & Antigravity authorized models (DevToolsConfig) -
    def test_dev_tools_inventory_parses_all_verified_fields_and_enums(self):
        status, payload = self.call("GET", "/api/v1/gemini-enterprise/dev-tools", identity=VIEWER)
        self.assertEqual(status, 200, payload)
        data = payload["data"]
        self.assertEqual(data["apiVersion"], "v1alpha")
        self.assertFalse(data["writeSupported"])
        self.assertEqual(data["writeSupportReason"], "console_only")
        self.assertTrue(data["complete"])
        self.assertEqual(data["locations"], ["global", "us", "eu"])
        by_key = {c["key"]: c for c in data["configs"]}
        self.assertEqual(len(by_key), 6)  # 2 projects x 3 locations

        g = by_key["demo-project/global"]
        self.assertTrue(g["devToolsEnabled"])
        self.assertEqual(g["logging"], {"metadataLoggingEnabled": True, "promptAndResponseLoggingEnabled": False})
        self.assertEqual(g["agent"]["outsideWorkspaceFileAccessPolicy"], "always_ask")
        self.assertEqual(g["agent"]["terminalCommandAutoExecutionPolicy"], "proceed_in_sandbox")
        self.assertTrue(g["agent"]["sandboxModeEnabled"])
        self.assertTrue(g["browser"]["browserEnabled"])
        self.assertEqual(g["browser"]["javascriptExecutionPolicy"], "request_review")
        self.assertEqual(g["browser"]["extra"], {"allowedUrls": ["https://docs.example.com/*"]})
        self.assertTrue(g["mcp"]["mcpEnabled"])
        self.assertTrue(g["mcp"]["hasAllowlist"])
        self.assertEqual(g["mcp"]["localServerCount"], 1)
        self.assertEqual(g["mcp"]["remoteServerCount"], 2)
        self.assertEqual(g["mcp"]["localServerIds"], ["gopls-mcp"])
        self.assertEqual(g["mcp"]["remoteServerIds"], ["bigquery", "custom"])
        self.assertEqual(g["plugins"], {"pluginsDisabled": True, "marketplaceDisabled": False})
        # Malformed model ID ("Bad Model!") dropped by sanitizer.
        self.assertEqual(g["models"], {
            "mode": "allowlist",
            "allowedModels": ["gemini-4-argon", "gemini-3.8-flash"],
            "allowedThirdPartyModels": ["claude-opus-5-5"],
        })
        self.assertEqual(g["overage"], {"overageEnabled": True})
        self.assertEqual(g["features"], {"boostEnabled": False})

        # Verified *_NOT_ENFORCED enum mappings & default all_gemini mode.
        u = by_key["linked-project/us"]
        self.assertEqual(u["agent"]["outsideWorkspaceFileAccessPolicy"], "allow")
        self.assertEqual(u["agent"]["terminalCommandAutoExecutionPolicy"], "always_proceed")
        self.assertEqual(u["browser"]["javascriptExecutionPolicy"], "allowed")
        self.assertEqual(u["models"], {"mode": "all_gemini", "allowedModels": [], "allowedThirdPartyModels": []})

        # Unset location has clean proto3 defaults.
        e = by_key["demo-project/eu"]
        self.assertFalse(e["devToolsEnabled"])
        self.assertFalse(e["hasAnyConfig"])
        self.assertEqual(e["agent"]["outsideWorkspaceFileAccessPolicy"], "default")
        self.assertEqual(data["observedModels"], ["gemini-3.8-flash", "gemini-4-argon"])
        self.assertEqual(data["observedThirdPartyModels"], ["claude-opus-5-5"])

    def test_dev_tools_inventory_handles_unreadable_and_disabled_projects(self):
        self.cloud.fail["/projects/%s/locations/eu/devToolsConfig" % LINKED] = GcpError(
            "denied", status=403, message=UPSTREAM_SECRET)
        status, payload = self.call("GET", "/api/v1/gemini-enterprise/dev-tools")
        self.assertEqual(status, 200, payload)
        data, warnings = payload["data"], payload["meta"]["warnings"]
        self.assertEqual(data["unreadableProjects"], [LINKED])
        self.assertFalse(data["complete"])
        self.assertTrue(any(w["code"] == "partial_data" and LINKED in w["message"] for w in warnings))
        self.assertNotIn(UPSTREAM_SECRET, json.dumps(payload))

    def test_dev_tools_inventory_is_null_when_all_reads_fail(self):
        self.cloud.fail["/devToolsConfig"] = GcpError("denied", status=403, message=UPSTREAM_SECRET)
        status, payload = self.call("GET", "/api/v1/gemini-enterprise/dev-tools")
        self.assertEqual(status, 200)
        self.assertIsNone(payload["data"]["configs"])
        self.assertNotIn(UPSTREAM_SECRET, json.dumps(payload))

    def test_dev_tools_refresh_bypasses_cache(self):
        path = "/api/v1/gemini-enterprise/dev-tools"
        _, first = self.call("GET", path)
        eu_before = next(c for c in first["data"]["configs"] if c["key"] == "demo-project/eu")
        self.assertFalse(eu_before["devToolsEnabled"])
        self.cloud.dev_tools[(PROJECT, "eu")] = {
            "name": "projects/987654321098/locations/eu/devToolsConfig",
            "devToolsEnabled": True,
        }
        _, cached = self.call("GET", path)
        self.assertFalse(next(c for c in cached["data"]["configs"] if c["key"] == "demo-project/eu")["devToolsEnabled"])
        _, fresh = self.call("GET", path + "?refresh=1")
        self.assertTrue(next(c for c in fresh["data"]["configs"] if c["key"] == "demo-project/eu")["devToolsEnabled"])

    def test_inventory_and_dev_tools_auto_discover_model_garden_models(self):
        self.cloud.publisher_models[("aiplatform.googleapis.com", "google")] = [
            {"name": "publishers/google/models/gemini-4-neon", "launchStage": "GA"},
            {"name": "publishers/google/models/gemini-3.5-flash-lite", "launchStage": "GA"},
            {"name": "publishers/google/models/gemini-3.1-pro-preview", "launchStage": "PUBLIC_PREVIEW"},
            {"name": "publishers/google/models/gemini-3.1-flash-image-preview", "launchStage": "PUBLIC_PREVIEW"},
            # Non-GE modalities & legacy snapshots must be filtered out automatically.
            {"name": "publishers/google/models/gemini-1.5-pro-002", "launchStage": "GA"},
            {"name": "publishers/google/models/gemini-2.5-flash-preview-04-17", "launchStage": "PUBLIC_PREVIEW"},
            {"name": "publishers/google/models/gemini-embedding-2", "launchStage": "GA"},
            {"name": "publishers/google/models/gemini-2.5-pro-tts", "launchStage": "GA"},
            {"name": "publishers/google/models/gemini-3.5-transcribe", "launchStage": "GA"},
            {"name": "publishers/google/models/gemini-live-2.5-flash-native-audio", "launchStage": "GA"},
            {"name": "publishers/google/models/gemini-omni-1.2-flash", "launchStage": "GA"},
            {"name": "publishers/google/models/gemini-nano-banana-2.1", "launchStage": "GA"},
            {"name": "publishers/google/models/veo-3.1-generate-001", "launchStage": "GA"},
        ]
        self.cloud.publisher_models[("us-central1-aiplatform.googleapis.com", "google")] = [
            {"name": "publishers/google/models/gemini-2.5-flash-image", "launchStage": "GA"},
            {"name": "publishers/google/models/gemini-4-neon", "launchStage": "GA"},  # duplicate deduped
            {"name": "publishers/google/models/gemini-robotics-er-2-preview", "launchStage": "PUBLIC_PREVIEW"},
        ]
        self.cloud.publisher_models[("aiplatform.googleapis.com", "anthropic")] = [
            {"name": "publishers/anthropic/models/claude-sonnet-5-5", "launchStage": "GA"},
            {"name": "publishers/anthropic/models/claude-opus-5-5", "launchStage": "GA"},
            {"name": "publishers/anthropic/models/claude-haiku-4-5", "launchStage": "GA"},
        ]

        status, feat_payload = self.call("GET", "/api/v1/gemini-enterprise/features?refresh=1")
        self.assertEqual(status, 200, feat_payload)
        self.assertEqual(feat_payload["meta"]["warnings"], [])
        discovered = feat_payload["data"]["discoveredModels"]
        self.assertEqual(
            [m["modelId"] for m in discovered],
            [
                "gemini-4-neon",
                "gemini-3.5-flash-lite",
                "gemini-3.1-pro-preview",
                "gemini-3.1-flash-image-preview",
                "gemini-2.5-flash-image",
                "claude-opus-5-5",
                "claude-sonnet-5-5",
                "claude-haiku-4-5",
            ],
        )
        by_id = {m["modelId"]: m for m in discovered}
        self.assertEqual(by_id["gemini-4-neon"]["displayName"], "Gemini 4 Neon")
        self.assertEqual(by_id["gemini-3.5-flash-lite"]["displayName"], "Gemini 3.5 Flash-Lite")
        self.assertEqual(by_id["gemini-3.1-pro-preview"]["displayName"], "Gemini 3.1 Pro")
        self.assertTrue(by_id["gemini-3.1-pro-preview"]["isPreview"])
        self.assertEqual(by_id["gemini-2.5-flash-image"]["group"], "image")
        self.assertEqual(by_id["claude-opus-5-5"]["displayName"], "Anthropic Claude Opus 5.5")
        self.assertEqual(by_id["claude-opus-5-5"]["group"], "partner")
        self.assertIn(ge_features.MODEL_GARDEN_SOURCE, feat_payload["meta"]["sources"])

        # DevTools (Antigravity) excludes image-only models while keeping Gemini chat + Claude partner models.
        status, dt_payload = self.call("GET", "/api/v1/gemini-enterprise/dev-tools?refresh=1")
        self.assertEqual(status, 200, dt_payload)
        self.assertEqual(dt_payload["meta"]["warnings"], [])
        dt_ids = [m["modelId"] for m in dt_payload["data"]["discoveredModels"]]
        self.assertEqual(
            dt_ids,
            [
                "gemini-4-neon",
                "gemini-3.5-flash-lite",
                "gemini-3.1-pro-preview",
                "claude-opus-5-5",
                "claude-sonnet-5-5",
                "claude-haiku-4-5",
            ],
        )

    def test_model_garden_failure_is_fail_open_without_warnings(self):
        self.cloud.fail["aiplatform.googleapis.com"] = GcpError("denied", status=403, message=UPSTREAM_SECRET)
        status, payload = self.call("GET", "/api/v1/gemini-enterprise/features?refresh=1")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["meta"]["warnings"], [])
        self.assertEqual(payload["data"]["discoveredModels"], [])
        self.assertNotIn(UPSTREAM_SECRET, json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
