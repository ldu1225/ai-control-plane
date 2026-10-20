"""Authentication modes, roles, CSRF double-submit, security headers and rate limiting."""

import base64
import json
import unittest

import support

from controlplane import httpkit, security
from controlplane.httpkit import ApiError


class FakeVerifier:
    def __init__(self, email=None, error=None):
        self.email = email
        self.error = error
        self.calls = []

    def verify(self, token, audience):
        self.calls.append((token, audience))
        if self.error:
            raise self.error
        return self.email


def settings_with(**security_values):
    return support.make_settings(security=security_values)


class AuthModeTest(unittest.TestCase):
    def test_auto_resolution(self):
        self.assertEqual(security.resolve_auth_mode("auto", {"K_SERVICE": "control-plane"}), "iap")
        self.assertEqual(security.resolve_auth_mode("auto", {}), "local")
        self.assertEqual(security.resolve_auth_mode(None, {}), "local")
        self.assertEqual(security.resolve_auth_mode("public_readonly", {"K_SERVICE": "x"}), "public_readonly")
        with self.assertRaises(ValueError):
            security.resolve_auth_mode("disabled", {})

    def test_local_mode_admin_only_on_loopback(self):
        s = settings_with(auth_mode="local")
        for peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            ident = security.authenticate({}, peer, s, FakeVerifier())
            self.assertEqual((ident.email, ident.role, ident.can_write), ("local-admin@localhost", "admin", True))
        ident = security.authenticate({}, "10.1.2.3", s, FakeVerifier())
        self.assertEqual(ident.role, "viewer")
        self.assertFalse(ident.can_write)

    def test_public_readonly_never_writes(self):
        ident = security.authenticate({}, "127.0.0.1", settings_with(auth_mode="public_readonly"), FakeVerifier())
        self.assertEqual((ident.email, ident.role, ident.can_write), ("anonymous", "viewer", False))

    def test_iap_roles(self):
        s = settings_with(auth_mode="iap", admins=["boss@example.com", "*@admins.example.com"], iap_audience="/aud")
        headers = {"x-goog-iap-jwt-assertion": "a.b.c"}
        verifier = FakeVerifier("boss@example.com")
        ident = security.authenticate(headers, "10.0.0.1", s, verifier)
        self.assertEqual((ident.role, ident.can_write), ("admin", True))
        self.assertEqual(verifier.calls, [("a.b.c", "/aud")])
        self.assertEqual(security.authenticate(headers, "10.0.0.1", s,
                                               FakeVerifier("ops@admins.example.com")).role, "admin")
        self.assertEqual(security.authenticate(headers, "10.0.0.1", s,
                                               FakeVerifier("user@example.com")).role, "viewer")
        self.assertEqual(security.authenticate(headers, "10.0.0.1", s,
                                               FakeVerifier("x@evil-admins.example.com")).role, "viewer")

    def test_iap_failure_is_401(self):
        s = settings_with(auth_mode="iap")
        with self.assertLogs("controlplane.security", level="WARNING"):
            with self.assertRaises(ApiError) as ctx:
                security.authenticate({}, "10.0.0.1", s, FakeVerifier(error=ValueError("bad signature")))
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("unauthenticated", 401))

    def test_iap_verifier_rejects_without_network(self):
        verifier = security.IapVerifier()
        for token in ("", "abc", "a.b", "x" * 9000):
            with self.assertRaises(ValueError):
                verifier.verify(token, "")
        header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).decode().rstrip("=")
        with self.assertRaises(ValueError):
            verifier.verify("%s.e30.sig" % header, "")

    def test_admin_matching(self):
        admins = ["boss@example.com", "*@corp.example.com"]
        self.assertTrue(security.is_admin_email("BOSS@example.com", admins))
        self.assertTrue(security.is_admin_email("a@corp.example.com", admins))
        self.assertFalse(security.is_admin_email("a@sub.corp.example.com", admins))
        self.assertFalse(security.is_admin_email("", admins))


class CsrfTest(unittest.TestCase):
    TOKEN = "A" * 43

    def test_issue_cookie_http(self):
        token, cookie = httpkit.issue_csrf_token({}, https=False)
        self.assertRegex(token, r"^[A-Za-z0-9_\-]{43}$")
        self.assertTrue(cookie.startswith("cp_csrf=%s;" % token))
        for attr in ("Path=/", "HttpOnly", "SameSite=Strict"):
            self.assertIn(attr, cookie)
        self.assertNotIn("Secure", cookie)

    def test_issue_cookie_https_and_reuse(self):
        token, cookie = httpkit.issue_csrf_token({"Cookie": "__Host-cp_csrf=%s" % self.TOKEN}, https=True)
        self.assertEqual(token, self.TOKEN)
        self.assertTrue(cookie.startswith("__Host-cp_csrf="))
        self.assertIn("Secure", cookie)
        fresh, _ = httpkit.issue_csrf_token({"Cookie": "__Host-cp_csrf=short"}, https=True)
        self.assertNotEqual(fresh, "short")

    def headers(self, **overrides):
        h = {"Content-Type": "application/json", "X-CSRF-Token": self.TOKEN, "Cookie": "cp_csrf=%s" % self.TOKEN}
        h.update(overrides)
        return h

    def test_valid_double_submit(self):
        self.assertTrue(httpkit.csrf_valid(self.headers(), https=False))
        self.assertTrue(httpkit.csrf_valid(self.headers(**{"Content-Type": "application/json; charset=utf-8"}),
                                           https=False))

    def test_rejections(self):
        self.assertFalse(httpkit.csrf_valid(self.headers(**{"X-CSRF-Token": ""}), https=False))
        self.assertFalse(httpkit.csrf_valid(self.headers(**{"X-CSRF-Token": "B" * 43}), https=False))
        self.assertFalse(httpkit.csrf_valid(self.headers(Cookie=""), https=False))
        self.assertFalse(httpkit.csrf_valid(self.headers(**{"Content-Type": "text/plain"}), https=False))
        self.assertFalse(httpkit.csrf_valid(self.headers(**{"Sec-Fetch-Site": "cross-site"}), https=False))
        # Over https only the __Host- cookie counts.
        self.assertFalse(httpkit.csrf_valid(self.headers(), https=True))
        short = "abc"
        self.assertFalse(httpkit.csrf_valid(self.headers(**{"X-CSRF-Token": short, "Cookie": "cp_csrf=" + short}),
                                            https=False))


class HeadersTest(unittest.TestCase):
    def test_security_headers(self):
        headers = dict(httpkit.security_headers(https=False, nonce="n0nce"))
        self.assertIn("script-src 'self' 'nonce-n0nce'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("object-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Referrer-Policy"], "strict-origin-when-cross-origin")
        self.assertEqual(headers["Cross-Origin-Opener-Policy"], "same-origin")
        self.assertNotIn("Strict-Transport-Security", headers)
        https_headers = dict(httpkit.security_headers(https=True))
        self.assertEqual(https_headers["Strict-Transport-Security"], "max-age=31536000; includeSubDomains")
        self.assertNotIn("nonce-", https_headers["Content-Security-Policy"])

    def test_https_detection(self):
        self.assertTrue(httpkit.request_is_https({"X-Forwarded-Proto": "https"}))
        self.assertTrue(httpkit.request_is_https({"X-Forwarded-Proto": "https, http"}))
        self.assertFalse(httpkit.request_is_https({}))


class RateLimitTest(unittest.TestCase):
    def test_bucket_exhaustion_and_isolation(self):
        limiter = security.RateLimiter({"ai": 3, "general": 240})
        for _ in range(3):
            self.assertTrue(limiter.allow("user-a", "ai")[0])
        allowed, retry = limiter.allow("user-a", "ai")
        self.assertFalse(allowed)
        self.assertGreaterEqual(retry, 1)
        self.assertTrue(limiter.allow("user-b", "ai")[0])
        self.assertTrue(limiter.allow("user-a", "general")[0])
        with self.assertRaises(ApiError) as ctx:
            limiter.check("user-a", "ai")
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("rate_limited", 429))
        self.assertIn("Retry-After", dict(ctx.exception.headers))

    def test_rate_limits(self):
        self.assertEqual(security.RATE_LIMITS["general"], 240)
        self.assertEqual(security.RATE_LIMITS["ai"], 20)
        self.assertEqual(security.RATE_LIMITS["governance"], 10)


class QueryParsingTest(unittest.TestCase):
    def test_body_and_query_limits(self):
        self.assertEqual(httpkit.parse_json_object(b'{"a": 1}'), {"a": 1})
        for raw in (b"[1, 2]", b"{bad", b"\xff\xfe"):
            with self.assertRaises(ApiError):
                httpkit.parse_json_object(raw)
        self.assertEqual(httpkit.parse_query("start=2026-10-01&start=2026-10-02"), {"start": "2026-10-01"})
        with self.assertRaises(ApiError):
            httpkit.parse_query("x=" + "a" * 600)


if __name__ == "__main__":
    unittest.main()
