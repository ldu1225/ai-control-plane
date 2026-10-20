"""Static file serving and the HTTP request pipeline.

The pipeline tests start the real application on 127.0.0.1 with an ephemeral port, an
unconfigured workspace and in-memory settings, so no Google Cloud call is ever made.
"""

import contextlib
import http.client
import io
import json
import os
import re
import tempfile
import threading
import unittest
from unittest import mock

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane import app as app_module
from controlplane.httpkit import StaticSite, etag_matches

INDEX = (b"<!doctype html><html><head><script nonce=\"__CSP_NONCE__\" src=\"/assets/js/app.js\"></script>"
         b"</head><body data-version=\"__APP_VERSION__\"></body></html>")


def build_web_root(root):
    web = os.path.join(root, "web")
    os.makedirs(os.path.join(web, "assets", "css"))
    os.makedirs(os.path.join(web, "assets", "img"))
    os.makedirs(os.path.join(web, "assets", "js"))
    with open(os.path.join(web, "index.html"), "wb") as fh:
        fh.write(INDEX)
    files = {"css/app.css": b"body{color:#111}", "css/.hidden.css": b"x", "js/app.js": b"console.log(1);",
             "img/favicon.svg": b"<svg xmlns=\"http://www.w3.org/2000/svg\"/>", "js/notes.py": b"print(1)",
             "js/page.html": b"<p>x</p>"}
    for rel, data in files.items():
        with open(os.path.join(web, "assets", rel), "wb") as fh:
            fh.write(data)
    with open(os.path.join(root, "secret.css"), "wb") as fh:
        fh.write(b"secret")
    os.symlink(os.path.join(root, "secret.css"), os.path.join(web, "assets", "css", "link.css"))
    os.makedirs(os.path.join(web, "assets", "css", "dir.css"))
    return web


class StaticSiteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.site = StaticSite(build_web_root(self.tmp.name), "2.0.0")

    def tearDown(self):
        self.tmp.cleanup()

    def test_index_templating_uses_fresh_nonce(self):
        body1, nonce1 = self.site.render_index()
        body2, nonce2 = self.site.render_index()
        self.assertNotEqual(nonce1, nonce2)
        self.assertIn(("nonce=\"%s\"" % nonce1).encode(), body1)
        self.assertIn(b"data-version=\"2.0.0\"", body1)
        self.assertNotIn(b"__CSP_NONCE__", body2)

    def test_allowed_assets_and_mime_types(self):
        full, ctype = self.site.resolve_asset("css/app.css")
        self.assertTrue(full.endswith(os.path.join("assets", "css", "app.css")))
        self.assertEqual(ctype, "text/css; charset=utf-8")
        self.assertEqual(self.site.resolve_asset("js/app.js")[1], "text/javascript; charset=utf-8")
        self.assertEqual(self.site.resolve_asset("img/favicon.svg")[1], "image/svg+xml")
        data, etag = self.site.load(full)
        self.assertEqual(data, b"body{color:#111}")
        self.assertRegex(etag, r'^"[0-9a-f]{64}"$')

    def test_path_traversal_and_disallowed_files_are_rejected(self):
        for rel in ("../index.html", "css/../../index.html", "../../secret.css", "/etc/passwd", "css//app.css",
                    "./css/app.css", "css/.hidden.css", "css\\app.css", "css/app.css\x00.js", "js/notes.py",
                    "js/page.html", "css/link.css", "css/dir.css", "css/missing.css", "", "a" * 600 + ".css"):
            self.assertIsNone(self.site.resolve_asset(rel), repr(rel))

    def test_etag_matching(self):
        self.assertTrue(etag_matches('"abc"', '"abc"'))
        self.assertTrue(etag_matches('W/"abc", "def"', '"abc"'))
        self.assertTrue(etag_matches("*", '"abc"'))
        self.assertFalse(etag_matches('"abd"', '"abc"'))
        self.assertFalse(etag_matches(None, '"abc"'))


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = os.path.join(cls.tmp.name, "config.yaml")
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write("security:\n  auth_mode: local\nstorage:\n  backend: memory\nmodules:\n  telemetry: false\n")
        web = build_web_root(cls.tmp.name)
        with mock.patch.dict(os.environ, {"CONFIG_PATH": cfg}):
            cls.app = app_module.App(environ={"CONFIG_PATH": cfg})
        cls.app.site = StaticSite(web, "2.0.0-test")
        cls.server = app_module.make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
        finally:
            conn.close()

    def json(self, method, path, **kwargs):
        status, headers, data = self.request(method, path, **kwargs)
        return status, headers, json.loads(data) if data else None

    def bootstrap(self):
        status, headers, payload = self.json("GET", "/api/v1/bootstrap")
        self.assertEqual(status, 200)
        cookie = headers["set-cookie"].split(";")[0]
        return payload, cookie

    def assertSecurityHeaders(self, headers):
        self.assertIn("default-src 'self'", headers["content-security-policy"])
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(headers["cross-origin-opener-policy"], "same-origin")
        self.assertNotIn("access-control-allow-origin", headers)

    def test_healthz(self):
        status, headers, payload = self.json("GET", "/healthz")
        self.assertEqual((status, payload), (200, {"status": "ok"}))
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertSecurityHeaders(headers)

    def test_bootstrap_session_and_csrf_cookie(self):
        status, headers, payload = self.json("GET", "/api/v1/bootstrap")
        self.assertEqual(status, 200)
        self.assertSecurityHeaders(headers)
        cookie = headers["set-cookie"]
        self.assertTrue(cookie.startswith("cp_csrf="))
        for attr in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(attr, cookie)
        data = payload["data"]
        self.assertEqual(cookie.split(";")[0].split("=", 1)[1], data["session"]["csrfToken"])
        self.assertEqual(data["session"]["role"], "admin")  # loopback peer in local mode
        self.assertEqual(data["session"]["authMode"], "local")
        self.assertFalse(data["workspace"]["configured"])
        self.assertEqual(len(data["modules"]), 13)
        self.assertEqual(data["product"]["version"], "2.0.0")
        self.assertEqual(payload["meta"]["warnings"], [])
        self.assertRegex(payload["meta"]["generatedAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_post_requires_csrf(self):
        body = json.dumps({"section": "branding", "values": {"accent_color": "#123456"}})
        status, _, payload = self.json("POST", "/api/v1/settings", body=body,
                                       headers={"Content-Type": "application/json"})
        self.assertEqual((status, payload["error"]["code"]), (403, "csrf_failed"))
        _, cookie = self.bootstrap()
        token = cookie.split("=", 1)[1]
        status, _, payload = self.json("POST", "/api/v1/settings", body=body,
                                       headers={"Content-Type": "application/json", "Cookie": cookie,
                                                "X-CSRF-Token": "x" * 43})
        self.assertEqual(status, 403)
        status, _, payload = self.json("POST", "/api/v1/settings", body=body,
                                       headers={"Content-Type": "application/json", "Cookie": cookie,
                                                "X-CSRF-Token": token})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["effective"]["branding"]["accent_color"], "#123456")
        self.assertEqual(payload["data"]["overrides"]["branding"]["accent_color"], "#123456")

    def test_settings_validation_error_is_generic(self):
        _, cookie = self.bootstrap()
        token = cookie.split("=", 1)[1]
        status, _, payload = self.json("POST", "/api/v1/settings",
                                       body=json.dumps({"section": "branding", "values": {"nope": 1}}),
                                       headers={"Content-Type": "application/json", "Cookie": cookie,
                                                "X-CSRF-Token": token})
        self.assertEqual((status, payload["error"]["code"]), (400, "bad_request"))
        self.assertNotIn("Traceback", payload["error"]["message"])

    def test_only_get_and_post(self):
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            status, headers, payload = self.json(method, "/api/v1/settings")
            self.assertEqual(status, 405, method)
            self.assertEqual(payload["error"]["code"], "bad_request")
            self.assertIn("GET", headers["allow"])
        status, _, _ = self.request("POST", "/assets/css/app.css", body=b"{}",
                                    headers={"Content-Type": "application/json"})
        self.assertEqual(status, 405)

    def test_module_gating_and_configuration(self):
        status, _, payload = self.json("GET", "/api/v1/overview/summary")
        self.assertEqual((status, payload["error"]["code"]), (409, "not_configured"))
        status, _, payload = self.json("GET", "/api/v1/telemetry/discover")
        self.assertEqual((status, payload["error"]["code"]), (409, "module_disabled"))
        status, _, payload = self.json("GET", "/api/v1/does-not-exist")
        self.assertEqual((status, payload["error"]["code"]), (404, "not_found"))

    def test_index_and_spa_fallback(self):
        for path in ("/", "/users/abc"):
            status, headers, body = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
            self.assertEqual(headers["cache-control"], "no-store")
            nonce = re.search(r"'nonce-([^']+)'", headers["content-security-policy"]).group(1)
            self.assertIn(("nonce=\"%s\"" % nonce).encode(), body)
            self.assertIn(b"2.0.0-test", body)

    def test_assets_etag_and_conditional_get(self):
        status, headers, body = self.request("GET", "/assets/css/app.css")
        self.assertEqual((status, body), (200, b"body{color:#111}"))
        self.assertEqual(headers["content-type"], "text/css; charset=utf-8")
        self.assertEqual(headers["cache-control"], "no-cache")
        status, headers2, body = self.request("GET", "/assets/css/app.css",
                                              headers={"If-None-Match": headers["etag"]})
        self.assertEqual((status, body), (304, b""))
        self.assertEqual(headers2["etag"], headers["etag"])
        status, headers, _ = self.request("GET", "/favicon.ico")
        self.assertEqual((status, headers["content-type"]), (200, "image/svg+xml"))

    def test_static_traversal_is_rejected(self):
        for path in ("/assets/../config.yaml", "/assets/%2e%2e/%2e%2e/config.yaml", "/assets/css/.hidden.css",
                     "/assets/js/../../index.html", "/assets/js/notes.py", "/assets/css/link.css",
                     "/assets/css/app.css%00.js", "/assets/%2e%2e%2fsecret.css", "/assets/css%5capp.css"):
            status, _, payload = self.json("GET", path)
            self.assertEqual((status, payload["error"]["code"]), (404, "not_found"), path)

    def test_server_errors_are_single_structured_lines(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertLogs("controlplane.http", level="DEBUG") as logs:
            try:
                raise ConnectionResetError(54, "Connection reset by peer")
            except ConnectionResetError:
                self.server.handle_error(None, ("127.0.0.1", 1))
            try:
                raise RuntimeError("boom")
            except RuntimeError:
                self.server.handle_error(None, ("127.0.0.1", 1))
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(len(logs.output), 2)
        self.assertIn("client connection closed reason=ConnectionResetError", logs.output[0])
        self.assertIn("unhandled server error reason=RuntimeError", logs.output[1])
        self.assertFalse(any("Traceback" in line or "\n" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
