"""Authentication, authorization, rate limiting and PII minimization."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass

from .httpkit import ApiError

LOG = logging.getLogger("controlplane.security")

IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"
IAP_HEADER = "x-goog-iap-jwt-assertion"
# Google-signed OIDC ID tokens (Cloud Scheduler HTTP targets with an OIDC token).
GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
CACHED_CERT_URLS = (IAP_CERTS_URL, GOOGLE_CERTS_URL)
UID_SALT = "cp-uid-v1"
LOCAL_IDENTITY = "local-admin@localhost"

AUTH_MODES = ("auto", "iap", "google", "local")
# A request that carries one of these headers came through a proxy, so its peer address is not the client.
LOCAL_ONLY_MESSAGE = ("Local development mode accepts only connections from this computer. Use Google sign-in "
                      "or IAP for shared access.")
FORWARDING_HEADERS = ("X-Forwarded-For", "Forwarded", "X-Real-IP")
# Guest access (security.guest_access, google mode only): a visitor without a session browses as a read-only
# guest. Meant for public demos; customer deployments leave it off.
GUEST_MODE = "guest"
GUEST_READ_ONLY_MESSAGE = "Guests have read-only access. Sign in with an approved account to make changes."


def resolve_auth_mode(configured, environ=None):
    """auto resolves to iap on Cloud Run and to local elsewhere. local is a development mode for one
    computer (the loopback caller is the administrator); it is refused on Cloud Run, where only google
    and iap are valid. Raises ValueError, which aborts startup."""
    environ = os.environ if environ is None else environ
    mode = (configured or "auto").strip().lower()
    if mode not in AUTH_MODES:
        raise ValueError("invalid auth mode")
    on_cloud_run = bool(environ.get("K_SERVICE"))
    if mode == "auto":
        return "iap" if on_cloud_run else "local"
    if mode == "local" and on_cloud_run:
        raise ValueError("auth_mode=local is a development mode for one computer and is refused on Cloud Run; "
                         "use google or iap")
    return mode


def is_loopback_host(host):
    """True for a bind address that only this computer can reach (127.0.0.1, ::1, localhost)."""
    host = (host or "").strip().strip("[]").lower()
    return host == "localhost" or is_loopback(host)


def is_loopback(peer):
    try:
        addr = ipaddress.ip_address((peer or "").split("%")[0])
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return addr.is_loopback


def is_admin_email(email, admins):
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return False
    domain = email.rpartition("@")[2]
    for entry in admins or []:
        entry = str(entry).strip().lower()
        if entry in ("*", "*@*"):
            return True
        if entry.startswith("*@"):
            if domain == entry[2:]:
                return True
        elif entry == email:
            return True
    return False


@dataclass(frozen=True)
class Identity:
    email: str
    role: str
    auth_mode: str
    rate_key: str
    name: str = ""
    picture: str = ""

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def can_write(self):
        return self.role == "admin"


def admin_guard(identity):
    """Settings write check: refuses changes that would remove the caller's own administrator role.
    google and iap derive roles from security.admins (exact emails and *@domain patterns); local mode
    derives the role from the loopback peer and is not affected. Non-email ID accounts (like built-in
    'admin') derive their role from the internal account store and are not affected by security.admins."""
    def check(effective):
        if identity.auth_mode in ("google", "iap") and "@" in (identity.email or "") and \
                not is_admin_email(identity.email, (effective.get("security") or {}).get("admins")):
            raise ApiError("conflict", "This change would remove your own administrator access.")
    return check


# ---------------------------------------------------------------------------
# IAP JWT verification
# ---------------------------------------------------------------------------

class _CachingRequest:
    """google-auth transport request that caches the IAP and Google public key sets for one hour."""

    def __init__(self, ttl_seconds=3600):
        from google.auth.transport import requests as ga_requests  # lazy import
        self._inner = ga_requests.Request()
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._cached = {}

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        if method == "GET" and url in CACHED_CERT_URLS:
            with self._lock:
                hit = self._cached.get(url)
                if hit and hit[0] > time.monotonic():
                    return hit[1]
            resp = self._inner(url, method=method, body=body, headers=headers, timeout=timeout or 10,
                               **kwargs)
            if getattr(resp, "status", None) == 200:
                _ = resp.data  # materialize the body so the cached response can be re-read
                with self._lock:
                    self._cached[url] = (time.monotonic() + self._ttl, resp)
            return resp
        return self._inner(url, method=method, body=body, headers=headers, timeout=timeout, **kwargs)


def _unverified_header(token):
    try:
        segment = token.split(".")[0]
        segment += "=" * (-len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(segment.encode("ascii")))
    except Exception:  # pylint: disable=broad-except
        return {}


class IapVerifier:
    def __init__(self):
        self._request = None
        self._lock = threading.Lock()

    def _get_request(self):
        with self._lock:
            if self._request is None:
                self._request = _CachingRequest()
            return self._request

    def verify(self, token, audience):
        """Returns the verified, lower-cased email claim or raises ValueError."""
        if not token or len(token) > 8192 or token.count(".") != 2:
            raise ValueError("malformed assertion")
        # Hardcode the expected algorithm; never derive it from the unverified token.
        if _unverified_header(token).get("alg") != "ES256":
            raise ValueError("unexpected algorithm")
        from google.oauth2 import id_token  # lazy import
        claims = id_token.verify_token(token, self._get_request(), audience=audience or None,
                                       certs_url=IAP_CERTS_URL, clock_skew_in_seconds=10)
        if claims.get("iss") != IAP_ISSUER:
            raise ValueError("unexpected issuer")
        email = (claims.get("email") or "").strip().lower()
        if not email or "@" not in email:
            raise ValueError("missing email claim")
        return email

    def verify_google_oidc(self, token, audiences):
        """Verifies a Google-signed OIDC ID token (for example from Cloud Scheduler) for one of the
        expected audiences. Returns the verified, lower-cased email claim or raises ValueError."""
        audiences = [a for a in audiences or [] if a]
        if not audiences:
            raise ValueError("no audience configured")
        if not token or len(token) > 8192 or token.count(".") != 2:
            raise ValueError("malformed token")
        if _unverified_header(token).get("alg") != "RS256":
            raise ValueError("unexpected algorithm")
        from google.oauth2 import id_token  # lazy import
        claims = id_token.verify_token(token, self._get_request(), audience=audiences,
                                       certs_url=GOOGLE_CERTS_URL, clock_skew_in_seconds=10)
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise ValueError("unexpected issuer")
        if claims.get("email_verified") is not True:
            raise ValueError("email not verified")
        email = (claims.get("email") or "").strip().lower()
        if not email or "@" not in email:
            raise ValueError("missing email claim")
        return email


SCHEDULER_MODE = "scheduler"


def scheduler_identity(headers, settings, verifier, path):
    """Identity of a Cloud Scheduler call authenticated with a Google OIDC token (Authorization:
    Bearer). Only service accounts in governance.antigravity.scheduler_invokers are accepted, and
    the token audience must be this deployment's public URL (with or without the path)."""
    ag = (settings.get("governance") or {}).get("antigravity") or {}
    invokers = [str(e).strip().lower() for e in ag.get("scheduler_invokers") or []]
    base = ((settings.get("security") or {}).get("public_base_url") or "").rstrip("/")
    if not invokers or not base:
        raise ApiError("unauthenticated", "Scheduled enforcement is not configured for this deployment.")
    auth = headers.get("Authorization") or ""
    token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    try:
        email = verifier.verify_google_oidc(token, [base + path, base, base + "/"])
    except Exception as err:  # pylint: disable=broad-except
        LOG.warning("scheduler token rejected reason=%s", type(err).__name__)
        raise ApiError("unauthenticated", "Authentication is required.")
    if email not in invokers:
        LOG.warning("scheduler token from a service account that is not allowed")
        raise ApiError("forbidden", "This service account may not run enforcement.")
    return Identity(email, "admin", SCHEDULER_MODE, "scheduler:" + email)


def client_key(headers, peer):
    """Per-visitor key for rate limits. Behind Cloud Run the TCP peer is Google's front end, so the client is the
    last X-Forwarded-For entry (appended by the front end; earlier entries come from the client and are ignored)."""
    forwarded = (headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
    try:
        return str(ipaddress.ip_address(forwarded))
    except ValueError:
        return peer or "unknown"


def guest_identity(headers, peer):
    return Identity("", "viewer", GUEST_MODE, "guest:%s" % client_key(headers, peer))


def authenticate(headers, peer, settings, verifier, environ=None, sessions=None):
    """Resolves the caller identity for one request. Fails closed."""
    sec = settings.get("security", {})
    mode = resolve_auth_mode(sec.get("auth_mode"), environ)
    admins = sec.get("admins") or []
    if mode == "google":
        user = sessions.user_from_headers(headers, settings) if sessions is not None else None
        if user is None:
            if sec.get("guest_access"):
                return guest_identity(headers, peer)
            raise ApiError("unauthenticated", "Sign in to continue.")
        email = user["email"]
        role = "admin" if (email == "admin" or user.get("role") == "admin" or
                           is_admin_email(email, admins)) else "viewer"
        return Identity(email, role, mode, "google:" + email, name=str(user.get("name") or ""),
                        picture=str(user.get("picture") or ""))
    if mode == "iap":
        token = headers.get(IAP_HEADER) or ""
        try:
            email = verifier.verify(token, sec.get("iap_audience") or "")
        except Exception as err:  # pylint: disable=broad-except
            LOG.warning("IAP assertion rejected reason=%s", type(err).__name__)
            raise ApiError("unauthenticated", "Authentication is required.")
        role = "admin" if is_admin_email(email, admins) else "viewer"
        return Identity(email, role, mode, "iap:" + email)
    if mode == "local":
        # Development on one computer: only a direct loopback caller is accepted, as the administrator.
        # Other computers, and requests forwarded by a proxy (even one on this computer), are refused,
        # so nobody reaches the dashboard without a Google identity except the developer at this machine.
        forwarded = any(headers.get(name) for name in FORWARDING_HEADERS)
        if not is_loopback(peer) or forwarded:
            raise ApiError("forbidden", LOCAL_ONLY_MESSAGE)
        return Identity(LOCAL_IDENTITY, "admin", mode, "local:%s" % peer)
    raise ApiError("unauthenticated", "Authentication is required.")


# ---------------------------------------------------------------------------
# Rate limiting (per identity, in-memory token buckets)
# ---------------------------------------------------------------------------

# guest_ai: AI calls (analyst, spike analysis) of all guests together, on top of each guest's own "ai" bucket.
RATE_LIMITS = {"general": 240, "ai": 20, "governance": 10, "refresh": 6, "login": 120, "guest_ai": 30}


class RateLimiter:
    def __init__(self, limits=None):
        self.limits = dict(limits or RATE_LIMITS)
        self._lock = threading.Lock()
        self._buckets = {}
        self._last_prune = time.monotonic()

    def allow(self, key, bucket):
        """Consumes one token. Returns (allowed, retry_after_seconds)."""
        limit = self.limits[bucket]
        rate = limit / 60.0
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get((key, bucket), (float(limit), now))
            tokens = min(float(limit), tokens + (now - last) * rate)
            if tokens < 1.0:
                self._buckets[(key, bucket)] = (tokens, now)
                return False, max(1, int(math.ceil((1.0 - tokens) / rate)))
            self._buckets[(key, bucket)] = (tokens - 1.0, now)
            if now - self._last_prune > 60:
                self._prune(now)
            return True, 0

    def _prune(self, now):
        self._last_prune = now
        stale = [k for k, (_, last) in self._buckets.items() if now - last > 600]
        for k in stale:
            del self._buckets[k]

    def check(self, key, bucket):
        allowed, retry = self.allow(key, bucket)
        if not allowed:
            raise ApiError("rate_limited", "Too many requests. Please retry shortly.",
                           headers=[("Retry-After", str(retry))])


# ---------------------------------------------------------------------------
# PII minimization
# ---------------------------------------------------------------------------

EMAIL_IN_TEXT_RE = re.compile(r"[A-Za-z0-9._%+\-']+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")


def uid_for(email):
    return hashlib.sha256(((email or "").strip().lower() + UID_SALT).encode("utf-8")).hexdigest()[:16]


def is_service_account(principal):
    return isinstance(principal, str) and principal.lower().endswith(".gserviceaccount.com")


def is_human(principal):
    if not isinstance(principal, str) or "@" not in principal:
        return False
    low = principal.lower()
    return not low.endswith(".gserviceaccount.com") and not low.startswith("principal://")


def mask_email(value):
    """ad***@example.com: keeps the first 2 characters of the local part.
    Service-account emails are not personal data and are returned unchanged."""
    if not isinstance(value, str) or "@" not in value or is_service_account(value):
        return value
    local, _, domain = value.rpartition("@")
    keep = local[:2] if len(local) > 2 else local[:1]
    return "%s***@%s" % (keep, domain)


def mask_text_emails(text):
    if not isinstance(text, str) or "@" not in text:
        return text
    return EMAIL_IN_TEXT_RE.sub(lambda m: mask_email(m.group(0)), text)


def mask_ip(value):
    if not isinstance(value, str) or not value:
        return value
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return value
    if addr.version == 4:
        return ".".join(value.split(".")[:3] + ["x"])
    groups = addr.exploded.split(":")
    return ":".join(groups[:4] + ["x"])


def mask_for_viewer(payload, restrict_keys=(), ip_keys=("callerIp",), null_keys=()):
    """Returns a masked deep copy: emails masked in every string, IPs truncated,
    restricted text fields replaced by null with restricted=true."""
    restrict_keys = set(restrict_keys)
    ip_keys = set(ip_keys)
    null_keys = set(null_keys)

    def walk(node):
        if isinstance(node, dict):
            out = {}
            restricted = False
            for key, value in node.items():
                if key in restrict_keys:
                    if value is not None:
                        restricted = True
                    out[key] = None
                elif key in ip_keys:
                    out[key] = mask_ip(value)
                elif key in null_keys:
                    out[key] = None
                else:
                    out[key] = walk(value)
            if restricted:
                out["restricted"] = True
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            return mask_text_emails(node)
        return node

    return walk(payload)


# ---------------------------------------------------------------------------
# Guest scrubbing (public demos): applied on top of the viewer masking to every string a guest receives
# ---------------------------------------------------------------------------

BULLET = "\u2022"
# Local part may already be viewer-masked (ad***@...); domain labels may already hold bullets.
_GUEST_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+\-'*\u2022]+)@((?:[A-Za-z0-9\-\u2022]+\.)+)([A-Za-z]{2,})\b")
_BILLING_ID_RE = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{6}([-_])[0-9A-Fa-f]{6}\1[0-9A-Fa-f]{6}(?![0-9A-Za-z])")
_NUM12_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")  # project numbers
_RRN_RE = re.compile(r"(?<!\d)\d{6}-[1-4]\d{6}(?!\d)")
_PHONE_RE = re.compile(r"(?<!\d)01[016789][- ]\d{3,4}[- ]\d{4}(?!\d)")
_CARD_RE = re.compile(r"(?<!\d)(?:\d{4}[- ]){3}\d{4}(?!\d)")
_IPV4_RE = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.\d{1,3}\.\d{1,3}(?![\d.])")
# Values the browser needs verbatim (CSRF token).
GUEST_VERBATIM_KEYS = frozenset({"csrfToken"})
_TERM_CACHE = {}


def _bullets(n):
    return BULLET * max(3, min(8, n))


def _guest_email(match):
    local, domain, tld = match.group(1), match.group(2), match.group(3)
    labels = domain[:-1].split(".")
    if labels[-1].lower() == "gserviceaccount":
        labels[0] = _bullets(len(labels[0]))  # project ID in a service-account address
        return "%s@%s.%s" % (local, ".".join(labels), tld)
    keep = local.replace("*", "").replace(BULLET, "")[:2]
    return "%s%s@%s.%s" % (keep, BULLET * 3, ".".join(_bullets(len(p)) for p in labels), tld)


def _term_pattern(terms):
    """Case-insensitive alternation of the terms, longest first. A term that starts or ends with an ASCII letter
    or digit only matches at that end on a boundary, so "AB" masks "AB" and "AB전자" but not "crab"."""
    key = tuple(terms or ())
    compiled = _TERM_CACHE.get(key, False)
    if compiled is False:
        parts = []
        for term in sorted({str(t).strip() for t in key if str(t).strip()}, key=len, reverse=True):
            pattern = re.escape(term)
            if re.match(r"[A-Za-z0-9]", term[0]):
                pattern = r"(?<![A-Za-z0-9])" + pattern
            if re.match(r"[A-Za-z0-9]", term[-1]):
                pattern += r"(?![A-Za-z0-9])"
            parts.append(pattern)
        compiled = re.compile("|".join(parts), re.IGNORECASE) if parts else None
        if len(_TERM_CACHE) > 16:
            _TERM_CACHE.clear()
        _TERM_CACHE[key] = compiled
    return compiled


def guest_scrub_text(text, terms=()):
    if not isinstance(text, str) or len(text) < 3:
        return text
    out = _GUEST_EMAIL_RE.sub(_guest_email, text)
    # Structured identifiers first: a term that matches part of one must not leave the rest visible.
    out = _BILLING_ID_RE.sub(lambda m: "XXXXXX{0}XXXXXX{0}XXXXXX".format(m.group(1)), out)
    out = _NUM12_RE.sub(BULLET * 12, out)
    term_re = _term_pattern(terms)
    if term_re is not None:
        out = term_re.sub(lambda m: _bullets(len(m.group(0))), out)
    out = _RRN_RE.sub(BULLET * 6 + "-" + BULLET * 7, out)
    out = _PHONE_RE.sub("010-" + BULLET * 4 + "-" + BULLET * 4, out)
    out = _CARD_RE.sub("-".join([BULLET * 4] * 4), out)
    return _IPV4_RE.sub(lambda m: "%s.%s.%s.%s" % (m.group(1), m.group(2), BULLET * 3, BULLET * 3), out)


def guest_scrub(payload, terms=()):
    """Deep copy of an API payload for a guest with identifiers removed from every string value (dict keys,
    numbers and the CSRF token are kept). Viewer masking runs first; this adds whole e-mail domains, the
    configured terms (security.guest_redact_terms), billing account IDs and 12-digit project numbers."""
    def walk(node):
        if isinstance(node, dict):
            return {k: (v if k in GUEST_VERBATIM_KEYS else walk(v)) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [walk(v) for v in node]
        if isinstance(node, str):
            return guest_scrub_text(node, terms)
        return node

    return walk(payload)
