"""Regression tests of the pre-publication security review that have no feature test file of their own: what viewers
and guests get from responses that are cached once for every role (enforcement status, user details), the month of
the billing report, and the text sent to chat webhooks."""

from __future__ import annotations

import copy
import datetime as dt
import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane import app as app_module
from controlplane.httpkit import ApiError
from controlplane.security import Identity, uid_for
from controlplane.services import governance as gov
from controlplane.services import users

CONFIG = """workspace:
  project_id: demo-project
  scan_all_projects: false
  audit_dataset: ge_analytics
  timezone: UTC
security:
  auth_mode: local
storage:
  backend: memory
"""
ADMIN = Identity("admin@example.com", "admin", "google", "user:admin@example.com")
VIEWER = Identity("viewer@example.com", "viewer", "google", "user:viewer@example.com")
GUEST = Identity("", "viewer", "guest", "guest:198.51.100.7")
ACCOUNT = "000000-111111-222222"
BUDGET = "billingAccounts/%s/budgets/b-0001" % ACCOUNT
ENFORCEMENT_PATH = "/api/v1/governance/antigravity/enforcement"
DETAIL_PATH = "/api/v1/users/detail"
# Shaped like governance.enforcement_status with spend caps on (spend_caps.get_spend_caps).
ENFORCEMENT = {"backend": "iam_deny", "spendCaps": {
    "enabled": True, "accountConfigured": True, "error": None, "currency": "USD",
    "desired": {"finance-secret": 25.0},
    "caps": {"finance-secret": {"budget": BUDGET, "amount": 25.0, "currency": "USD", "outputState": "CONFIGURED"}},
    "plan": [{"project": "finance-secret", "op": "none", "amount": 25.0, "budget": BUDGET, "blocked": None}]}}
DETAIL = {"user": {"uid": uid_for("alice@example.com"), "email": "alice@example.com"},
          "iam": {"state": "EA", "actions": {"block": True, "blockAll": False}}}


class ViewerViewTest(unittest.TestCase):
    def test_enforcement_status_keeps_only_whether_spend_caps_are_on(self):
        original = copy.deepcopy(ENFORCEMENT)
        out = gov.enforcement_viewer_view(ENFORCEMENT)
        self.assertEqual(out["spendCaps"], {"enabled": True, "accountConfigured": True, "error": None,
                                            "adminOnly": True})
        self.assertEqual(out["backend"], "iam_deny")
        for hidden in (ACCOUNT, "finance-secret"):   # dict keys are project ids, which guest redaction never scrubs
            self.assertNotIn(hidden, json.dumps(out))
        self.assertEqual(ENFORCEMENT, original)   # the cached response is shared by every role: never changed
        self.assertEqual(gov.enforcement_viewer_view({"backend": "notify_only", "spendCaps": None}),
                         {"backend": "notify_only", "spendCaps": None})

    def test_user_details_lose_the_access_actions(self):
        original = copy.deepcopy(DETAIL)
        out = users.detail_viewer_view(DETAIL)
        self.assertEqual(out["iam"], {"state": "EA", "actions": None})
        self.assertEqual(out["user"], DETAIL["user"])
        self.assertEqual(DETAIL, original)
        part = {"part": "activity", "iam": None}
        self.assertIs(users.detail_viewer_view(part), part)


class ViewerViewHttpTest(unittest.TestCase):
    """Both routes are cached under a key that ignores the caller, so their viewer views run after the cache: an
    administrator's answer filled the cache first here."""

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
        self.app.cache.clear()
        self.app.limiter = app_module.RateLimiter()

    def get(self, path, identity):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            with mock.patch.object(app_module, "authenticate", return_value=identity):
                conn.request("GET", path)
                resp = conn.getresponse()
                return resp.status, json.loads(resp.read() or b"{}")
        finally:
            conn.close()

    def serve(self, path, data):
        """Answers `path` with `data`, keeping the route's options (cache, masks, viewer view). Returns the calls."""
        key = ("GET", path)
        _fn, opts = app_module.ROUTER.routes[key]
        calls = []

        def handler(ctx):
            calls.append(ctx.identity.role)
            return copy.deepcopy(data)
        patcher = mock.patch.dict(app_module.ROUTER.routes, {key: (handler, opts)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_enforcement_status(self):
        calls = self.serve(ENFORCEMENT_PATH, ENFORCEMENT)
        status, admin = self.get(ENFORCEMENT_PATH, ADMIN)
        self.assertEqual(status, 200, admin)
        self.assertEqual(admin["data"]["spendCaps"]["caps"]["finance-secret"]["budget"], BUDGET)
        for identity in (VIEWER, GUEST):
            status, payload = self.get(ENFORCEMENT_PATH, identity)
            self.assertEqual(status, 200, payload)
            self.assertTrue(payload["meta"]["cached"], identity)
            self.assertTrue(payload["data"]["spendCaps"]["adminOnly"], identity)
            for hidden in (ACCOUNT, "finance-secret", "desired", "plan"):
                self.assertNotIn(hidden, json.dumps(payload["data"]["spendCaps"]), identity)
        status, again = self.get(ENFORCEMENT_PATH, ADMIN)
        self.assertEqual(again["data"]["spendCaps"], admin["data"]["spendCaps"])
        self.assertEqual(calls, ["admin"])   # one computation for every role

    def test_user_details(self):
        calls = self.serve(DETAIL_PATH, DETAIL)
        path = "%s?uid=%s" % (DETAIL_PATH, uid_for("alice@example.com"))
        status, admin = self.get(path, ADMIN)
        self.assertEqual(status, 200, admin)
        self.assertEqual(admin["data"]["iam"]["actions"], DETAIL["iam"]["actions"])
        status, viewer = self.get(path, VIEWER)
        self.assertEqual(status, 200, viewer)
        self.assertTrue(viewer["meta"]["cached"])
        self.assertIsNone(viewer["data"]["iam"]["actions"])
        self.assertEqual(self.get(path, ADMIN)[1]["data"]["iam"]["actions"], DETAIL["iam"]["actions"])
        self.assertEqual(calls, ["admin"])


class ReportMonthTest(unittest.TestCase):
    NOW = dt.datetime(2026, 11, 20, 12, 0, tzinfo=dt.timezone.utc)

    def month(self, raw):
        return gov._report_month(support.FakeCtx(params={"month": raw}), dt.timezone.utc, self.NOW)  # pylint: disable=protected-access

    def test_months_take_ascii_digits_only(self):
        # str.isdigit() also takes superscripts ("2026-0" + superscript one made int() fail: a 500) and the digits of
        # other scripts.
        for bad in ("2026-0\u00b9", "\u0662\u0660\u0662\u0666-10", "2026-\uff11\uff10", "2026-13", "2019-12",
                    "2026-12", "2026/10", "2026-10-01", "26-10"):
            with self.assertRaises(ApiError, msg=repr(bad)):
                self.month(bad)
        start, end = self.month("2026-1")
        self.assertEqual((start, end), (dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                                        dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc)))
        self.assertEqual(self.month(" 2026-10 ")[0], dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc))

    def test_other_number_parameters_take_ascii_digits_only(self):
        # The same int() failure: the period preview and the status caps are read from the query string.
        for bad in ("\u00b2", "1\u00b9", "\u0661\u0662"):
            with self.assertRaises(ApiError, msg=repr(bad)):
                gov._preview_int({"day": bad}, "day", 1, 1, 31)  # pylint: disable=protected-access
            with self.assertRaises(ApiError, msg=repr(bad)):
                gov.policy_params(support.FakeCtx(), {"tokenCap": bad})
        self.assertEqual(gov._preview_int({"day": "12"}, "day", 1, 1, 31), 12)  # pylint: disable=protected-access
        self.assertEqual(gov.policy_params(support.FakeCtx(), {"tokenCap": "500"})[2], 500)


class WebhookTextTest(unittest.TestCase):
    def test_markup_in_names_stays_plain_text(self):
        # Unit paths come from the HR CSV: <!channel>, <users/all> or a <url|label> link must not reach the chat as
        # markup (Slack and Google Chat read <...> as mentions and links).
        self.assertEqual(gov.webhook_text("Unit <!channel> & <https://evil.example|Sales> reached 100%."),
                         "Unit \u2039!channel\u203a & \u2039https://evil.example|Sales\u203a reached 100%.")
        self.assertEqual(gov.webhook_text("AI Control Plane budgets: Unit Sales/Korea reached 50%."),
                         "AI Control Plane budgets: Unit Sales/Korea reached 50%.")
        # Google Chat shows entities as written, so & stays plain ("R&D", not "R&amp;D").
        self.assertEqual(gov.webhook_text("Unit R&D reached 80%."), "Unit R&D reached 80%.")

    def test_send_webhook_posts_the_neutralised_text(self):
        ctx = support.FakeCtx()
        ctx.settings["governance"]["antigravity"]["webhook_url"] = "https://chat.googleapis.com/v1/spaces/AAAA/messages"
        with mock.patch("requests.post", return_value=mock.Mock(status_code=200)) as post:
            self.assertTrue(gov.send_webhook(ctx, "Unit <users/all> reached 100%."))
        self.assertEqual(post.call_args.kwargs["json"], {"text": "Unit \u2039users/all\u203a reached 100%."})
        self.assertIs(post.call_args.kwargs["allow_redirects"], False)


class DiscoveryHealthCheckTest(unittest.TestCase):
    def test_discovery_health_check_uses_configured_non_global_locations(self):
        from controlplane.services import system as system_svc
        ctx = support.FakeCtx()
        ctx.settings["workspace"]["discovery_engine_locations"] = ["eu", "us"]
        called = []

        def fake_rest(method, url, **kwargs):  # pylint: disable=unused-argument
            called.append(url)
            if "locations/eu/licenseConfigs" in url:
                return {"licenseConfigs": [{"name": "projects/demo-project/locations/eu/licenseConfigs/c1"}]}
            if "locations/us/licenseConfigs" in url:
                return {"licenseConfigs": [{"name": "projects/demo-project/locations/us/licenseConfigs/c2"},
                                           {"name": "projects/demo-project/locations/us/licenseConfigs/c3"}]}
            return {}

        ctx.rest = fake_rest
        ctx.max_bytes = 10**9
        ctx.app = mock.Mock()
        ctx.app.ai.client.return_value = mock.Mock()
        ctx.app.ai.client.return_value.models.get.return_value = mock.Mock()
        ctx.registry = mock.Mock()
        ctx.registry.tables.return_value = {}
        with mock.patch("controlplane.services.governance.get_policy", return_value={"bindings": []}), \
                mock.patch("controlplane.services.governance.permission_status", return_value={}):
            res = system_svc.run_health(ctx)
        disc = next(c for c in res["checks"] if c["id"] == "discovery_engine")
        self.assertEqual(disc["status"], "ok")
        self.assertIn("3 license configuration(s) found (eu, us)", disc["detail"])
        self.assertTrue(any(u.startswith("https://eu-discoveryengine.googleapis.com/") for u in called))
        self.assertTrue(any(u.startswith("https://us-discoveryengine.googleapis.com/") for u in called))
        self.assertFalse(any("/locations/global/licenseConfigs" in u for u in called))


if __name__ == "__main__":
    unittest.main()
