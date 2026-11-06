"""Google OAuth client connected by an administrator (Settings > Access control), with no redeployment.

Covers the stored sign-in state (sanitize), which identity provider signs people in (deployment client > connected
client > IAP sign-in bridge), the connected client's secret read by exact version from Secret Manager (cached; a failure
falls back to the bridge and is retried), Google's checks of the client (wrong ID or secret, unregistered redirect URI),
and the whole flow on a real application in bridge mode: connect against a Secret Manager double, sign an account of
another organization in with the connected client, replace the client (the previous secret version is destroyed and a
sign-in started with the old client expires), and disconnect (back to the bridge, the version destroyed).
"""

import base64
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
from controlplane.auth import GoogleSignIn
from controlplane.gcp import GcpError
from controlplane.httpkit import ApiError
from controlplane.services import iap_access, oauth_client
from controlplane.settings_store import sanitize_auth
from test_iap_access import BRIDGE, CONFIG, INITIAL, FakeIap

CLIENT_ID = "123456789012-abcdefghijklmnop.apps.googleusercontent.com"
CLIENT_ID_2 = "123456789012-qrstuvwxyzabcdef.apps.googleusercontent.com"
SECRET = "GOCSPX-test-secret-0001"
SECRET_2 = "GOCSPX-test-secret-0002"
SECRET_RES = "projects/demo-project/secrets/ai-control-plane-oauth-connected-secret"
VERSION = "projects/123456789012/secrets/ai-control-plane-oauth-connected-secret/versions/%d"
CALLBACK = "/auth/google/callback"


def auth_error_location(reason):
    """Google's authorization error redirect: /signin/oauth/error/v2?authError=<base64url protobuf>."""
    raw = b"\n" + bytes([len(reason)]) + reason.encode("ascii") + b"\x12\x1fThe redirect URI in the request"
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return "https://accounts.google.com/signin/oauth/error/v2?authError=%s&client_id=x" % token


def google_answers(token_error="invalid_grant", location="https://accounts.google.com/v3/signin/identifier?x=1"):
    """Mocks for check_client: the token endpoint's answer to a dummy code and the authorization endpoint's redirect."""
    token = mock.Mock(status_code=401 if token_error == "invalid_client" else 400,
                      content=json.dumps({"error": token_error}).encode("utf-8"))
    token.json.return_value = {"error": token_error}
    start = mock.Mock(status_code=302, headers={"Location": location})
    return mock.patch("requests.post", return_value=token), mock.patch("requests.get", return_value=start)


class FakeSettings:
    def __init__(self, oauth=None):
        self.state = {"requests": [], "lastLogin": {}, "revoked": {}, "accounts": {}}
        if oauth:
            self.state["oauth"] = dict(oauth)

    def auth_state(self):
        return json.loads(json.dumps(self.state))

    def mutate_auth(self, fn, actor):  # pylint: disable=unused-argument
        fn(self.state)

    def revoked_at(self, email):  # pylint: disable=unused-argument
        return 0


def sign_in_for(settings=None, bridge="", client_id="", environ=None, reader=None):
    base = {"security": {"sign_in_bridge_url": bridge, "google_client_id": client_id, "public_base_url": ""}}
    env = dict({"SESSION_SECRET": "s" * 48}, **(environ or {}))
    return GoogleSignIn(base, env, settings, secret_reader=reader)


class SanitizeTest(unittest.TestCase):
    def test_keeps_a_valid_connection_and_drops_anything_else(self):
        good = {"clientId": CLIENT_ID, "secretVersion": VERSION % 3, "updatedAt": "2026-11-06T05:00:00Z",
                "updatedBy": "admin", "clientSecret": "must-not-be-kept"}
        out = sanitize_auth({"oauth": good})
        self.assertEqual(out["oauth"], {"clientId": CLIENT_ID, "secretVersion": VERSION % 3,
                                        "updatedAt": "2026-11-06T05:00:00Z", "updatedBy": "admin"})
        for bad in ({"clientId": "not-a-client", "secretVersion": VERSION % 3},
                    {"clientId": CLIENT_ID, "secretVersion": "projects/p/secrets/s/versions/latest"},
                    {"clientId": CLIENT_ID}, "text", None):
            self.assertNotIn("oauth", sanitize_auth({"oauth": bad}), bad)


class ProviderTest(unittest.TestCase):
    def test_deployment_client_wins_then_connected_client_then_bridge(self):
        conn = {"clientId": CLIENT_ID_2, "secretVersion": VERSION % 1}
        deployed = sign_in_for(FakeSettings(conn), bridge=BRIDGE, client_id=CLIENT_ID,
                               environ={"GOOGLE_OAUTH_CLIENT_SECRET": SECRET}, reader=lambda v: SECRET_2)
        self.assertEqual(deployed.oauth_client(), (CLIENT_ID, SECRET, "deployment"))

        reads = []
        connected = sign_in_for(FakeSettings(conn), bridge=BRIDGE, reader=lambda v: reads.append(v) or SECRET_2)
        self.assertEqual(connected.oauth_client(), (CLIENT_ID_2, SECRET_2, "settings"))
        self.assertTrue(connected.google_enabled)
        self.assertIsNone(connected.switch_account_url())
        connected.oauth_client()
        self.assertEqual(reads, [VERSION % 1])  # versions are immutable: read once

        bridge = sign_in_for(FakeSettings(), bridge=BRIDGE, reader=lambda v: SECRET_2)
        self.assertEqual(bridge.oauth_client(), (None, None, None))
        self.assertEqual(bridge.switch_account_url(), BRIDGE + "/_gcp_iap/clear_login_cookie")
        location, _ = bridge.start({"Host": "127.0.0.1:8080"}, "")
        self.assertTrue(location.startswith(BRIDGE + "/auth/bridge?"), location)

        off = sign_in_for(FakeSettings())
        self.assertFalse(off.google_enabled)
        self.assertEqual(off.start({"Host": "127.0.0.1:8080"}, ""), ("/login?error=unavailable", []))
        on = sign_in_for(FakeSettings(conn), reader=lambda v: SECRET_2)  # password-only deployment, client connected
        self.assertTrue(on.google_enabled)
        location, _ = on.start({"Host": "127.0.0.1:8080"}, "")
        self.assertIn("client_id=" + urllib.parse.quote(CLIENT_ID_2), location)

    def test_unreadable_secret_falls_back_to_the_bridge_and_is_retried(self):
        calls = []

        def reader(version):
            calls.append(version)
            if len(calls) == 1:
                raise GcpError("denied", status=403)
            return SECRET_2
        sign_in = sign_in_for(FakeSettings({"clientId": CLIENT_ID_2, "secretVersion": VERSION % 1}), bridge=BRIDGE,
                              reader=reader)
        self.assertEqual(sign_in.oauth_client(), (None, None, None))
        self.assertEqual(sign_in.oauth_client(), (None, None, None))
        self.assertEqual(len(calls), 1)  # not retried within SECRET_RETRY_SECONDS
        location, _ = sign_in.start({"Host": "127.0.0.1:8080"}, "")
        self.assertTrue(location.startswith(BRIDGE), location)
        with mock.patch.object(GoogleSignIn, "SECRET_RETRY_SECONDS", 0):
            self.assertEqual(sign_in.oauth_client(), (CLIENT_ID_2, SECRET_2, "settings"))
        self.assertEqual(len(calls), 2)


class CheckClientTest(unittest.TestCase):
    URI = "https://dashboard.example.com" + CALLBACK

    def check(self, **answers):
        post, get = google_answers(**answers)
        with post, get:
            return oauth_client.check_client(CLIENT_ID, SECRET, self.URI)

    def test_valid_client(self):
        self.assertEqual(self.check(), {"token": "invalid_grant", "redirect": "ok"})

    def test_wrong_id_or_secret(self):
        with self.assertRaises(ApiError) as err:
            self.check(token_error="invalid_client")
        self.assertIn("rejected this client ID or client secret", err.exception.message)

    def test_redirect_uri_not_registered(self):
        with self.assertRaises(ApiError) as err:
            self.check(location=auth_error_location("redirect_uri_mismatch"))
        self.assertEqual(err.exception.message, "Add %s as an authorized redirect URI of this client (Google Auth "
                                                "Platform > Clients > the client), save, and connect again. A change "
                                                "saved a moment ago can take a few minutes to apply." % self.URI)

    def test_unknown_client(self):
        with self.assertRaises(ApiError) as err:
            self.check(location=auth_error_location("invalid_client"))
        self.assertIn("does not know this client ID", err.exception.message)

    def test_google_unreachable_skips_the_checks(self):
        import requests
        with mock.patch("requests.post", side_effect=requests.ConnectionError()), \
                mock.patch("requests.get", side_effect=requests.ConnectionError()):
            self.assertEqual(oauth_client.check_client(CLIENT_ID, SECRET, self.URI),
                             {"token": "unreachable", "redirect": "unreachable"})


class FakeGcp(FakeIap):
    """IAP double plus Secret Manager: addVersion, access and destroy on one secret (versions named by project
    number, as Secret Manager does)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.versions = {}
        self.deny_secret = False

    def request(self, method, url, *, quota_project=None, params=None, json_body=None, timeout=None, **kw):
        if not url.startswith(oauth_client.SECRET_API):
            return super().request(method, url, quota_project=quota_project, params=params, json_body=json_body,
                                   timeout=timeout, **kw)
        path = url[len(oauth_client.SECRET_API):]
        self.calls.append((method, path))
        if self.deny_secret:
            raise GcpError("denied", status=403, message="Permission 'secretmanager.versions.add' denied")
        if path.endswith(":addVersion"):
            self.assertEqual_(path[:-len(":addVersion")], SECRET_RES)
            name = VERSION % (len(self.versions) + 1)
            self.versions[name] = {"data": base64.b64decode(json_body["payload"]["data"]).decode("utf-8"),
                                   "state": "ENABLED"}
            return {"name": name, "state": "ENABLED"}, {}
        name = path.rpartition(":")[0]
        version = self.versions.get(name)
        if version is None:
            raise GcpError("not_found", status=404, message="Secret version not found")
        if path.endswith(":access"):
            if version["state"] != "ENABLED":
                raise GcpError("precondition", status=400, message="Secret version is in DESTROYED state")
            return {"name": name, "payload": {"data": base64.b64encode(version["data"].encode()).decode()}}, {}
        if path.endswith(":destroy"):
            version["state"] = "DESTROYED"
            return {"name": name, "state": "DESTROYED"}, {}
        raise AssertionError("unexpected Secret Manager call " + path)

    @staticmethod
    def assertEqual_(a, b):  # pylint: disable=invalid-name
        assert a == b, (a, b)

    def states(self):
        return {name.rpartition("/")[2]: v["state"] for name, v in sorted(self.versions.items())}


class Response:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    def header(self, name):
        return next((v for k, v in self.headers if k.lower() == name.lower()), None)

    def cookie(self, name):
        for key, value in self.headers:
            if key.lower() == "set-cookie" and value.startswith(name + "="):
                return value.split(";", 1)[0].split("=", 1)[1]
        return None


class ConnectFlowTest(unittest.TestCase):
    """Bridge-mode deployment on a real application: the administrator (built-in account) connects, replaces and
    disconnects an OAuth client; an approved account of another organization signs in with it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = os.path.join(cls.tmp.name, "config.yaml")
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write(CONFIG)
        cls.app = app_module.App(environ={
            "CONFIG_PATH": cfg, "SESSION_SECRET": "s" * 48, "SIGN_IN_BRIDGE_URL": BRIDGE,
            "ADMIN_INITIAL_PASSWORD": INITIAL, "IAP_ORG_DOMAIN": "cymbal.example.com",
            "OAUTH_CONNECT_SECRET": SECRET_RES})
        cls.server = app_module.make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        iap_access._cache.clear()  # pylint: disable=protected-access
        self.gcp = FakeGcp(members=["domain:cymbal.example.com"])
        self.app.gcp = self.gcp
        self.app.settings.mutate_auth(lambda state: state.pop("oauth", None), "test")
        self.app.auth._secret_failures.clear()  # pylint: disable=protected-access

    # -- helpers ----------------------------------------------------------------------
    def request(self, method, path, cookies=None, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        if cookies:
            hdrs["Cookie"] = "; ".join("%s=%s" % kv for kv in cookies.items())
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        out = Response(resp.status, resp.getheaders(), resp.read())
        conn.close()
        return out

    def admin(self):
        csrf = self.request("GET", "/api/v1/auth/state").json()["data"]["csrfToken"]
        res = self.request("POST", "/api/v1/auth/login", cookies={"cp_csrf": csrf}, headers={"X-CSRF-Token": csrf},
                           body={"username": "admin", "password": INITIAL})
        self.assertEqual(res.status, 200, res.body)
        return res.cookie("cp_session"), csrf

    def post(self, path, body, status=200):
        session, csrf = self.admin()
        res = self.request("POST", path, cookies={"cp_session": session, "cp_csrf": csrf},
                           headers={"X-CSRF-Token": csrf}, body=body)
        self.assertEqual(res.status, status, res.body)
        return res.json()

    def summary(self):
        session, _ = self.admin()
        return self.request("GET", "/api/v1/access", cookies={"cp_session": session}).json()["data"]

    def connect(self, client_id, secret, status=200, **answers):
        post, get = google_answers(**answers)
        with post, get:
            return self.post("/api/v1/access/oauth", {"clientId": client_id, "clientSecret": secret}, status)

    def start(self):
        res = self.request("GET", "/auth/google/start?next=%2F%23%2Foverview")
        self.assertEqual(res.status, 302)
        return res.header("Location"), res.cookie("cp_oauth")

    def finish(self, location, txn, email, client_id):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
        claims = {"iss": "https://accounts.google.com", "aud": client_id, "sub": "sub-" + email, "email": email,
                  "email_verified": True, "name": "Partner", "nonce": query["nonce"][0]}
        token = mock.Mock(status_code=200)
        token.json.return_value = {"id_token": "header.payload.signature"}
        with mock.patch("requests.post", return_value=token) as post, \
                mock.patch("google.oauth2.id_token.verify_oauth2_token", return_value=claims) as verify:
            cb = self.request("GET", CALLBACK + "?state=%s&code=4%%2Fcode" % query["state"][0],
                              cookies={"cp_oauth": txn})
        return cb, post, verify

    # -- tests ------------------------------------------------------------------------
    def test_connect_sign_in_replace_and_disconnect(self):
        callback = "http://127.0.0.1:%d%s" % (self.port, CALLBACK)
        state = self.request("GET", "/api/v1/auth/state").json()["data"]
        self.assertTrue(state["googleEnabled"])
        self.assertEqual(state["switchAccountUrl"], BRIDGE + "/_gcp_iap/clear_login_cookie")
        self.assertTrue(state["googleHint"]["internalOnly"])
        oauth = self.summary()["oauth"]
        self.assertEqual({k: oauth[k] for k in ("available", "active", "source", "canConnect", "bridge", "secret")},
                         {"available": True, "active": False, "source": None, "canConnect": True, "bridge": True,
                          "secret": "ai-control-plane-oauth-connected-secret"})

        # Google refuses: nothing is stored and sign-in is unchanged.
        bad = self.connect(CLIENT_ID, SECRET, status=400, token_error="invalid_client")
        self.assertIn("rejected this client ID or client secret", bad["error"]["message"])
        bad = self.connect(CLIENT_ID, SECRET, status=400, location=auth_error_location("redirect_uri_mismatch"))
        self.assertIn("Add %s as an authorized redirect URI" % callback, bad["error"]["message"])
        self.assertEqual(self.gcp.versions, {})
        self.assertEqual(self.post("/api/v1/access/oauth", {"clientId": "nope", "clientSecret": SECRET}, 400)
                         ["error"]["message"], "Enter the client ID of a Web application client "
                                               "(…apps.googleusercontent.com).")

        # Connected: the secret is in Secret Manager only; IAP is no longer on the sign-in path.
        data = self.connect(CLIENT_ID, SECRET)["data"]
        self.assertEqual((data["oauth"]["active"], data["oauth"]["source"], data["oauth"]["clientId"],
                          data["oauth"]["connectedBy"]), (True, "settings", CLIENT_ID, "admin"))
        self.assertFalse(data["google"]["enabled"])
        self.assertEqual(self.gcp.states(), {"1": "ENABLED"})
        stored = self.app.settings.auth_state()["oauth"]
        self.assertEqual(stored["secretVersion"], VERSION % 1)
        self.assertNotIn(SECRET, json.dumps(self.app.settings.auth_state()))
        state = self.request("GET", "/api/v1/auth/state").json()["data"]
        self.assertEqual((state["googleEnabled"], state["switchAccountUrl"], state["googleHint"]), (True, None, None))

        # An approved account of another organization signs in with the connected client.
        self.post("/api/v1/access/users", {"email": "partner@outside.example.net", "role": "viewer"})
        location, txn = self.start()
        url = urllib.parse.urlsplit(location)
        self.assertEqual((url.netloc, url.path), ("accounts.google.com", "/o/oauth2/v2/auth"))
        self.assertEqual(urllib.parse.parse_qs(url.query)["client_id"], [CLIENT_ID])
        cb, post, verify = self.finish(location, txn, "partner@outside.example.net", CLIENT_ID)
        self.assertEqual((cb.status, cb.header("Location")), (302, "/#/overview"))
        self.assertTrue(cb.cookie("cp_session"))
        sent = post.call_args.kwargs["data"]
        self.assertEqual((sent["client_id"], sent["client_secret"], sent["redirect_uri"]), (CLIENT_ID, SECRET, callback))
        self.assertEqual(verify.call_args.kwargs["audience"], CLIENT_ID)

        # Replaced: the previous secret version is destroyed; a sign-in started with the old client expires.
        location, txn = self.start()
        data = self.connect(CLIENT_ID_2, SECRET_2)["data"]
        self.assertEqual(data["oauth"]["clientId"], CLIENT_ID_2)
        self.assertEqual(self.gcp.states(), {"1": "DESTROYED", "2": "ENABLED"})
        cb, _, _ = self.finish(location, txn, "partner@outside.example.net", CLIENT_ID)
        self.assertEqual(cb.header("Location"), "/login?error=expired")

        # Disconnected: back to the sign-in bridge; the secret version is destroyed.
        data = self.post("/api/v1/access/oauth/delete", {})["data"]
        self.assertEqual((data["oauth"]["active"], data["oauth"]["canConnect"]), (False, True))
        self.assertTrue(data["google"]["enabled"])
        self.assertEqual(self.gcp.states(), {"1": "DESTROYED", "2": "DESTROYED"})
        self.assertNotIn("oauth", self.app.settings.auth_state())
        location, _ = self.start()
        self.assertTrue(location.startswith(BRIDGE + "/auth/bridge?"), location)

    def test_unreadable_secret_keeps_the_bridge_and_is_reported(self):
        self.app.settings.mutate_auth(lambda s: s.update(oauth={"clientId": CLIENT_ID, "secretVersion": VERSION % 9,
                                                                "updatedAt": "", "updatedBy": "admin"}), "test")
        oauth = self.summary()["oauth"]
        self.assertEqual((oauth["active"], oauth["secretUnreadable"], oauth["canConnect"]), (False, True, True))
        location, _ = self.start()
        self.assertTrue(location.startswith(BRIDGE), location)
        self.post("/api/v1/access/oauth/delete", {})
        self.assertNotIn("oauth", self.app.settings.auth_state())

    def test_secret_manager_permission_missing(self):
        self.gcp.deny_secret = True
        res = self.connect(CLIENT_ID, SECRET, status=403)
        self.assertIn("may not use the OAuth client secret in Secret Manager", res["error"]["message"])
        self.assertNotIn("oauth", self.app.settings.auth_state())

    def test_viewers_cannot_connect(self):
        res = self.request("POST", "/api/v1/access/oauth", body={"clientId": CLIENT_ID, "clientSecret": SECRET})
        self.assertIn(res.status, (401, 403))
        self.assertEqual(self.gcp.versions, {})


class NotConnectableTest(unittest.TestCase):
    def fake_app(self, environ=None, client_id="", settings=None):
        sign_in = sign_in_for(settings or FakeSettings(), bridge=BRIDGE, client_id=client_id,
                              environ=environ, reader=lambda v: SECRET)
        return mock.Mock(auth=sign_in, environ=dict(environ or {}), settings=sign_in.settings, gcp=FakeGcp())

    def test_without_the_deployment_secret(self):
        app = self.fake_app()
        self.assertFalse(oauth_client.summary(app, None)["canConnect"])
        with self.assertRaises(ApiError) as err:
            oauth_client.connect(app, "demo-project", "admin", CLIENT_ID, SECRET, "https://d.example.com" + CALLBACK)
        self.assertEqual(err.exception.code, "conflict")

    def test_deployment_client_is_read_only(self):
        env = {"GOOGLE_OAUTH_CLIENT_SECRET": SECRET, "OAUTH_CONNECT_SECRET": SECRET_RES}
        app = self.fake_app(environ=env, client_id=CLIENT_ID)
        summary = oauth_client.summary(app, None)
        self.assertEqual((summary["source"], summary["canConnect"], summary["active"]), ("deployment", False, True))
        for call in (lambda: oauth_client.connect(app, "p", "admin", CLIENT_ID_2, SECRET_2, "https://d.example.com"),
                     lambda: oauth_client.disconnect(app, "p", "admin")):
            with self.assertRaises(ApiError) as err:
                call()
            self.assertEqual(err.exception.code, "conflict")

    def test_resource_of_the_secret_is_validated(self):
        for value in ("", "projects/p/secrets/s/versions/1", "secrets/s", "projects/p/secrets/s;rm"):
            self.assertEqual(oauth_client.secret_resource(mock.Mock(environ={"OAUTH_CONNECT_SECRET": value})), "")
        self.assertEqual(oauth_client.secret_resource(mock.Mock(environ={"OAUTH_CONNECT_SECRET": SECRET_RES})),
                         SECRET_RES)


if __name__ == "__main__":
    unittest.main()
