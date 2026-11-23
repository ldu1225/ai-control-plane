"""HTTP pipeline of the organization CSV import: the only route with a body above 64 KB."""

import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane import app as app_module


class ImportRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = os.path.join(cls.tmp.name, "config.yaml")
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write("workspace:\n  project_id: demo-project\nstorage:\n  backend: memory\nsecurity:\n"
                     "  auth_mode: local\n")
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

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, json.loads(resp.read() or b"{}")
        finally:
            conn.close()

    def announce(self, path, length, headers):
        """Sends only the request head with a Content-Length, never the body."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            conn.putrequest("POST", path)
            for key, value in dict(headers, **{"Content-Length": str(length)}).items():
                conn.putheader(key, value)
            conn.endheaders()
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read() or b"{}")
        finally:
            conn.close()

    def session(self):
        status, headers, payload = self.call("GET", "/api/v1/bootstrap")
        self.assertEqual(status, 200)
        return {"Content-Type": "application/json", "Cookie": headers["set-cookie"].split(";")[0],
                "X-CSRF-Token": payload["data"]["session"]["csrfToken"]}

    def test_a_large_csv_goes_through_after_the_checks(self):
        rows = ["principal,unit_path,cost_center"] + [
            "user%d@example.com,Company/Team%d,CC-%d" % (i, i % 50, i % 50) for i in range(3000)]
        body = json.dumps({"csv": "\n".join(rows), "dryRun": True}).encode("utf-8")
        self.assertGreater(len(body), 64 * 1024)
        status, headers, payload = self.call("POST", "/api/v1/org/import", body=body, headers=self.session())
        self.assertEqual(status, 200, payload)
        self.assertEqual((payload["data"]["rows"], payload["data"]["valid"], payload["data"]["dryRun"]),
                         (3000, True, True))
        self.assertEqual(headers.get("connection"), "close")  # large-body routes never reuse the connection

    def test_checks_come_before_the_body(self):
        status, _, payload = self.call("POST", "/api/v1/org/import", body=b"",
                                       headers={"Content-Type": "application/json"})
        self.assertEqual((status, payload["error"]["code"]), (403, "csrf_failed"))
        status, payload = self.announce("/api/v1/org/import", 9 * 1024 * 1024, self.session())
        self.assertEqual((status, payload["error"]["message"]), (400, "Request body is too large."))

    def test_other_routes_keep_the_small_limit(self):
        status, payload = self.announce("/api/v1/chargeback/close", 100 * 1024, self.session())
        self.assertEqual((status, payload["error"]["message"]), (400, "Request body is too large."))


if __name__ == "__main__":
    unittest.main()
