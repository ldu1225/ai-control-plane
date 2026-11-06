"""Google OAuth client for sign-in, connected by an administrator (Settings > Access control).

Google sign-in through the IAP sign-in bridge admits accounts of the project's organization only (IAP's Google-managed
OAuth client). An OAuth client of the customer's own project lets every approved Google account sign in, including
accounts of other organizations (partners, affiliates, Google): Google Auth Platform with audience External and the
app published, and a Web application client whose authorized redirect URI is the dashboard's /auth/google/callback.
Google offers no API to create such a client, so that part is a console step.

Connecting it needs no redeployment. The client secret is added as a new version of the Secret Manager secret named by
OAUTH_CONNECT_SECRET (created by the deployment; the dashboard's service account may add, read and retire versions of
that one secret, and nothing else); the client ID and that exact version are kept in the internal sign-in state. Every
instance picks the client up within the settings reload interval. Before anything is stored, Google is asked whether
the client ID and secret are valid and whether the redirect URI is registered, so a wrong value never locks people out.
A replaced or disconnected client's secret version is destroyed, so no unused client secret stays behind.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import urllib.parse

from ..gcp import GcpError
from ..httpkit import ApiError, now_iso
from ..settings_store import OAUTH_CLIENT_ID_RE, SECRET_VERSION_RE

LOG = logging.getLogger("controlplane.oauth_client")

SECRET_API = "https://secretmanager.googleapis.com/v1/"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
CALLBACK_PATH = "/auth/google/callback"
_SECRET_RE = re.compile(r"^projects/[A-Za-z0-9._:-]{1,64}/secrets/[A-Za-z0-9_-]{1,255}$")
_CLIENT_SECRET_RE = re.compile(r"^[A-Za-z0-9_\-]{10,200}$")


def secret_resource(app):
    """projects/P/secrets/NAME that holds connected client secrets (OAUTH_CONNECT_SECRET), or ''."""
    value = str((getattr(app, "environ", None) or {}).get("OAUTH_CONNECT_SECRET") or "").strip()
    return value if _SECRET_RE.match(value) else ""


def _secret_id(name):
    """NAME of projects/P/secrets/NAME[/versions/N] (versions name the project by number, the deployment by ID)."""
    return str(name or "").partition("/secrets/")[2].partition("/")[0]


def redirect_uri(app, headers=None):
    """The callback Google redirects to, which the client must list as an authorized redirect URI:
    GOOGLE_OAUTH_REDIRECT_URI, else PUBLIC_BASE_URL + /auth/google/callback, else the request host."""
    auth = getattr(app, "auth", None)
    if auth is None:
        return None
    if auth.oauth_redirect_uri:
        return auth.oauth_redirect_uri
    if auth.public_base_url:
        return auth.public_base_url + CALLBACK_PATH
    if headers is not None:
        try:
            return auth.callback_uri(headers)
        except ApiError:
            return None
    return None


def read_version(gcp, version, quota_project):
    """Value of one Secret Manager secret version (UTF-8)."""
    if not SECRET_VERSION_RE.match(str(version or "")):
        raise ValueError("invalid secret version")
    payload, _ = gcp.request("GET", SECRET_API + version + ":access", quota_project=quota_project, timeout=10)
    data = ((payload or {}).get("payload") or {}).get("data") or ""
    return base64.b64decode(data).decode("utf-8")


def _add_version(gcp, secret, value, quota_project):
    body = {"payload": {"data": base64.b64encode(value.encode("utf-8")).decode("ascii")}}
    payload, _ = gcp.request("POST", SECRET_API + secret + ":addVersion", quota_project=quota_project, json_body=body,
                             timeout=10)
    name = str((payload or {}).get("name") or "")
    if not SECRET_VERSION_RE.match(name):
        raise GcpError("invalid", message="unexpected secret version name")
    return name


def _auth_error(location):
    """Decoded reason from Google's /signin/oauth/error?authError=... redirect, '' when the redirect is not an error."""
    parts = urllib.parse.urlsplit(location or "")
    if "/signin/oauth/error" not in parts.path:
        return ""
    raw = (urllib.parse.parse_qs(parts.query).get("authError") or [""])[0]
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (binascii.Error, ValueError):
        return "error"
    text = re.sub(r"[^\x20-\x7e]+", " ", decoded.decode("latin-1")).strip()
    return text[:200] or "error"


def check_client(client_id, client_secret, redirect_uri):
    """Asks Google whether the client works for this dashboard. Raises ApiError with what to fix:
    - the ID and secret: an authorization-code exchange with a dummy code answers invalid_grant for a valid client and
      invalid_client for a wrong ID or secret;
    - the redirect URI: the authorization endpoint redirects to an error page (redirect_uri_mismatch) when the URI is
      not registered on the client.
    Checks Google cannot be reached for are skipped (the dashboard may have restricted egress)."""
    import requests
    token_error = "unreachable"
    try:
        token = requests.post(TOKEN_URL, data={"code": "control-plane-client-check", "client_id": client_id,
                                               "client_secret": client_secret, "redirect_uri": redirect_uri,
                                               "grant_type": "authorization_code"}, timeout=10, allow_redirects=False)
        token_error = str(((token.json() or {}) if token.content else {}).get("error") or "")
    except (requests.RequestException, ValueError):
        pass
    if token_error in ("invalid_client", "unauthorized_client"):
        raise ApiError("bad_request", "Google rejected this client ID or client secret. Copy both again from the "
                                      "client in Google Auth Platform > Clients. A client created a moment ago can "
                                      "take a few minutes to become active.")
    reason = "unreachable"
    try:
        start = requests.get(AUTH_URL, params={"client_id": client_id, "redirect_uri": redirect_uri,
                                               "response_type": "code", "scope": "openid email"},
                             timeout=10, allow_redirects=False)
        reason = _auth_error(start.headers.get("Location") or "")
    except requests.RequestException:
        pass
    if "redirect_uri_mismatch" in reason:
        raise ApiError("bad_request", "Add {uri} as an authorized redirect URI of this client (Google Auth Platform > "
                                      "Clients > the client), save, and connect again. A change saved a moment ago "
                                      "can take a few minutes to apply.".format(uri=redirect_uri))
    if "invalid_client" in reason or "deleted_client" in reason:
        raise ApiError("bad_request", "Google does not know this client ID. Copy it again from Google Auth Platform "
                                      "> Clients. A client created a moment ago can take a few minutes to become "
                                      "active.")
    return {"token": token_error or "ok", "redirect": reason or "ok"}


def summary(app, redirect_uri):
    """Sign-in method for Settings > Access control: OAuth client in use (and its source), connectable or not, and
    what to register in the console."""
    auth = getattr(app, "auth", None)
    if auth is None:
        return {"available": False}
    client_id, _secret, source = auth.oauth_client()
    connected = auth.connected_oauth()
    secret = secret_resource(app)
    return {"available": True, "active": bool(client_id), "source": source,
            "clientId": client_id or (connected or {}).get("clientId") or None,
            "bridge": bool(auth.bridge_url), "canConnect": bool(secret) and source != "deployment",
            "secret": _secret_id(secret) or None, "redirectUri": redirect_uri or None,
            "connectedAt": (connected or {}).get("updatedAt") or None,
            "connectedBy": (connected or {}).get("updatedBy") or None,
            "secretUnreadable": bool(connected) and source is None}


def _destroy_version(gcp, version, quota_project):
    """Destroys a secret version that is no longer used (best effort: a failure leaves it in place and is logged)."""
    try:
        gcp.request("POST", SECRET_API + version + ":destroy", quota_project=quota_project, json_body={}, timeout=10)
        return True
    except GcpError as err:
        LOG.warning("unused OAuth client secret version not destroyed version=%s reason=%s",
                    version.rpartition("/")[2], err.kind)
        return False


def _retire(app, secret, version, quota_project):
    """Destroys `version` when it is a version of this deployment's connect secret."""
    if version and secret and SECRET_VERSION_RE.match(version) and _secret_id(version) == _secret_id(secret):
        _destroy_version(app.gcp, version, quota_project)


def connect(app, quota_project, actor, client_id, client_secret, redirect_uri):
    """Validates the client with Google, stores the secret in Secret Manager, reads it back, then switches sign-in.
    The previously connected client's secret version is destroyed afterwards."""
    auth = getattr(app, "auth", None)
    if auth is None:
        raise ApiError("conflict", "Google sign-in is not used on this deployment.")
    if auth.client_id and auth.client_secret:
        raise ApiError("conflict", "The OAuth client is set by the deployment (GOOGLE_OAUTH_CLIENT_ID) and cannot "
                                   "be changed here.")
    secret = secret_resource(app)
    if not secret:
        raise ApiError("conflict", "This deployment has no secret for an OAuth client yet. Run the installer again "
                                   "(scripts/deploy.sh) and connect again.")
    client_id = str(client_id or "").strip().lower()
    client_secret = str(client_secret or "").strip()
    if not OAUTH_CLIENT_ID_RE.match(client_id):
        raise ApiError("bad_request", "Enter the client ID of a Web application client "
                                      "(…apps.googleusercontent.com).")
    if not _CLIENT_SECRET_RE.match(client_secret):
        raise ApiError("bad_request", "Enter the client secret shown with the client (for example GOCSPX-…).")
    check_client(client_id, client_secret, redirect_uri)
    previous = (auth.connected_oauth() or {}).get("secretVersion")
    try:
        version = _add_version(app.gcp, secret, client_secret, quota_project)
    except GcpError as err:
        LOG.warning("OAuth client secret not stored reason=%s", err.kind)
        if err.kind == "denied":
            raise ApiError("forbidden", "The dashboard's service account may not use the OAuth client secret in "
                                        "Secret Manager. Run the installer again (scripts/deploy.sh) and connect "
                                        "again.")
        raise ApiError("upstream_error", "The client secret could not be stored in Secret Manager. Try again.")
    try:
        stored = read_version(app.gcp, version, quota_project)
    except GcpError as err:
        LOG.warning("OAuth client secret not readable reason=%s", err.kind)
        stored = None
    if (stored or "").strip() != client_secret:
        _retire(app, secret, version, quota_project)
        raise ApiError("upstream_error", "The client secret could not be read back from Secret Manager. Run the "
                                         "installer again (scripts/deploy.sh) and connect again.")
    when = now_iso()

    def apply(state):
        state["oauth"] = {"clientId": client_id, "secretVersion": version, "updatedAt": when, "updatedBy": actor}
    try:
        app.settings.mutate_auth(apply, actor)
    except ApiError:
        _retire(app, secret, version, quota_project)
        raise
    LOG.info("OAuth client connected version=%s", version.rpartition("/")[2])
    if previous and previous != version:
        _retire(app, secret, previous, quota_project)
    return version


def disconnect(app, quota_project, actor):
    """Goes back to the IAP sign-in bridge (or ID/password only) and destroys the client secret version it used."""
    auth = getattr(app, "auth", None)
    if auth is not None and auth.client_id and auth.client_secret:
        raise ApiError("conflict", "The OAuth client is set by the deployment (GOOGLE_OAUTH_CLIENT_ID) and cannot "
                                   "be changed here.")
    previous = ((auth.connected_oauth() if auth is not None else None) or {}).get("secretVersion")

    def apply(state):
        state.pop("oauth", None)
    app.settings.mutate_auth(apply, actor)
    LOG.info("OAuth client disconnected")
    _retire(app, secret_resource(app), previous, quota_project)
