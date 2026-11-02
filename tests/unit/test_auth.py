"""Google sign-in (auth_mode "google"): signed tokens, redirect safety and the full HTTP flow.

The flow runs against the real application on 127.0.0.1 with Google's token endpoint and ID token
signature check mocked: sign-in page, access gate, OAuth start (PKCE, state, nonce), callback
(state / nonce / verified-email checks), approved vs unapproved accounts, access requests,
administrator approval, removal with immediate revocation, sign-out revocation and lock-out guards.
"""

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane import app as app_module
from controlplane.auth import Signer, load_session_secret, safe_next

CLIENT_ID = "123456789012-abcdefghijklmnop.apps.googleusercontent.com"
CONFIG = """workspace:
  project_id: demo-project
  audit_dataset: ge_analytics
  timezone: UTC
security:
  auth_mode: google
  google_client_id: %s
  admins: [admin@example.com]
  allowed_users: ["*@example.org"]
storage:
  backend: memory
""" % CLIENT_ID
ENV = {"GOOGLE_OAUTH_CLIENT_SECRET": "test-client-secret", "SESSION_SECRET": "s" * 48}


class SignerTest(unittest.TestCase):
    def test_roundtrip_tamper_purpose_expiry(self):
        signer = Signer(b"k" * 32, "session")
        token = signer.sign({"email": "a@example.com"}, 60)
        self.assertEqual(signer.verify(token)["email"], "a@example.com")
        payload, mac = token.split(".")
        forged = Signer(b"k" * 32, "session").sign({"email": "admin@example.com"}, 60).split(".")[0]
        self.assertIsNone(signer.verify("%s.%s" % (forged, mac)))
        self.assertIsNone(signer.verify(payload + "." + ("A" * 43)))
        self.assertIsNone(Signer(b"k" * 32, "notice").verify(token))
        self.assertIsNone(Signer(b"x" * 32, "session").verify(token))
        self.assertIsNone(signer.verify(signer.sign({"email": "a@example.com"}, 60, now=time.time() - 120)))
        for junk in (None, "", "abc", "a.b.c", "%s.%s" % ("x" * 20, "y" * 10)):
            self.assertIsNone(signer.verify(junk))

    def test_session_secret(self):
        with self.assertRaises(ValueError):
            load_session_secret({"SESSION_SECRET": "short"})
        with self.assertRaises(ValueError):
            load_session_secret({"K_SERVICE": "svc"})
        self.assertEqual(len(load_session_secret({})), 32)
        self.assertEqual(load_session_secret({"SESSION_SECRET": "z" * 40}), b"z" * 40)

    def test_safe_next(self):
        self.assertEqual(safe_next("/"), "/")
        self.assertEqual(safe_next("/#/settings/access"), "/#/settings/access")
        for bad in ("//evil.example", "https://evil.example", "/\\evil.example", "/login?x", "/logout",
                    "/auth/google/start", "/api/v1/bootstrap", "/ok\r\nSet-Cookie: x", "", None, "relative"):
            self.assertEqual(safe_next(bad), "/", bad)


class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    def header(self, name):
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return None

    def cookies(self):
        out = {}
        for key, value in self.headers:
            if key.lower() == "set-cookie":
                pair = value.split(";", 1)[0]
                name, _, val = pair.partition("=")
                out[name] = (val, value)
        return out

    def json(self):
        return json.loads(self.body.decode("utf-8"))


class GoogleSignInFlowTest(unittest.TestCase):
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

    # -- helpers --------------------------------------------------------------------
    def request(self, method, path, cookies=None, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        if cookies:
            hdrs["Cookie"] = "; ".join("%s=%s" % kv for kv in cookies.items())
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        out = Response(resp.status, resp.getheaders(), resp.read())
        conn.close()
        return out

    def sign_in(self, email, verified=True, nonce=None, token_status=200):
        start = self.request("GET", "/auth/google/start?next=%2F%23%2Fsettings%2Faccess")
        self.assertEqual(start.status, 302)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(start.header("Location")).query)
        txn = start.cookies()["cp_oauth"][0]
        claims = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": "sub-" + email, "email": email,
                  "email_verified": verified, "name": "Test " + email.split("@")[0],
                  "picture": "https://lh3.googleusercontent.com/a/test", "nonce": nonce or query["nonce"][0]}
        token_resp = mock.Mock(status_code=token_status)
        token_resp.json.return_value = {"id_token": "header.payload.signature"}
        with mock.patch("requests.post", return_value=token_resp) as post, \
                mock.patch("google.oauth2.id_token.verify_oauth2_token", return_value=claims) as verify:
            cb = self.request("GET", "/auth/google/callback?state=%s&code=4%%2Fcode" % query["state"][0],
                              cookies={"cp_oauth": txn})
        if token_status == 200:
            sent = post.call_args.kwargs["data"]
            self.assertEqual(sent["code_verifier"] and len(sent["code_verifier"]) >= 43, True)
            self.assertEqual(sent["redirect_uri"], "http://127.0.0.1:%d/auth/google/callback" % self.port)
            self.assertEqual(verify.call_args.kwargs["audience"], CLIENT_ID)
        return cb

    def session_cookie(self, email):
        cb = self.sign_in(email)
        self.assertEqual(cb.status, 302, cb.header("Location"))
        return cb.cookies()["cp_session"][0]

    def csrf(self, session):
        res = self.request("GET", "/api/v1/auth/state", cookies={"cp_session": session})
        return res.json()["data"]["csrfToken"], res.cookies()["cp_csrf"][0]

    def post(self, path, session, body):
        token, cookie = self.csrf(session)
        return self.request("POST", path, cookies={"cp_session": session, "cp_csrf": cookie},
                            headers={"X-CSRF-Token": token}, body=body)

    # -- tests ----------------------------------------------------------------------
    def test_gate_and_public_pages(self):
        res = self.request("GET", "/")
        self.assertEqual(res.status, 302)
        self.assertTrue(res.header("Location").startswith("/login?next=/"))
        self.assertEqual(self.request("GET", "/api/v1/bootstrap").status, 401)
        page = self.request("GET", "/login")
        self.assertEqual(page.status, 200)
        self.assertIn(b"/assets/js/login.js", page.body)
        self.assertNotIn(b"__CSP_NONCE__", page.body)
        self.assertIn("frame-ancestors 'none'", page.header("Content-Security-Policy"))
        self.assertEqual(self.request("GET", "/assets/css/login.css").status, 200)
        state = self.request("GET", "/api/v1/auth/state").json()["data"]
        self.assertEqual((state["authMode"], state["googleEnabled"], state["user"]), ("google", True, None))

    def test_start_redirect(self):
        res = self.request("GET", "/auth/google/start?next=%2F%23%2Fmodels")
        self.assertEqual(res.status, 302)
        url = urllib.parse.urlsplit(res.header("Location"))
        self.assertEqual((url.scheme, url.netloc, url.path), ("https", "accounts.google.com", "/o/oauth2/v2/auth"))
        query = urllib.parse.parse_qs(url.query)
        self.assertEqual(query["client_id"], [CLIENT_ID])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["scope"], ["openid email profile"])
        self.assertEqual(query["prompt"], ["select_account"])
        self.assertGreaterEqual(len(query["state"][0]), 40)
        cookie = res.cookies()["cp_oauth"][1]
        for attr in ("HttpOnly", "SameSite=Lax", "Path=/", "Max-Age=600"):
            self.assertIn(attr, cookie)
        https = self.request("GET", "/auth/google/start", headers={"X-Forwarded-Proto": "https"})
        secure = https.cookies()["__Host-cp_oauth"][1]
        self.assertIn("Secure", secure)

    def test_callback_rejections(self):
        start = self.request("GET", "/auth/google/start")
        txn = start.cookies()["cp_oauth"][0]
        bad_state = self.request("GET", "/auth/google/callback?state=wrong&code=x", cookies={"cp_oauth": txn})
        self.assertEqual(bad_state.header("Location"), "/login?error=expired")
        no_cookie = self.request("GET", "/auth/google/callback?state=x&code=x")
        self.assertEqual(no_cookie.header("Location"), "/login?error=expired")
        cancelled = self.request("GET", "/auth/google/callback?error=access_denied", cookies={"cp_oauth": txn})
        self.assertEqual(cancelled.header("Location"), "/login?error=cancelled")
        self.assertEqual(self.sign_in("admin@example.com", nonce="other").header("Location"), "/login?error=failed")
        self.assertEqual(self.sign_in("admin@example.com", verified=False).header("Location"), "/login?error=failed")
        self.assertEqual(self.sign_in("admin@example.com", token_status=400).header("Location"), "/login?error=failed")

    def test_unapproved_account_requests_access(self):
        cb = self.sign_in("stranger1@example.net")
        self.assertEqual(cb.header("Location"), "/login?error=not_approved")
        self.assertNotIn("cp_session", {k: v for k, v in cb.cookies().items() if v[0]})
        notice = cb.cookies()["cp_notice"][0]
        state = self.request("GET", "/api/v1/auth/state", cookies={"cp_notice": notice}).json()["data"]
        self.assertEqual(state["deniedEmail"], "stranger1@example.net")
        emails = [r["email"] for r in self.app.settings.auth_state()["requests"]]
        self.assertIn("stranger1@example.net", emails)

    def test_viewer_session_and_admin_only_endpoints(self):
        cb = self.sign_in("person@example.org")
        self.assertEqual(cb.header("Location"), "/#/settings/access")
        session = cb.cookies()["cp_session"][0]
        cookie = cb.cookies()["cp_session"][1]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertEqual(self.request("GET", "/", cookies={"cp_session": session}).status, 200)
        boot = self.request("GET", "/api/v1/bootstrap", cookies={"cp_session": session}).json()["data"]
        self.assertEqual((boot["session"]["email"], boot["session"]["role"], boot["session"]["authMode"]),
                         ("person@example.org", "viewer", "google"))
        self.assertFalse(boot["session"]["piiMasked"])
        self.assertEqual(boot["workspace"]["billing"]["accountId"], "")
        # Viewers get the read-only settings view without deployment identifiers.
        view = self.request("GET", "/api/v1/settings", cookies={"cp_session": session}).json()["data"]
        self.assertEqual(view["effective"]["security"]["google_client_id"], "***")
        self.assertFalse(view["editable"])
        self.assertEqual(self.request("GET", "/api/v1/access", cookies={"cp_session": session}).status, 403)
        self.assertEqual(self.post("/api/v1/access/users", session, {"email": "x@example.com", "role": "admin"}).status,
                         403)
        self.assertEqual(self.request("GET", "/", cookies={"cp_session": session + "x"}).status, 302)

    def test_viewer_settings_view_hides_identifiers(self):
        admin = self.session_cookie("admin@example.com")
        saved = self.post("/api/v1/settings", admin,
                          {"section": "workspace", "values": {"billing": {"account_id": "ABCDEF-123456-ABCDEF"}}})
        self.assertEqual(saved.status, 200, saved.body if hasattr(saved, "body") else None)
        viewer = self.session_cookie("person@example.org")
        view = self.request("GET", "/api/v1/settings", cookies={"cp_session": viewer}).json()["data"]
        self.assertEqual(set(view["persistence"]), {"backend", "durable", "updatedAt"})
        self.assertEqual(view["overrides"]["workspace"]["billing"]["account_id"], "***")
        self.assertEqual(view["effective"]["workspace"]["billing"]["account_id"], "***")
        for member in ((view["overrides"].get("security") or {}).get("admins") or []):
            self.assertTrue(member.startswith("*@") or "***@" in member, member)
        text = json.dumps(view)
        for secret in ("admin@example.com", "ABCDEF-123456-ABCDEF", CLIENT_ID):
            self.assertNotIn(secret, text)
        self.post("/api/v1/settings", admin, {"section": "security", "values": {"mask_pii_for_viewers": True}})
        denied = self.post("/api/v1/analyst/chat", viewer, {"question": "How many prompts last week?"})
        self.assertEqual(denied.status, 403)
        self.assertIn("available to administrators", denied.json()["error"]["message"])
        self.post("/api/v1/settings", admin, {"section": "security", "values": {"mask_pii_for_viewers": False}})
        full = self.request("GET", "/api/v1/settings", cookies={"cp_session": admin}).json()["data"]
        self.assertEqual(full["overrides"]["workspace"]["billing"]["account_id"], "ABCDEF-123456-ABCDEF")
        self.assertEqual(full["persistence"]["updatedBy"], "admin@example.com")

    def test_admin_approves_removes_and_guards(self):
        admin = self.session_cookie("admin@example.com")
        self.assertEqual(self.sign_in("stranger2@example.net").header("Location"), "/login?error=not_approved")
        summary = self.request("GET", "/api/v1/access", cookies={"cp_session": admin}).json()["data"]
        self.assertIn("stranger2@example.net", [r["email"] for r in summary["requests"]])
        me = [u for u in summary["users"] if u["email"] == "admin@example.com"][0]
        self.assertEqual((me["role"], me["source"], me["self"], me["canRemove"]), ("admin", "config", True, False))

        res = self.post("/api/v1/access/users", admin, {"email": "stranger2@example.net", "role": "viewer"})
        self.assertEqual(res.status, 200)
        self.assertNotIn("stranger2@example.net", [r["email"] for r in res.json()["data"]["requests"]])
        stranger = self.session_cookie("stranger2@example.net")
        self.assertEqual(self.request("GET", "/api/v1/bootstrap", cookies={"cp_session": stranger}).status, 200)

        self.assertEqual(self.post("/api/v1/access/users", admin, {"email": "admin@example.com", "role": "viewer"}).status,
                         409)
        self.assertEqual(self.post("/api/v1/access/users/delete", admin, {"email": "admin@example.com"}).status, 409)
        self.assertEqual(self.post("/api/v1/access/users", admin, {"email": "not an email", "role": "viewer"}).status,
                         400)

        # An administrator approved in the app cannot remove their own administrator access.
        self.assertEqual(self.post("/api/v1/access/users", admin, {"email": "ops@example.com", "role": "admin"}).status,
                         200)
        ops = self.session_cookie("ops@example.com")
        self.assertEqual(self.request("GET", "/api/v1/access", cookies={"cp_session": ops}).status, 200)
        self.assertEqual(self.post("/api/v1/settings", ops, {"section": "security", "values": {"admins": []}}).status,
                         409)
        self.assertEqual(self.post("/api/v1/settings/reset", ops, {"section": "security", "keys": []}).status, 409)
        self.assertEqual(self.post("/api/v1/access/users", ops, {"email": "ops@example.com", "role": "viewer"}).status,
                         409)

        removed = self.post("/api/v1/access/users/delete", admin, {"email": "stranger2@example.net"})
        self.assertEqual(removed.status, 200)
        self.assertEqual(self.request("GET", "/api/v1/bootstrap", cookies={"cp_session": stranger}).status, 401)
        self.assertEqual(self.request("GET", "/", cookies={"cp_session": stranger}).status, 302)

    def test_bulk_pre_add_users_and_oauth_sign_in(self):
        admin = self.session_cookie("admin@example.com")
        # 1. Pre-add multiple viewer accounts via comma/newline-separated email string
        res = self.post("/api/v1/access/users", admin, {
            "email": "pre1@example.com, pre2@example.com;\npre3@example.com",
            "role": "viewer",
        })
        self.assertEqual(res.status, 200)
        users_map = {u["email"]: u["role"] for u in res.json()["data"]["users"]}
        for email in ("pre1@example.com", "pre2@example.com", "pre3@example.com"):
            self.assertEqual(users_map.get(email), "viewer")
            sess = self.session_cookie(email)
            boot = self.request("GET", "/api/v1/bootstrap", cookies={"cp_session": sess})
            self.assertEqual(boot.status, 200)
            self.assertEqual(boot.json()["data"]["session"]["role"], "viewer")
            self.assertEqual(boot.json()["data"]["session"]["email"], email)
            self.assertFalse(boot.json()["data"]["session"]["piiMasked"])

        # 2. Pre-add multiple admin accounts and a domain pattern via emails array
        res2 = self.post("/api/v1/access/users", admin, {
            "emails": ["lead1@example.com", "*@partner.example.org"],
            "role": "admin",
        })
        self.assertEqual(res2.status, 200)
        for email in ("lead1@example.com", "member99@partner.example.org"):
            sess = self.session_cookie(email)
            boot = self.request("GET", "/api/v1/bootstrap", cookies={"cp_session": sess})
            self.assertEqual(boot.status, 200)
            self.assertEqual(boot.json()["data"]["session"]["role"], "admin")

        # 3. Unregistered user is still blocked when guest_access is false and * is not in allowed_users
        self.assertEqual(self.sign_in("unlisted@example.net").header("Location"), "/login?error=not_approved")

        # 4. Wildcard "*" in allowed_users lets any Google ID sign in as unmasked Viewer (but never as Admin)
        self.assertEqual(self.post("/api/v1/access/users", admin, {"email": "*", "role": "admin"}).status, 400)
        res3 = self.post("/api/v1/access/users", admin, {"email": "*", "role": "viewer"})
        self.assertEqual(res3.status, 200)
        any_sess = self.session_cookie("anybody@example.net")
        any_boot = self.request("GET", "/api/v1/bootstrap", cookies={"cp_session": any_sess}).json()["data"]
        self.assertEqual((any_boot["session"]["email"], any_boot["session"]["role"], any_boot["session"]["piiMasked"]),
                         ("anybody@example.net", "viewer", False))
        self.assertEqual(self.post("/api/v1/settings", any_sess, {"section": "security", "values": {}}).status, 403)

        # 5. Cleanup added entries
        for entry in ("*", "pre1@example.com", "pre2@example.com", "pre3@example.com", "lead1@example.com", "*@partner.example.org"):
            self.post("/api/v1/access/users/delete", admin, {"email": entry})

    def test_sign_out_revokes_every_session(self):
        first = self.session_cookie("admin@example.com")
        time.sleep(0.01)
        second = self.session_cookie("admin@example.com")
        time.sleep(0.01)
        res = self.post("/api/v1/auth/logout", second, {})
        self.assertEqual(res.status, 200)
        self.assertEqual(res.json()["data"]["redirect"], "/login?signed_out=1")
        cleared = res.cookies()["cp_session"]
        self.assertEqual(cleared[0], "")
        self.assertIn("Max-Age=0", cleared[1])
        for old in (first, second):
            self.assertEqual(self.request("GET", "/api/v1/bootstrap", cookies={"cp_session": old}).status, 401)
        time.sleep(0.01)
        fresh = self.session_cookie("admin@example.com")
        self.assertEqual(self.request("GET", "/api/v1/bootstrap", cookies={"cp_session": fresh}).status, 200)

    def test_logout_requires_csrf(self):
        session = self.session_cookie("admin@example.com")
        res = self.request("POST", "/api/v1/auth/logout", cookies={"cp_session": session}, body={})
        self.assertEqual(res.status, 403)

    def test_password_login_and_account_management(self):
        # 1. Initial auth state advertises passwordEnabled and defaultAdminPassword
        state = self.request("GET", "/api/v1/auth/state").json()["data"]
        self.assertTrue(state.get("passwordEnabled"))
        self.assertTrue(state.get("defaultAdminPassword"))
        csrf = state["csrfToken"]

        # 2. Default admin/admin login succeeds and grants administrator access
        bad = self.request("POST", "/api/v1/auth/login",
                           cookies={"cp_csrf": csrf}, headers={"X-CSRF-Token": csrf},
                           body={"username": "admin", "password": "wrong"})
        self.assertEqual(bad.status, 401)
        ok = self.request("POST", "/api/v1/auth/login",
                          cookies={"cp_csrf": csrf}, headers={"X-CSRF-Token": csrf},
                          body={"username": "admin", "password": "admin", "next": "/"})
        self.assertEqual(ok.status, 200)
        self.assertEqual(ok.json()["data"]["user"]["email"], "admin")
        self.assertEqual(ok.json()["data"]["user"]["role"], "admin")
        admin_sess = ok.cookies()["cp_session"][0]
        boot = self.request("GET", "/api/v1/bootstrap", cookies={"cp_session": admin_sess}).json()["data"]
        self.assertEqual((boot["session"]["email"], boot["session"]["role"], boot["session"]["canWrite"]),
                         ("admin", "admin", True))

        # 3. Built-in admin can create custom ID/Password accounts (admin & viewer) and change admin password
        r1 = self.post("/api/v1/access/users", admin_sess,
                       {"email": "team-viewer", "role": "viewer", "password": "viewer-pass-123"})
        self.assertEqual(r1.status, 200)
        r2 = self.post("/api/v1/access/users", admin_sess,
                       {"email": "team-admin", "role": "admin", "password": "admin-pass-456"})
        self.assertEqual(r2.status, 200)

        # 4. Log in with team-viewer and verify read-only viewer role
        v_login = self.request("POST", "/api/v1/auth/login",
                               cookies={"cp_csrf": csrf}, headers={"X-CSRF-Token": csrf},
                               body={"username": "team-viewer", "password": "viewer-pass-123"})
        self.assertEqual(v_login.status, 200)
        v_sess = v_login.cookies()["cp_session"][0]
        v_boot = self.request("GET", "/api/v1/bootstrap", cookies={"cp_session": v_sess}).json()["data"]
        self.assertEqual((v_boot["session"]["email"], v_boot["session"]["role"], v_boot["session"]["canWrite"]),
                         ("team-viewer", "viewer", False))
        self.assertEqual(self.post("/api/v1/settings", v_sess, {"section": "product", "values": {"name": "X"}}).status,
                         403)

        # 5. Update built-in admin password; default "admin" password stops working, new password works
        r3 = self.post("/api/v1/access/users", admin_sess,
                       {"email": "admin", "role": "admin", "password": "new-admin-pw"})
        self.assertEqual(r3.status, 200)
        state2 = self.request("GET", "/api/v1/auth/state").json()["data"]
        self.assertFalse(state2.get("defaultAdminPassword"))
        self.assertEqual(self.request("POST", "/api/v1/auth/login",
                                      cookies={"cp_csrf": csrf}, headers={"X-CSRF-Token": csrf},
                                      body={"username": "admin", "password": "admin"}).status, 401)
        self.assertEqual(self.request("POST", "/api/v1/auth/login",
                                      cookies={"cp_csrf": csrf}, headers={"X-CSRF-Token": csrf},
                                      body={"username": "admin", "password": "new-admin-pw"}).status, 200)

        # 6. Built-in admin cannot be deleted or demoted
        self.assertEqual(self.post("/api/v1/access/users/delete", admin_sess, {"email": "admin"}).status, 409)
        self.assertEqual(self.post("/api/v1/access/users", admin_sess, {"email": "admin", "role": "viewer"}).status, 409)

        # 7. Delete custom ID account and verify immediate session revocation
        del_res = self.post("/api/v1/access/users/delete", admin_sess, {"email": "team-viewer"})
        self.assertEqual(del_res.status, 200)
        self.assertEqual(self.request("GET", "/api/v1/bootstrap", cookies={"cp_session": v_sess}).status, 401)
        self.post("/api/v1/access/users/delete", admin_sess, {"email": "team-admin"})


class GoogleModeConfigTest(unittest.TestCase):
    def test_canonical_host_redirect(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "config.yaml")
            with open(cfg, "w", encoding="utf-8") as fh:
                fh.write(CONFIG)
            app = app_module.App(environ=dict(ENV, CONFIG_PATH=cfg, PUBLIC_BASE_URL="https://dash.example.com"))
            server = app_module.make_server(app, "127.0.0.1", 0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                conn.request("GET", "/login?next=%2F")
                resp = conn.getresponse()
                resp.read()
                self.assertEqual(resp.status, 302)
                self.assertEqual(resp.getheader("Location"), "https://dash.example.com/login?next=%2F")
                conn.request("GET", "/auth/google/start", headers={"Host": "dash.example.com"})
                resp = conn.getresponse()
                resp.read()
                self.assertTrue(resp.getheader("Location").startswith("https://accounts.google.com/"))
                self.assertIn("redirect_uri=https%3A%2F%2Fdash.example.com%2Fauth%2Fgoogle%2Fcallback",
                              resp.getheader("Location"))
                conn.close()
            finally:
                server.shutdown()
                server.server_close()

    def test_missing_client_secret_fails_fast(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "config.yaml")
            with open(cfg, "w", encoding="utf-8") as fh:
                fh.write(CONFIG)
            with self.assertRaises(ValueError):
                app_module.App(environ={"CONFIG_PATH": cfg, "SESSION_SECRET": "s" * 48})
            with self.assertRaises(ValueError):
                app_module.App(environ={"CONFIG_PATH": cfg, "GOOGLE_OAUTH_CLIENT_SECRET": "x", "K_SERVICE": "svc"})



BRIDGE_A_CONFIG = """workspace:
  project_id: demo-project
  timezone: UTC
security:
  auth_mode: google
  admins: [admin@example.com]
storage:
  backend: memory
"""
BRIDGE_B_CONFIG = """security:
  auth_mode: iap
  iap_audience: /projects/1/locations/us-central1/services/signin
storage:
  backend: memory
"""


def _serve(app):
    server = app_module.make_server(app, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _get(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    out = Response(resp.status, resp.getheaders(), resp.read())
    conn.close()
    return out


class SignInBridgeTest(unittest.TestCase):
    """auth_mode google without an OAuth client: the IAP-protected bridge returns a signed assertion."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        a_cfg = os.path.join(cls.tmp.name, "a.yaml")
        b_cfg = os.path.join(cls.tmp.name, "b.yaml")
        with open(a_cfg, "w", encoding="utf-8") as fh:
            fh.write(BRIDGE_A_CONFIG)
        with open(b_cfg, "w", encoding="utf-8") as fh:
            fh.write(BRIDGE_B_CONFIG)
        cls.a = app_module.App(environ={"CONFIG_PATH": a_cfg, "SESSION_SECRET": ENV["SESSION_SECRET"],
                                        "SIGN_IN_BRIDGE_URL": "https://bridge.example.com"})
        cls.a_server = _serve(cls.a)
        cls.a_port = cls.a_server.server_address[1]
        cls.b = app_module.App(environ={"CONFIG_PATH": b_cfg, "SESSION_SECRET": ENV["SESSION_SECRET"],
                                        "BRIDGE_RETURN_URL": "http://127.0.0.1:%d" % cls.a_port})
        cls.iap_email = "admin@example.com"
        cls.b.verifier.verify = lambda token, audience: cls.iap_email
        cls.b_server = _serve(cls.b)
        cls.b_port = cls.b_server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        for server in (cls.a_server, cls.b_server):
            server.shutdown()
            server.server_close()
        cls.tmp.cleanup()

    def start(self):
        res = _get(self.a_port, "/auth/google/start?next=%2F%23%2Fusers")
        self.assertEqual(res.status, 302)
        url = urllib.parse.urlsplit(res.header("Location"))
        self.assertEqual((url.scheme, url.netloc, url.path), ("https", "bridge.example.com", "/auth/bridge"))
        state = urllib.parse.parse_qs(url.query)["state"][0]
        return state, res.cookies()["cp_oauth"][0]

    def bridge(self, state):
        res = _get(self.b_port, "/auth/bridge?state=%s" % state, headers={"x-goog-iap-jwt-assertion": "x.y.z"})
        self.assertEqual(res.status, 302)
        loc = urllib.parse.urlsplit(res.header("Location"))
        self.assertEqual(loc.netloc, "127.0.0.1:%d" % self.a_port)
        self.assertEqual(loc.path, "/auth/google/callback")
        return loc.path + "?" + loc.query

    def test_full_bridge_sign_in(self):
        state, txn = self.start()
        callback = self.bridge(state)
        res = _get(self.a_port, callback, headers={"Cookie": "cp_oauth=%s" % txn})
        self.assertEqual(res.header("Location"), "/#/users")
        session = res.cookies()["cp_session"][0]
        boot = _get(self.a_port, "/api/v1/bootstrap", headers={"Cookie": "cp_session=%s" % session})
        self.assertEqual(boot.status, 200)
        self.assertEqual(boot.json()["data"]["session"]["role"], "admin")
        st = _get(self.a_port, "/api/v1/auth/state").json()["data"]
        self.assertEqual(st["switchAccountUrl"], "https://bridge.example.com/_gcp_iap/clear_login_cookie")

    def test_unapproved_account_via_bridge(self):
        type(self).iap_email = "someone@example.net"
        try:
            state, txn = self.start()
            res = _get(self.a_port, self.bridge(state), headers={"Cookie": "cp_oauth=%s" % txn})
            self.assertEqual(res.header("Location"), "/login?error=not_approved")
            self.assertIn("someone@example.net", [r["email"] for r in self.a.settings.auth_state()["requests"]])
        finally:
            type(self).iap_email = "admin@example.com"

    def test_assertion_bound_to_browser_state(self):
        state1, _txn1 = self.start()
        _state2, txn2 = self.start()
        stolen = self.bridge(state1)
        res = _get(self.a_port, stolen, headers={"Cookie": "cp_oauth=%s" % txn2})
        self.assertEqual(res.header("Location"), "/login?error=expired")
        res = _get(self.a_port, stolen)
        self.assertEqual(res.header("Location"), "/login?error=expired")
        state3, txn3 = self.start()
        forged = "/auth/google/callback?state=%s&assertion=%s" % (state3, "e30." + "A" * 43)
        self.assertEqual(_get(self.a_port, forged, headers={"Cookie": "cp_oauth=%s" % txn3}).header("Location"),
                         "/login?error=failed")

    def test_bridge_service_surface(self):
        self.assertEqual(_get(self.b_port, "/auth/bridge?state=bad", headers={"x-goog-iap-jwt-assertion": "x"}).status,
                         400)
        home = _get(self.b_port, "/")
        self.assertEqual(home.header("Location"), "http://127.0.0.1:%d/login" % self.a_port)
        self.assertEqual(_get(self.b_port, "/api/v1/bootstrap").status, 404)

    def test_bridge_requires_iap_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "c.yaml")
            with open(cfg, "w", encoding="utf-8") as fh:
                fh.write(BRIDGE_A_CONFIG)
            with self.assertRaises(ValueError):
                app_module.App(environ={"CONFIG_PATH": cfg, "SESSION_SECRET": ENV["SESSION_SECRET"],
                                        "SIGN_IN_BRIDGE_URL": "https://bridge.example.com",
                                        "BRIDGE_RETURN_URL": "https://dash.example.com"})

    def test_oauth_redirect_and_start_url_precedence_over_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "c.yaml")
            with open(cfg, "w", encoding="utf-8") as fh:
                fh.write(CONFIG)
            environ = dict(
                ENV,
                CONFIG_PATH=cfg,
                GOOGLE_OAUTH_CLIENT_ID=CLIENT_ID,
                GOOGLE_OAUTH_REDIRECT_URI="https://relay.example.com/__/auth/handler",
                GOOGLE_OAUTH_START_URL="https://relay.example.com/auth/bridge",
                SIGN_IN_BRIDGE_URL="https://iap-bridge.example.com",
                PUBLIC_BASE_URL="https://dash.example.com",
            )
            with mock.patch.dict(os.environ, {"CONFIG_PATH": cfg}):
                custom_app = app_module.App(environ=environ)
            srv = app_module.make_server(custom_app, "127.0.0.1", 0)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                start = _get(port, "/auth/google/start", headers={"Host": "dash.example.com"})
                self.assertEqual(start.status, 302)
                loc = start.header("Location")
                self.assertTrue(loc.startswith("https://relay.example.com/auth/bridge?"), loc)
                outer_q = urllib.parse.parse_qs(urllib.parse.urlsplit(loc).query)
                self.assertEqual(outer_q["return_to"], ["https://dash.example.com/auth/google/callback"])
                self.assertEqual(outer_q["redirect_uri"], ["https://relay.example.com/__/auth/handler"])
                self.assertEqual(outer_q["client_id"], [CLIENT_ID])
                self.assertEqual(outer_q["code_challenge_method"], ["S256"])
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()

