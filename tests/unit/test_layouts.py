"""Personal widget layouts: validation, per-user storage and the HTTP endpoints."""

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
from controlplane.services import layouts
from controlplane.settings_store import FileStore, MemoryStore, UserDocStore

W1 = {"id": "w_0000000a", "connector": "conn_0000abcd", "view": "summary", "size": "m"}
W2 = {"id": "w_0000000b", "connector": "conn_0000beef", "view": "operations", "size": "l"}


class CleanWidgetsTest(unittest.TestCase):
    def test_valid_list_is_normalized(self):
        extra = dict(W1, color="red")
        self.assertEqual(layouts.clean_widgets([extra, W2]), [W1, W2])
        self.assertEqual(layouts.clean_widgets([]), [])

    def test_invalid_lists_are_rejected(self):
        bad = [
            None, "x", [W1, W1],                                   # not a list / duplicate id
            [dict(W1, id="widget-1")], [dict(W1, connector="conn_1")],
            [dict(W1, view="pie")], [dict(W1, size="xl")], ["w"],
            [dict(W1, id="w_%08x" % i) for i in range(layouts.MAX_WIDGETS + 1)],
        ]
        for widgets in bad:
            with self.assertRaises(ApiError, msg=repr(widgets)[:60]):
                layouts.clean_widgets(widgets)


class UserDocStoreTest(unittest.TestCase):
    def test_memory_round_trip_and_conflict_retry(self):
        docs = UserDocStore(MemoryStore())
        self.assertEqual(docs.load("0123456789abcdef"), ({}, 0))
        docs.mutate("0123456789abcdef", lambda d: d.setdefault("layouts", {}).update(overview=[W1]))
        doc, generation = docs.load("0123456789abcdef")
        self.assertEqual((doc["layouts"]["overview"], generation), ([W1], 1))
        self.assertEqual(docs.load("fedcba9876543210"), ({}, 0))  # other people are separate

    def test_file_backend_writes_next_to_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = UserDocStore(FileStore(os.path.join(tmp, "settings.json"), durable=True))
            docs.mutate("0123456789abcdef", lambda d: d.update(layouts={"overview": [W1]}))
            path = os.path.join(tmp, "users", "0123456789abcdef.json")
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["layouts"]["overview"], [W1])

    def test_keys_must_be_user_hashes(self):
        docs = UserDocStore(MemoryStore())
        for key in ("../../etc", "ABCDEF0123456789", "short", "", None):
            with self.assertRaises(ValueError):
                docs.load(key)


class LayoutEndpointsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = os.path.join(cls.tmp.name, "config.yaml")
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write("security:\n  auth_mode: local\nstorage:\n  backend: memory\n")
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

    def call(self, method, path, body=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers = {"Content-Type": "application/json", "Cookie": cookie, "X-CSRF-Token": cookie.split("=", 1)[1]}
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, json.loads(resp.read() or b"{}")
        finally:
            conn.close()

    def csrf_cookie(self):
        _, headers, _ = self.call("GET", "/api/v1/bootstrap")
        return headers["set-cookie"].split(";")[0]

    def test_save_read_and_validate(self):
        status, _, payload = self.call("GET", "/api/v1/layouts")
        self.assertEqual((status, payload["data"]), (200, {"layouts": {}, "editable": True}))
        cookie = self.csrf_cookie()
        status, _, payload = self.call("POST", "/api/v1/layouts", {"page": "overview", "widgets": [W1, W2]}, cookie)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["layouts"], {"overview": [W1, W2]})
        status, _, payload = self.call("POST", "/api/v1/layouts", {"page": "users", "widgets": [W2]}, cookie)
        self.assertEqual(payload["data"]["layouts"], {"overview": [W1, W2], "users": [W2]})
        status, _, payload = self.call("GET", "/api/v1/layouts")
        self.assertEqual(payload["data"]["layouts"]["users"], [W2])
        # An empty list removes the page; bad input is rejected without changing anything.
        status, _, payload = self.call("POST", "/api/v1/layouts", {"page": "users", "widgets": []}, cookie)
        self.assertEqual(payload["data"]["layouts"], {"overview": [W1, W2]})
        for body in ({"page": "settings", "widgets": []}, {"page": "overview", "widgets": [dict(W1, view="pie")]},
                     {"page": "overview"}):
            status, _, payload = self.call("POST", "/api/v1/layouts", body, cookie)
            self.assertEqual((status, payload["error"]["code"]), (400, "bad_request"), body)
        status, _, payload = self.call("GET", "/api/v1/layouts")
        self.assertEqual(payload["data"]["layouts"], {"overview": [W1, W2]})

    def test_post_requires_csrf(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", "/api/v1/layouts", body=json.dumps({"page": "overview", "widgets": []}),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            payload = json.loads(resp.read())
        finally:
            conn.close()
        self.assertEqual((resp.status, payload["error"]["code"]), (403, "csrf_failed"))


if __name__ == "__main__":
    unittest.main()
