"""System endpoints: bootstrap, session, health checks, settings and AI engine."""

from __future__ import annotations

import logging
import os
import time

from .. import __version__, config, timeutil
from ..gcp import GcpError
from ..httpkit import ApiError, issue_csrf_token
from ..security import GUEST_MODE, admin_guard, is_admin_email, mask_for_viewer, resolve_auth_mode
from .base import AUDIT_DATA_ACCESS, describe_error
from . import analyst

LOG = logging.getLogger("controlplane.system")

HEALTH_TTL = 300


def session_object(ctx, handler):
    token, cookie = issue_csrf_token(handler.headers, handler.is_https)
    identity = ctx.identity
    masked = identity.role != "admin" and (identity.auth_mode == GUEST_MODE or
                                           bool(ctx.settings["security"].get("mask_pii_for_viewers", False)))
    return {"email": identity.email, "role": identity.role, "authMode": identity.auth_mode, "csrfToken": token,
            "canWrite": identity.can_write, "piiMasked": masked, "name": identity.name,
            "picture": identity.picture}, cookie


def bootstrap(ctx, handler):
    s = ctx.settings
    session, cookie = session_object(ctx, handler)
    ws = s["workspace"]
    persistence = ctx.app.settings.persistence()
    data = {
        "product": {"name": s["product"]["name"], "tagline": s["product"]["tagline"], "version": __version__},
        "branding": {"organizationName": s["branding"]["organization_name"], "logoUrl": s["branding"]["logo_url"],
                     "accentColor": s["branding"]["accent_color"], "defaultLocale": s["branding"]["default_locale"],
                     "defaultTheme": s["branding"]["default_theme"]},
        "workspace": {"configured": config.workspace_configured(s), "projectId": ws["project_id"],
                      "projects": list(getattr(ctx, "all_projects", None) or getattr(ctx, "projects", None) or [ws["project_id"]]),
                      "scanAllProjects": bool(ws.get("scan_all_projects", True)),
                      "auditDataset": ws["audit_dataset"],
                      # The billing account ID is an administrator detail; viewers do not receive it.
                      "billing": {"projectId": config.billing_project(s), "dataset": ws["billing"]["dataset"],
                                  "table": ws["billing"]["table"],
                                  "accountId": ws["billing"]["account_id"] if ctx.identity.is_admin else ""},
                      "timezone": ws["timezone"], "defaultRangeDays": ws["default_range_days"],
                      "today": timeutil.today_in(ws["timezone"]).isoformat()},
        # AI Analyst is shown to viewers only when they may use it (analyst.available_to).
        "modules": [{"id": m, "enabled": bool(s["modules"].get(m, False)) and
                     (m != "analyst" or analyst.available_to(ctx.identity, s))} for m in config.MODULE_IDS],
        "aiEngine": {"defaultModel": s["ai_engine"]["default_model"], "models": list(s["ai_engine"]["models"]),
                     "location": s["ai_engine"]["location"]},
        "session": session,
        "persistence": {"backend": persistence["backend"], "durable": persistence["durable"]},
        "deployment": {"service": os.environ.get("K_SERVICE", ""), "revision": os.environ.get("K_REVISION", ""),
                       "region": ""},
    }
    return data, [("Set-Cookie", cookie)]


def auth_state(ctx, handler):
    """GET /api/v1/auth/state (public): branding and sign-in state for the sign-in page."""
    s = ctx.settings
    app = ctx.app
    mode = resolve_auth_mode(s["security"]["auth_mode"], app.environ)
    token, cookie = issue_csrf_token(handler.headers, handler.is_https)
    user = None
    denied = None
    sign_in = getattr(app, "auth", None)
    if mode == "google" and sign_in is not None:
        claims = sign_in.user_from_headers(handler.headers, s)
        if claims:
            role = "admin" if (claims["email"] == "admin" or claims.get("role") == "admin" or
                               is_admin_email(claims["email"], s["security"].get("admins"))) else "viewer"
            user = {"email": claims["email"], "name": claims.get("name") or "", "picture": claims.get("picture") or "",
                    "role": role}
        else:
            denied = sign_in.denied_email(handler.headers)
    data = {
        "product": {"name": s["product"]["name"], "tagline": s["product"]["tagline"], "version": __version__},
        "branding": {"organizationName": s["branding"]["organization_name"], "logoUrl": s["branding"]["logo_url"],
                     "accentColor": s["branding"]["accent_color"], "defaultLocale": s["branding"]["default_locale"],
                     "defaultTheme": s["branding"]["default_theme"]},
        "authMode": mode,
        "googleEnabled": bool(mode == "google" and sign_in is not None),
        "passwordEnabled": bool(mode == "google" and sign_in is not None),
        "defaultAdminPassword": bool(mode == "google" and sign_in is not None and
                                     sign_in.default_admin_password_active()),
        "guestAccess": bool(mode == "google" and s["security"].get("guest_access")),
        "switchAccountUrl": sign_in.switch_account_url() if mode == "google" and sign_in is not None else None,
        "user": user,
        "deniedEmail": denied,
        "csrfToken": token,
    }
    return data, [("Set-Cookie", cookie)]


def password_login(ctx, handler):
    """POST /api/v1/auth/login (public): sign in with an account ID/email and password."""
    s = ctx.settings
    app = ctx.app
    mode = resolve_auth_mode(s["security"]["auth_mode"], app.environ)
    sign_in = getattr(app, "auth", None)
    if mode != "google" or sign_in is None:
        raise ApiError("bad_request", "Password sign-in is not enabled on this deployment.")
    body = ctx.body or {}
    username = body.get("username") if "username" in body else body.get("email")
    password = body.get("password")
    next_path = body.get("next") or "/"
    user, redirect_to, cookies = sign_in.password_login(username, password, next_path, handler.headers, s)
    return {"user": user, "redirect": redirect_to}, [("Set-Cookie", c) for c in cookies]


def logout(ctx, handler):
    """POST /api/v1/auth/logout: ends every session of the signed-in account (Google sign-in)."""
    mode = ctx.identity.auth_mode
    sign_in = getattr(ctx.app, "auth", None)
    if mode == "google" and sign_in is not None:
        cookies = sign_in.logout(ctx.identity, handler.headers)
        return {"redirect": "/login?signed_out=1"}, [("Set-Cookie", c) for c in cookies]
    if mode == "iap":
        return {"redirect": "/_gcp_iap/clear_login_cookie"}, []
    return {"redirect": "/"}, []


def session(ctx, handler):
    obj, cookie = session_object(ctx, handler)
    return {"session": obj}, [("Set-Cookie", cookie)]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def _check(check_id, label, fn):
    start = time.monotonic()
    try:
        status, detail = fn()
    except GcpError as err:
        code, message = describe_error(err, label)
        status, detail = "error", message
    except ApiError as err:
        status, detail = "error", err.message
    except Exception:  # pylint: disable=broad-except
        LOG.exception("health check failed id=%s", check_id)
        status, detail = "error", "The check failed unexpectedly."
    return {"id": check_id, "label": label, "status": status, "detail": detail,
            "latencyMs": int((time.monotonic() - start) * 1000)}


def run_health(ctx):
    s = ctx.settings
    configured = config.workspace_configured(s)
    unconfigured = ("unconfigured", "The workspace project is not configured.")

    def bq_audit():
        if not configured:
            return unconfigured
        groups = ctx.registry.tables(ctx.project, ctx.audit_dataset, ctx.quota_project, ctx.max_bytes)
        core = [t for t in groups if t.startswith(("cloudaudit_", "discoveryengine_", "modelarmor_", "businessaicode_"))]
        return ("ok" if groups else "warning"), "%d tables found (%d core log tables)" % (len(groups), len(core))

    def bq_billing():
        if not config.billing_configured(s):
            return "unconfigured", "The billing export table is not configured."
        b = s["workspace"]["billing"]
        meta = ctx.registry.schema(config.billing_project(s), b["dataset"], b["table"], ctx.quota_project)
        schema = meta["schema"]
        if not (schema.has("usage_start_time") and schema.has("cost") and schema.has("sku.description")):
            return "warning", "The table does not look like a billing export."
        detail = "Billing export table found"
        if not schema.has("resource.name"):
            detail += " (standard export; resource-level runtime cost unavailable)"
        return "ok", detail

    def discovery():
        if not configured:
            return unconfigured
        url = "https://discoveryengine.googleapis.com/v1alpha/projects/%s/locations/global/licenseConfigs" % ctx.project
        data = ctx.rest("GET", url)
        return "ok", "%d license configuration(s) found" % len(data.get("licenseConfigs") or [])

    def agent_engine():
        if not configured:
            return unconfigured
        regions = [r for r in s["workspace"].get("agent_engine_regions") or [] if config.LOCATION_RE.match(r or "")]
        if not regions:
            return "unconfigured", "No Agent Engine regions are configured."
        region = regions[0]
        url = "https://%s-aiplatform.googleapis.com/v1/projects/%s/locations/%s/reasoningEngines" % (
            region, ctx.project, region)
        ctx.rest("GET", url, params={"pageSize": 1})
        return "ok", "Reachable in %s" % region

    def monitoring():
        if not configured:
            return unconfigured
        url = "https://monitoring.googleapis.com/v3/projects/%s/metricDescriptors" % ctx.project
        data = ctx.rest("GET", url, params={"filter": 'metric.type = starts_with("aiplatform.googleapis.com/publisher")',
                                            "pageSize": 1})
        return "ok", ("Agent Platform publisher metrics available" if data.get("metricDescriptors")
                      else "Reachable; no Agent Platform publisher metrics yet")

    def logging_check():
        if not configured:
            return unconfigured
        url = "https://logging.googleapis.com/v2/projects/%s/logs" % ctx.project
        ctx.rest("GET", url, params={"pageSize": 1})
        return "ok", "Log list readable"

    def iam():
        if not configured:
            return unconfigured
        from .governance import get_policy, permission_status
        get_policy(ctx)
        perms = permission_status(ctx)
        missing = [p for p, ok in perms.items() if ok is False]
        if missing:
            return "warning", "Policy readable; missing: %s" % ", ".join(missing)
        return "ok", "Policy readable"

    def ai_engine():
        if not configured:
            return unconfigured
        model = s["ai_engine"]["default_model"]
        loc = s["ai_engine"].get("location") or "global"
        client = ctx.app.ai.client(ctx.project, loc, ctx.quota_project)
        try:
            client.models.get(model=model, config={"http_options": {"timeout": 12000}})
        except TypeError:
            try:
                client.models.get(model=model)
            except Exception as err:  # pylint: disable=broad-except
                status = getattr(err, "code", None) or getattr(err, "status_code", None)
                LOG.info("ai engine health check failed status=%s", status)
                if status in (401, 403):
                    return "error", "Access to the AI engine was denied for the service identity."
                if status == 404:
                    return "error", "The default model %s is not available in %s." % (model, s["ai_engine"]["location"])
                return "error", "The AI engine is temporarily unavailable."
        except Exception as err:  # pylint: disable=broad-except
            status = getattr(err, "code", None) or getattr(err, "status_code", None)
            LOG.info("ai engine health check failed status=%s", status)
            if status in (401, 403):
                return "error", "Access to the AI engine was denied for the service identity."
            if status == 404:
                return "error", "The default model %s is not available in %s." % (model, s["ai_engine"]["location"])
            return "error", "The AI engine is temporarily unavailable."
        return "ok", "%s reachable in %s" % (model, loc)

    def store():
        p = ctx.app.settings.persistence()
        if ctx.app.settings.load_error:
            return "error", "The settings store could not be read."
        if p["backend"] == "memory":
            return "warning", "Settings are kept in memory and are lost on restart."
        if not p["durable"]:
            return "warning", "Settings are stored on an ephemeral file system."
        return "ok", "%s (%s)" % ({"gcs": "Cloud Storage", "file": "Local file"}.get(p["backend"], p["backend"]),
                                  "durable" if p["durable"] else "ephemeral")

    checks = [("bigquery_audit", "BigQuery audit dataset", bq_audit),
              ("bigquery_billing", "BigQuery billing export", bq_billing),
              ("discovery_engine", "Discovery Engine API", discovery),
              ("agent_engine", "Agent Engine (Agent Platform)", agent_engine),
              ("cloud_monitoring", "Cloud Monitoring", monitoring),
              ("cloud_logging", "Cloud Logging", logging_check),
              ("iam_policy", "Project IAM policy", iam),
              ("ai_engine", "AI engine", ai_engine),
              ("settings_store", "Settings store", store)]
    res = ctx.run({cid: (lambda c=(cid, label, fn): _check(*c)) for cid, label, fn in checks})
    out = []
    for cid, label, _fn in checks:
        ok, value = res[cid]
        out.append(value if ok else {"id": cid, "label": label, "status": "error",
                                     "detail": "The check failed unexpectedly.", "latencyMs": 0})
    return {"checks": out}


def health(ctx):
    refresh = ctx.params.get("refresh") == "1"
    key = ("health", ctx.version)
    if refresh:
        ctx.app.limiter.check(ctx.identity.rate_key, "refresh")
        ctx.app.cache.delete(key)
    value, cached = ctx.app.cache.get_or_compute(key, HEALTH_TTL, lambda: run_health(ctx))
    return value


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def settings_payload(ctx):
    """Full settings payload. GET /api/v1/settings declares settings_viewer_view for viewers in the registry;
    the write routes that also return this payload are administrator-only."""
    effective, _ = ctx.app.settings.snapshot()
    return {"effective": effective, "overrides": ctx.app.settings.overrides(),
            "persistence": ctx.app.settings.persistence(), "editable": ctx.identity.can_write}


def settings_viewer_view(payload):
    """Read-only settings view for viewers: the same masking for effective settings and for overrides (a value
    saved in the UI must not bypass it), and storage details reduced to whether settings persist (no bucket or
    object name, no administrator e-mail)."""
    persistence = payload.get("persistence") or {}
    return {"effective": mask_for_viewer(config.mask_settings_for_viewer(payload.get("effective") or {})),
            "overrides": mask_for_viewer(config.mask_settings_for_viewer(payload.get("overrides") or {})),
            "persistence": {"backend": persistence.get("backend"), "durable": persistence.get("durable"),
                            "updatedAt": persistence.get("updatedAt")},
            "editable": False}


def settings_get(ctx):
    return settings_payload(ctx)


def settings_post(ctx):
    section = ctx.body.get("section")
    values = ctx.body.get("values")
    ctx.app.settings.update_section(section, values, ctx.identity.email, check=admin_guard(ctx.identity))
    ctx.app.on_settings_changed()
    LOG.info("settings updated section=%s", section if section in config.EDITABLE_SECTIONS else "invalid")
    return settings_payload(ctx)


def settings_reset(ctx):
    ctx.app.settings.reset(ctx.body.get("section"), ctx.body.get("keys"), ctx.identity.email,
                           check=admin_guard(ctx.identity))
    ctx.app.on_settings_changed()
    return settings_payload(ctx)


# ---------------------------------------------------------------------------
# AI engine
# ---------------------------------------------------------------------------

def ai_models(ctx):
    key = ("ai_models", ctx.version)

    def load():
        models, status = ctx.app.ai.catalog(ctx.settings)
        return {"defaultModel": ctx.settings["ai_engine"]["default_model"], "models": models,
                "discoveryStatus": status}
    value, _ = ctx.app.cache.get_or_compute(key, 3600, load,
                                            cacheable=lambda v: v["discoveryStatus"] != "failed")
    return value


def ai_test(ctx):
    if not config.workspace_configured(ctx.settings):
        raise ApiError("not_configured", "The workspace project is not configured.")
    model = ctx.app.ai.resolve_model(ctx.settings, ctx.body.get("model"))
    try:
        latency = ctx.app.ai.ping(ctx.settings, model)
        return {"model": model, "ok": True, "latencyMs": latency, "message": "The model responded successfully."}
    except GcpError as err:
        _code, message = describe_error(err, "The model %s" % model)
        return {"model": model, "ok": False, "latencyMs": None, "message": message}
