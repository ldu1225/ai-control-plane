"""AI Control Plane HTTP application: routing, auth, CSRF, rate limiting, caching, masking, startup."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import urllib.parse

from . import __version__, config, timeutil
from .ai import AiEngine
from .auth import GoogleSignIn, SignInBridge, safe_next
from .cache import TTLCache
from .gcp import Gcp, GcpError
from .httpkit import ApiError, BaseHandler, Router, Server, StaticSite, csrf_valid, envelope, now_iso, \
    parse_json_object, parse_query
from .schema import SchemaRegistry
from .security import GUEST_MODE, GUEST_READ_ONLY_MESSAGE, Identity, IapVerifier, RateLimiter, authenticate, \
    guest_scrub, is_loopback_host, mask_for_viewer, resolve_auth_mode, scheduler_identity
from .settings_store import AuxDocStore, SettingsManager, UserDocStore, make_store
from .services import base as svc_base
from .services import access
from .services import oauth_client
from .services import agent_activity
from .services import agent_detail
from .services import agent_gateway
from .services import (agents, analyst, finops, gateway, gemini_enterprise, governance, layouts, maintenance,
                       model_armor, models, notebooklm, overview, pricing_sync, system, telemetry, telemetry_routing,
                       users)
from .services import budgets, chargeback, org_model, scope, spend_caps
from .services import usage_events

LOG = logging.getLogger("controlplane.app")

WEB_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web"))
RANGE_PARAMS = {"start", "end", "granularity"}
ENFORCE_PATH = "/api/v1/governance/antigravity/enforce"

ROUTER = Router()


def _route(method, path, fn, **opts):
    opts.setdefault("mask", {})
    ROUTER.route(method, path, **opts)(fn)


# System
_route("GET", "/api/v1/bootstrap", system.bootstrap, handler=True, mask=False)
_route("GET", "/api/v1/session", system.session, handler=True, mask=False)
# Sign-in (Google / ID-Password) and account access
_route("GET", "/api/v1/auth/state", system.auth_state, handler=True, public=True, mask=False)
_route("POST", "/api/v1/auth/login", system.password_login, handler=True, public=True, rate="login", mask=False)
_route("POST", "/api/v1/auth/logout", system.logout, handler=True, mask=False)
_route("GET", "/api/v1/access", access.summary, admin_read=True, mask=False)
_route("POST", "/api/v1/access/users", access.save_user, admin=True, mask=False)
_route("POST", "/api/v1/access/users/delete", access.delete_user, admin=True, mask=False)
_route("POST", "/api/v1/access/requests/dismiss", access.dismiss_request, admin=True, mask=False)
_route("POST", "/api/v1/access/iap", access.allow_iap, admin=True, mask=False)
_route("POST", "/api/v1/access/iap/sync", access.sync_iap, admin=True, mask=False)
_route("POST", "/api/v1/access/oauth", access.connect_oauth, admin=True, handler=True, mask=False)
_route("POST", "/api/v1/access/oauth/delete", access.disconnect_oauth, admin=True, mask=False)
_route("GET", "/api/v1/health", system.health)
# Viewers get the read-only settings view (deployment identifiers hidden, storage details reduced).
_route("GET", "/api/v1/settings", system.settings_get, mask=False, viewer_view=system.settings_viewer_view)
_route("POST", "/api/v1/settings", system.settings_post, admin=True, mask=False)
_route("POST", "/api/v1/settings/reset", system.settings_reset, admin=True, mask=False)
_route("GET", "/api/v1/ai/models", system.ai_models)
_route("POST", "/api/v1/ai/test", system.ai_test, admin=True, rate="ai")
# Personal widget layouts (any signed-in person, own layout only; never cached across users)
_route("GET", "/api/v1/layouts", layouts.get_layouts, mask=False)
_route("POST", "/api/v1/layouts", layouts.save_layout, mask=False)
# Modules
_route("GET", "/api/v1/overview/summary", overview.summary, module="overview", range=True, cache=True)
_route("GET", "/api/v1/gemini-enterprise/summary", gemini_enterprise.summary, module="gemini_enterprise", range=True,
       cache=True)
_route("GET", "/api/v1/gemini-enterprise/licenses", gemini_enterprise.licenses, module="gemini_enterprise", cache=True)
_route("GET", "/api/v1/gemini-enterprise/file-uploads", gemini_enterprise.file_uploads, module="gemini_enterprise",
       range=True, cache=True, mask={"restrict": ("fileName", "fileId")})
_route("GET", "/api/v1/notebooklm/summary", notebooklm.summary, module="notebooklm", range=True, cache=True)
_route("GET", "/api/v1/models/usage", models.usage, module="model_usage", range=True, cache=True)
_route("GET", "/api/v1/models/catalog", models.catalog, module="model_usage", cache=True)
_route("GET", "/api/v1/agent-platform/gateway", gateway.summary, module="agent_gateway", range=True, cache=True,
       mask={"ips": ("callerIp",)})
# Agent Gateway (the product): gateways with their authorization policies, extensions, telemetry policies and
# bound workloads; traffic from the gateway request log, access decisions and metrics. The POST routes create,
# update and delete gateways and switch access authorization; each call is written to the governance audit log.
_route("GET", "/api/v1/agent-platform/gateways", agent_gateway.inventory, module="agent_gateway", cache=True, ttl=60)
_route("GET", "/api/v1/agent-platform/gateways/traffic", agent_gateway.traffic, module="agent_gateway", range=True,
       cache=True)
_route("GET", "/api/v1/agent-platform/gateways/operation", agent_gateway.operation, module="agent_gateway",
       mask=False)
_route("POST", "/api/v1/agent-platform/gateways", agent_gateway.create, module="agent_gateway", admin=True,
       rate="governance", mask=False)
_route("POST", "/api/v1/agent-platform/gateways/update", agent_gateway.update, module="agent_gateway", admin=True,
       rate="governance", mask=False)
_route("POST", "/api/v1/agent-platform/gateways/delete", agent_gateway.delete, module="agent_gateway", admin=True,
       rate="governance", mask=False)
_route("POST", "/api/v1/agent-platform/gateways/access", agent_gateway.access, module="agent_gateway", admin=True,
       rate="governance", mask=False)
_route("GET", "/api/v1/agent-platform/registry", agents.registry, module="agent_registry", range=True, cache=True)
_route("GET", "/api/v1/agent-platform/deployments", agents.deployments, module="agent_deployments", range=True,
       cache=True)
# One Agent Engine deployment: telemetry, models and tools, revisions, traces, topology, evaluation. Each route
# resolves project, region and engine against the engines the workspace lists (agent_detail.target).
_route("GET", "/api/v1/agent-platform/agent", agent_detail.overview, module="agent_deployments", cache=True, ttl=60)
_route("GET", "/api/v1/agent-platform/agent/telemetry", agent_detail.telemetry, module="agent_deployments",
       range=True, cache=True)
_route("GET", "/api/v1/agent-platform/agent/models", agent_detail.models_tools, module="agent_deployments",
       range=True, cache=True)
_route("GET", "/api/v1/agent-platform/agent/revisions", agent_detail.revisions, module="agent_deployments",
       cache=True, ttl=60)
_route("GET", "/api/v1/agent-platform/agent/traces", agent_detail.traces, module="agent_deployments", range=True,
       cache=True, ttl=30, mask={"restrict": ("userId",)})
_route("GET", "/api/v1/agent-platform/agent/trace", agent_detail.trace, module="agent_deployments", cache=True,
       ttl=300, mask={"restrict": ("userId",)})
_route("GET", "/api/v1/agent-platform/agent/topology", agent_detail.topology, module="agent_deployments",
       range=True, cache=True)
_route("GET", "/api/v1/agent-platform/agent/evaluation", agent_detail.evaluation, module="agent_deployments",
       range=True, cache=True)
# Playground: an administrator sends a message to a deployed agent. It runs the agent (model and tool calls),
# so it is an administrator action with the AI rate limit, and every call is written to the audit log.
_route("POST", "/api/v1/agent-platform/agent/playground", agent_detail.playground, module="agent_deployments",
       admin=True, rate="ai", mask=False)
_route("GET", "/api/v1/model-armor/summary", model_armor.summary, module="model_armor", range=True, cache=True)
_route("GET", "/api/v1/model-armor/events", model_armor.events, module="model_armor", range=True, cache=True,
       mask={"restrict": ("text",)})
_route("GET", "/api/v1/users", users.list_users, module="users", range=True, cache=True)
# Viewers get no access action flags (viewer_view, after the role-blind cache): they cannot act, and the flags tell
# who is an administrator.
_route("GET", "/api/v1/users/detail", users.detail, module="users", range=True, cache=True,
       mask={"restrict": ("text", "fileName", "fileId")}, viewer_view=users.detail_viewer_view)
_route("POST", "/api/v1/users/access-action", users.access_action, module="users", admin=True, rate="governance",
       mask=False)
_route("GET", "/api/v1/governance/antigravity/status", governance.status, module="antigravity", cache=True, ttl=60)
_route("GET", "/api/v1/governance/antigravity/billing-report", governance.billing_report, module="antigravity",
       cache=True, ttl=300)
_route("POST", "/api/v1/governance/antigravity/cap", governance.cap, module="antigravity", admin=True,
       rate="governance")
_route("POST", "/api/v1/governance/antigravity/user-action", governance.user_action, module="antigravity",
       admin=True, rate="governance")
_route("POST", "/api/v1/governance/antigravity/setup", governance.setup, module="antigravity", admin=True,
       rate="governance")
# Per-user caps: scheduled/administrator enforcement runs, manual review decisions, exception rules.
_route("POST", ENFORCE_PATH, governance.enforce, module="antigravity", admin=True, rate="governance",
       scheduler=True)
_route("POST", "/api/v1/governance/antigravity/review", governance.review, module="antigravity", admin=True,
       rate="governance")
_route("POST", "/api/v1/governance/antigravity/exceptions", governance.exception_save, module="antigravity",
       admin=True, rate="governance")
_route("POST", "/api/v1/governance/antigravity/exceptions/delete", governance.exception_delete,
       module="antigravity", admin=True, rate="governance")
# Current and next cap periods for period settings being edited (computation only, no data).
_route("GET", "/api/v1/governance/antigravity/period-preview", governance.period_preview, module="antigravity",
       mask=False)
# Pub/Sub push of every Antigravity inference log entry (real-time enforcement in automatic mode). Only a Google OIDC
# token of the push subscription's service account is accepted (usage_events.handle refuses sessions); the route uses
# its own rate bucket instead of the general one (one call per Antigravity request of every user).
_route("POST", usage_events.PATH, usage_events.handle, module="antigravity", admin=True, scheduler=True,
       general="usage_event", mask=False)
# Enforcement method, deny policy attachment points, enforcement job and spend caps. Viewers and guests get only whether
# spend caps are on (viewer_view, after the cache): budget names carry the billing account id, caps are keyed by project.
# Cached like the status: the IAM policy API allows only 10 policy reads per minute per project by default.
_route("GET", "/api/v1/governance/antigravity/enforcement", governance.enforcement_status, module="antigravity",
       mask={"nulls": ("budget", "etag")}, cache=True, ttl=60, viewer_view=governance.enforcement_viewer_view)
_route("GET", "/api/v1/governance/audit-log", governance.audit_log, module="antigravity", cache=True, ttl=30)
_route("GET", "/api/v1/governance/antigravity/agent-activity", agent_activity.summary, module="antigravity", range=True,
       cache=True)
_route("GET", "/api/v1/finops/summary", finops.summary, module="finops", range=True, cache=True)
_route("GET", "/api/v1/finops/pricing", pricing_sync.get_pricing, module="finops", cache=True, ttl=60)
_route("POST", "/api/v1/finops/pricing/sync", pricing_sync.trigger_sync, module="finops", admin=True,
       rate="governance", scheduler=True)
_route("POST", "/api/v1/finops/pricing/ack", pricing_sync.acknowledge_alerts, module="finops", admin=True,
       rate="governance")
# guest=True: one of the few POST routes a guest may call (read-only AI analysis, rate limited).
_route("POST", "/api/v1/finops/spikes/analyze", finops.analyze_spike, module="finops", rate="ai", guest=True)
# Workspace scope: projects under the configured folder or organization (or the listed projects).
_route("GET", "/api/v1/scope/projects", scope.scope_projects, cache=True, ttl=300, mask={"restrict": ("labels",)})
# Organization chargeback: the organization tree from an HR CSV (dry run, then apply), usage cost by person, project
# and organization unit, and the month close. The CSV import is the only route with a body above 64 KB.
_route("GET", "/api/v1/org/tree", org_model.tree, module="chargeback", cache=True, ttl=60)
_route("POST", "/api/v1/org/import", org_model.import_csv, module="chargeback", admin=True, rate="governance",
       max_body=8 * 1024 * 1024)
_route("GET", "/api/v1/chargeback/summary", chargeback.summary, module="chargeback", cache=True, ttl=300)
_route("GET", "/api/v1/chargeback/report", chargeback.report, module="chargeback", cache=True, ttl=300)
_route("POST", "/api/v1/chargeback/close", chargeback.close, module="chargeback", admin=True, rate="governance")
# Monthly budgets of organization units and projects (5,000 budgets are about 850 KB) and the Google Cloud spend caps of
# hard-capped projects. Spend cap budget names carry the billing account id, so only administrators read them.
_route("GET", "/api/v1/budgets", budgets.get_budgets, module="chargeback", cache=True, ttl=60)
_route("POST", "/api/v1/budgets", budgets.save_budgets, module="chargeback", admin=True, rate="governance",
       max_body=2 * 1024 * 1024)
_route("GET", "/api/v1/budgets/spend-caps", spend_caps.get_spend_caps, module="chargeback", admin_read=True)
_route("POST", "/api/v1/budgets/spend-caps/apply", spend_caps.apply_spend_caps, module="chargeback", admin=True,
       rate="governance")
_route("POST", "/api/v1/budgets/spend-caps/lift", spend_caps.lift_spend_cap, module="chargeback", admin=True,
       rate="governance")
# Scope picker: projects, folders and organizations the service identity can see (administrators).
_route("GET", "/api/v1/scope/candidates", scope.candidates, admin_read=True)
_route("GET", "/api/v1/telemetry/discover", telemetry.discover, module="telemetry")
_route("GET", "/api/v1/telemetry/connectors", telemetry.list_connectors, module="telemetry", cache=True, ttl=60)
_route("POST", "/api/v1/telemetry/connectors", telemetry.save_connector, module="telemetry", admin=True)
_route("POST", "/api/v1/telemetry/connectors/delete", telemetry.delete_connector, module="telemetry", admin=True)
_route("POST", "/api/v1/telemetry/sources/override", telemetry.save_override, module="telemetry", admin=True)
_route("POST", "/api/v1/telemetry/profiles/refresh", telemetry.refresh_profiles, module="telemetry", admin=True,
       rate="ai")
_route("POST", "/api/v1/telemetry/groups/dashboard", telemetry.group_dashboard, module="telemetry", admin=True)
# Log Router -> BigQuery routes with cost controls (sampling, severity, exclusions, retention, daily budgets).
_route("POST", "/api/v1/telemetry/sinks", telemetry_routing.legacy_create_sink, module="telemetry", admin=True)
_route("GET", "/api/v1/telemetry/routing", telemetry_routing.get_routing, module="telemetry", cache=True, ttl=30)
_route("POST", "/api/v1/telemetry/routing/estimate", telemetry_routing.estimate_route, module="telemetry", admin=True)
_route("POST", "/api/v1/telemetry/routing/routes", telemetry_routing.save_route, module="telemetry", admin=True,
       rate="governance")
_route("POST", "/api/v1/telemetry/routing/routes/action", telemetry_routing.route_action, module="telemetry",
       admin=True, rate="governance")
_route("POST", "/api/v1/telemetry/routing/enforce", telemetry_routing.enforce, module="telemetry", admin=True,
       rate="governance", scheduler=True)
_route("GET", "/api/v1/telemetry/connectors/data", telemetry.connector_data, module="telemetry", range=True,
       cache=True, mask={"nulls": ("payload", "message")})
# Scheduled maintenance tick: daily price check at the configured time, route budget guard, route upkeep.
_route("POST", "/api/v1/system/maintenance", maintenance.run, admin=True, rate="governance", scheduler=True)
# Results can contain free text that the dashboards withhold from viewers: administrators always, viewers
# only when an administrator allows it (analyst.available_to); guests follow the viewer policy.
_route("POST", "/api/v1/analyst/chat", analyst.chat, module="analyst", rate="ai", allow=analyst.available_to,
       deny=analyst.VIEWER_DENIED, guest=True)


class App:
    def __init__(self, environ=None):
        self.environ = os.environ if environ is None else environ
        base, self.loaded_files = config.load_base_settings(self.environ)
        resolve_auth_mode(base["security"]["auth_mode"], self.environ)  # validates early
        _refuse_unprotected_bind(base, self.environ)
        self.gcp = Gcp()
        self.registry = SchemaRegistry(self.gcp)
        self.ai = AiEngine(self.gcp)
        self.cache = TTLCache(1024)
        self.limiter = RateLimiter()
        self.verifier = IapVerifier()
        self.site = StaticSite(WEB_ROOT, __version__)
        self.router = ROUTER
        self.settings = SettingsManager(base, make_store(base, self.gcp, self.environ))
        self.user_docs = UserDocStore(self.settings.store)
        self.aux_docs = AuxDocStore(self.settings.store)
        mode = resolve_auth_mode(base["security"]["auth_mode"], self.environ)
        self.auth = None
        if mode == "google":
            quota_project = config.quota_project(base) or ""
            self.auth = GoogleSignIn(base, self.environ, self.settings,
                                     secret_reader=lambda version: oauth_client.read_version(self.gcp, version,
                                                                                             quota_project))
        self.bridge = None
        if base["security"].get("bridge_return_url"):
            # Sign-in bridge service: only /auth/bridge behind Identity-Aware Proxy.
            if mode != "iap":
                raise ValueError("security.bridge_return_url (BRIDGE_RETURN_URL) requires auth_mode=iap")
            self.bridge = SignInBridge(base, self.environ)

    def on_settings_changed(self):
        self.cache.clear()

    def invalidate_governance(self):
        self.cache.clear()


def _cache_key(path, params, rng, version):
    rest = tuple(sorted((k, v) for k, v in params.items() if k not in RANGE_PARAMS))
    window = (rng.start.isoformat(), rng.end.isoformat(), rng.granularity, rng.tz_name) if rng else None
    return ("route", path, window, rest, version)


class ControlPlaneHandler(BaseHandler):
    app = None

    def handle_api(self, method, path, query):
        app = self.app
        if app.bridge is not None:
            raise ApiError("not_found", "Endpoint not found.")
        # Routes with a larger body limit (max_body) read the body only after authentication, CSRF and the
        # administrator check; their connection is not reused, so a rejection never leaves unread bytes behind.
        hit = app.router.routes.get((method, path)) if method == "POST" else None
        big = int((hit[1] if hit else {}).get("max_body") or 0)
        if big:
            self.close_connection = True
        raw = self.read_body_bytes() if (method == "POST" and not big) else b""
        fn, opts = app.router.resolve(method, path)
        app.settings.maybe_reload()
        settings, version = app.settings.snapshot()
        bearer = opts.get("scheduler") and (self.headers.get("Authorization") or "")[:7].lower() == "bearer "
        if bearer:
            # Cloud Scheduler: Google-signed OIDC token of an allowed service account (no cookies).
            identity = scheduler_identity(self.headers, settings, app.verifier, path)
        elif opts.get("public"):
            mode = resolve_auth_mode(settings["security"]["auth_mode"], app.environ)
            identity = Identity("anonymous", "viewer", mode, "anon:%s" % self.client_ip())
        else:
            identity = authenticate(self.headers, self.client_ip(), settings, app.verifier, app.environ,
                                    sessions=app.auth, settings_mgr=app.settings)
        guest = identity.auth_mode == GUEST_MODE
        app.limiter.check(identity.rate_key, (opts.get("general") if bearer else None) or "general")
        if opts.get("admin_read") and not identity.is_admin:
            raise ApiError("forbidden", "Administrator privileges are required to view or manage access settings. "
                                        "Grant Administrator permissions to this account to proceed.")
        body = None
        if method == "POST":
            if guest and not opts.get("guest"):
                raise ApiError("forbidden", GUEST_READ_ONLY_MESSAGE)
            if not bearer and not csrf_valid(self.headers, self.is_https):
                raise ApiError("csrf_failed", "CSRF validation failed. Reload the page and try again.")
            if opts.get("admin") and not identity.can_write:
                raise ApiError("forbidden", "Administrator privileges are required to change settings or run controls. "
                                            "Grant the required Administrator / IAM permissions to proceed.")
            if big:
                if opts.get("rate"):
                    app.limiter.check(identity.rate_key, opts["rate"])
                if opts.get("module") and not settings["modules"].get(opts["module"], False):
                    raise ApiError("module_disabled", "This module is disabled in the workspace settings.")
                raw = self.read_body_bytes(limit=big)
            body = parse_json_object(raw)
        if opts.get("rate") and not big:
            app.limiter.check(identity.rate_key, opts["rate"])
        module = opts.get("module")
        if module:
            if not settings["modules"].get(module, False):
                raise ApiError("module_disabled", "This module is disabled in the workspace settings.")
            if not config.workspace_configured(settings):
                raise ApiError("not_configured", "The workspace is not configured. Set workspace.project_id first.")
        allow = opts.get("allow")
        if allow is not None and not allow(identity, settings):
            raise ApiError("forbidden", opts.get("deny") or "This feature is not available to your role.")
        if guest and opts.get("rate") == "ai":
            app.limiter.check("guest:all", "guest_ai")
        params = parse_query(query)
        rng = None
        if opts.get("range"):
            rng = timeutil.parse_range(params, settings["workspace"]["timezone"],
                                       settings["workspace"]["default_range_days"])
        ctx = svc_base.Ctx(app, settings, version, identity, params, body, rng)
        extra_headers = []
        cached = False
        generated = None
        try:
            if opts.get("cache") and method == "GET":
                ttl = opts.get("ttl") or int(settings["server"].get("cache_ttl_seconds") or 0)

                def compute():
                    value = fn(ctx)
                    return {"data": value, "warnings": list(ctx.warnings), "sources": sorted(ctx.sources),
                            "generatedAt": now_iso()}
                result, cached = app.cache.get_or_compute(
                    _cache_key(path, params, rng, version), ttl, compute,
                    cacheable=lambda r: not any(w["code"] in svc_base.TRANSIENT_CODES for w in r["warnings"]))
                data, warnings, sources, generated = (result["data"], result["warnings"], result["sources"],
                                                      result["generatedAt"])
            else:
                out = fn(ctx, self) if opts.get("handler") else fn(ctx)
                if isinstance(out, tuple):
                    out, extra_headers = out
                data, warnings, sources = out, ctx.warnings, sorted(ctx.sources)
        except GcpError as err:
            LOG.warning("unhandled upstream error path=%s kind=%s", path, err.kind)
            if err.kind == "credentials":
                raise ApiError("upstream_error", "Google Cloud credentials are unavailable.")
            raise ApiError("upstream_error", "An upstream Google Cloud service is unavailable.")
        viewer_view = opts.get("viewer_view")
        if viewer_view is not None and identity.role != "admin":
            data = viewer_view(data)
        mask = opts.get("mask")
        if mask is not False and identity.role != "admin" and \
                (guest or settings["security"].get("mask_pii_for_viewers", False)):
            data = mask_for_viewer(data, restrict_keys=mask.get("restrict", ()),
                                   ip_keys=mask.get("ips", ("callerIp",)), null_keys=mask.get("nulls", ()))
            warnings = mask_for_viewer(warnings)
        if guest:
            # Public demo: identifiers are removed from everything a guest receives, including the routes that
            # opt out of viewer masking (bootstrap, session, settings view).
            terms = settings["security"].get("guest_redact_terms") or ()
            data, warnings, sources = guest_scrub(data, terms), guest_scrub(warnings, terms), guest_scrub(sources, terms)
        payload = envelope(data, cached=cached, range_meta=rng.meta() if rng else None, sources=sources,
                           warnings=warnings, generated_at=generated)
        self.send_json(200, payload, extra_headers)

    # -- server-handled pages: sign-in page, Google sign-in flow and the access gate --------
    def handle_page(self, method, path, query):
        app = self.app
        if method != "GET" or path.startswith("/assets/") or path == "/favicon.ico":
            return False
        if app.bridge is not None:
            return self._handle_bridge(path, query)
        canonical = app.auth.canonical_host() if app.auth is not None else None
        if canonical and (self.headers.get("Host") or "").strip().lower() != canonical:
            self.send_redirect(app.auth.public_base_url + path + ("?" + query if query else ""))
            return True
        app.settings.maybe_reload()
        settings, _ = app.settings.snapshot()
        if path in ("/login", "/logout"):
            self.send_template("login.html")
            return True
        if path.startswith("/auth/"):
            allowed, _retry = app.limiter.allow("login:%s" % self.client_ip(), "login")
            if not allowed:
                self.send_redirect("/login?error=rate_limited")
                return True
            if app.auth is None:
                self.send_redirect("/login?error=unavailable")
                return True
            if path == "/auth/google/start":
                location, cookies = app.auth.start(self.headers, query)
            elif path == "/auth/google/callback":
                location, cookies = app.auth.callback(self.headers, query, settings)
            else:
                raise ApiError("not_found", "Page not found.")
            self.send_redirect(location, cookies)
            return True
        if app.auth is not None and not settings["security"].get("guest_access") and \
                app.auth.user_from_headers(self.headers, settings) is None:
            # Every application page requires a session (unless guest access is on); static assets stay public
            # for the sign-in page.
            self.send_redirect("/login?next=" + urllib.parse.quote(safe_next(path), safe="/"))
            return True
        return False

    def _handle_bridge(self, path, query):
        """Sign-in bridge service: IAP has authenticated the caller; return an assertion to the
        dashboard. Every other page goes to the dashboard's sign-in page."""
        app = self.app
        if path != "/auth/bridge":
            self.send_redirect(app.bridge.return_url + "/login")
            return True
        settings, _ = app.settings.snapshot()
        identity = authenticate(self.headers, self.client_ip(), settings, app.verifier, app.environ)
        allowed, _retry = app.limiter.allow("login:%s" % identity.rate_key, "login")
        if not allowed:
            self.send_redirect(app.bridge.return_url + "/login?error=rate_limited")
            return True
        self.send_redirect(app.bridge.redirect_for(identity.email, query))
        return True


def _refuse_unprotected_bind(base, environ):
    """auth_mode auto means local outside Cloud Run, which suits only a server that this computer alone
    can reach. A shared deployment elsewhere (a VM, GKE, a container bound to 0.0.0.0) must choose
    google or iap; an explicit AUTH_MODE=local stays possible for development."""
    configured = (base["security"].get("auth_mode") or "auto").strip().lower()
    if configured == "auto" and resolve_auth_mode(configured, environ) == "local" and \
            not is_loopback_host(base["server"].get("host")):
        raise ValueError("auth_mode auto resolves to local outside Cloud Run, but server.host %s is reachable "
                         "from other computers; set AUTH_MODE=google or iap for a shared deployment, or "
                         "AUTH_MODE=local explicitly for development on one computer" % base["server"].get("host"))


def make_server(app, host, port):
    handler = type("Handler", (ControlPlaneHandler,), {"app": app, "site": app.site})
    return Server((host, port), handler)


def _configure_logging():
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO), stream=sys.stdout,
                        format='level=%(levelname)s module=%(name)s msg="%(message)s"')
    for noisy in ("urllib3", "google.auth", "google.cloud", "google_genai", "httpx", "httpcore", "google.api_core"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main():
    _configure_logging()
    try:
        app = App()
    except ValueError as err:
        LOG.critical("configuration error: %s", err)
        return 2
    settings, _ = app.settings.snapshot()
    host = settings["server"]["host"]
    port = int(settings["server"]["port"])
    mode = resolve_auth_mode(settings["security"]["auth_mode"])
    persistence = app.settings.persistence()
    LOG.info("AI Control Plane %s starting host=%s port=%d auth_mode=%s project=%s persistence=%s config=%s",
             __version__, host, port, mode, settings["workspace"]["project_id"], persistence["backend"],
             ",".join(app.loaded_files) or "defaults")
    if mode == "local":
        LOG.warning("auth_mode=local: development mode; only loopback clients are accepted (as administrators), "
                    "other clients and proxied requests are refused")
    if mode == "iap" and not settings["security"].get("iap_audience"):
        LOG.warning("auth_mode=iap without security.iap_audience: signature, expiry and issuer are verified but "
                    "the audience is not; set IAP_AUDIENCE")
    if mode == "google":
        LOG.info("auth_mode=google: sign-in page at /login; approved accounts only")
        if settings["security"].get("guest_access"):
            LOG.warning("guest access is on: visitors without a session browse read-only with identifiers hidden "
                        "(%d redaction terms); intended for demos", len(settings["security"].get("guest_redact_terms")
                                                                         or []))
    if not config.workspace_configured(settings):
        LOG.warning("workspace.project_id is not configured; data endpoints return not_configured")
    if not persistence["durable"]:
        LOG.warning("settings persistence backend=%s is not durable", persistence["backend"])
    if not os.path.isfile(os.path.join(WEB_ROOT, "index.html")):
        LOG.warning("web frontend not found at src/web/index.html")
    monitor = maintenance.MaintenanceMonitor(app)
    monitor.start()
    server = make_server(app, host, port)

    def _stop(*_args):
        monitor.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()
    try:
        signal.signal(signal.SIGTERM, _stop)
    except ValueError:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop()
        server.server_close()
        LOG.info("server stopped")
    return 0
