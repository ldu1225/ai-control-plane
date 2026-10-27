"""Guest access (security.guest_access): read-only visitors without a session for public demos.

Covers the identity, page and API behaviour in google mode, the scrubbing of identifiers from everything a
guest receives, the environment switches and the default (off).
"""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane import app as app_module
from controlplane import config
from controlplane.httpkit import ApiError
from controlplane.security import (BULLET, GUEST_MODE, GUEST_READ_ONLY_MESSAGE, authenticate, client_key,
                                   guest_scrub, guest_scrub_text)

CLIENT_ID = "123456789012-abcdefghijklmnop.apps.googleusercontent.com"
CONFIG = """workspace:
  project_id: demo-project
  audit_dataset: ge_analytics
  timezone: UTC
security:
  auth_mode: google
  google_client_id: %s
  admins: [admin@example.com]
  guest_access: true
  guest_redact_terms: [demo-project, Contoso]
storage:
  backend: memory
""" % CLIENT_ID
ENV = {"GOOGLE_OAUTH_CLIENT_SECRET": "test-client-secret", "SESSION_SECRET": "s" * 48}
TERMS = ("demo-project", "Contoso", "AB", "acme", "콘토소")


class GuestScrubTest(unittest.TestCase):
    def test_emails_lose_their_domain(self):
        self.assertEqual(guest_scrub_text("by admin@acme.example.com"),
                         "by ad%s@%s.%s.com" % (BULLET * 3, BULLET * 4, BULLET * 7))
        # Already viewer-masked addresses keep their first characters only.
        self.assertEqual(guest_scrub_text("ad***@corp.example.com"),
                         "ad%s@%s.%s.com" % (BULLET * 3, BULLET * 4, BULLET * 7))
        # Service accounts keep their name; the project ID label is hidden.
        self.assertEqual(guest_scrub_text("runtime@my-project.iam.gserviceaccount.com"),
                         "runtime@%s.iam.gserviceaccount.com" % (BULLET * 8))

    def test_terms_respect_word_boundaries(self):
        self.assertEqual(guest_scrub_text("AB CD agent", TERMS), BULLET * 3 + " CD agent")
        self.assertEqual(guest_scrub_text("AB전자 bot", TERMS), BULLET * 3 + "전자 bot")
        self.assertEqual(guest_scrub_text("crab ABC", TERMS), "crab ABC")
        self.assertEqual(guest_scrub_text("bucket demo-project-settings", TERMS),
                         "bucket %s-settings" % (BULLET * 8))
        self.assertEqual(guest_scrub_text("Contoso and CONTOSO", TERMS), "%s and %s" % (BULLET * 7, BULLET * 7))
        self.assertEqual(guest_scrub_text("콘토소 데모", TERMS), BULLET * 3 + " 데모")

    def test_billing_ids_and_project_numbers(self):
        self.assertEqual(guest_scrub_text("account 01AB23-CD45EF-678901"), "account XXXXXX-XXXXXX-XXXXXX")
        self.assertEqual(guest_scrub_text("gcp_billing_export_resource_v1_01AB23_CD45EF_678901"),
                         "gcp_billing_export_resource_v1_XXXXXX_XXXXXX_XXXXXX")
        self.assertEqual(guest_scrub_text("service-123456789012@gcp-sa-iap.iam.gserviceaccount.com"),
                         "service-%s@%s.iam.gserviceaccount.com" % (BULLET * 12, BULLET * 8))
        self.assertEqual(guest_scrub_text("tokens 1234567890123"), "tokens 1234567890123")  # 13 digits stay

    def test_payload_walk_keeps_keys_numbers_and_csrf(self):
        payload = {"projectId": "demo-project", "count": 123456789012, "session": {"csrfToken": "demo-project"},
                   "rows": [{"agent": "Contoso helper"}], "sources": ("demo-project.dataset",)}
        out = guest_scrub(payload, TERMS)
        self.assertEqual(out["projectId"], BULLET * 8)
        self.assertEqual(out["count"], 123456789012)
        self.assertEqual(out["session"]["csrfToken"], "demo-project")
        self.assertEqual(out["rows"][0]["agent"], BULLET * 7 + " helper")
        self.assertEqual(out["sources"], [BULLET * 8 + ".dataset"])
        self.assertEqual(payload["projectId"], "demo-project")  # deep copy

    def test_client_key_uses_last_forwarded_address(self):
        self.assertEqual(client_key({"X-Forwarded-For": "1.1.1.1, 203.0.113.9"}, "10.0.0.1"), "203.0.113.9")
        self.assertEqual(client_key({"X-Forwarded-For": "not-an-ip"}, "10.0.0.1"), "10.0.0.1")
        self.assertEqual(client_key({}, ""), "unknown")


class GuestSwitchTest(unittest.TestCase):
    def test_environment_switches(self):
        layer = config.env_layer({"GUEST_ACCESS": "true", "GUEST_REDACT_TERMS": "Contoso, demo-project ,"})
        self.assertIs(layer["security"]["guest_access"], True)
        self.assertEqual(layer["security"]["guest_redact_terms"], ["Contoso", "demo-project"])
        self.assertIs(config.env_layer({"GUEST_ACCESS": "off"})["security"]["guest_access"], False)
        self.assertNotIn("security", config.env_layer({"GUEST_ACCESS": "maybe"}))

    def test_off_by_default_and_locked_in_the_ui(self):
        self.assertIs(config.DEFAULTS["security"]["guest_access"], False)
        self.assertIn("security.guest_access", config.UI_LOCKED_KEYS)
        self.assertIn("security.guest_redact_terms", config.UI_LOCKED_KEYS)
        settings = support.make_settings(security={"auth_mode": "google"})
        with self.assertRaises(ApiError):
            authenticate({}, "127.0.0.1", settings, None, environ={}, sessions=None)
        settings = support.make_settings(security={"auth_mode": "google", "guest_access": True})
        identity = authenticate({"X-Forwarded-For": "198.51.100.7"}, "127.0.0.1", settings, None, environ={},
                                sessions=None)
        self.assertEqual((identity.email, identity.role, identity.auth_mode, identity.rate_key),
                         ("", "viewer", GUEST_MODE, "guest:198.51.100.7"))
        self.assertFalse(identity.can_write)


class GuestHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = os.path.join(cls.tmp.name, "config.yaml")
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write(CONFIG)
        environ = dict(ENV, CONFIG_PATH=cfg)
        with mock.patch.dict(os.environ, {"CONFIG_PATH": cfg}):
            cls.app = app_module.App(environ=environ)
        cls.server = app_module.make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def request(self, method, path, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        hdrs = dict(headers or {})
        if data is not None:
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        status, raw, cookies = resp.status, resp.read(), resp.getheader("Set-Cookie") or ""
        conn.close()
        return status, (json.loads(raw) if raw[:1] == b"{" else raw), cookies

    def guest_post(self, path, body):
        status, state, cookie = self.request("GET", "/api/v1/auth/state")
        self.assertEqual(status, 200)
        pair = cookie.split(";", 1)[0]
        return self.request("POST", path, body=body,
                            headers={"Cookie": pair, "X-CSRF-Token": state["data"]["csrfToken"]})

    def test_pages_open_without_a_session(self):
        self.assertEqual(self.request("GET", "/")[0], 200)
        self.assertEqual(self.request("GET", "/login")[0], 200)
        state = self.request("GET", "/api/v1/auth/state")[1]["data"]
        self.assertIs(state["guestAccess"], True)
        self.assertIsNone(state["user"])

    def test_bootstrap_is_a_scrubbed_read_only_guest(self):
        status, body, _ = self.request("GET", "/api/v1/bootstrap")
        self.assertEqual(status, 200)
        data = body["data"]
        session = data["session"]
        self.assertEqual((session["authMode"], session["role"], session["email"], session["canWrite"],
                          session["piiMasked"]), (GUEST_MODE, "viewer", "", False, True))
        self.assertTrue(session["csrfToken"])
        self.assertEqual(data["workspace"]["projectId"], BULLET * 8)
        self.assertNotIn("demo-project", json.dumps(body))

    def test_guest_writes_are_refused(self):
        for path, body in (("/api/v1/layouts", {"page": "overview", "widgets": []}),
                           ("/api/v1/settings", {"section": "branding", "values": {"accent_color": "#000000"}}),
                           ("/api/v1/auth/logout", {})):
            status, payload, _ = self.guest_post(path, body)
            self.assertEqual(status, 403, path)
            self.assertEqual(payload["error"]["message"], GUEST_READ_ONLY_MESSAGE, path)
        # The analyst accepts guests only when an administrator allows viewers.
        status, payload, _ = self.guest_post("/api/v1/analyst/chat", {"question": "How many prompts?"})
        self.assertEqual(status, 403)
        self.assertIn("available to administrators", payload["error"]["message"])

    def test_settings_view_hides_redaction_terms(self):
        status, body, _ = self.request("GET", "/api/v1/settings")
        self.assertEqual(status, 200)
        security = body["data"]["effective"]["security"]
        self.assertEqual(security["guest_redact_terms"], "***")
        self.assertFalse(body["data"]["editable"])
        text = json.dumps(body)
        for hidden in ("demo-project", "Contoso", "admin@example.com"):
            self.assertNotIn(hidden, text)

    def test_admin_sign_in_still_required_for_admin_reads(self):
        self.assertEqual(self.request("GET", "/api/v1/access")[0], 403)


if __name__ == "__main__":
    unittest.main()
