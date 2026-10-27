"""HTTP plumbing for the control plane server.

Only the Python standard library is used in this module, so it stays importable without any
Google Cloud client library (the unit tests rely on this).

Responsibilities:
- JSON encoding and the response envelope
- Security headers on every response
- Static file serving with CSP nonce templating, MIME allowlist, strong ETags and
  path traversal protection
- CSRF double-submit cookie helpers
- A small decorator router and a base request handler that only accepts GET/POST
"""

from __future__ import annotations

import base64
import datetime as _dt
import decimal
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("controlplane.http")

ERROR_STATUS = {
    "bad_request": 400,
    "unauthenticated": 401,
    "forbidden": 403,
    "csrf_failed": 403,
    "not_found": 404,
    "conflict": 409,
    "module_disabled": 409,
    "not_configured": 409,
    "rate_limited": 429,
    "upstream_error": 502,
    "internal": 500,
}

MAX_BODY_BYTES = 64 * 1024
MAX_QUERY_FIELDS = 64


class ApiError(Exception):
    """An error that is rendered as the API error envelope {"error": {"code", "message"}}."""

    def __init__(self, code, message, status=None, headers=None):
        super().__init__(message)
        self.code = code if code in ERROR_STATUS else "internal"
        self.message = message
        self.status = int(status or ERROR_STATUS[self.code])
        self.headers = list(headers or [])


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def utc_now():
    return _dt.datetime.now(_dt.timezone.utc)


def iso_z(value):
    """Formats datetimes as ISO-8601 UTC strings (2026-10-19T05:20:51Z)."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.timezone.utc)
        return value.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, _dt.date):
        return value.isoformat()
    return str(value)


def now_iso():
    return iso_z(utc_now())


def _json_default(obj):
    if isinstance(obj, _dt.datetime):
        return iso_z(obj)
    if isinstance(obj, _dt.date):
        return obj.isoformat()
    if isinstance(obj, _dt.time):
        return obj.isoformat()
    if isinstance(obj, decimal.Decimal):
        value = float(obj)
        return value if math.isfinite(value) else None
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, (bytes, bytearray)):
        return base64.b64encode(bytes(obj)).decode("ascii")
    raise TypeError("Object of type %s is not JSON serializable" % type(obj).__name__)


def sanitize_json(value):
    """Replaces non-finite floats by None recursively (JSON has no NaN/Infinity)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: sanitize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json(v) for v in value]
    return value


def json_bytes(payload):
    try:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                          default=_json_default, allow_nan=False)
    except ValueError:
        text = json.dumps(sanitize_json(payload), ensure_ascii=False, separators=(",", ":"),
                          default=_json_default, allow_nan=False)
    return text.encode("utf-8")


def envelope(data, *, cached=False, range_meta=None, sources=(), warnings=(), generated_at=None):
    """Builds the success envelope {"data": ..., "meta": {...}}."""
    meta = {"generatedAt": generated_at or now_iso(), "cached": bool(cached)}
    if range_meta is not None:
        meta["range"] = range_meta
    meta["sources"] = sorted(set(sources))
    meta["warnings"] = list(warnings)
    return {"data": data, "meta": meta}


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"


def content_security_policy(nonce=None):
    script_src = "'self'"
    if nonce:
        script_src += " 'nonce-%s'" % nonce
    return ("default-src 'self'; script-src %s; style-src 'self'; img-src 'self' data: https:; "
            "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'" % script_src)


def security_headers(https, nonce=None):
    headers = [
        ("Content-Security-Policy", content_security_policy(nonce)),
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "strict-origin-when-cross-origin"),
        ("Permissions-Policy", PERMISSIONS_POLICY),
        ("Cross-Origin-Opener-Policy", "same-origin"),
    ]
    if https:
        headers.append(("Strict-Transport-Security", "max-age=31536000; includeSubDomains"))
    return headers


def request_is_https(headers):
    proto = (headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    return proto == "https"


# ---------------------------------------------------------------------------
# CSRF (double-submit cookie)
# ---------------------------------------------------------------------------

CSRF_HEADER = "X-CSRF-Token"
CSRF_COOKIE_MAX_AGE = 86400
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{32,128}$")


def csrf_cookie_name(https):
    return "__Host-cp_csrf" if https else "cp_csrf"


def read_cookie(headers, name):
    raw = headers.get("Cookie") or ""
    if len(raw) > 8192:
        return None
    for part in raw.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key.strip() == name:
            return value.strip().strip('"')
    return None


def issue_csrf_token(headers, https):
    """Returns (token, Set-Cookie value). Reuses a well-formed existing token so that
    several open tabs keep working; the cookie expiry is refreshed either way."""
    name = csrf_cookie_name(https)
    token = read_cookie(headers, name)
    if not token or not _TOKEN_RE.match(token):
        token = secrets.token_urlsafe(32)
    attrs = ["%s=%s" % (name, token), "Path=/", "Max-Age=%d" % CSRF_COOKIE_MAX_AGE, "HttpOnly",
             "SameSite=Strict"]
    if https:
        attrs.append("Secure")
    return token, "; ".join(attrs)


def csrf_valid(headers, https):
    """Validates Content-Type, the X-CSRF-Token header and the CSRF cookie."""
    ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        return False
    if (headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
        return False
    sent = (headers.get(CSRF_HEADER) or "").strip()
    cookie = read_cookie(headers, csrf_cookie_name(https)) or ""
    if not _TOKEN_RE.match(sent) or not _TOKEN_RE.match(cookie):
        return False
    return hmac.compare_digest(sent.encode("ascii"), cookie.encode("ascii"))


# ---------------------------------------------------------------------------
# Query / body helpers
# ---------------------------------------------------------------------------

def parse_query(query):
    try:
        pairs = urllib.parse.parse_qsl(query or "", keep_blank_values=True,
                                       max_num_fields=MAX_QUERY_FIELDS)
    except ValueError:
        raise ApiError("bad_request", "Too many query parameters.")
    out = {}
    for key, value in pairs:
        if len(key) > 64 or len(value) > 512:
            raise ApiError("bad_request", "Query parameter is too long.")
        out.setdefault(key, value)
    return out


def parse_json_object(raw):
    if not raw:
        return {}
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ApiError("bad_request", "Request body must be valid JSON.")
    if not isinstance(obj, dict):
        raise ApiError("bad_request", "Request body must be a JSON object.")
    return obj


# ---------------------------------------------------------------------------
# Static site
# ---------------------------------------------------------------------------

ASSET_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".webp": "image/webp",
}
MAX_ASSET_BYTES = 10 * 1024 * 1024


class StaticSite:
    """Serves src/web: templated index.html and allowlisted files under assets/."""

    def __init__(self, web_root, version):
        self.web_root = os.path.realpath(web_root)
        self.assets_root = os.path.realpath(os.path.join(self.web_root, "assets"))
        self.version = str(version)
        self._lock = threading.Lock()
        self._cache = {}

    def _read(self, path):
        """Reads a file with an mtime/size keyed cache. Returns (bytes, etag) or None."""
        try:
            st = os.stat(path)
        except OSError:
            return None
        if st.st_size > MAX_ASSET_BYTES:
            return None
        key = (st.st_mtime_ns, st.st_size)
        with self._lock:
            hit = self._cache.get(path)
            if hit and hit[0] == key:
                return hit[1], hit[2]
        try:
            with open(path, "rb") as fh:
                data = fh.read(MAX_ASSET_BYTES + 1)
        except OSError:
            return None
        if len(data) > MAX_ASSET_BYTES:
            return None
        etag = '"%s"' % hashlib.sha256(data).hexdigest()
        with self._lock:
            if len(self._cache) > 512:
                self._cache.clear()
            self._cache[path] = (key, data, etag)
        return data, etag

    TEMPLATES = ("index.html", "login.html")

    def render_index(self):
        """Returns (body, nonce) with a fresh nonce, or (None, None) if index.html is missing."""
        return self.render_template("index.html")

    def render_template(self, name):
        """Renders one of the allowlisted HTML templates with a fresh CSP nonce and the app version."""
        if name not in self.TEMPLATES:
            return None, None
        hit = self._read(os.path.join(self.web_root, name))
        if hit is None:
            return None, None
        nonce = secrets.token_urlsafe(16)
        body = hit[0].replace(b"__CSP_NONCE__", nonce.encode("ascii"))
        body = body.replace(b"__APP_VERSION__", self.version.encode("ascii", "replace"))
        return body, nonce

    def resolve_asset(self, rel):
        """Maps a decoded path relative to assets/ to (absolute path, content type).

        Rejects traversal, hidden segments, backslashes, NUL bytes, symlinks that
        escape the assets directory, and extensions outside the allowlist."""
        if not rel or len(rel) > 512 or "\x00" in rel or "\\" in rel:
            return None
        parts = rel.split("/")
        for part in parts:
            if part in ("", ".", "..") or part.startswith("."):
                return None
        ext = os.path.splitext(parts[-1])[1].lower()
        ctype = ASSET_TYPES.get(ext)
        if ctype is None:
            return None
        full = os.path.realpath(os.path.join(self.assets_root, *parts))
        if not full.startswith(self.assets_root + os.sep):
            return None
        if not os.path.isfile(full):
            return None
        return full, ctype

    def load(self, full):
        return self._read(full)


def etag_matches(header_value, etag):
    if not header_value or not etag:
        return False
    for candidate in header_value.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class Router:
    """Exact-path decorator router. Options are free-form route metadata."""

    def __init__(self):
        self.routes = {}

    def route(self, method, path, **opts):
        def deco(fn):
            self.routes[(method, path)] = (fn, opts)
            return fn
        return deco

    def get(self, path, **opts):
        return self.route("GET", path, **opts)

    def post(self, path, **opts):
        return self.route("POST", path, **opts)

    def resolve(self, method, path):
        hit = self.routes.get((method, path))
        if hit is not None:
            return hit
        if any(p == path for (_, p) in self.routes):
            raise ApiError("bad_request", "Method not allowed.", status=405,
                           headers=[("Allow", "GET, POST")])
        raise ApiError("not_found", "Endpoint not found.")

    def paths(self):
        return sorted(self.routes)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class Server(ThreadingHTTPServer):
    """Threading HTTP server that reports client disconnects and unexpected handler failures
    as single structured log lines instead of printing tracebacks to stderr."""

    daemon_threads = True
    request_queue_size = 128
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        err = sys.exc_info()[1]
        if isinstance(err, (ConnectionError, TimeoutError)):
            LOG.debug("client connection closed reason=%s", type(err).__name__)
            return
        LOG.error("unhandled server error reason=%s", type(err).__name__ if err else "unknown")


# ---------------------------------------------------------------------------
# Base request handler
# ---------------------------------------------------------------------------

class BaseHandler(BaseHTTPRequestHandler):
    """GET/POST-only handler with static serving and JSON helpers.

    Subclasses set `site` (a StaticSite) and implement handle_api()."""

    server_version = "ControlPlane"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30
    site = None

    # -- method dispatch ------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def __getattr__(self, name):
        # Any other HTTP method (PUT, DELETE, PATCH, HEAD, OPTIONS, TRACE, ...) is rejected.
        if name.startswith("do_"):
            return self._reject_method
        raise AttributeError(name)

    def _reject_method(self):
        self._t0 = getattr(self, "_t0", None) or time.monotonic()
        self.close_connection = True
        self.send_error_json(ApiError("bad_request", "Method not allowed.", status=405,
                                      headers=[("Allow", "GET, POST")]))
        self._log_access(self.command)

    @property
    def is_https(self):
        return request_is_https(self.headers)

    def client_ip(self):
        try:
            return self.client_address[0]
        except (AttributeError, IndexError, TypeError):
            return ""

    def _dispatch(self, method):
        self._t0 = time.monotonic()
        self._status = 0
        self._sent = False
        try:
            split = urllib.parse.urlsplit(self.path)
            path = split.path or "/"
            # Liveness. Cloud Run's front end answers /healthz itself (paths ending in "z" are reserved), so
            # uptime checks on Cloud Run use /health; /healthz stays for local and container checks.
            if path in ("/health", "/healthz"):
                self.send_json(200, {"status": "ok"})
            elif path == "/api" or path.startswith("/api/"):
                self.handle_api(method, path, split.query)
            elif self.handle_page(method, path, split.query):
                pass
            elif method != "GET":
                self.close_connection = True
                raise ApiError("bad_request", "Method not allowed.", status=405,
                               headers=[("Allow", "GET")])
            else:
                self.serve_static(path)
        except ApiError as err:
            self.send_error_json(err)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:  # pylint: disable=broad-except
            LOG.exception("unhandled error method=%s path=%s", method, self._safe_path())
            self.send_error_json(ApiError("internal", "An internal error occurred."))
        finally:
            self._log_access(method)

    def handle_api(self, method, path, query):  # pragma: no cover - abstract
        raise ApiError("not_found", "Endpoint not found.")

    def handle_page(self, method, path, query):  # pylint: disable=unused-argument
        """Hook for server-handled pages (sign-in flow, access gate). Returns True when handled."""
        return False

    # -- responses ------------------------------------------------------------
    def send_response(self, code, message=None):
        self._status = code
        super().send_response(code, message)

    def log_request(self, code="-", size="-"):
        # Access logging is done once per request in _log_access (no query strings).
        return

    def log_message(self, fmt, *args):
        LOG.debug("http-server " + fmt, *args)

    def _safe_path(self):
        try:
            return urllib.parse.urlsplit(self.path).path[:200]
        except Exception:  # pylint: disable=broad-except
            return "-"

    def _log_access(self, method):
        try:
            elapsed = int((time.monotonic() - (self._t0 or time.monotonic())) * 1000)
        except Exception:  # pylint: disable=broad-except
            elapsed = 0
        LOG.info("%s %s status=%s ms=%d", method, self._safe_path(), getattr(self, "_status", 0),
                 elapsed)

    def _write(self, status, body, content_type, cache_control, extra_headers=(), nonce=None):
        if getattr(self, "_sent", False):
            return
        self._sent = True
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
        else:
            self.send_header("Content-Length", "0")
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        for key, value in security_headers(self.is_https, nonce):
            self.send_header(key, value)
        for key, value in extra_headers:
            self.send_header(key, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def send_json(self, status, payload, extra_headers=()):
        self._write(status, json_bytes(payload), "application/json; charset=utf-8", "no-store",
                    extra_headers)

    def send_error_json(self, err, extra_headers=()):
        payload = {"error": {"code": err.code, "message": err.message}}
        self.send_json(err.status, payload, list(err.headers) + list(extra_headers))

    def read_body_bytes(self, limit=MAX_BODY_BYTES):
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            if (self.headers.get("Transfer-Encoding") or "").lower():
                self.close_connection = True
                raise ApiError("bad_request", "Chunked request bodies are not supported.")
            return b""
        try:
            length = int(raw_len)
        except ValueError:
            self.close_connection = True
            raise ApiError("bad_request", "Invalid Content-Length.")
        if length < 0 or length > limit:
            self.close_connection = True
            raise ApiError("bad_request", "Request body is too large.")
        return self.rfile.read(length) if length else b""

    # -- static ---------------------------------------------------------------
    def serve_static(self, path):
        decoded = urllib.parse.unquote(path)
        if decoded == "/favicon.ico":
            return self._send_asset("img/favicon.svg")
        if decoded == "/assets" or decoded.startswith("/assets/"):
            return self._send_asset(decoded[len("/assets/"):])
        return self._send_index()

    def _send_index(self):
        self.send_template("index.html")

    def send_template(self, name):
        body, nonce = (None, None)
        if self.site is not None:
            body, nonce = self.site.render_template(name)
        if body is None:
            raise ApiError("not_found", "The web frontend is not installed.")
        self._write(200, body, "text/html; charset=utf-8", "no-store", nonce=nonce)

    def send_redirect(self, location, cookies=()):
        """302 to a same-origin path or a fixed external URL built by the server (never user input)."""
        if not isinstance(location, str) or any(ord(ch) < 32 or ord(ch) == 127 for ch in location):
            raise ApiError("internal", "Invalid redirect.")
        headers = [("Location", location)] + [("Set-Cookie", c) for c in cookies]
        self._write(302, b"", "text/plain; charset=utf-8", "no-store", headers)

    def _send_asset(self, rel):
        resolved = self.site.resolve_asset(rel) if self.site is not None else None
        if resolved is None:
            raise ApiError("not_found", "Asset not found.")
        full, ctype = resolved
        hit = self.site.load(full)
        if hit is None:
            raise ApiError("not_found", "Asset not found.")
        data, etag = hit
        if etag_matches(self.headers.get("If-None-Match"), etag):
            self._write(304, None, ctype, "no-cache", [("ETag", etag)])
            return
        self._write(200, data, ctype, "no-cache", [("ETag", etag)])
