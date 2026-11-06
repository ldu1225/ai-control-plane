"""Google sign-in through Identity-Aware Proxy: who IAP admits and keeping it in step with approved accounts.

Covers the runtime module (services/iap_access.py) against an IAP REST double, the access endpoints end to end on a real
application (bridge mode, built-in admin signed in with its initial password), the sign-in page hint, and the
deployment helper configure_iap_access (scripts/lib/common.sh) against a fake gcloud that refuses principals the way
domain restricted sharing does.
"""

import http.client
import json
import os
import subprocess
import tempfile
import threading
import types
import unittest
from unittest import mock

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane import app as app_module
from controlplane.gcp import GcpError
from controlplane.services import iap_access

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ROLE = iap_access.ROLE
BRIDGE = "https://ai-control-plane-signin-" + "123456789012" + ".asia-northeast3.run.app"  # split: publish scan rule
RES = "projects/123456789012/iap_web/cloud_run-asia-northeast3/services/ai-control-plane-signin"
DRS_MESSAGE = ("One or more users named in the policy do not belong to a permitted customer, perhaps due to an "
               "organization policy.")


class FakeIap:
    """IAP REST double: one IAM policy per resource (etag checked), settings per resource name, and domain restricted
    sharing as a set of principals that setIamPolicy refuses when they are added."""

    def __init__(self, members=(), refused=(), denied=False, settings=None):
        self.members = list(members)
        self.refused = set(refused)
        self.denied = denied
        self.settings = dict(settings or {})
        self.policies = {}
        self.calls = []

    def policy(self, res=RES):
        return self.policies.setdefault(res, {
            "version": 1, "etag": "e0",
            "bindings": [{"role": ROLE, "members": list(self.members)}] if self.members else []})

    def admitted(self, res=RES):
        return sorted({m for b in self.policy(res)["bindings"] if b["role"] == ROLE for m in b["members"]})

    def request(self, method, url, *, quota_project=None, params=None, json_body=None, timeout=None, **_):
        assert url.startswith(iap_access.API), url
        path = url[len(iap_access.API):]
        self.calls.append((method, path))
        if self.denied:
            raise GcpError("denied", status=403, message="The caller does not have permission")
        if path.endswith(":getIamPolicy"):
            return json.loads(json.dumps(self.policy(path[:-len(":getIamPolicy")]))), {}
        if path.endswith(":setIamPolicy"):
            res = path[:-len(":setIamPolicy")]
            current = self.policy(res)
            new = json_body["policy"]
            if new.get("etag") != current.get("etag"):
                raise GcpError("conflict", status=409, message="etag mismatch")
            before = {m for b in current["bindings"] for m in b["members"]}
            after = {m for b in new["bindings"] for m in b["members"]}
            if (after - before) & self.refused:
                raise GcpError("invalid", status=400, message=DRS_MESSAGE, reason="FAILED_PRECONDITION")
            stored = json.loads(json.dumps(new))
            stored["etag"] = "e%d" % len(self.calls)
            self.policies[res] = stored
            return stored, {}
        if path.endswith(":iapSettings"):
            name = path[:-len(":iapSettings")]
            value = self.settings.get(name, {"name": name})
            if isinstance(value, Exception):
                raise value
            return value, {}
        raise AssertionError("unexpected call " + path)

    def sets(self):
        return [c for c in self.calls if c[1].endswith(":setIamPolicy")]


def fake_app(mode="google", bridge=BRIDGE, client=("", ""), audience="", environ=None, gcp=None):
    auth = None
    if mode == "google":
        auth = types.SimpleNamespace(bridge_url=bridge, client_id=client[0], client_secret=client[1],
                                     oauth_active=lambda: bool(client[0] and client[1]))
    return types.SimpleNamespace(
        environ=dict(environ or {}),
        settings=types.SimpleNamespace(base={"security": {"auth_mode": mode, "iap_audience": audience}}),
        auth=auth, gcp=gcp or FakeIap())


class ResourceTest(unittest.TestCase):
    def setUp(self):
        iap_access._cache.clear()  # pylint: disable=protected-access

    def test_bridge_url_maps_to_the_cloud_run_iap_resource(self):
        self.assertEqual(iap_access.resource(fake_app()), RES)

    def test_explicit_resource_wins_and_must_be_well_formed(self):
        other = "projects/42/iap_web/cloud_run-us-central1/services/signin"
        self.assertEqual(iap_access.resource(fake_app(environ={"IAP_ACCESS_RESOURCE": other})), other)
        self.assertEqual(iap_access.resource(fake_app(environ={"IAP_ACCESS_RESOURCE": "projects/x/../evil"})), RES)

    def test_dashboard_behind_iap_uses_its_audience(self):
        app = fake_app(mode="iap", audience="/projects/77/locations/asia-northeast3/services/ai-control-plane")
        self.assertEqual(iap_access.resource(app),
                         "projects/77/iap_web/cloud_run-asia-northeast3/services/ai-control-plane")

    def test_no_iap_on_the_path(self):
        self.assertIsNone(iap_access.resource(fake_app(client=("id.apps.googleusercontent.com", "secret"))))
        self.assertIsNone(iap_access.resource(fake_app(bridge="")))
        self.assertIsNone(iap_access.resource(fake_app(bridge="https://bridge.example.com")))
        self.assertIsNone(iap_access.resource(fake_app(mode="iap", audience="")))

    def test_principals_and_coverage(self):
        self.assertEqual(iap_access.principal_for("Alice@Cymbal.Example.com"), "user:alice@cymbal.example.com")
        self.assertEqual(iap_access.principal_for("*@partner.example.org"), "domain:partner.example.org")
        for nothing in ("*", "team-viewer", "", None, "*@bad_domain"):
            self.assertIsNone(iap_access.principal_for(nothing), nothing)
        members = ["domain:cymbal.example.com", "user:ally@partner.example.org"]
        self.assertTrue(iap_access.covers(members, "bob@cymbal.example.com"))
        self.assertTrue(iap_access.covers(members, "ally@partner.example.org"))
        self.assertTrue(iap_access.covers(members, "*@cymbal.example.com"))
        self.assertFalse(iap_access.covers(members, "eve@partner.example.org"))
        self.assertFalse(iap_access.covers(members, "*"))
        self.assertTrue(iap_access.covers(["allAuthenticatedUsers"], "anyone@else.example.net"))


class GrantTest(unittest.TestCase):
    def setUp(self):
        iap_access._cache.clear()  # pylint: disable=protected-access
        iap_access._last.clear()  # pylint: disable=protected-access

    def test_one_request_for_several_accounts(self):
        gcp = FakeIap(members=["domain:cymbal.example.com"])
        out = iap_access.grant(fake_app(gcp=gcp), "proj", ["new@cymbal.example.com", "ally@partner.example.org",
                                                           "*@second.example.org", "team-viewer"])
        self.assertEqual({r["entry"]: r["result"] for r in out}, {
            "new@cymbal.example.com": "covered", "ally@partner.example.org": "added",
            "*@second.example.org": "added"})
        self.assertEqual(len(gcp.sets()), 1)
        self.assertEqual(gcp.admitted(), ["domain:cymbal.example.com", "domain:second.example.org",
                                          "user:ally@partner.example.org"])

    def test_refused_account_does_not_block_the_others(self):
        gcp = FakeIap(members=["user:owner@cymbal.example.com"], refused={"user:partner@outside.example.net"})
        out = iap_access.grant(fake_app(gcp=gcp), "proj", ["partner@outside.example.net", "ally@partner.example.org"])
        results = {r["entry"]: r for r in out}
        self.assertEqual(results["partner@outside.example.net"]["result"], "rejected")
        self.assertIn("permitted customer", results["partner@outside.example.net"]["message"])
        self.assertEqual(results["ally@partner.example.org"]["result"], "added")
        self.assertIn("user:ally@partner.example.org", gcp.admitted())
        self.assertNotIn("user:partner@outside.example.net", gcp.admitted())
        self.assertEqual(iap_access.last_result("partner@outside.example.net")["result"], "rejected")

    def test_no_permission_is_reported_not_raised(self):
        out = iap_access.grant(fake_app(gcp=FakeIap(denied=True)), "proj", ["new@cymbal.example.com"])
        self.assertEqual([r["result"] for r in out], ["denied"])

    def test_nothing_to_do_without_iap(self):
        gcp = FakeIap()
        self.assertEqual(iap_access.grant(fake_app(bridge="", gcp=gcp), "proj", ["new@cymbal.example.com"]), [])
        self.assertEqual(gcp.calls, [])

    def test_revoke_removes_user_bindings_only(self):
        gcp = FakeIap(members=["domain:cymbal.example.com", "user:ally@partner.example.org"])
        app = fake_app(gcp=gcp)
        self.assertEqual(iap_access.revoke(app, "proj", ["ally@partner.example.org", "*@cymbal.example.com"]),
                         ["user:ally@partner.example.org"])
        self.assertEqual(gcp.admitted(), ["domain:cymbal.example.com"])
        self.assertEqual(iap_access.revoke(app, "proj", ["ally@partner.example.org"]), [])

    def test_conditional_bindings_stay_untouched(self):
        gcp = FakeIap()
        gcp.policies[RES] = {"version": 3, "etag": "e0", "bindings": [
            {"role": ROLE, "members": ["group:vip@cymbal.example.com"],
             "condition": {"title": "office", "expression": "true"}}]}
        iap_access.grant(fake_app(gcp=gcp), "proj", ["new@cymbal.example.com"])
        bindings = gcp.policy()["bindings"]
        self.assertEqual(gcp.policy()["version"], 3)
        self.assertIn({"role": ROLE, "members": ["group:vip@cymbal.example.com"],
                       "condition": {"title": "office", "expression": "true"}}, bindings)
        self.assertIn({"role": ROLE, "members": ["user:new@cymbal.example.com"]}, bindings)


class StatusTest(unittest.TestCase):
    def setUp(self):
        iap_access._cache.clear()  # pylint: disable=protected-access

    def test_members_and_managed_oauth_client(self):
        gcp = FakeIap(members=["domain:cymbal.example.com", "user:ally@partner.example.org", "group:ops@cymbal.example.com"])
        st = iap_access.status(fake_app(gcp=gcp, environ={"IAP_ORG_DOMAIN": "cymbal.example.com"}), "proj")
        self.assertEqual((st["enabled"], st["open"], st["oauth"], st["orgDomain"], st["error"]),
                         (True, False, "managed", "cymbal.example.com", None))
        self.assertEqual(st["domains"], ["cymbal.example.com"])
        self.assertEqual(st["users"], ["ally@partner.example.org"])
        self.assertEqual(st["groups"], ["ops@cymbal.example.com"])
        self.assertEqual(iap_access.coverage(st, "bob@cymbal.example.com"), "covered")
        self.assertEqual(iap_access.coverage(st, "eve@outside.example.net"), "missing")
        self.assertIsNone(iap_access.coverage(st, "team-viewer"))
        self.assertIsNone(iap_access.coverage(st, "*"))
        calls = len(gcp.calls)
        iap_access.status(fake_app(gcp=gcp), "proj")
        self.assertEqual(len(gcp.calls), calls, "a second read within a minute comes from the cache")

    def test_custom_oauth_client_on_service_or_project(self):
        for name in (RES, "projects/123456789012"):
            iap_access._cache.clear()  # pylint: disable=protected-access
            gcp = FakeIap(members=["allAuthenticatedUsers"], settings={
                name: {"accessSettings": {"oauthSettings": {"clientId": "123-abc.apps.googleusercontent.com"}}}})
            st = iap_access.status(fake_app(gcp=gcp), "proj")
            self.assertEqual((st["oauth"], st["open"]), ("custom", True), name)
            self.assertEqual(iap_access.coverage(st, "*"), "covered")

    def test_unreadable_settings_fall_back_to_the_deployment_finding(self):
        denied = GcpError("denied", status=403, message="no")
        gcp = FakeIap(members=["allAuthenticatedUsers"], settings={RES: denied, "projects/123456789012": denied})
        self.assertEqual(iap_access.status(fake_app(gcp=gcp), "proj")["oauth"], "unknown")
        iap_access._cache.clear()  # pylint: disable=protected-access
        st = iap_access.status(fake_app(gcp=gcp, environ={"IAP_OAUTH_CLIENT": "custom"}), "proj")
        self.assertEqual(st["oauth"], "custom")

    def test_unreadable_policy(self):
        st = iap_access.status(fake_app(gcp=FakeIap(denied=True)), "proj")
        self.assertEqual(st["error"], "denied")
        self.assertEqual(iap_access.coverage(st, "bob@cymbal.example.com"), "unknown")
        self.assertEqual(iap_access.status(fake_app(bridge=""), "proj"), {"enabled": False})

    def test_unreachable_iap_costs_one_call(self):
        calls = []

        class Down:
            def request(self, *args, **kwargs):  # pylint: disable=unused-argument
                calls.append(args)
                raise GcpError("timeout", message="request timed out")

        st = iap_access.status(fake_app(gcp=Down(), environ={"IAP_OAUTH_CLIENT": "managed"}), "proj")
        self.assertEqual((st["error"], st["oauth"], len(calls)), ("timeout", "managed", 1))
        iap_access.status(fake_app(gcp=Down()), "proj")
        self.assertEqual(len(calls), 1, "failures are cached too")


CONFIG = """workspace:
  project_id: demo-project
  audit_dataset: ge_analytics
  timezone: UTC
security:
  auth_mode: google
  admins: [owner@cymbal.example.com]
storage:
  backend: memory
"""
INITIAL = "Init-Pass-2026-x9"


class Response:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    def cookie(self, name):
        for key, value in self.headers:
            if key.lower() == "set-cookie" and value.startswith(name + "="):
                return value.split(";", 1)[0].split("=", 1)[1]
        return None


class AccessEndpointsTest(unittest.TestCase):
    """Bridge mode on a real application: approving accounts admits them at IAP and the administrator sees the
    outcome per account."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = os.path.join(cls.tmp.name, "config.yaml")
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write(CONFIG)
        cls.app = app_module.App(environ={
            "CONFIG_PATH": cfg, "SESSION_SECRET": "s" * 48, "SIGN_IN_BRIDGE_URL": BRIDGE,
            "ADMIN_INITIAL_PASSWORD": INITIAL, "IAP_ORG_DOMAIN": "cymbal.example.com"})
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
        iap_access._last.clear()  # pylint: disable=protected-access
        self.gcp = FakeIap(members=["domain:cymbal.example.com"], refused={"user:partner@outside.example.net"})
        self.app.gcp = self.gcp

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
        state = self.request("GET", "/api/v1/auth/state")
        csrf = state.json()["data"]["csrfToken"]
        res = self.request("POST", "/api/v1/auth/login", cookies={"cp_csrf": csrf}, headers={"X-CSRF-Token": csrf},
                           body={"username": "admin", "password": INITIAL})
        self.assertEqual(res.status, 200, res.body)
        return res.cookie("cp_session"), csrf

    def post(self, path, body):
        session, csrf = self.admin()
        res = self.request("POST", path, cookies={"cp_session": session, "cp_csrf": csrf},
                           headers={"X-CSRF-Token": csrf}, body=body)
        self.assertEqual(res.status, 200, res.body)
        return res.json()["data"]

    def test_sign_in_page_hint(self):
        hint = self.request("GET", "/api/v1/auth/state").json()["data"]["googleHint"]
        self.assertEqual(hint, {"iap": True, "orgDomain": "cymbal.example.com", "internalOnly": True})
        with mock.patch.dict(self.app.environ, {"IAP_OAUTH_CLIENT": "custom"}):
            hint = self.request("GET", "/api/v1/auth/state").json()["data"]["googleHint"]
        self.assertFalse(hint["internalOnly"])

    def test_approvals_are_admitted_at_iap_with_a_result_per_account(self):
        session, _ = self.admin()
        summary = self.request("GET", "/api/v1/access", cookies={"cp_session": session}).json()["data"]
        google = summary["google"]
        self.assertEqual((google["enabled"], google["resource"], google["oauth"], google["orgDomain"]),
                         (True, RES, "managed", "cymbal.example.com"))
        owner = next(u for u in summary["users"] if u["email"] == "owner@cymbal.example.com")
        self.assertEqual(owner["iap"], "covered")
        self.assertNotIn("iap", next(u for u in summary["users"] if u["email"] == "admin"))

        out = self.post("/api/v1/access/users", {"email": "viewer@cymbal.example.com", "role": "viewer"})
        self.assertEqual([r["result"] for r in out["iapResults"]], ["covered"])
        self.assertEqual(self.gcp.sets(), [])

        out = self.post("/api/v1/access/users", {"emails": ["partner@outside.example.net", "ally@partner.example.org"],
                                                 "role": "viewer"})
        results = {r["entry"]: r["result"] for r in out["iapResults"]}
        self.assertEqual(results, {"partner@outside.example.net": "rejected", "ally@partner.example.org": "added"})
        users = {u["email"]: u for u in out["users"]}
        self.assertEqual(users["ally@partner.example.org"]["iap"], "covered")
        self.assertEqual(users["partner@outside.example.net"]["iap"], "missing")
        self.assertEqual(users["partner@outside.example.net"]["iapLast"]["result"], "rejected")
        self.assertIn("user:ally@partner.example.org", self.gcp.admitted())

        out = self.post("/api/v1/access/iap", {"member": "partner.example.org"})
        self.assertEqual([(r["principal"], r["result"]) for r in out["iapResults"]],
                         [("domain:partner.example.org", "added")])
        self.assertIn("partner.example.org", out["google"]["domains"])

        self.post("/api/v1/access/users/delete", {"email": "ally@partner.example.org"})
        self.assertNotIn("user:ally@partner.example.org", self.gcp.admitted())
        self.assertIn("domain:partner.example.org", self.gcp.admitted())

        out = self.post("/api/v1/access/iap/sync", {})
        results = {r["entry"]: r["result"] for r in out["iapResults"]}
        self.assertEqual(results["partner@outside.example.net"], "rejected")
        self.assertEqual(results["owner@cymbal.example.com"], "covered")

    def test_bad_iap_member_is_refused(self):
        session, csrf = self.admin()
        res = self.request("POST", "/api/v1/access/iap", cookies={"cp_session": session, "cp_csrf": csrf},
                           headers={"X-CSRF-Token": csrf}, body={"member": "not a domain"})
        self.assertEqual(res.status, 400)


FAKE_GCLOUD = r'''#!/usr/bin/env python3
import json, os, sys
state_path = os.environ["FAKE_IAP_STATE"]
state = json.load(open(state_path))
args = [a for a in sys.argv[1:] if a != "--quiet"]
opts = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in args if a.startswith("--") and "=" in a}
def save():
    json.dump(state, open(state_path, "w"))
if args[:3] == ["iap", "web", "add-iam-policy-binding"]:
    member = opts["--member"]
    state["calls"].append(member)
    save()
    if member in state["refused"]:
        sys.stderr.write("ERROR: (gcloud.iap.web.add-iam-policy-binding) FAILED_PRECONDITION: One or more users "
                         "named in the policy do not belong to a permitted customer, perhaps due to an organization "
                         "policy.\n")
        sys.exit(1)
    if member not in state["members"]:
        state["members"].append(member)
        save()
    sys.exit(0)
if args[:3] == ["iap", "web", "get-iam-policy"]:
    print(json.dumps({"bindings": [{"role": "roles/iap.httpsResourceAccessor", "members": state["members"]}]}))
    sys.exit(0)
if args[:3] == ["iap", "settings", "get"]:
    print(json.dumps(state["settings"].get(opts.get("--service", "project"), {})))
    sys.exit(0)
if args[:2] == ["projects", "get-iam-policy"]:
    print(json.dumps({"bindings": [{"role": "roles/owner", "members": state["project_members"]}]}))
    sys.exit(0)
if args[:2] == ["auth", "print-access-token"]:
    sys.exit(1)
sys.exit(0)
'''


class ConfigureIapAccessScriptTest(unittest.TestCase):
    """scripts/lib/common.sh configure_iap_access against a fake gcloud."""

    def run_configure(self, mode, drs, refused=(), extra="", settings=None, admins="owner@cymbal.example.com",
                      viewers="", org_domain="cymbal.example.com"):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.json")
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump({"members": [], "calls": [], "refused": list(refused), "settings": settings or {},
                           "project_members": ["user:owner@cymbal.example.com", "user:carol@corp.example.org",
                                               "serviceAccount:svc@demo.iam.gserviceaccount.com"]}, fh)
            gcloud = os.path.join(tmp, "gcloud")
            with open(gcloud, "w", encoding="utf-8") as fh:
                fh.write(FAKE_GCLOUD)
            os.chmod(gcloud, 0o755)
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ.get("PATH", ""), FAKE_IAP_STATE=state_path)
            script = (
                "set -euo pipefail; source %s; DRY_RUN=0; ADMIN_EMAILS=%s; ALLOWED_USERS=%s; ORGPOL_DRS=%s; "
                "IAP_ORG_DOMAIN=%s; DISCOVERY_ENGINE_LOCATIONS=global; "
                "configure_iap_access demo 123456789012 asia-northeast3 svc %s %s; "
                "echo \"OAUTH=${IAP_OAUTH_CLIENT}\"; iap_access_report"
            ) % (os.path.join(ROOT, "scripts/lib/common.sh"), json.dumps(admins), json.dumps(viewers),
                 "1" if drs else "0", json.dumps(org_domain), mode, json.dumps(extra))
            res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, check=False)
            with open(state_path, encoding="utf-8") as fh:
                state = json.load(fh)
        self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        return res.stdout + res.stderr, state

    def test_bridge_without_domain_restricted_sharing_opens_to_the_organization(self):
        out, state = self.run_configure("bridge", drs=False, extra="allAuthenticatedUsers")
        self.assertEqual(state["calls"], ["allAuthenticatedUsers", "user:owner@cymbal.example.com"])
        self.assertIn("OAUTH=managed", out)
        self.assertIn("통과 허용   : allAuthenticatedUsers, user:owner@cymbal.example.com", out)
        self.assertIn("조직(cymbal.example.com) 계정만 Google 로그인 가능", out)

    def test_bridge_under_domain_restricted_sharing_binds_customer_domains_and_reports_refusals(self):
        out, state = self.run_configure("bridge", drs=True, admins="owner@cymbal.example.com,dev@vendor.example.net",
                                        refused={"user:dev@vendor.example.net", "domain:vendor.example.net"})
        self.assertNotIn("allAuthenticatedUsers", state["calls"])
        self.assertEqual(state["members"], ["user:owner@cymbal.example.com", "domain:cymbal.example.com",
                                            "domain:corp.example.org"])
        self.assertIn("refused by an organization policy", out)
        self.assertIn("조직 정책 거부: user:dev@vendor.example.net, domain:vendor.example.net", out)
        self.assertNotIn("gserviceaccount.com", " ".join(state["calls"]))

    def test_refusing_every_principal_says_nobody_can_sign_in(self):
        out, _ = self.run_configure("bridge", drs=True, org_domain="",
                                    refused={"user:owner@cymbal.example.com", "domain:cymbal.example.com",
                                             "domain:corp.example.org"})
        self.assertIn("아무 Google 계정도 IAP를 통과할 수 없습니다", out)
        self.assertIn("gcloud iap web add-iam-policy-binding --resource-type=cloud-run --service=svc", out)

    def test_gate_is_an_allowlist(self):
        out, state = self.run_configure("gate", drs=False, viewers="*@cymbal.example.com", extra="ops@corp.example.org")
        self.assertEqual(state["calls"], ["user:owner@cymbal.example.com", "domain:cymbal.example.com",
                                          "user:ops@corp.example.org"])
        self.assertIn("OAUTH=managed", out)

    def test_custom_oauth_client_is_detected(self):
        out, _ = self.run_configure("bridge", drs=False, settings={
            "svc": {"accessSettings": {"oauthSettings": {"clientId": "123-abc.apps.googleusercontent.com"}}}})
        self.assertIn("OAUTH=custom", out)
        self.assertIn("맞춤 OAuth 클라이언트", out)


if __name__ == "__main__":
    unittest.main()
