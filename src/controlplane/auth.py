"""Google sign-in for auth_mode "google": the application's own sign-in page, the OpenID Connect
authorization code flow with PKCE, and signed session cookies.

Flow
1. GET /login renders the sign-in page. "Sign in with Google" navigates to /auth/google/start.
2. /auth/google/start stores state, nonce and the PKCE verifier in a short-lived signed cookie and
   redirects to Google (account chooser).
3. Google redirects to /auth/google/callback. The state is checked, the code is exchanged at the
   token endpoint (with the client secret and PKCE verifier), and the ID token is verified
   (signature, issuer, audience, expiry, nonce, verified email).
4. Only approved accounts (security.admins and security.allowed_users: exact emails or *@domain
   patterns) receive a session. Other accounts are recorded as access requests for administrators.

Sessions are stateless HMAC-SHA256 signed cookies (fixed algorithm, expiry, HttpOnly, Secure on
HTTPS, SameSite=Lax, __Host- prefix on HTTPS). Every request re-checks that the account is still
approved, so removing an account revokes its access immediately. Signing out records a per-account
revocation epoch in the settings store, which invalidates every session of that account.

Identity providers
- OAuth client (security.google_client_id + GOOGLE_OAUTH_CLIENT_SECRET): Google's authorization
  code flow described above.
- Sign-in bridge (security.sign_in_bridge_url): no OAuth client is needed. A second Cloud Run service
  (the same image with auth_mode iap and security.bridge_return_url) sits behind Identity-Aware
  Proxy. IAP signs the user in with Google; the bridge returns a short-lived assertion (HMAC with
  the shared SESSION_SECRET, 2 minutes) bound to this browser's sign-in state to the callback.

Secrets come from the environment (Secret Manager on Cloud Run): GOOGLE_OAUTH_CLIENT_SECRET and
SESSION_SECRET. They are never part of the settings, the API or the logs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import urllib.parse

from . import config
from .httpkit import ApiError, now_iso, read_cookie, request_is_https
from .security import is_admin_email, uid_for

LOG = logging.getLogger("controlplane.auth")

PBKDF2_ITERATIONS = 200_000


def hash_password(password):
    """PBKDF2-HMAC-SHA256 password hash with a 16-byte random salt."""
    if not isinstance(password, str) or len(password) < 4 or len(password) > 256:
        raise ApiError("bad_request", "Password must be between 4 and 256 characters.")
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS).hex()
    return "pbkdf2_sha256$%s$%s" % (salt, dk)


def verify_password(password, stored_hash):
    """Constant-time verification of a PBKDF2-HMAC-SHA256 password hash."""
    if not isinstance(password, str) or not isinstance(stored_hash, str):
        return False
    parts = stored_hash.split("$")
    if len(parts) != 3 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        salt_bytes = bytes.fromhex(parts[1])
        expected = parts[2]
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, PBKDF2_ITERATIONS).hex()
    return hmac.compare_digest(dk.encode("ascii"), expected.encode("ascii"))


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
SCOPES = "openid email profile"
CALLBACK_PATH = "/auth/google/callback"
BRIDGE_PATH = "/auth/bridge"
BRIDGE_TTL = 120

SESSION_COOKIE = "cp_session"
TRANSACTION_COOKIE = "cp_oauth"
NOTICE_COOKIE = "cp_notice"
TRANSACTION_TTL = 600
NOTICE_TTL = 300
MIN_SECRET_LENGTH = 32
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{8,3000}\.[A-Za-z0-9_\-]{43}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}(:[0-9]{1,5})?$")
_STATE_RE = re.compile(r"^[A-Za-z0-9_\-]{20,128}$")


def _b64e(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Signer:
    """Signed, expiring tokens: base64url(JSON claims) "." base64url(HMAC-SHA256).

    The algorithm is fixed (no algorithm header to negotiate) and every purpose (session, OAuth
    transaction, notice) uses its own derived key, so tokens cannot be replayed across purposes."""

    def __init__(self, secret, purpose):
        self._key = hmac.new(secret, b"control-plane/" + purpose.encode("ascii"), hashlib.sha256).digest()

    def _mac(self, payload):
        return _b64e(hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).digest())

    def sign(self, claims, ttl, now=None):
        now = int(now if now is not None else time.time())
        body = dict(claims)
        body["iat"] = now
        body["exp"] = now + int(ttl)
        payload = _b64e(json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        return "%s.%s" % (payload, self._mac(payload))

    def verify(self, token, now=None):
        if not isinstance(token, str) or not _TOKEN_RE.match(token):
            return None
        payload, mac = token.split(".")
        if not hmac.compare_digest(mac.encode("ascii"), self._mac(payload).encode("ascii")):
            return None
        try:
            claims = json.loads(_b64d(payload).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(claims, dict):
            return None
        exp = claims.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, int):
            return None
        if exp <= int(now if now is not None else time.time()):
            return None
        return claims


def load_session_secret(environ):
    """SESSION_SECRET (at least 32 characters) is required on Cloud Run. Locally an ephemeral random
    key is generated with a warning (sessions reset on restart and are not shared across instances)."""
    raw = (environ.get("SESSION_SECRET") or "").strip()
    if raw:
        if len(raw) < MIN_SECRET_LENGTH:
            raise ValueError("SESSION_SECRET must be at least %d characters" % MIN_SECRET_LENGTH)
        return raw.encode("utf-8")
    if environ.get("K_SERVICE"):
        raise ValueError("SESSION_SECRET is required for auth_mode=google on Cloud Run (store it in Secret "
                         "Manager and map it to the SESSION_SECRET environment variable)")
    LOG.warning("SESSION_SECRET is not set: using an ephemeral random signing key; sessions end when the "
                "process restarts and are not shared across instances")
    return secrets.token_bytes(32)


def safe_next(value):
    """Post-sign-in destination: a same-origin absolute path only (no scheme, host or backslash)."""
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//") or len(value) > 512:
        return "/"
    if "\\" in value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return "/"
    for prefix in ("/auth/", "/login", "/logout", "/api/"):
        if value.startswith(prefix):
            return "/"
    return value


def is_approved(email, settings):
    sec = settings.get("security", {})
    return is_admin_email(email, sec.get("admins")) or is_admin_email(email, sec.get("allowed_users"))


class GoogleSignIn:
    """Google OpenID Connect sign-in, built-in ID/Password sign-in, and session handling."""

    def __init__(self, base_settings, environ, settings_manager=None):
        sec = base_settings["security"]
        self.environ = environ
        self.bridge_url = (sec.get("sign_in_bridge_url") or "").rstrip("/")
        self.client_id = sec.get("google_client_id") or ""
        self.client_secret = (environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip()
        self.oauth_redirect_uri = (environ.get("GOOGLE_OAUTH_REDIRECT_URI") or "").strip()
        self.oauth_start_url = (environ.get("GOOGLE_OAUTH_START_URL") or "").strip()
        if not self.bridge_url:
            if not self.client_id:
                raise ValueError("auth_mode=google requires security.google_client_id (GOOGLE_OAUTH_CLIENT_ID) "
                                 "or security.sign_in_bridge_url (SIGN_IN_BRIDGE_URL)")
            if not self.client_secret:
                raise ValueError("auth_mode=google requires the GOOGLE_OAUTH_CLIENT_SECRET environment variable")
        self.public_base_url = (sec.get("public_base_url") or "").rstrip("/")
        if environ.get("K_SERVICE") and not self.public_base_url:
            LOG.warning("PUBLIC_BASE_URL is not set: the OAuth redirect URI is derived from the request host")
        secret = load_session_secret(environ)
        self.sessions = Signer(secret, "session")
        self.transactions = Signer(secret, "oauth-transaction")
        self.notices = Signer(secret, "notice")
        self.bridge = Signer(secret, "sign-in-bridge")
        self.settings = settings_manager
        self._verify_request = None

    # -- helpers ------------------------------------------------------------------
    def canonical_host(self):
        """Host of PUBLIC_BASE_URL (lower case) or None. Pages requested on another host (for example
        the second run.app URL of the same service) are redirected there, so the sign-in cookies and
        Google's callback always use the same host."""
        if not self.public_base_url:
            return None
        return (urllib.parse.urlsplit(self.public_base_url).netloc or "").lower() or None

    def callback_uri(self, headers):
        if self.public_base_url:
            return self.public_base_url + CALLBACK_PATH
        host = (headers.get("Host") or "").strip()
        if not _HOST_RE.match(host):
            raise ApiError("bad_request", "The request host is invalid.")
        scheme = "https" if request_is_https(headers) else "http"
        return "%s://%s%s" % (scheme, host, CALLBACK_PATH)

    def redirect_uri(self, headers):
        if self.oauth_redirect_uri:
            return self.oauth_redirect_uri
        return self.callback_uri(headers)

    @staticmethod
    def _cookie(base, value, https, max_age, same_site="Lax"):
        name = ("__Host-" + base) if https else base
        attrs = ["%s=%s" % (name, value), "Path=/", "Max-Age=%d" % max_age, "HttpOnly", "SameSite=%s" % same_site]
        if https:
            attrs.append("Secure")
        return "; ".join(attrs)

    def _clear(self, base, https):
        return self._cookie(base, "", https, 0)

    @staticmethod
    def _read(headers, base):
        name = ("__Host-" + base) if request_is_https(headers) else base
        return read_cookie(headers, name)

    def session_hours(self, settings):
        try:
            return max(1, min(168, int(settings["security"].get("session_hours") or 12)))
        except (TypeError, ValueError):
            return 12

    # -- sessions -----------------------------------------------------------------
    def _accounts(self):
        if self.settings is None:
            return {}
        return (self.settings.auth_state() or {}).get("accounts") or {}

    def default_admin_password_active(self):
        accounts = self._accounts()
        admin_meta = accounts.get("admin") or {}
        return not bool(admin_meta.get("passwordHash"))

    def password_login(self, username, password, next_path, headers, settings):
        """Authenticates an ID/Email + Password login and returns (user_dict, redirect_path, cookies)."""
        username = str(username or "").strip().lower()
        password = str(password or "")
        if not username or not password or len(username) > 254 or len(password) > 256:
            raise ApiError("unauthenticated", "Invalid account ID or password.")
        accounts = self._accounts()
        meta = accounts.get(username)
        sec = settings.get("security") or {}
        verified = False
        role = "viewer"
        name = username
        if meta and meta.get("passwordHash"):
            verified = verify_password(password, meta["passwordHash"])
            role = "admin" if (username == "admin" or meta.get("role") == "admin" or
                               is_admin_email(username, sec.get("admins"))) else "viewer"
            name = meta.get("name") or username
        elif username == "admin":
            default_pw = (self.environ.get("ADMIN_INITIAL_PASSWORD") or "admin")
            verified = hmac.compare_digest(password.encode("utf-8"), default_pw.encode("utf-8"))
            role = "admin"
            name = "Administrator"
        if not verified:
            LOG.info("password sign-in rejected uid=%s", uid_for(username))
            raise ApiError("unauthenticated", "Invalid account ID or password.")
        https = request_is_https(headers)
        hours = self.session_hours(settings)
        claims = {"email": username, "name": name, "picture": "", "sub": "local:" + username,
                  "pw": True, "role": role, "ims": int(time.time() * 1000)}
        session = self.sessions.sign(claims, hours * 3600)
        cookies = [self._cookie(SESSION_COOKIE, session, https, hours * 3600),
                   self._clear(NOTICE_COOKIE, https)]
        self._record_login(username)
        LOG.info("password sign-in ok uid=%s role=%s", uid_for(username), role)
        return ({"email": username, "name": name, "picture": "", "role": role},
                safe_next(next_path or "/"), cookies)

    def user_from_headers(self, headers, settings):
        """Returns the session claims of an approved, unrevoked account or None."""
        claims = self.sessions.verify(self._read(headers, SESSION_COOKIE))
        if not claims:
            return None
        email = claims.get("email")
        issued_ms = claims.get("ims")
        if not isinstance(email, str) or not isinstance(issued_ms, int):
            return None
        if self.settings is not None and issued_ms <= self.settings.revoked_at(email):
            return None
        if claims.get("pw") is True:
            accounts = self._accounts()
            meta = accounts.get(email)
            sec = settings.get("security") or {}
            if email == "admin":
                claims = dict(claims)
                claims["role"] = "admin"
                return claims
            if meta and meta.get("passwordHash"):
                claims = dict(claims)
                claims["role"] = "admin" if (meta.get("role") == "admin" or
                                             is_admin_email(email, sec.get("admins"))) else "viewer"
                return claims
            if is_approved(email, settings):
                claims = dict(claims)
                claims["role"] = "admin" if is_admin_email(email, sec.get("admins")) else "viewer"
                return claims
            return None
        if not is_approved(email, settings):
            return None
        return claims

    def denied_email(self, headers):
        claims = self.notices.verify(self._read(headers, NOTICE_COOKIE))
        email = claims.get("email") if claims else None
        return email if isinstance(email, str) else None

    # -- flow ---------------------------------------------------------------------
    def start(self, headers, query):
        """Returns (location, cookies) for the redirect to Google."""
        params = urllib.parse.parse_qs(query or "", max_num_fields=8)
        next_path = safe_next((params.get("next") or ["/"])[0])
        https = request_is_https(headers)
        state = secrets.token_urlsafe(32)
        if self.bridge_url and not (self.client_id and self.client_secret):
            txn = self.transactions.sign({"s": state, "next": next_path, "m": "bridge"}, TRANSACTION_TTL)
            cookies = [self._cookie(TRANSACTION_COOKIE, txn, https, TRANSACTION_TTL), self._clear(NOTICE_COOKIE, https)]
            return "%s%s?%s" % (self.bridge_url, BRIDGE_PATH, urllib.parse.urlencode({"state": state})), cookies
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = _b64e(hashlib.sha256(verifier.encode("ascii")).digest())
        txn = self.transactions.sign({"s": state, "n": nonce, "v": verifier, "next": next_path}, TRANSACTION_TTL)
        oauth_params = {
            "client_id": self.client_id, "redirect_uri": self.redirect_uri(headers), "response_type": "code",
            "scope": SCOPES, "state": state, "nonce": nonce, "code_challenge": challenge,
            "code_challenge_method": "S256", "prompt": "select_account", "access_type": "online",
            "include_granted_scopes": "false"}
        if self.oauth_start_url:
            oauth_params["return_to"] = self.callback_uri(headers)
        query_string = urllib.parse.urlencode(oauth_params)
        cookies = [self._cookie(TRANSACTION_COOKIE, txn, https, TRANSACTION_TTL), self._clear(NOTICE_COOKIE, https)]
        return "%s?%s" % (self.oauth_start_url or GOOGLE_AUTH_URL, query_string), cookies

    def callback(self, headers, query, settings):
        """Completes the sign-in. Returns (location, cookies). Never raises for user-caused errors."""
        https = request_is_https(headers)
        cookies = [self._clear(TRANSACTION_COOKIE, https)]
        params = urllib.parse.parse_qs(query or "", max_num_fields=16)
        get = lambda key: (params.get(key) or [""])[0]  # noqa: E731
        txn = self.transactions.verify(self._read(headers, TRANSACTION_COOKIE))
        if get("error"):
            return "/login?error=cancelled", cookies
        state = get("state")
        if not txn or not state or not hmac.compare_digest(state.encode("utf-8", "replace"),
                                                         str(txn.get("s", "")).encode("utf-8", "replace")):
            return "/login?error=expired", cookies
        if txn.get("m") == "bridge":
            claims = self._verify_bridge(get("assertion"), state) if self.bridge_url else None
            if claims is None:
                return "/login?error=failed", cookies
        else:
            code = get("code")
            if not code or len(code) > 2048:
                return "/login?error=failed", cookies
            try:
                claims = self._exchange_and_verify(code, txn, headers)
            except ApiError:
                return "/login?error=failed", cookies
        email = claims["email"]
        name = str(claims.get("name") or "")[:120]
        picture = claims.get("picture") if isinstance(claims.get("picture"), str) else ""
        if not picture.startswith("https://") or len(picture) > 500:
            picture = ""
        if not is_approved(email, settings):
            LOG.info("sign-in denied uid=%s reason=not_approved", uid_for(email))
            self._record_request(email, name)
            cookies.append(self._cookie(NOTICE_COOKIE, self.notices.sign({"email": email}, NOTICE_TTL), https,
                                        NOTICE_TTL))
            cookies.append(self._clear(SESSION_COOKIE, https))
            return "/login?error=not_approved", cookies
        hours = self.session_hours(settings)
        session = self.sessions.sign({"email": email, "name": name, "picture": picture, "sub": claims.get("sub"),
                                      "ims": int(time.time() * 1000)}, hours * 3600)
        cookies.append(self._cookie(SESSION_COOKIE, session, https, hours * 3600))
        cookies.append(self._clear(NOTICE_COOKIE, https))
        self._record_login(email)
        LOG.info("sign-in ok uid=%s", uid_for(email))
        return safe_next(txn.get("next")), cookies

    def logout(self, identity, headers):
        """Revokes every session of the account and clears the cookie. Returns the cookies to set."""
        https = request_is_https(headers)
        if self.settings is not None and identity.email and identity.email != "anonymous":
            epoch = int(time.time() * 1000)

            def apply(auth):
                auth["revoked"][identity.email] = epoch
            try:
                self.settings.mutate_auth(apply, identity.email)
            except ApiError as err:
                # Fail closed for this browser (cookie cleared) and report the problem.
                LOG.error("session revocation could not be persisted code=%s", err.code)
                raise ApiError("upstream_error", "Signed out on this browser, but other sessions could not be "
                                                 "revoked. Try again.", headers=[("Set-Cookie", self._clear(
                                                     SESSION_COOKIE, https))])
        LOG.info("sign-out uid=%s", uid_for(identity.email))
        return [self._clear(SESSION_COOKIE, https), self._clear(NOTICE_COOKIE, https)]

    # -- Google endpoints ----------------------------------------------------------
    def _exchange_and_verify(self, code, txn, headers):
        import requests
        data = {"code": code, "client_id": self.client_id, "client_secret": self.client_secret,
                "redirect_uri": self.redirect_uri(headers), "grant_type": "authorization_code",
                "code_verifier": txn.get("v", "")}
        try:
            resp = requests.post(GOOGLE_TOKEN_URL, data=data, timeout=10, allow_redirects=False)
        except requests.RequestException as err:
            LOG.warning("token exchange failed reason=%s", type(err).__name__)
            raise ApiError("upstream_error", "Google sign-in is unavailable.")
        if resp.status_code != 200:
            # The response body may echo request details; log the status only.
            LOG.warning("token exchange rejected status=%s", resp.status_code)
            raise ApiError("unauthenticated", "Google sign-in failed.")
        try:
            id_token = resp.json().get("id_token")
        except ValueError:
            id_token = None
        if not isinstance(id_token, str) or not id_token:
            raise ApiError("unauthenticated", "Google sign-in failed.")
        claims = self.verify_id_token(id_token)
        if not hmac.compare_digest(str(claims.get("nonce", "")).encode("utf-8", "replace"),
                                   str(txn.get("n", "")).encode("utf-8", "replace")):
            LOG.warning("id token rejected reason=nonce_mismatch")
            raise ApiError("unauthenticated", "Google sign-in failed.")
        return claims

    def _verify_bridge(self, assertion, state):
        """Assertion from the IAP sign-in bridge: valid signature and expiry, bound to this browser's
        sign-in state, verified email format. Returns claims or None."""
        claims = self.bridge.verify(assertion)
        if not claims or not hmac.compare_digest(str(claims.get("st", "")).encode("utf-8", "replace"),
                                                 state.encode("utf-8", "replace")):
            LOG.warning("bridge assertion rejected reason=invalid")
            return None
        email = str(claims.get("email") or "").strip().lower()
        if not config.EMAIL_RE.match(email):
            LOG.warning("bridge assertion rejected reason=email")
            return None
        return {"email": email, "sub": str(claims.get("sub") or ""), "name": "", "picture": ""}

    def switch_account_url(self):
        """Bridge mode: clears the IAP session on the bridge so the next sign-in asks for an account."""
        if self.bridge_url and not (self.client_id and self.client_secret):
            return self.bridge_url + "/_gcp_iap/clear_login_cookie"
        return None

    def verify_id_token(self, id_token):
        """Verifies signature (Google certificates), audience, issuer and expiry; requires a
        verified email. Returns the claims with a normalized lower-case email."""
        from google.oauth2 import id_token as google_id_token
        if self._verify_request is None:
            from google.auth.transport import requests as ga_requests
            self._verify_request = ga_requests.Request()
        try:
            claims = google_id_token.verify_oauth2_token(id_token, self._verify_request, audience=self.client_id,
                                                         clock_skew_in_seconds=10)
        except Exception as err:  # pylint: disable=broad-except
            LOG.warning("id token rejected reason=%s", type(err).__name__)
            raise ApiError("unauthenticated", "Google sign-in failed.")
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise ApiError("unauthenticated", "Google sign-in failed.")
        email = str(claims.get("email") or "").strip().lower()
        if claims.get("email_verified") is not True or not config.EMAIL_RE.match(email):
            LOG.warning("id token rejected reason=email_not_verified")
            raise ApiError("unauthenticated", "Google sign-in failed.")
        claims = dict(claims)
        claims["email"] = email
        return claims

    # -- bookkeeping (best effort: never blocks a sign-in) -----------------------------
    def _record_login(self, email):
        if self.settings is None:
            return
        when = now_iso()

        def apply(auth):
            auth["lastLogin"].pop(email, None)
            auth["lastLogin"][email] = when
            auth["requests"] = [r for r in auth["requests"] if r.get("email") != email]
        try:
            self.settings.mutate_auth(apply, "sign-in")
        except Exception as err:  # pylint: disable=broad-except
            LOG.info("sign-in bookkeeping skipped reason=%s", type(err).__name__)

    def _record_request(self, email, name):
        if self.settings is None:
            return
        when = now_iso()

        def apply(auth):
            auth["requests"] = [r for r in auth["requests"] if r.get("email") != email]
            auth["requests"].append({"email": email, "name": name, "requestedAt": when})
        try:
            self.settings.mutate_auth(apply, "access-request")
        except Exception as err:  # pylint: disable=broad-except
            LOG.info("access request not recorded reason=%s", type(err).__name__)


class SignInBridge:
    """Runs on the IAP-protected sign-in bridge service (auth_mode iap + security.bridge_return_url).
    IAP has already signed the Google account in; /auth/bridge returns a short-lived assertion bound
    to the dashboard's sign-in state. The return target comes from configuration only (never from the
    request), so the bridge cannot be used as an open redirect."""

    def __init__(self, base_settings, environ):
        self.return_url = (base_settings["security"].get("bridge_return_url") or "").rstrip("/")
        self.signer = Signer(load_session_secret(environ), "sign-in-bridge")

    def redirect_for(self, email, query):
        params = urllib.parse.parse_qs(query or "", max_num_fields=8)
        state = (params.get("state") or [""])[0]
        if not _STATE_RE.match(state):
            raise ApiError("bad_request", "The sign-in request is invalid. Start again from the sign-in page.")
        assertion = self.signer.sign({"email": email, "st": state}, BRIDGE_TTL)
        LOG.info("bridge sign-in uid=%s", uid_for(email))
        return "%s%s?%s" % (self.return_url, CALLBACK_PATH,
                            urllib.parse.urlencode({"state": state, "assertion": assertion}))
