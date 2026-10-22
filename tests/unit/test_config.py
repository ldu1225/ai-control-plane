"""Configuration layering, validation and settings-update rules."""

import copy
import os
import tempfile
import unittest
from unittest import mock

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane import config
from controlplane.httpkit import ApiError


class IdentifierValidationTest(unittest.TestCase):
    def test_project_ids(self):
        self.assertTrue(config.valid_project("acme-prod-123"))
        self.assertTrue(config.valid_project("my-project-123"))
        self.assertTrue(config.valid_project("example.com:my-project"))
        self.assertFalse(config.valid_project("Bad_Project"))
        self.assertFalse(config.valid_project("abc"))
        self.assertFalse(config.valid_project("proj`; DROP"))
        self.assertFalse(config.valid_project(None))

    def test_dataset_table_and_columns(self):
        self.assertTrue(config.valid_dataset("ge_analytics"))
        self.assertFalse(config.valid_dataset("ge-analytics"))
        self.assertTrue(config.valid_table("gcp_billing_export_resource_v1_ABCDEF_123456_7890AB"))
        self.assertTrue(config.valid_table("events-2026"))
        self.assertFalse(config.valid_table("t`x"))
        self.assertFalse(config.valid_table("a.b"))
        self.assertTrue(config.valid_column_path("protopayload_auditlog.authenticationInfo.principalEmail"))
        self.assertFalse(config.valid_column_path("a..b"))
        self.assertFalse(config.valid_column_path("1abc"))

    def test_dataset_refs(self):
        self.assertEqual(config.parse_dataset_ref("ds", "p-project"), ("p-project", "ds"))
        self.assertEqual(config.parse_dataset_ref("other-proj.ds", "p-project"), ("other-proj", "ds"))
        self.assertEqual(config.parse_dataset_ref("other-proj:ds", "p-project"), ("other-proj", "ds"))
        self.assertIsNone(config.parse_dataset_ref("bad ref", "p-project"))

    def test_webhook_and_logo(self):
        self.assertTrue(config.valid_webhook(""))
        self.assertTrue(config.valid_webhook("https://hooks.slack.com/services/T/B/X"))
        self.assertFalse(config.valid_webhook("http://hooks.slack.com/services/T/B/X"))
        self.assertFalse(config.valid_webhook("https://evil.example.com/hook"))
        self.assertFalse(config.valid_webhook("https://user:pw@hooks.slack.com/x"))
        self.assertTrue(config.valid_logo_url("/assets/img/logo.svg"))
        self.assertTrue(config.valid_logo_url("https://cdn.example.com/logo.png"))
        self.assertFalse(config.valid_logo_url("/assets/../config.yaml"))
        self.assertFalse(config.valid_logo_url("javascript:alert(1)"))
        self.assertFalse(config.valid_logo_url("https://x.example.com/a\"onerror=\"x"))


class LayerMergeTest(unittest.TestCase):
    def test_deep_merge_does_not_mutate(self):
        base = {"a": {"b": 1, "c": 2}, "d": [1]}
        out = config.deep_merge(base, {"a": {"c": 3}, "d": [2]})
        self.assertEqual(out, {"a": {"b": 1, "c": 3}, "d": [2]})
        self.assertEqual(base, {"a": {"b": 1, "c": 2}, "d": [1]})

    def test_validate_layer_drops_unknown_and_invalid(self):
        with self.assertLogs("controlplane.config", level="WARNING"):
            clean = config.validate_layer({"branding": {"accent_color": "red", "organization_name": "Acme"},
                                           "unknown_section": {"x": 1},
                                           "server": {"port": 99999, "cache_ttl_seconds": 60}}, "test")
        self.assertEqual(clean, {"branding": {"organization_name": "Acme"}, "server": {"cache_ttl_seconds": 60}})

    def test_invalid_auth_mode_aborts(self):
        with self.assertRaises(ValueError):
            config.validate_layer({"security": {"auth_mode": "none"}}, "test")

    def test_env_layer(self):
        layer = config.env_layer({"PROJECT_ID": "env-project", "PORT": "9090", "ADMIN_EMAILS": "a@example.net, *@corp.example.com",
                                  "ORGANIZATION_NAME": "Env Org", "WORKSPACE_TIMEZONE": "Asia/Seoul",
                                  "BILLING_TABLE_ID": "", "AUTH_MODE": "iap"})
        self.assertEqual(layer["workspace"]["project_id"], "env-project")
        self.assertEqual(layer["server"]["port"], 9090)
        self.assertEqual(layer["security"]["admins"], ["a@example.net", "*@corp.example.com"])
        self.assertEqual(layer["branding"]["organization_name"], "Env Org")
        self.assertEqual(layer["workspace"]["timezone"], "Asia/Seoul")
        self.assertEqual(layer["security"]["auth_mode"], "iap")
        self.assertNotIn("billing", layer["workspace"])

    def test_unknown_environment_variables_are_ignored(self):
        layer = config.env_layer({"DASHBOARD_TITLE": "Old", "ORGANIZATION_NAME": "Current"})
        self.assertEqual(layer["branding"]["organization_name"], "Current")
        self.assertEqual(config.env_layer({"DASHBOARD_TITLE": "Old"}), {})

    def test_load_order_and_admin_union(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = os.path.join(tmp, "config.yaml")
            with open(main, "w", encoding="utf-8") as fh:
                fh.write("workspace:\n  project_id: yaml-project\n  timezone: Asia/Seoul\n"
                         "security:\n  admins: [main@example.com]\nbranding:\n  organization_name: From Yaml\n")
            with open(os.path.join(tmp, "config.local.yaml"), "w", encoding="utf-8") as fh:
                fh.write("workspace:\n  audit_dataset: local_ds\nsecurity:\n  admins: [local@example.com]\n")
            with mock.patch.dict(os.environ, {"CONFIG_PATH": main}):
                settings, loaded = config.load_base_settings({"ORGANIZATION_NAME": "From Env",
                                                              "ADMIN_EMAILS": "env@example.com"})
        self.assertEqual(loaded, ["config.yaml", "config.local.yaml"])
        self.assertEqual(settings["workspace"]["project_id"], "yaml-project")
        self.assertEqual(settings["workspace"]["audit_dataset"], "local_ds")
        self.assertEqual(settings["workspace"]["default_range_days"], 14)
        self.assertEqual(settings["branding"]["organization_name"], "From Env")
        self.assertEqual(settings["security"]["admins"], ["main@example.com", "local@example.com",
                                                          "env@example.com"])

    def test_finalize_invariants(self):
        settings = copy.deepcopy(config.DEFAULTS)
        settings["model_catalog"]["excluded_sku_patterns"] = ["preview"]
        settings["ai_engine"]["models"] = ["gemini-3.8-pro"]
        settings["ai_engine"]["default_model"] = "gemini-4-argon"
        config.finalize(settings)
        self.assertEqual(settings["model_catalog"]["excluded_sku_patterns"], ["code assist", "duet ai", "preview"])
        self.assertEqual(settings["ai_engine"]["models"], ["gemini-4-argon", "gemini-3.8-pro"])

    def test_workspace_configured(self):
        settings = copy.deepcopy(config.DEFAULTS)
        self.assertFalse(config.workspace_configured(settings))
        self.assertFalse(config.billing_configured(settings))
        settings["workspace"]["project_id"] = "real-project"
        self.assertTrue(config.workspace_configured(settings))
        self.assertFalse(config.billing_configured(settings))  # placeholder XXXXXX table
        settings["workspace"]["billing"]["table"] = "gcp_billing_export_v1_ABC"
        self.assertTrue(config.billing_configured(settings))
        self.assertEqual(config.billing_project(settings), "real-project")
        self.assertEqual(config.quota_project(settings), "real-project")


class SettingsUpdateTest(unittest.TestCase):
    def setUp(self):
        self.base = copy.deepcopy(config.DEFAULTS)
        self.base["security"]["admins"] = ["root@example.com"]
        self.effective = config.finalize(copy.deepcopy(self.base))

    def update(self, section, values):
        return config.validate_section_update(section, values, self.effective, self.base)

    def assertBadRequest(self, section, values):
        with self.assertRaises(ApiError) as ctx:
            self.update(section, values)
        self.assertEqual(ctx.exception.code, "bad_request")
        self.assertEqual(ctx.exception.status, 400)

    def test_valid_branding_update(self):
        out = self.update("branding", {"accent_color": "#112233", "default_locale": "ko"})
        self.assertEqual(out, {"branding.accent_color": "#112233", "branding.default_locale": "ko"})

    def test_unknown_section_and_keys(self):
        self.assertBadRequest("server", {"port": 1})
        self.assertBadRequest("branding", {"nope": 1})
        self.assertBadRequest("model_catalog", {"known_models": []})
        self.assertBadRequest("branding", {})

    def test_type_and_range_checks(self):
        self.assertBadRequest("branding", {"accent_color": "blue"})
        self.assertBadRequest("finops", {"spike_threshold_pct": "50"})
        self.assertBadRequest("modules", {"overview": "yes"})
        self.assertBadRequest("governance", {"token_cap": -1})
        self.assertBadRequest("governance", {"webhook_url": "https://evil.example.com/x"})

    def test_governance_section_maps_to_antigravity(self):
        out = self.update("governance", {"token_cap": 1000, "period": "weekly"})
        self.assertEqual(out, {"governance.antigravity.token_cap": 1000, "governance.antigravity.period": "weekly"})

    def test_ui_locked_keys(self):
        self.assertBadRequest("security", {"auth_mode": "iap"})
        out = self.update("security", {"auth_mode": "auto", "mask_pii_for_viewers": False})
        self.assertEqual(out, {"security.mask_pii_for_viewers": False})

    def test_admins_are_additive(self):
        out = self.update("security", {"admins": ["root@example.com", "New@Example.com", "*@corp.example.com"]})
        self.assertEqual(out["security.admins"], ["new@example.com", "*@corp.example.com"])
        self.assertBadRequest("security", {"admins": ["not-an-email"]})

    def test_cross_field_checks(self):
        self.assertBadRequest("agent_lifecycle", {"idle_days": 60, "deprecation_days": 30})
        self.assertBadRequest("ai_engine", {"default_model": "gemini-9-unknown"})
        out = self.update("ai_engine", {"models": ["gemini-3.8-flash", "gemini-4-argon"],
                                        "default_model": "gemini-4-argon"})
        self.assertEqual(out["ai_engine.default_model"], "gemini-4-argon")

    def test_reset_keys(self):
        self.assertEqual(config.reset_keys("governance", []), ["governance.antigravity"])
        self.assertEqual(config.reset_keys("branding", ["accent_color"]), ["branding.accent_color"])
        with self.assertRaises(ApiError):
            config.reset_keys("branding", ["nope"])
        with self.assertRaises(ApiError):
            config.reset_keys("security", ["auth_mode"])
        with self.assertRaises(ApiError):
            config.reset_keys("server", [])

    def test_viewer_masking(self):
        eff = copy.deepcopy(self.effective)
        eff["governance"]["antigravity"]["webhook_url"] = "https://hooks.slack.com/services/T/B/SECRET"
        eff["security"]["admins"] = ["admin@example.com", "*@corp.example.com"]
        masked = config.mask_settings_for_viewer(eff)
        self.assertEqual(masked["governance"]["antigravity"]["webhook_url"], "https://hooks.slack.com/***")
        self.assertEqual(masked["security"]["admins"], ["ad***@example.com", "*@corp.example.com"])
        self.assertIn("SECRET", eff["governance"]["antigravity"]["webhook_url"])

    def test_viewer_masking_hides_deployment_identifiers(self):
        eff = copy.deepcopy(self.effective)
        eff["storage"]["gcs_bucket"] = "acme-control-plane-settings"
        eff["security"]["google_client_id"] = "123-abc.apps.googleusercontent.com"
        eff["workspace"]["billing"]["account_id"] = "ABCDEF-123456-ABCDEF"
        eff["governance"]["antigravity"]["scheduler_invokers"] = ["cp-scheduler@acme.iam.gserviceaccount.com"]
        masked = config.mask_settings_for_viewer(eff)
        self.assertEqual(masked["storage"]["gcs_bucket"], "***")
        self.assertEqual(masked["security"]["google_client_id"], "***")
        self.assertEqual(masked["workspace"]["billing"]["account_id"], "***")
        self.assertEqual(masked["governance"]["antigravity"]["scheduler_invokers"],
                         ["cp***@acme.iam.gserviceaccount.com"])
        self.assertEqual(masked["security"]["iap_audience"], eff["security"]["iap_audience"] and "***")
        # A partial override document is masked without gaining keys.
        partial = {"workspace": {"billing": {"account_id": "ABCDEF-123456-ABCDEF"}}}
        self.assertEqual(config.mask_settings_for_viewer(partial), {"workspace": {"billing": {"account_id": "***"}}})
        # Policy values stay visible to viewers.
        self.assertEqual(masked["governance"]["antigravity"]["token_cap"], eff["governance"]["antigravity"]["token_cap"])


if __name__ == "__main__":
    unittest.main()
