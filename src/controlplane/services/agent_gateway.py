"""Agent Gateway (Gemini Enterprise Agent Platform): inventory, governance posture, traffic and management.

Agent Gateway is the policy enforcement point for agent traffic: Client-to-Agent (ingress) and
Agent-to-Anywhere (egress). This module reads the gateways and everything that governs them from the Google
Cloud APIs, and lets an administrator create, update and delete gateways and switch access authorization
between audit-only and enforced:

- Network Services API v1: agentGateways, authzExtensions, long-running operations
- Network Services API v1beta1: telemetryPolicies (tracing, Preview)
- Network Security API v1: authzPolicies
- Agent Platform API (reasoningEngines) and Discovery Engine API (engines): the workloads bound to a gateway
- Cloud Logging: gateway request log, Identity-Aware Proxy decisions, policy engine verdicts, Log Router sinks
- Cloud Monitoring: request counts, latency and authorization extension metrics

Upstream error text is never returned to clients: failures become data source notices or curated messages.
services/gateway.py keeps the older view of AI API calls recorded in Cloud Audit Logs.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import random
import re
import time
import urllib.parse

from .. import config, timeutil
from ..gcp import GcpError, log_safe
from ..httpkit import ApiError
from . import agents, governance
from .base import discovery_host, discovery_locations, iso, parse_rfc3339, ratio, require_identity_admin, to_int

LOG = logging.getLogger("controlplane.agent_gateway")

NETWORK_SERVICES = "https://networkservices.googleapis.com"
NETWORK_SECURITY = "https://networksecurity.googleapis.com"
LOGGING_V2 = "https://logging.googleapis.com/v2"
MONITORING_V3 = "https://monitoring.googleapis.com/v3"
API_HOSTS = {"networkservices": NETWORK_SERVICES, "networksecurity": NETWORK_SECURITY}

SRC_GATEWAYS = "api:networkservices.agentGateways"
SRC_EXTENSIONS = "api:networkservices.authzExtensions"
SRC_POLICIES = "api:networksecurity.authzPolicies"
SRC_TELEMETRY = "api:networkservices.telemetryPolicies"
SRC_SINKS = "api:logging.sinks"
SRC_ENGINES = "api:discoveryengine.engines"
SRC_REQUEST_LOG = "logging:networkservices.googleapis.com/gateway_requests"
SRC_IAP_LOG = "logging:cloudaudit.googleapis.com/data_access"
SRC_SGP_LOG = "logging:semantic-governance-policy"
SRC_ARMOR_LOG = "logging:modelarmor.googleapis.com/sanitize_operations"
SRC_METRICS = "monitoring:networkservices.googleapis.com/https/request_count"
SRC_EXTENSION_METRICS = "monitoring:networkservices.googleapis.com/extension/invocation_count"

REQUEST_COUNT = "networkservices.googleapis.com/https/request_count"
TOTAL_LATENCIES = "networkservices.googleapis.com/https/total_latencies"
GATEWAY_LATENCIES = "networkservices.googleapis.com/agentgateway/total_latencies"
EXTENSION_INVOCATIONS = "networkservices.googleapis.com/extension/invocation_count"
EXTENSION_FAILED_OPEN = "networkservices.googleapis.com/extension/failed_open_count"
GATEWAY_RESOURCE_TYPE = "networkservices.googleapis.com/Gateway"

ACCESS_PATHS = ("AGENT_TO_ANYWHERE", "CLIENT_TO_AGENT")
IAP_SERVICE = "iap.googleapis.com"
# A Gemini Enterprise app must use a gateway in the region that matches the app location.
GE_GATEWAY_REGION = {"global": "us-central1", "us": "us-central1", "eu": "europe-west1"}
# Documented limits of the product (shown next to the configuration they apply to).
EGRESS_POLICY_LIMIT = 4
REGISTRY_RESOURCE_LIMIT = 5000
# Reasoning engines created before this day cannot be bound to a gateway.
BINDING_CUTOFF = dt.datetime(2026, 4, 29, tzinfo=dt.timezone.utc)

GATEWAY_ID_RE = re.compile(r"^[a-z]([a-z0-9\-]{0,61}[a-z0-9])?$")
# A gateway lives in a region such as us-central1 (not in a multi-region such as "us" or in "global").
REGION_RE = re.compile(r"^[a-z]+(?:-[a-z]+)+[0-9]+$")
GATEWAY_NAME_RE = re.compile(r"^projects/([^/]+)/locations/([^/]+)/agentGateways/([^/]+)$")
REGISTRY_RE = re.compile(r"^//agentregistry\.googleapis\.com/projects/([^/]+)/locations/([^/]+)/?$")
OPERATION_RE = re.compile(r"^projects/([a-z0-9\-\.:]+)/locations/[a-z0-9\-]+/operations/[A-Za-z0-9_.\-]+$")
MODEL_ARMOR_SERVICE_RE = re.compile(r"^modelarmor\.[a-z0-9\-]+\.rep\.googleapis\.com$")
LABEL_KEY_RE = re.compile(r"^[a-z][a-z0-9_\-]{0,62}$")
LABEL_VALUE_RE = re.compile(r"^[a-z0-9_\-]{0,63}$")
TEXT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1, "info": 0}
MAX_REQUEST_ENTRIES = 2000
MAX_DECISION_ENTRIES = 2000
MAX_VERDICT_ENTRIES = 500
MAX_TRAFFIC_PROJECTS = 5
MAX_ROWS = 50
LOG_RETRY_DELAYS = (0.8, 1.8, 3.5)
LOG_READ_DEADLINE_SECONDS = 12.0
# A management request waits this long for Google Cloud to finish; after that the client polls the operation.
OPERATION_WAIT_SECONDS = 20
POLL_SECONDS = 2

MSG_API_DISABLED = "The Network Services API is not enabled in the primary project, so Agent Gateway is not set up."
MSG_LOG_QUOTA = ("Cloud Logging read quota was temporarily reached. Wait a moment and refresh, or choose a shorter "
                 "period.")
MSG_DENIED = ("Google Cloud denied the request. Grant the runtime service account the Agent Gateway management "
              "permissions and check that the Network Services and Network Security APIs are enabled.")
MSG_UNAVAILABLE = "Google Cloud is temporarily unavailable. Try again shortly."
MSG_PROJECT = "Field 'project' must be a project of this workspace."
MSG_IN_USE = ("Agents or Gemini Enterprise apps still route traffic through this gateway. Disassociate them "
              "first.")
MSG_UNVERIFIED = ("Could not verify that no agent or Gemini Enterprise app uses this gateway, so its "
                  "authorization policies were not deleted.")


def _short(name):
    return name.rsplit("/", 1)[-1] if isinstance(name, str) and name else None


def _strings(value):
    return [v for v in value if isinstance(v, str) and v] if isinstance(value, list) else []


def gateway_key(project, location, name):
    return "%s/%s/%s" % (project, location, name)


def scan_regions(ctx):
    """Regions where a gateway can govern this workspace: the Agent Engine regions and the regions that the
    configured Gemini Enterprise locations require."""
    out = []
    for region in ctx.ws.get("agent_engine_regions") or []:
        if isinstance(region, str) and len(region) <= 40 and REGION_RE.match(region) and region not in out:
            out.append(region)
    for location in discovery_locations(ctx):
        region = GE_GATEWAY_REGION.get(location)
        if region and region not in out:
            out.append(region)
    return out


def _projects(ctx):
    return list(getattr(ctx, "projects", None) or [ctx.project])


def _workspace_projects(ctx):
    out = list(getattr(ctx, "all_projects", None) or [])
    if ctx.project not in out:
        out.append(ctx.project)
    return out


# ---------------------------------------------------------------------------
# Inventory: gateways, authorization policies and extensions, telemetry policies, bound workloads
# ---------------------------------------------------------------------------

def _list_all(ctx, url, items_key, params=None, max_pages=10):
    """Pages through a list method. Returns (items, unreachable locations, truncated)."""
    items, unreachable, token = [], [], None
    for _ in range(max_pages):
        page = dict(params or {})
        page["pageSize"] = 100
        if token:
            page["pageToken"] = token
        payload = ctx.rest("GET", url, params=page, timeout=30)
        items.extend(i for i in payload.get(items_key) or [] if isinstance(i, dict))
        unreachable.extend(_strings(payload.get("unreachable")))
        token = payload.get("nextPageToken")
        if not token:
            return items, unreachable, False
    return items, unreachable, True


def _api_disabled(err):
    text = getattr(err, "message", "") or ""
    return getattr(err, "kind", "") == "denied" and (
        "SERVICE_DISABLED" in text or "has not been used in project" in text or "it is disabled" in text)


def _project_gateways(ctx, project, regions):
    """Gateways of one project: one list across all locations, or one list per region when that is refused."""
    url = "%s/v1/projects/%s/locations/%%s/agentGateways" % (NETWORK_SERVICES, project)
    try:
        items, unreachable, truncated = _list_all(ctx, url % "-", "agentGateways", {"returnPartialSuccess": "true"})
        return {"items": items, "unreachable": sorted({_short(u) for u in unreachable}), "truncated": truncated}
    except GcpError as err:
        if _api_disabled(err) or err.kind not in ("invalid", "not_found", "denied", "http") or not regions:
            raise
        first = err
    items, failed, truncated, last = [], [], False, None
    for region in regions:
        try:
            part, _, cut = _list_all(ctx, url % region, "agentGateways")
        except GcpError as err:
            failed.append(region)
            last = err
            continue
        items.extend(part)
        truncated = truncated or cut
    if len(failed) == len(regions):
        raise last or first
    return {"items": items, "unreachable": failed, "truncated": truncated}


def _parse_gateway(raw, project):
    match = GATEWAY_NAME_RE.match(raw.get("name") or "")
    if not match:
        return None
    location, name = match.group(2), match.group(3)
    managed = raw.get("googleManaged") if isinstance(raw.get("googleManaged"), dict) else None
    own = raw.get("selfManaged") if isinstance(raw.get("selfManaged"), dict) else None
    path = (managed or {}).get("governedAccessPath")
    net = raw.get("networkConfig") if isinstance(raw.get("networkConfig"), dict) else {}
    dns = net.get("dnsPeeringConfig") if isinstance(net.get("dnsPeeringConfig"), dict) else {}
    card = raw.get("agentGatewayCard") if isinstance(raw.get("agentGatewayCard"), dict) else {}
    labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
    proxies = _strings((own or {}).get("resourceUris"))
    if isinstance((own or {}).get("resourceUri"), str) and own["resourceUri"]:
        proxies.append(own["resourceUri"])
    gateway = {
        "key": gateway_key(project, location, name), "name": name, "project": project, "location": location,
        "projectRef": match.group(1),
        "resource": "projects/%s/locations/%s/agentGateways/%s" % (project, location, name),
        "description": raw.get("description") if isinstance(raw.get("description"), str) else "",
        "labels": {str(k): str(v) for k, v in labels.items()},
        "createTime": raw.get("createTime"), "updateTime": raw.get("updateTime"), "etag": raw.get("etag"),
        "deployment": "google_managed" if managed is not None else ("self_managed" if own is not None else "unknown"),
        "accessPath": path if path in ACCESS_PATHS else None,
        "protocols": _strings(raw.get("protocols")),
        "registries": _strings(raw.get("registries")),
        "network": {
            "attachment": ((net.get("egress") or {}).get("networkAttachment")
                           if isinstance(net.get("egress"), dict) else None) or None,
            "connectivityTemplate": raw.get("agentConnectivityTemplate") or None,
            "dnsDomains": _strings(dns.get("domains")), "dnsNetwork": dns.get("targetNetwork") or None,
            "proxies": proxies,
        },
        "endpoint": {"mtls": card.get("mtlsEndpoint") or None,
                     "rootCertificates": len(_strings(card.get("rootCertificates"))),
                     "serviceAccount": card.get("serviceExtensionsServiceAccount") or None},
        "consoleUrl": "https://console.cloud.google.com/agent-platform/gateways?project=%s"
                      % urllib.parse.quote(project, safe=""),
    }
    gateway["registryState"] = _registry_state(gateway)
    return gateway


def _registry_state(gateway):
    """An Agent-to-Anywhere gateway uses the regional Agent Registry of its own project and region."""
    if gateway["accessPath"] != "AGENT_TO_ANYWHERE":
        return "not_used"
    if not gateway["registries"]:
        return "missing"
    for registry in gateway["registries"]:
        match = REGISTRY_RE.match(registry)
        if not match or match.group(2) != gateway["location"]:
            return "mismatch"
        if not _project_matches(match.group(1), gateway):
            return "mismatch"
    return "ok"


def _project_matches(ref, gateway):
    """True when a project reference (ID or number) can be the project of the gateway. A project number
    cannot be compared with a project ID, so that case counts as a match."""
    known = {gateway["project"], gateway["projectRef"]}
    if ref in known:
        return True
    return not [k for k in known if k.isdigit() == ref.isdigit()]


def _armor_templates(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None, None
    if isinstance(raw, list):
        raw = raw[0] if raw and isinstance(raw[0], dict) else {}
    if not isinstance(raw, dict):
        return None, None
    request, response = raw.get("request_template_id"), raw.get("response_template_id")
    return (request if isinstance(request, str) else None), (response if isinstance(response, str) else None)


def _extension_summary(ext):
    """What an authorization extension delegates to, read from its service and metadata."""
    meta = ext.get("metadata") if isinstance(ext.get("metadata"), dict) else {}
    service = str(ext.get("service") or "")
    out = {"name": _short(ext.get("name")), "service": service, "kind": "custom", "failOpen": bool(ext.get("failOpen")),
           "timeout": ext.get("timeout") if isinstance(ext.get("timeout"), str) else None, "mode": None,
           "policyModel": None, "requestTemplate": None, "responseTemplate": None}
    if service == IAP_SERVICE:
        # Enforcement applies whenever iamEnforcementMode is absent or anything other than DRY_RUN.
        version = meta.get("iapPolicyVersion")
        out.update(kind="iap", mode="audit" if meta.get("iamEnforcementMode") == "DRY_RUN" else "enforced",
                   policyModel=version if isinstance(version, str) else None)
    elif MODEL_ARMOR_SERVICE_RE.match(service):
        request, response = _armor_templates(meta.get("model_armor_settings"))
        out.update(kind="model_armor", requestTemplate=request, responseTemplate=response)
    elif "sgpEnforcementMode" in meta or "sgpUserIntentMatching" in meta:
        out.update(kind="semantic_governance",
                   mode="audit" if meta.get("sgpEnforcementMode") == "DRY_RUN" else "enforced")
    return out


def _policy_targets(policy):
    target = policy.get("target") if isinstance(policy.get("target"), dict) else {}
    return _strings(target.get("resources"))


def _policy_extensions(policy):
    provider = policy.get("customProvider") if isinstance(policy.get("customProvider"), dict) else {}
    ext = provider.get("authzExtension") if isinstance(provider.get("authzExtension"), dict) else {}
    return [_short(r) for r in _strings(ext.get("resources"))]


def _mcp_rules(policy):
    """MCP methods and tools named by the rules of an ALLOW or DENY policy, for example 'tools/call: get_weather'."""
    out = []
    for rule in policy.get("httpRules") or []:
        to = rule.get("to") if isinstance(rule, dict) and isinstance(rule.get("to"), dict) else {}
        for operation in (to.get("operations") or []) + (to.get("notOperations") or []):
            mcp = operation.get("mcp") if isinstance(operation, dict) and isinstance(operation.get("mcp"), dict) else {}
            for method in mcp.get("methods") or []:
                if not isinstance(method, dict) or not isinstance(method.get("name"), str):
                    continue
                params = [p.get("exact") or p.get("prefix") or p.get("suffix") or p.get("contains")
                          for p in method.get("params") or [] if isinstance(p, dict)]
                params = [p for p in params if isinstance(p, str)]
                text = "%s: %s" % (method["name"], ", ".join(params)) if params else method["name"]
                if text not in out:
                    out.append(text[:200])
    return out[:12]


def _policy_summary(policy, extensions, suffix):
    """One authorization policy of a gateway. `extensions` is {short name: summary}, or None when unreadable."""
    refs = _policy_extensions(policy)
    action = policy.get("action") if isinstance(policy.get("action"), str) else "AUTHZ_ACTION_UNSPECIFIED"
    profile = policy.get("policyProfile")
    if profile not in ("REQUEST_AUTHZ", "CONTENT_AUTHZ"):
        profile = "REQUEST_AUTHZ"  # the default of the API
    extension = None
    if refs:
        extension = (extensions or {}).get(refs[0]) or {"name": refs[0], "service": None, "kind": "unknown",
                                                           "failOpen": None, "timeout": None, "mode": None,
                                                           "policyModel": None, "requestTemplate": None,
                                                           "responseTemplate": None}
    provider = policy.get("customProvider") if isinstance(policy.get("customProvider"), dict) else {}
    if action != "CUSTOM":
        kind = "rule"
    elif extension is not None:
        kind = extension["kind"]
    else:
        kind = "iap" if isinstance(provider.get("cloudIap"), dict) else "unknown"
    return {"name": _short(policy.get("name")), "profile": profile, "action": action, "kind": kind,
            "extension": extension, "rules": len(policy.get("httpRules") or []), "mcp": _mcp_rules(policy),
            "shared": any(not t.endswith(suffix) for t in _policy_targets(policy))}


def _controls(policies):
    """Access authorization, content security and semantic governance of a gateway, from its policies."""
    if policies is None:
        return ({"state": "unknown", "policyModel": None, "policy": None, "extension": None},
                {"state": "unknown", "requestTemplate": None, "responseTemplate": None, "policy": None},
                {"state": "unknown", "policy": None})
    unknown = any(p["kind"] == "unknown" for p in policies)
    iap = [p for p in policies if p["kind"] == "iap"]
    armor = [p for p in policies if p["kind"] == "model_armor"]
    sgp = [p for p in policies if p["kind"] == "semantic_governance"]
    access = {"state": "unknown" if unknown else "none", "policyModel": None, "policy": None, "extension": None}
    if iap:
        exts = [p["extension"] for p in iap if p["extension"]]
        access = {"state": "audit" if any(e["mode"] == "audit" for e in exts) else "enforced",
                  "policyModel": next((e["policyModel"] for e in exts if e["policyModel"]), None),
                  "policy": iap[0]["name"], "extension": exts[0]["name"] if exts else None}
    content = {"state": "unknown" if unknown else "none", "requestTemplate": None, "responseTemplate": None,
               "policy": None}
    if armor:
        ext = armor[0]["extension"] or {}
        content = {"state": "configured", "requestTemplate": ext.get("requestTemplate"),
                   "responseTemplate": ext.get("responseTemplate"), "policy": armor[0]["name"]}
    semantic = {"state": "unknown" if unknown else "none", "policy": None}
    if sgp:
        modes = [p["extension"]["mode"] for p in sgp if p["extension"]]
        semantic = {"state": "audit" if "audit" in modes else "enforced", "policy": sgp[0]["name"]}
    return access, content, semantic


def _telemetry(policies, suffix):
    """Tracing and logging settings that telemetry policies apply to a gateway (None: could not be read)."""
    if policies is None:
        return {"state": "unknown", "policy": None, "samplingRate": None, "parentBased": None, "logging": "unknown",
                "policies": None}
    mine = []
    for policy in policies:
        target = policy.get("telemetryTarget") if isinstance(policy.get("telemetryTarget"), dict) else {}
        if any(r.endswith(suffix) for r in _strings(target.get("resources"))):
            mine.append(policy)
    out = {"state": "none", "policy": None, "samplingRate": None, "parentBased": None, "logging": "default",
           "policies": len(mine)}
    for policy in mine:
        tracing = policy.get("tracingConfiguration")
        if isinstance(tracing, dict) and out["state"] == "none":
            parent = tracing.get("parentBasedSampling") if isinstance(tracing.get("parentBasedSampling"), dict) else {}
            try:
                rate = float(tracing.get("samplingRate") or 0)
            except (TypeError, ValueError):
                rate = 0.0
            out.update(state="configured", policy=_short(policy.get("name")), samplingRate=rate,
                       parentBased=bool(parent.get("enabled")))
        log_cfg = policy.get("loggingConfiguration")
        if isinstance(log_cfg, dict) and log_cfg.get("enable") is False:
            out["logging"] = "disabled"
    return out


def _engine_refs(engine):
    """(egress, ingress) gateway resource names that an Agent Runtime agent routes through."""
    spec = engine.get("spec") if isinstance(engine.get("spec"), dict) else {}
    deployment = spec.get("deploymentSpec") if isinstance(spec.get("deploymentSpec"), dict) else {}
    binding = deployment.get("agentGatewayConfig") if isinstance(deployment.get("agentGatewayConfig"), dict) else {}
    refs = []
    for field in ("agentToAnywhereConfig", "clientToAgentConfig"):
        value = binding.get(field) if isinstance(binding.get(field), dict) else {}
        ref = value.get("agentGateway")
        refs.append(ref if isinstance(ref, str) and ref else None)
    return refs[0], refs[1]


def _app_ref(engine):
    """Egress gateway resource name of a Gemini Enterprise app."""
    setting = engine.get("agentGatewaySetting") if isinstance(engine.get("agentGatewaySetting"), dict) else {}
    egress = setting.get("defaultEgressAgentGateway")
    ref = egress.get("name") if isinstance(egress, dict) else None
    return ref if isinstance(ref, str) and ref else None


def _engines_url(project, location):
    return "%s/v1/projects/%s/locations/%s/collections/default_collection/engines" % (
        discovery_host(location), project, location)


def _ge_apps(ctx):
    """Gemini Enterprise apps: (apps, complete). An app without a gateway is listed only when the API marks it
    as a Gemini Enterprise app, so search-only engines are not reported as ungoverned. complete is True when
    every location of the primary project was read (a project without the Discovery Engine API has no apps)."""
    tasks = {}
    for project in _projects(ctx):
        for location in discovery_locations(ctx):
            tasks[(project, location)] = (lambda u=_engines_url(project, location): _list_all(
                ctx, u, "engines", max_pages=3)[0])
    apps, complete = [], True
    for (project, location), (ok, value) in ctx.run(tasks).items():
        if not ok:
            if project == ctx.project and not (isinstance(value, GcpError) and _api_disabled(value)):
                complete = False
                ctx.warn_error(SRC_ENGINES, value, "Discovery Engine in %s" % location)
            continue
        ctx.add_source(SRC_ENGINES)
        for engine in value:
            ref = _app_ref(engine)
            engine_id = _short(engine.get("name"))
            if not engine_id or (ref is None and engine.get("appType") != "APP_TYPE_INTRANET"):
                continue
            apps.append({"type": "gemini_enterprise", "id": engine_id,
                         "displayName": engine.get("displayName") or engine_id, "project": project,
                         "location": location, "egressRef": ref, "ingressRef": None,
                         "createTime": engine.get("createTime"), "bindable": True})
    return apps, complete


def _workloads(ctx):
    """Agent Runtime agents and Gemini Enterprise apps with the gateways they name.
    Returns (workloads, complete): complete is True when both sources could be read."""
    engines, engine_ok = ctx.memo("reasoning_engines", "all", agents.reasoning_engines, ttl=300)
    out = []
    for region, engine in engines:
        engine_id = _short(engine.get("name"))
        if not engine_id:
            continue
        egress, ingress = _engine_refs(engine)
        created = parse_rfc3339(engine.get("createTime"))
        out.append({"type": "agent_runtime", "id": engine_id, "displayName": engine.get("displayName") or engine_id,
                    "project": engine.get("_project") or ctx.project, "location": region,
                    "egressRef": egress, "ingressRef": ingress, "createTime": engine.get("createTime"),
                    "bindable": None if created is None else created >= BINDING_CUTOFF})
    apps, apps_complete = _ge_apps(ctx)
    out.extend(apps)
    out.sort(key=lambda w: (w["type"], w["displayName"].lower(), w["project"], w["id"]))
    return out, bool(engine_ok and apps_complete)


def _referenced(ctx, gateway, project, location, name):
    """True when a workload that the workspace can list still names the gateway. Read without caches, right
    before authorization policies are deleted. Agents bind to a gateway of their own region, and a Gemini
    Enterprise app to a gateway of its own project, so those are the lists that must be readable; raises
    ApiError when they are not."""
    tasks = {}
    for candidate in _workspace_projects(ctx):
        url = "https://%s-aiplatform.googleapis.com/v1/projects/%s/locations/%s/reasoningEngines" % (
            location, candidate, location)
        tasks[("agents", candidate)] = (lambda u=url: _list_all(ctx, u, "reasoningEngines"))
    for app_location in discovery_locations(ctx):
        tasks[("apps", app_location)] = (lambda u=_engines_url(project, app_location): _list_all(
            ctx, u, "engines", max_pages=3))
    found = False
    for key, (ok, value) in ctx.run(tasks).items():
        required = key[0] == "apps" or key[1] == project
        if not ok:
            if required and not (isinstance(value, GcpError) and _api_disabled(value)):
                raise ApiError("conflict", MSG_UNVERIFIED)
            continue
        items, _, truncated = value
        if truncated and required:
            raise ApiError("conflict", MSG_UNVERIFIED)
        for item in items:
            refs = _engine_refs(item) if key[0] == "agents" else (_app_ref(item),)
            for ref in refs:
                match = GATEWAY_NAME_RE.match(ref or "")
                if match and (match.group(2), match.group(3)) == (location, name) and _project_matches(
                        match.group(1), gateway):
                    found = True
    return found


def _bind(gateways, workloads):
    """Resolves the gateway each workload names and attaches the workloads to their gateways."""
    index = {}
    for gateway in gateways:
        gateway["workloads"] = []
        index.setdefault((gateway["location"], gateway["name"]), []).append(gateway)

    def resolve(ref, home):
        match = GATEWAY_NAME_RE.match(ref or "")
        if not match:
            return None
        found = [g for g in index.get((match.group(2), match.group(3))) or [] if _project_matches(match.group(1), g)]
        if len(found) > 1:
            found = [g for g in found if g["project"] == home] or found
        return found[0] if found else None

    for workload in workloads:
        for path, field in (("egress", "egressRef"), ("ingress", "ingressRef")):
            ref = workload.pop(field)
            gateway = resolve(ref, workload["project"]) if ref else None
            workload[path] = {"ref": ref, "gateway": gateway["key"] if gateway else None} if ref else None
            if gateway is not None:
                gateway["workloads"].append({"type": workload["type"], "id": workload["id"],
                                             "displayName": workload["displayName"], "project": workload["project"],
                                             "location": workload["location"], "path": path})


def _findings(gateway, workloads_complete):
    """Governance findings of one gateway as {code, severity}; the page explains each code."""
    out = []

    def add(code, severity):
        out.append({"code": code, "severity": severity})

    egress = gateway["accessPath"] == "AGENT_TO_ANYWHERE"
    policies = gateway["policies"]
    if policies is not None:
        if egress and not policies:
            add("egress_default_deny", "high")
        elif egress and gateway["access"]["state"] == "none":
            add("no_access_authorization", "medium")
        if gateway["access"]["state"] == "audit":
            add("access_audit_only", "medium")
        if gateway["access"]["policyModel"] == "V1":
            add("legacy_policy_model", "low")
        if any(p["extension"] and p["extension"]["failOpen"] for p in policies):
            add("fail_open", "medium")
        if gateway["contentSecurity"]["state"] == "none":
            add("no_content_security" if egress else "ingress_unscreened", "low" if egress else "medium")
        if gateway["semantic"]["state"] == "audit":
            add("semantic_audit_only", "low")
    if gateway["registryState"] == "missing":
        add("no_registry", "medium")
    elif gateway["registryState"] == "mismatch":
        add("registry_mismatch", "medium")
    if gateway["telemetry"]["logging"] == "disabled":
        add("logging_disabled", "medium")
    if gateway["telemetry"]["state"] == "none":
        add("no_tracing", "info")
    if workloads_complete and not gateway["workloads"]:
        add("no_workloads", "info")
    out.sort(key=lambda f: -SEVERITY_RANK[f["severity"]])
    return out


def _sink_rows(project, sinks):
    """Log Router sinks whose filter names the gateway logs (or that have no filter and export every entry)."""
    rows = []
    for sink in sinks:
        text = sink.get("filter") if isinstance(sink.get("filter"), str) else ""
        covers = []
        if not text.strip():
            covers = ["all"]
        else:
            if "gateway_requests" in text:
                covers.append("gateway_requests")
            if "data_access" in text:
                covers.append("data_access")
        destination = sink.get("destination") if isinstance(sink.get("destination"), str) else ""
        if not covers or not sink.get("name"):
            continue
        rows.append({"project": project, "name": sink["name"], "service": destination.split("/", 1)[0] or None,
                     "destination": destination.split("/", 1)[1] if "/" in destination else destination,
                     "covers": covers, "disabled": bool(sink.get("disabled"))})
    return rows


def inventory_data(ctx):
    """Gateways with their governance configuration, the workloads of the workspace and the posture summary."""
    regions = scan_regions(ctx)
    projects = _projects(ctx)
    out = {"status": "ok", "gateways": None, "workloads": None, "kpis": None, "exports": None,
           "scope": {"projects": projects, "regions": regions},
           "manage": {"projects": _workspace_projects(ctx), "regions": regions,
                      "geRegions": dict(GE_GATEWAY_REGION)},
           "limits": {"egressPolicies": EGRESS_POLICY_LIMIT, "registryResources": REGISTRY_RESOURCE_LIMIT}}
    listed = ctx.run({p: (lambda p=p: _project_gateways(ctx, p, regions)) for p in projects})
    gateways = []
    unreadable = False
    for project in projects:
        ok, value = listed[project]
        # Failures of linked projects stay silent unless the project is the only one in scope.
        main = project == ctx.project or len(projects) == 1
        if not ok:
            if main:
                unreadable = True
                if isinstance(value, GcpError) and _api_disabled(value):
                    out["status"] = "api_disabled"
                    ctx.warn(SRC_GATEWAYS, "not_configured", MSG_API_DISABLED)
                else:
                    out["status"] = "denied" if getattr(value, "kind", "") == "denied" else "error"
                    ctx.warn_error(SRC_GATEWAYS, value, "Agent Gateway (Network Services API)")
            continue
        ctx.add_source(SRC_GATEWAYS)
        if main and value["unreachable"]:
            ctx.warn(SRC_GATEWAYS, "partial_data", "Some regions could not be read, so gateways there are missing.")
        if main and value["truncated"]:
            ctx.warn(SRC_GATEWAYS, "truncated", "Only the first 1000 gateways of the project are listed.")
        for raw in value["items"]:
            gateway = _parse_gateway(raw, project)
            if gateway is not None:
                gateways.append(gateway)
    if unreadable and not gateways:
        return out  # null: the gateways could not be read (an empty list means there are none)
    gateways.sort(key=lambda g: (g["project"] != ctx.project, g["project"], g["location"], g["name"]))

    pairs = sorted({(g["project"], g["location"]) for g in gateways})
    tasks = {("workloads",): (lambda: _workloads(ctx))}
    for project, location in pairs:
        base = "projects/%s/locations/%s" % (project, location)
        tasks[("ext", project, location)] = (lambda b=base: _list_all(
            ctx, "%s/v1/%s/authzExtensions" % (NETWORK_SERVICES, b), "authzExtensions")[0])
        tasks[("pol", project, location)] = (lambda b=base: _list_all(
            ctx, "%s/v1/%s/authzPolicies" % (NETWORK_SECURITY, b), "authzPolicies")[0])
        tasks[("tel", project, location)] = (lambda b=base: _list_all(
            ctx, "%s/v1beta1/%s/telemetryPolicies" % (NETWORK_SERVICES, b), "telemetryPolicies")[0])
    for project in sorted({p for p, _ in pairs}):
        tasks[("sinks", project)] = (lambda p=project: _list_all(
            ctx, "%s/projects/%s/sinks" % (LOGGING_V2, p), "sinks")[0])
    res = ctx.run(tasks)

    def read(key, source, what):
        ok, value = res[key]
        if ok:
            ctx.add_source(source)
            return value
        if key[1] == ctx.project or len(projects) == 1:
            ctx.warn_error(source, value, what)
        return None

    ok, value = res[("workloads",)]
    workloads, complete = value if ok else (None, False)
    if not ok:
        ctx.warn_error("api:aiplatform.reasoningEngines", value, "Agent workloads")
    if workloads is not None:
        _bind(gateways, workloads)
    for gateway in gateways:
        project, location = gateway["project"], gateway["location"]
        suffix = "/locations/%s/agentGateways/%s" % (location, gateway["name"])
        raw_ext = read(("ext", project, location), SRC_EXTENSIONS, "Authorization extensions in %s" % location)
        raw_pol = read(("pol", project, location), SRC_POLICIES, "Authorization policies in %s" % location)
        raw_tel = read(("tel", project, location), SRC_TELEMETRY, "Telemetry policies in %s" % location)
        extensions = None if raw_ext is None else {_short(e.get("name")): _extension_summary(e) for e in raw_ext}
        gateway["policies"] = None if raw_pol is None else [
            _policy_summary(p, extensions, suffix) for p in raw_pol
            if any(t.endswith(suffix) for t in _policy_targets(p))]
        gateway["access"], gateway["contentSecurity"], gateway["semantic"] = _controls(gateway["policies"])
        gateway["telemetry"] = _telemetry(raw_tel, suffix)
        if workloads is None:
            gateway["workloads"] = None
        gateway["findings"] = _findings(gateway, complete)
        gateway["severity"] = gateway["findings"][0]["severity"] if gateway["findings"] else "ok"
    exports = []
    exports_known = False
    for project in sorted({p for p, _ in pairs}):
        sinks = read(("sinks", project), SRC_SINKS, "Log Router sinks")
        if sinks is not None:
            exports_known = True
            exports.extend(_sink_rows(project, sinks))

    egress = [g for g in gateways if g["accessPath"] == "AGENT_TO_ANYWHERE"]
    counts = {"high": 0, "medium": 0, "low": 0, "info": 0}
    for gateway in gateways:
        for finding in gateway["findings"]:
            counts[finding["severity"]] += 1
    out.update({
        "gateways": gateways,
        "workloads": workloads,
        "workloadsComplete": complete,
        "exports": exports if (exports_known or not pairs) else None,
        "kpis": {
            "gateways": len(gateways), "egress": len(egress), "ingress": len(gateways) - len(egress),
            "enforced": sum(1 for g in egress if g["access"]["state"] == "enforced"),
            "audit": sum(1 for g in egress if g["access"]["state"] == "audit"),
            "unprotected": sum(1 for g in egress if g["access"]["state"] == "none"),
            "workloads": None if workloads is None else len(workloads),
            "governed": None if workloads is None else sum(1 for w in workloads if w["egress"]),
            "findings": counts,
        },
    })
    return out


def _inventory(ctx):
    return ctx.memo("agent_gateways", "inventory", inventory_data, ttl=60)


def inventory(ctx):
    """GET /api/v1/agent-platform/gateways"""
    return _inventory(ctx)


# ---------------------------------------------------------------------------
# Traffic: gateway request log, access decisions and metrics
# ---------------------------------------------------------------------------

OUTCOMES = (("allowed", "Allowed"), ("denied", "Denied"), ("client_error", "Client error"),
            ("server_error", "Server error"))
DECISIONS = (("granted", "Granted"), ("denied", "Denied"), ("would_block", "Would be blocked (audit-only)"))


def _rate_limited(err):
    return isinstance(err, GcpError) and (err.status == 429 or err.reason == "RESOURCE_EXHAUSTED")


def _read_log(ctx, project, flt, limit):
    """Newest entries first. Returns (entries, truncated)."""
    entries, token, empty_pages = [], None, 0
    deadline = time.monotonic() + LOG_READ_DEADLINE_SECONDS
    max_pages = max(1, (limit + 999) // 1000 + 1)
    for _ in range(max_pages):
        body = {"resourceNames": ["projects/%s" % project], "filter": flt, "orderBy": "timestamp desc",
                "pageSize": max(1, min(1000, limit - len(entries)))}
        if token:
            body["pageToken"] = token
        data = None
        for attempt in range(len(LOG_RETRY_DELAYS) + 1):
            if attempt:
                delay = LOG_RETRY_DELAYS[attempt - 1]
                if delay > 0:
                    time.sleep(delay + random.uniform(0.0, 0.35))
            try:
                data = ctx.rest("POST", "%s/entries:list" % LOGGING_V2, json_body=body, timeout=15)
                break
            except GcpError as err:
                if _rate_limited(err) and attempt < len(LOG_RETRY_DELAYS) and time.monotonic() < deadline:
                    continue
                if entries:
                    return entries[:limit], True
                raise
        batch = [e for e in (data or {}).get("entries") or [] if isinstance(e, dict)]
        entries.extend(batch)
        token = (data or {}).get("nextPageToken")
        if not token:
            return entries[:limit], False
        if len(entries) >= limit:
            return entries[:limit], True
        empty_pages = 0 if batch else empty_pages + 1
        if empty_pages >= 2 or time.monotonic() >= deadline:
            return entries[:limit], bool(entries)
    return entries[:limit], bool(token and entries)


def _url_parts(url):
    """(host, path) of a request URL. The query string is dropped: it can carry credentials."""
    if not isinstance(url, str) or not url:
        return None, None
    try:
        parts = urllib.parse.urlsplit(url if "//" in url else "//" + url)
        host = parts.hostname
    except ValueError:
        return None, None
    return (host or None), (parts.path or "/")[:200]


def _milliseconds(value):
    """Duration such as '0.250s' in milliseconds."""
    if isinstance(value, str) and value.endswith("s"):
        try:
            return float(value[:-1]) * 1000.0
        except ValueError:
            return None
    return None


def _pick(node, names, fragments):
    """First string value of a log payload object under one of `names`, or under a key containing a fragment."""
    if not isinstance(node, dict):
        return None
    for name in names:
        if isinstance(node.get(name), str) and node[name]:
            return node[name]
    for key, value in node.items():
        if isinstance(value, str) and value and any(f in str(key).lower() for f in fragments):
            return value
    return None


def _percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 1)


class _Requests:
    """Aggregates entries of the gateway request log (networkservices.googleapis.com/gateway_requests)."""

    def __init__(self, rng, egress):
        self.rng = rng
        self.egress = egress  # {(project, location, name)} of the Agent-to-Anywhere gateways
        self.total = 0
        self.outcomes = {key: 0 for key, _ in OUTCOMES}
        self.days = {key: [0] * len(rng.buckets()) for key, _ in OUTCOMES}
        self.gateways = {}
        self.hosts = {}
        self.mcp = {}
        self.denied = []
        self.latencies = []
        self.unregistered = 0

    def add(self, project, entry):
        http = entry.get("httpRequest") if isinstance(entry.get("httpRequest"), dict) else {}
        payload = entry.get("jsonPayload") if isinstance(entry.get("jsonPayload"), dict) else {}
        resource = entry.get("resource") if isinstance(entry.get("resource"), dict) else {}
        labels = resource.get("labels") if isinstance(resource.get("labels"), dict) else {}
        status = to_int(http.get("status")) or None
        authz = payload.get("authzPolicyInfo") if isinstance(payload.get("authzPolicyInfo"), dict) else {}
        policies = authz.get("policies")
        policies = [policies] if isinstance(policies, dict) else [p for p in policies or [] if isinstance(p, dict)]
        results = [authz.get("result")] + [p.get("result") for p in policies]
        if "DENIED" in results or status == 403:
            outcome = "denied"
        elif status and status >= 500:
            outcome = "server_error"
        elif status and status >= 400:
            outcome = "client_error"
        else:
            outcome = "allowed"
        when = parse_rfc3339(entry.get("timestamp"))
        self.total += 1
        self.outcomes[outcome] += 1
        index = self.rng.bucket_index(when) if when is not None else None
        if index is not None:
            self.days[outcome][index] += 1
        name, location = labels.get("gateway_name"), labels.get("location")
        if isinstance(name, str) and name:
            row = self.gateways.setdefault((project, location, name), {"requests": 0, "denied": 0, "errors": 0})
            row["requests"] += 1
            row["denied"] += outcome == "denied"
            row["errors"] += outcome == "server_error"
        host, path = _url_parts(http.get("requestUrl"))
        # The registry applies to outbound traffic only: an entry of an Agent-to-Anywhere gateway names the
        # Agent Registry resource that matched the destination, or none for an unregistered destination.
        registered = None
        if (project, location, name) in self.egress:
            registered = bool(payload.get("agentRegistryResource"))
            self.unregistered += not registered
        if host:
            row = self.hosts.setdefault(host, {"requests": 0, "denied": 0, "egress": 0, "registered": 0})
            row["requests"] += 1
            row["denied"] += outcome == "denied"
            if registered is not None:
                row["egress"] += 1
                row["registered"] += registered
        mcp = payload.get("mcpInfo") if isinstance(payload.get("mcpInfo"), dict) else None
        method = _pick(mcp, ("method", "methodName", "mcpMethod"), ("method",))
        if method:
            tool = _pick({k: v for k, v in mcp.items() if v != method},
                         ("param", "parameter", "toolName", "tool"), ("param", "tool", "name"))
            row = self.mcp.setdefault((method, tool), {"requests": 0, "denied": 0})
            row["requests"] += 1
            row["denied"] += outcome == "denied"
        latency = _milliseconds(http.get("latency"))
        if latency is not None:
            self.latencies.append(latency)
        if outcome == "denied":
            trace = entry.get("trace") if isinstance(entry.get("trace"), str) else ""
            self.denied.append({
                "time": iso(when) if when else None, "project": project, "gateway": name, "location": location,
                "method": http.get("requestMethod") if isinstance(http.get("requestMethod"), str) else None,
                "host": host, "path": path, "status": status, "registered": registered,
                "policy": next((_short(p.get("name")) for p in policies if p.get("result") == "DENIED"), None),
                "mcp": "%s: %s" % (method, tool) if method and tool else method,
                "traceId": _short(trace)})

    def timeline(self):
        return timeutil.timeline(self.rng, [timeutil.series(key, name, self.days[key]) for key, name in OUTCOMES])


class _Decisions:
    """Aggregates Identity-Aware Proxy decisions for gateway egress (Cloud Audit Logs, Data Access)."""

    def __init__(self, rng):
        self.rng = rng
        self.total = 0
        self.failures = 0
        self.would_block = 0
        self.unregistered = 0
        self.days = {key: [0] * len(rng.buckets()) for key, _ in DECISIONS}
        self.pairs = {}

    def add(self, entry):
        proto = entry.get("protoPayload") if isinstance(entry.get("protoPayload"), dict) else {}
        infos = [a for a in proto.get("authorizationInfo") or [] if isinstance(a, dict)]
        egress = [a for a in infos if "egressViaIAP" in str(a.get("permission") or "")] or infos
        granted = bool(egress) and all(a.get("granted") is True for a in egress)
        meta = proto.get("metadata") if isinstance(proto.get("metadata"), dict) else {}
        dry_run = meta.get("dryRun") in (True, "true") or meta.get("iamEnforcementMode") == "DRY_RUN"
        auth = proto.get("authenticationInfo") if isinstance(proto.get("authenticationInfo"), dict) else {}
        principal = auth.get("principalSubject") or auth.get("principalEmail")
        resource = next((a.get("resource") for a in egress if a.get("resource")), None) or proto.get("resourceName")
        labels = entry.get("labels") if isinstance(entry.get("labels"), dict) else {}
        unregistered = labels.get("iap.googleapis.com/audited_resource_name") == "unregisteredResource"
        outcome = "granted" if granted else ("would_block" if dry_run else "denied")
        when = parse_rfc3339(entry.get("timestamp"))
        self.total += 1
        self.failures += not granted
        self.would_block += outcome == "would_block"
        self.unregistered += unregistered
        index = self.rng.bucket_index(when) if when is not None else None
        if index is not None:
            self.days[outcome][index] += 1
        key = (principal if isinstance(principal, str) else None, resource if isinstance(resource, str) else None)
        row = self.pairs.setdefault(key, {"attempts": 0, "denied": 0, "wouldBlock": 0, "unregistered": False,
                                          "lastSeen": None})
        row["attempts"] += 1
        row["denied"] += outcome == "denied"
        row["wouldBlock"] += outcome == "would_block"
        row["unregistered"] = row["unregistered"] or unregistered
        stamp = iso(when) if when else None
        if stamp and (row["lastSeen"] is None or stamp > row["lastSeen"]):
            row["lastSeen"] = stamp

    def timeline(self):
        return timeutil.timeline(self.rng, [timeutil.series(key, name, self.days[key]) for key, name in DECISIONS])


def _metric_series(ctx, project, flt, rng, aligner, reducer, group_by=(), period=86400):
    """Reduced Cloud Monitoring series of one project: [(labels, [(time inside the period, number)])]."""
    params = [("filter", flt), ("interval.startTime", iso(rng.start_ts())), ("interval.endTime", iso(rng.end_ts())),
              ("aggregation.alignmentPeriod", "%ds" % period), ("aggregation.perSeriesAligner", aligner),
              ("aggregation.crossSeriesReducer", reducer), ("pageSize", "1000")]
    params += [("aggregation.groupByFields", g) for g in group_by]
    out, token = [], None
    for _ in range(5):
        page = params + ([("pageToken", token)] if token else [])
        try:
            payload = ctx.rest("GET", "%s/projects/%s/timeSeries" % (MONITORING_V3, project), params=page, timeout=30)
        except GcpError as err:
            if err.kind == "not_found":
                # Cloud Monitoring creates a metric descriptor only after the first sample is written.
                return out
            raise
        for series in payload.get("timeSeries") or []:
            labels = dict((series.get("resource") or {}).get("labels") or {})
            labels.update((series.get("metric") or {}).get("labels") or {})
            points = []
            for point in series.get("points") or []:
                end = parse_rfc3339((point.get("interval") or {}).get("endTime"))
                value = point.get("value") or {}
                number = value.get("int64Value", value.get("doubleValue"))
                try:
                    number = float(number)
                except (TypeError, ValueError):
                    continue
                if end is not None:
                    points.append((end - dt.timedelta(seconds=1), number))
            out.append((labels, points))
        token = payload.get("nextPageToken")
        if not token:
            break
    return out


def _gateway_metrics(ctx, project, names, rng):
    """Request counts by day and outcome, latency percentiles and extension invocations of the named gateways."""
    quoted = ",".join('"%s"' % n for n in names)
    scope = 'resource.type="%s" AND resource.labels.gateway_name=one_of(%s)' % (GATEWAY_RESOURCE_TYPE, quoted)
    days = {key: [0] * len(rng.buckets()) for key, _ in OUTCOMES}
    by_gateway = {}
    total = 0
    for labels, points in _metric_series(ctx, project, 'metric.type="%s" AND %s' % (REQUEST_COUNT, scope), rng,
                                         "ALIGN_SUM", "REDUCE_SUM",
                                         ("resource.label.gateway_name", "resource.label.location",
                                          "metric.label.response_code")):
        code = to_int(labels.get("response_code"))
        outcome = ("denied" if code == 403 else "server_error" if code >= 500 else
                   "client_error" if code >= 400 else "allowed")
        row = by_gateway.setdefault((project, labels.get("location"), labels.get("gateway_name")),
                                    {"requests": 0, "denied": 0, "errors": 0})
        for when, number in points:
            count = int(number)
            index = rng.bucket_index(when)
            if index is None:
                continue
            days[outcome][index] += count
            total += count
            row["requests"] += count
            row["denied"] += count if outcome == "denied" else 0
            row["errors"] += count if outcome == "server_error" else 0
    seconds = max(60, int((rng.end_ts() - rng.start_ts()).total_seconds()))
    latency = {}
    for key, reducer in (("p50", "REDUCE_PERCENTILE_50"), ("p95", "REDUCE_PERCENTILE_95")):
        values = [n for _, points in _metric_series(
            ctx, project, 'metric.type="%s" AND %s' % (TOTAL_LATENCIES, scope), rng, "ALIGN_DELTA", reducer,
            period=seconds) for _, n in points]
        latency[key] = round(max(values), 1) if values else None
    return {"total": total, "days": days, "byGateway": by_gateway, "latency": latency}


def _project_latency(ctx, project, rng):
    """Latency percentiles of the metric the product documents for gateways, across the whole project."""
    seconds = max(60, int((rng.end_ts() - rng.start_ts()).total_seconds()))
    out = {}
    for key, reducer in (("p50", "REDUCE_PERCENTILE_50"), ("p95", "REDUCE_PERCENTILE_95")):
        values = [n for _, points in _metric_series(
            ctx, project, 'metric.type="%s"' % GATEWAY_LATENCIES, rng, "ALIGN_DELTA", reducer, period=seconds)
            for _, n in points]
        out[key] = round(max(values), 1) if values else None
    return out


def _extension_metrics(ctx, project, rng):
    """{extension name: {invocations, failedOpen}} of the Service Extensions metrics of one project."""
    out = {}
    for field, metric in (("invocations", EXTENSION_INVOCATIONS), ("failedOpen", EXTENSION_FAILED_OPEN)):
        for labels, points in _metric_series(ctx, project, 'metric.type="%s"' % metric, rng, "ALIGN_SUM",
                                             "REDUCE_SUM", ("resource.label.resource_name",)):
            name = _short(labels.get("resource_name"))
            if name:
                row = out.setdefault(name, {"invocations": 0, "failedOpen": 0})
                row[field] += int(sum(n for _, n in points))
    return out


def _selected(ctx, gateways):
    raw = (ctx.params.get("gateway") or "").strip()
    if not raw:
        return None
    for gateway in gateways:
        if gateway["key"] == raw:
            return gateway
    raise ApiError("bad_request", "Parameter 'gateway' must name a gateway of this workspace.")


def traffic(ctx):
    """GET /api/v1/agent-platform/gateways/traffic[?gateway=<project>/<region>/<name>]"""
    rng = ctx.range
    inv = _inventory(ctx)
    known = inv.get("gateways")
    gateways = known or []
    out = {"scope": {"gateway": None,
                     "gateways": [{"key": g["key"], "name": g["name"], "project": g["project"],
                                   "location": g["location"]} for g in gateways]},
           "kpis": {"requests": None, "requestSource": None, "denied": None, "errors": None, "unregistered": None,
                    "authorizations": None, "authorizationFailures": None, "failureRate": None, "wouldBlock": None,
                    "latencyP50": None, "latencyP95": None, "semanticDenials": None, "armorMatches": None},
           "requestTrend": None, "decisionTrend": None, "byGateway": None, "destinations": None, "mcp": None,
           "deniedRequests": None, "decisions": None, "extensions": None,
           "sampled": {"requests": None, "decisions": None}}
    if known is None:
        # _inventory already recorded why gateways could not be read; skip querying logs and metrics blindly.
        return out
    selected = _selected(ctx, gateways)
    out["scope"]["gateway"] = selected["key"] if selected else None
    scope = [selected] if selected else gateways
    if not gateways:
        # No gateway exists, so there is no gateway traffic to read.
        zero = [0] * len(rng.buckets())
        out["kpis"].update(requests=0, requestSource="logs", denied=0, errors=0, unregistered=0, authorizations=0,
                           authorizationFailures=0, wouldBlock=0)
        out.update(requestTrend=timeutil.timeline(rng, [timeutil.series(k, n, list(zero)) for k, n in OUTCOMES]),
                   decisionTrend=timeutil.timeline(rng, [timeutil.series(k, n, list(zero)) for k, n in DECISIONS]),
                   byGateway=[], destinations=[], mcp=[], deniedRequests=[], decisions=[], extensions=[])
        return out
    by_project = {}
    for gateway in scope:
        by_project.setdefault(gateway["project"], []).append(gateway)
    projects = sorted(by_project, key=lambda p: (p != ctx.project, p)) or [ctx.project]
    if len(projects) > MAX_TRAFFIC_PROJECTS:
        ctx.warn(SRC_REQUEST_LOG, "truncated", "Traffic is read for the first five gateway projects only.")
        projects = projects[:MAX_TRAFFIC_PROJECTS]
    window = 'timestamp>="%s" AND timestamp<"%s"' % (iso(rng.start_ts()), iso(rng.end_ts()))
    tasks = {}
    for project in projects:
        mine = by_project.get(project) or []
        flt = ('logName="projects/%s/logs/networkservices.googleapis.com%%2Fgateway_requests" AND '
               'resource.type="%s" AND NOT httpRequest.requestMethod="CONNECT" AND %s'
               % (project, GATEWAY_RESOURCE_TYPE, window))
        if selected:
            flt += ' AND resource.labels.gateway_name="%s" AND resource.labels.location="%s"' % (
                selected["name"], selected["location"])
        tasks[("req", project)] = (lambda p=project, f=flt: _read_log(ctx, p, f, MAX_REQUEST_ENTRIES))
        iap = ('logName="projects/%s/logs/cloudaudit.googleapis.com%%2Fdata_access" AND '
               'protoPayload.serviceName="%s" AND protoPayload.authorizationInfo.permission:"egressViaIAP" AND %s'
               % (project, IAP_SERVICE, window))
        tasks[("iap", project)] = (lambda p=project, f=iap: _read_log(ctx, p, f, MAX_DECISION_ENTRIES))
        if any(g["semantic"]["state"] in ("audit", "enforced") for g in mine):
            sgp = 'logName="projects/%s/logs/semantic-governance-policy" AND jsonPayload.verdict="DENY" AND %s' % (
                project, window)
            tasks[("sgp", project)] = (lambda p=project, f=sgp: _read_log(ctx, p, f, MAX_VERDICT_ENTRIES))
        if any(g["contentSecurity"]["state"] == "configured" for g in mine):
            armor = ('logName="projects/%s/logs/modelarmor.googleapis.com%%2Fsanitize_operations" AND '
                     'jsonPayload.sanitizationResult.filterMatchState="MATCH_FOUND" AND %s' % (project, window))
            tasks[("armor", project)] = (lambda p=project, f=armor: _read_log(ctx, p, f, MAX_VERDICT_ENTRIES))
        names = sorted({g["name"] for g in mine})
        if names:
            tasks[("metrics", project)] = (lambda p=project, n=names: _gateway_metrics(ctx, p, n, rng))
            tasks[("ext", project)] = (lambda p=project: _extension_metrics(ctx, p, rng))
        if not selected:
            tasks[("lat", project)] = (lambda p=project: _project_latency(ctx, p, rng))
    res = ctx.run(tasks)

    def take(kind, project, source, what, quiet=False):
        if (kind, project) not in res:
            return None
        ok, value = res[(kind, project)]
        if ok:
            ctx.add_source(source)
            return value
        if (project == ctx.project or len(projects) == 1) and not quiet:
            if _rate_limited(value):
                ctx.warn(source, "upstream_unavailable", MSG_LOG_QUOTA)
            else:
                ctx.warn_error(source, value, what)
        return None

    egress = {(g["project"], g["location"], g["name"]) for g in gateways if g["accessPath"] == "AGENT_TO_ANYWHERE"}
    requests, decisions = _Requests(rng, egress), _Decisions(rng)
    request_ok = decision_ok = extension_ok = False
    read = {"requests": 0, "decisions": 0}
    truncated = {"requests": False, "decisions": False}
    verdicts = {"sgp": None, "armor": None}
    metrics, extensions, latency = [], {}, {}
    for project in projects:
        value = take("req", project, SRC_REQUEST_LOG, "The gateway request log")
        if value is not None:
            request_ok = True
            read["requests"] += len(value[0])
            truncated["requests"] = truncated["requests"] or value[1]
            for entry in value[0]:
                requests.add(project, entry)
        value = take("iap", project, SRC_IAP_LOG, "Identity-Aware Proxy decisions")
        if value is not None:
            decision_ok = True
            read["decisions"] += len(value[0])
            truncated["decisions"] = truncated["decisions"] or value[1]
            for entry in value[0]:
                decisions.add(entry)
        for kind, source, what in (("sgp", SRC_SGP_LOG, "Semantic governance verdicts"),
                                   ("armor", SRC_ARMOR_LOG, "Model Armor results")):
            value = take(kind, project, source, what)
            if value is not None:
                verdicts[kind] = (verdicts[kind] or 0) + len(value[0])
        measured = take("metrics", project, SRC_METRICS, "Gateway request metrics")
        if measured is not None:
            metrics.append(measured)
        value = take("ext", project, SRC_EXTENSION_METRICS, "Service Extensions metrics")
        if value is not None:
            extension_ok = True
            for name, row in value.items():
                merged = extensions.setdefault(name, {"invocations": 0, "failedOpen": 0})
                merged["invocations"] += row["invocations"]
                merged["failedOpen"] += row["failedOpen"]
        # The metric the product documents for gateway latency has no documented labels, so it is read for the
        # whole project and only when the per-gateway metric has no samples.
        fallback = take("lat", project, SRC_METRICS, "Gateway latency metrics", quiet=True)
        for key in ("p50", "p95"):
            number = measured["latency"][key] if measured is not None else None
            if number is None and fallback is not None:
                number = fallback[key]
            if number is not None:
                latency[key] = max(latency.get(key, 0.0), number)
    for kind in ("requests", "decisions"):
        if truncated[kind]:
            ctx.warn(SRC_REQUEST_LOG if kind == "requests" else SRC_IAP_LOG, "truncated",
                     "Log entries were truncated at %d." % read[kind])
    kpis = out["kpis"]
    kpis["semanticDenials"], kpis["armorMatches"] = verdicts["sgp"], verdicts["armor"]
    metric_total = sum(m["total"] for m in metrics)
    if request_ok:
        out["sampled"]["requests"] = {"read": read["requests"], "truncated": truncated["requests"]}
        kpis.update(requests=requests.total, requestSource="logs", denied=requests.outcomes["denied"],
                    errors=requests.outcomes["server_error"], unregistered=requests.unregistered)
        out["requestTrend"] = requests.timeline()
        out["byGateway"] = requests.gateways
        out["destinations"] = sorted(
            ({"host": host, "requests": r["requests"], "denied": r["denied"],
              "registered": (r["registered"] == r["egress"]) if r["egress"] else None}
             for host, r in requests.hosts.items()),
            key=lambda r: (-r["requests"], r["host"]))[:MAX_ROWS]
        out["mcp"] = sorted(({"method": m, "tool": t, "requests": r["requests"], "denied": r["denied"]}
                             for (m, t), r in requests.mcp.items()),
                            key=lambda r: (-r["requests"], r["method"], r["tool"] or ""))[:MAX_ROWS]
        out["deniedRequests"] = sorted(requests.denied, key=lambda r: r["time"] or "", reverse=True)[:MAX_ROWS]
        kpis["latencyP50"] = _percentile(requests.latencies, 0.5)
        kpis["latencyP95"] = _percentile(requests.latencies, 0.95)
    if metrics and (metric_total or not request_ok):
        # Metrics count every request; the request log can be sampled or cut at the read limit.
        days = {key: [sum(m["days"][key][i] for m in metrics) for i in range(len(rng.buckets()))]
                for key, _ in OUTCOMES}
        by_gateway = {}
        for m in metrics:
            by_gateway.update(m["byGateway"])
        kpis.update(requests=metric_total, requestSource="metrics", denied=sum(days["denied"]),
                    errors=sum(days["server_error"]))
        out["requestTrend"] = timeutil.timeline(rng, [timeutil.series(k, n, days[k]) for k, n in OUTCOMES])
        out["byGateway"] = by_gateway
    if latency.get("p50") is not None or latency.get("p95") is not None:
        kpis["latencyP50"], kpis["latencyP95"] = latency.get("p50"), latency.get("p95")
    if out["byGateway"] is not None:
        names = {(g["project"], g["location"], g["name"]): g["key"] for g in gateways}
        out["byGateway"] = sorted(
            ({"key": names.get(key), "project": key[0], "location": key[1], "name": key[2], "requests": r["requests"],
              "denied": r["denied"], "errors": r["errors"]} for key, r in out["byGateway"].items()),
            key=lambda r: (-r["requests"], r["name"] or ""))
    if decision_ok:
        out["sampled"]["decisions"] = {"read": read["decisions"], "truncated": truncated["decisions"]}
        kpis.update(authorizations=decisions.total, authorizationFailures=decisions.failures,
                    failureRate=ratio(decisions.failures, decisions.total), wouldBlock=decisions.would_block)
        out["decisionTrend"] = decisions.timeline()
        out["decisions"] = sorted(
            ({"principal": principal, "resource": resource, "attempts": r["attempts"], "denied": r["denied"],
              "wouldBlock": r["wouldBlock"], "unregistered": r["unregistered"], "lastSeen": r["lastSeen"]}
             for (principal, resource), r in decisions.pairs.items() if r["denied"] or r["wouldBlock"]),
            key=lambda r: (-(r["denied"] + r["wouldBlock"]), r["principal"] or "", r["resource"] or ""))[:MAX_ROWS]
    if extension_ok:
        attached = {p["extension"]["name"]: p["extension"]["kind"] for g in scope for p in g["policies"] or []
                    if p["extension"]}
        out["extensions"] = sorted(
            ({"name": name, "kind": attached[name], "invocations": row["invocations"],
              "failedOpen": row["failedOpen"]} for name, row in extensions.items() if name in attached),
            key=lambda r: (-r["invocations"], r["name"]))
    return out


# ---------------------------------------------------------------------------
# Management (administrators): create, update, delete, access authorization
# ---------------------------------------------------------------------------

def _raise_for(kind, reason, action):
    """Curated error for a failed management call. Upstream text stays in the server log."""
    if kind == "denied":
        raise ApiError("upstream_error", MSG_DENIED)
    if kind == "credentials":
        raise ApiError("upstream_error", "Google Cloud credentials are unavailable.")
    if kind == "not_found":
        raise ApiError("not_found", "The gateway was not found.")
    if kind == "conflict" and (reason == "ALREADY_EXISTS" or action == "create"):
        raise ApiError("conflict", "A gateway with this name already exists in this project and region.")
    if kind == "conflict":
        raise ApiError("conflict", "The gateway changed since it was loaded. Reload and try again.")
    if kind == "precondition" or reason == "FAILED_PRECONDITION":
        raise ApiError("conflict", "Google Cloud refused the change because the gateway is still in use or "
                                   "another change is in progress.")
    if kind == "invalid":
        raise ApiError("bad_request", "Google Cloud rejected the gateway configuration.")
    if kind in ("unavailable", "timeout", "network"):
        raise ApiError("upstream_error", MSG_UNAVAILABLE)
    raise ApiError("upstream_error", "The operation failed in Google Cloud.")


def _raise_upstream(err, action):
    _raise_for(getattr(err, "kind", "http"), getattr(err, "reason", ""), action)


# google.rpc.Code of a failed long-running operation -> (kind, reason)
OPERATION_CODES = {3: ("invalid", ""), 5: ("not_found", ""), 6: ("conflict", "ALREADY_EXISTS"), 7: ("denied", ""),
                   9: ("precondition", "FAILED_PRECONDITION"), 10: ("conflict", "ABORTED"),
                   14: ("unavailable", ""), 4: ("timeout", "")}


def _operation_failure(op, action):
    """Raises the curated error of a finished operation that failed."""
    error = op.get("error") if isinstance(op, dict) else None
    if not isinstance(error, dict):
        return
    code = to_int(error.get("code"))
    LOG.warning("agent gateway operation failed action=%s code=%s message=%s", action, code,
                log_safe(error.get("message")))
    kind, reason = OPERATION_CODES.get(code, ("http", ""))
    _raise_for(kind, reason, action)


def _await(ctx, api, op, deadline, action):
    """Waits for a long-running operation until the request budget ends. Returns None when it finished, or
    {"api", "name"} for the client to poll."""
    name = op.get("name") if isinstance(op, dict) else None
    while isinstance(op, dict) and not op.get("done"):
        if not isinstance(name, str) or not OPERATION_RE.match(name):
            return None  # nothing to poll: the call answered without an operation
        if time.monotonic() >= deadline:
            return {"api": api, "name": name}
        time.sleep(POLL_SECONDS)
        try:
            op = ctx.rest("GET", "%s/v1/%s" % (API_HOSTS[api], name), timeout=15)
        except GcpError as err:
            _raise_upstream(err, action)
    _operation_failure(op, action)
    return None


class _Audit:
    """Writes one governance audit entry for a management call (also when it fails) and clears the caches."""

    def __init__(self, ctx, action, target, **details):
        self.ctx, self.action, self.target, self.details = ctx, action, target, details
        self.logged = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        payload = dict(self.details, target=self.target, result="success" if exc_type is None else "failed")
        if exc is not None:
            payload["error"] = getattr(exc, "code", None) or type(exc).__name__
        self.logged = governance.write_audit(self.ctx, self.action, payload)
        cache = getattr(self.ctx.app, "cache", None)
        if cache is not None:
            cache.clear()
        return False


def _target(ctx, body):
    project = str(body.get("project") or "").strip().lower()
    location = str(body.get("location") or "").strip().lower()
    name = str(body.get("name") or "").strip()
    if not config.valid_project(project) or project not in _workspace_projects(ctx):
        raise ApiError("bad_request", MSG_PROJECT)
    if len(location) > 40 or not REGION_RE.match(location):
        raise ApiError("bad_request", "Field 'location' must be a Google Cloud region.")
    if not GATEWAY_ID_RE.match(name):
        raise ApiError("bad_request", "The gateway name must start with a lowercase letter and contain only "
                                      "lowercase letters, digits and hyphens (up to 63 characters).")
    return project, location, name


def _clean_description(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 1024 or TEXT_CONTROL_RE.search(value):
        raise ApiError("bad_request", "Field 'description' must be text of at most 1024 characters.")
    return value.strip()


def _clean_labels(value):
    if value is None:
        return None
    if not isinstance(value, dict) or len(value) > 32:
        raise ApiError("bad_request", "Field 'labels' must be an object with at most 32 entries.")
    for key, item in value.items():
        if not (isinstance(key, str) and isinstance(item, str) and LABEL_KEY_RE.match(key)
                and LABEL_VALUE_RE.match(item)):
            raise ApiError("bad_request", "Labels use lowercase letters, digits, '_' and '-'. A key starts with a "
                                          "letter, and keys and values have at most 63 characters.")
    return dict(value)


def _registry(project, location):
    return "//agentregistry.googleapis.com/projects/%s/locations/%s" % (project, location)


def _child_id(name, suffix):
    """Resource ID derived from the gateway name, within the 63-character limit."""
    return name[:63 - len(suffix)].rstrip("-") + suffix


def create(ctx):
    """POST /api/v1/agent-platform/gateways {project, location, name, accessPath, description?, labels?,
    registry?}: creates a Google-managed gateway. An Agent-to-Anywhere gateway blocks all traffic until an
    authorization policy is attached (see access)."""
    require_identity_admin(ctx)
    body = ctx.body or {}
    project, location, name = _target(ctx, body)
    path = body.get("accessPath")
    if path not in ACCESS_PATHS:
        raise ApiError("bad_request", "Field 'accessPath' must be AGENT_TO_ANYWHERE or CLIENT_TO_AGENT.")
    description = _clean_description(body.get("description"))
    labels = _clean_labels(body.get("labels"))
    resource = {"protocols": ["MCP"], "googleManaged": {"governedAccessPath": path}}
    if description:
        resource["description"] = description
    if labels:
        resource["labels"] = labels
    if path == "AGENT_TO_ANYWHERE" and body.get("registry") is not False:
        resource["registries"] = [_registry(project, location)]
    deadline = time.monotonic() + OPERATION_WAIT_SECONDS
    with _Audit(ctx, "agent_gateway_create", gateway_key(project, location, name), accessPath=path) as audit:
        try:
            op = ctx.rest("POST", "%s/v1/projects/%s/locations/%s/agentGateways" % (NETWORK_SERVICES, project, location),
                          params={"agentGatewayId": name}, json_body=resource, timeout=45)
        except GcpError as err:
            _raise_upstream(err, "create")
        pending = _await(ctx, "networkservices", op, deadline, "create")
    return {"status": "pending" if pending else "done", "operation": pending, "auditLogged": audit.logged,
            "gateway": {"key": gateway_key(project, location, name), "project": project, "location": location,
                        "name": name, "accessPath": path}}


def update(ctx):
    """POST /api/v1/agent-platform/gateways/update {project, location, name, etag?, description?, labels?,
    registry?}: changes the mutable fields. Name, region, access path and network attachment cannot change."""
    require_identity_admin(ctx)
    body = ctx.body or {}
    project, location, name = _target(ctx, body)
    patch, mask = {}, []
    if "description" in body:
        patch["description"] = _clean_description(body.get("description")) or ""
        mask.append("description")
    if "labels" in body:
        patch["labels"] = _clean_labels(body.get("labels")) or {}
        mask.append("labels")
    if "registry" in body:
        if not isinstance(body.get("registry"), bool):
            raise ApiError("bad_request", "Field 'registry' must be true or false.")
        patch["registries"] = [_registry(project, location)] if body["registry"] else []
        mask.append("registries")
    if not mask:
        raise ApiError("bad_request", "Nothing to change.")
    etag = body.get("etag")
    if etag is not None and (not isinstance(etag, str) or len(etag) > 200 or config.CONTROL_RE.search(etag)):
        raise ApiError("bad_request", "Field 'etag' is invalid.")
    resource = "projects/%s/locations/%s/agentGateways/%s" % (project, location, name)
    patch["name"] = resource
    if etag:
        patch["etag"] = etag
    deadline = time.monotonic() + OPERATION_WAIT_SECONDS
    with _Audit(ctx, "agent_gateway_update", gateway_key(project, location, name), fields=mask) as audit:
        try:
            op = ctx.rest("PATCH", "%s/v1/%s" % (NETWORK_SERVICES, resource), params={"updateMask": ",".join(mask)},
                          json_body=patch, timeout=45)
        except GcpError as err:
            _raise_upstream(err, "update")
        pending = _await(ctx, "networkservices", op, deadline, "update")
    return {"status": "pending" if pending else "done", "operation": pending, "auditLogged": audit.logged}


def _governance_state(ctx, project, location):
    base = "projects/%s/locations/%s" % (project, location)
    extensions = {_short(e.get("name")): e for e in _list_all(
        ctx, "%s/v1/%s/authzExtensions" % (NETWORK_SERVICES, base), "authzExtensions")[0]}
    policies = _list_all(ctx, "%s/v1/%s/authzPolicies" % (NETWORK_SECURITY, base), "authzPolicies")[0]
    return base, extensions, policies


def access(ctx):
    """POST /api/v1/agent-platform/gateways/access {project, location, name, mode: audit|enforce}: access
    authorization of an Agent-to-Anywhere gateway through Identity-Aware Proxy with Unified Access Policies.

    Without an Identity-Aware Proxy policy it creates the authorization extension and then the authorization
    policy; with one it switches the extension between audit-only (DRY_RUN) and enforced. The call does the next
    step that is possible and reports a pending operation, so the client repeats it until the status is done."""
    require_identity_admin(ctx)
    body = ctx.body or {}
    project, location, name = _target(ctx, body)
    mode = body.get("mode")
    if mode not in ("audit", "enforce"):
        raise ApiError("bad_request", "Field 'mode' must be 'audit' or 'enforce'.")
    suffix = "/locations/%s/agentGateways/%s" % (location, name)
    deadline = time.monotonic() + OPERATION_WAIT_SECONDS
    with _Audit(ctx, "agent_gateway_access", gateway_key(project, location, name), mode=mode) as audit:
        try:
            gateway = ctx.rest("GET", "%s/v1/projects/%s/locations/%s/agentGateways/%s" % (
                NETWORK_SERVICES, project, location, name), timeout=30)
            base, extensions, policies = _governance_state(ctx, project, location)
        except GcpError as err:
            _raise_upstream(err, "access")
        managed = gateway.get("googleManaged") if isinstance(gateway.get("googleManaged"), dict) else {}
        if managed.get("governedAccessPath") != "AGENT_TO_ANYWHERE":
            raise ApiError("bad_request", "Access authorization applies to Agent-to-Anywhere gateways only.")

        def is_iap(ext_name):
            return (extensions.get(ext_name) or {}).get("service") == IAP_SERVICE

        mine = [p for p in policies if p.get("action") == "CUSTOM" and any(t.endswith(suffix) for t in _policy_targets(p))
                and any(is_iap(r) for r in _policy_extensions(p))]
        ext_id, policy_id = _child_id(name, "-iap-authz-ext"), _child_id(name, "-iap-authzpolicy")
        names = sorted({r for p in mine for r in _policy_extensions(p) if is_iap(r)})
        if not mine and ext_id in extensions:
            if not is_iap(ext_id):
                raise ApiError("conflict", "An authorization extension with the name reserved for this gateway "
                                           "already exists and does not use Identity-Aware Proxy.")
            names = [ext_id]
        for policy in policies:
            if set(_policy_extensions(policy)) & set(names) and any(not t.endswith(suffix) for t in _policy_targets(policy)):
                raise ApiError("conflict", "The authorization extension of this gateway is shared with another "
                                           "gateway. Change it in the Google Cloud console.")
        result = {"status": "done", "mode": mode, "operation": None, "step": "mode", "changed": []}
        try:
            for ext_name in names:
                meta = dict(extensions[ext_name].get("metadata") or {})
                if ("audit" if meta.get("iamEnforcementMode") == "DRY_RUN" else "enforce") == mode:
                    continue
                # updateMask=metadata replaces the whole map, so every other key is sent back unchanged.
                if mode == "audit":
                    meta["iamEnforcementMode"] = "DRY_RUN"
                else:
                    meta.pop("iamEnforcementMode", None)
                op = ctx.rest("PATCH", "%s/v1/%s/authzExtensions/%s" % (NETWORK_SERVICES, base, ext_name),
                              params={"updateMask": "metadata"}, json_body={"metadata": meta}, timeout=30)
                result["changed"].append(ext_name)
                pending = _await(ctx, "networkservices", op, deadline, "access")
                if pending:
                    return dict(result, status="pending", operation=pending)
            if not names:
                meta = {"iapPolicyVersion": "V2"}
                if mode == "audit":
                    meta["iamEnforcementMode"] = "DRY_RUN"
                op = ctx.rest("POST", "%s/v1/%s/authzExtensions" % (NETWORK_SERVICES, base),
                              params={"authzExtensionId": ext_id},
                              json_body={"service": IAP_SERVICE, "failOpen": False, "timeout": "1s", "metadata": meta},
                              timeout=30)
                result.update(step="extension", changed=[ext_id])
                pending = _await(ctx, "networkservices", op, deadline, "access")
                if pending:
                    return dict(result, status="pending", operation=pending)
            if not mine:
                op = ctx.rest("POST", "%s/v1/%s/authzPolicies" % (NETWORK_SECURITY, base),
                              params={"authzPolicyId": policy_id},
                              json_body={"target": {"resources": ["%s/agentGateways/%s" % (base, name)]},
                                         "policyProfile": "REQUEST_AUTHZ", "action": "CUSTOM",
                                         "customProvider": {"authzExtension": {
                                             "resources": ["%s/authzExtensions/%s" % (base, (names or [ext_id])[0])]}}},
                              timeout=30)
                result.update(step="policy", policy=policy_id)
                pending = _await(ctx, "networksecurity", op, deadline, "access")
                if pending:
                    return dict(result, status="pending", operation=pending)
        except GcpError as err:
            _raise_upstream(err, "access")
    result["auditLogged"] = audit.logged
    return result


def delete(ctx):
    """POST /api/v1/agent-platform/gateways/delete {project, location, name, removePolicies?, cleanup?}.

    Google Cloud refuses to delete a gateway that agents, authorization policies or telemetry policies still
    reference. With removePolicies the authorization policies that target only this gateway are deleted first,
    then the extensions no other policy uses, then the gateway. Policies are never removed while a workload
    is bound: an Agent-to-Anywhere gateway without a policy blocks all traffic."""
    require_identity_admin(ctx)
    body = ctx.body or {}
    project, location, name = _target(ctx, body)
    remove = body.get("removePolicies") is True
    cleanup = body.get("cleanup") or []
    if not isinstance(cleanup, list) or len(cleanup) > 20 or not all(
            isinstance(c, str) and GATEWAY_ID_RE.match(c) for c in cleanup):
        raise ApiError("bad_request", "Field 'cleanup' must list authorization extension names.")
    key = gateway_key(project, location, name)
    suffix = "/locations/%s/agentGateways/%s" % (location, name)
    deadline = time.monotonic() + OPERATION_WAIT_SECONDS
    with _Audit(ctx, "agent_gateway_delete", key, removePolicies=remove) as audit:
        try:
            gateway = ctx.rest("GET", "%s/v1/projects/%s/locations/%s/agentGateways/%s" % (
                NETWORK_SERVICES, project, location, name), timeout=30)
            base, extensions, policies = _governance_state(ctx, project, location)
        except GcpError as err:
            _raise_upstream(err, "delete")
        try:
            telemetry = _list_all(ctx, "%s/v1beta1/%s/telemetryPolicies" % (NETWORK_SERVICES, base),
                                  "telemetryPolicies")[0]
        except GcpError:
            telemetry = []  # Google Cloud still refuses the deletion when a telemetry policy targets the gateway
        if _telemetry(telemetry, suffix)["policies"]:
            raise ApiError("conflict", "A telemetry policy still targets this gateway. Delete the telemetry policy "
                                       "first.")
        mine = [p for p in policies if any(t.endswith(suffix) for t in _policy_targets(p))]
        if mine and not remove:
            raise ApiError("conflict", "Authorization policies still target this gateway. Delete them together "
                                       "with the gateway or remove them first.")
        if any(not t.endswith(suffix) for p in mine for t in _policy_targets(p)):
            raise ApiError("conflict", "An authorization policy of this gateway also targets other resources. "
                                       "Remove the gateway from that policy in the Google Cloud console.")
        if mine or cleanup:
            parsed = _parse_gateway(gateway, project) or {"project": project, "projectRef": project}
            if _referenced(ctx, parsed, project, location, name):
                raise ApiError("conflict", MSG_IN_USE)
        result = {"status": "done", "operation": None, "step": "gateway", "removedPolicies": [], "cleanup": []}
        try:
            others = {r for p in policies if p not in mine for r in _policy_extensions(p)}
            orphans = sorted(({r for p in mine for r in _policy_extensions(p)} | set(cleanup)) - others)
            result["cleanup"] = orphans
            for policy in mine:
                policy_id = _short(policy.get("name"))
                op = ctx.rest("DELETE", "%s/v1/%s/authzPolicies/%s" % (NETWORK_SECURITY, base, policy_id), timeout=30)
                result["removedPolicies"].append(policy_id)
                pending = _await(ctx, "networksecurity", op, deadline, "delete")
                if pending:
                    return dict(result, status="pending", operation=pending, step="policies")
            for ext_name in orphans:
                if ext_name not in extensions:
                    continue
                op = ctx.rest("DELETE", "%s/v1/%s/authzExtensions/%s" % (NETWORK_SERVICES, base, ext_name), timeout=30)
                pending = _await(ctx, "networkservices", op, deadline, "delete")
                if pending:
                    return dict(result, status="pending", operation=pending, step="extensions")
            etag = gateway.get("etag")
            op = ctx.rest("DELETE", "%s/v1/%s/agentGateways/%s" % (NETWORK_SERVICES, base, name),
                          params={"etag": etag} if isinstance(etag, str) and etag else None, timeout=45)
            result["cleanup"] = []
            pending = _await(ctx, "networkservices", op, deadline, "delete")
            if pending:
                return dict(result, status="pending", operation=pending, deleting=True)
        except GcpError as err:
            _raise_upstream(err, "delete")
    result["auditLogged"] = audit.logged
    return result


def operation(ctx):
    """GET /api/v1/agent-platform/gateways/operation?api=networkservices|networksecurity&name=projects/...:
    state of a long-running operation that a management call reported as pending."""
    if not ctx.identity.can_write:
        raise ApiError("forbidden", "Administrator privileges are required.")
    api = ctx.params.get("api")
    name = ctx.params.get("name") or ""
    match = OPERATION_RE.match(name)
    if api not in API_HOSTS or not match:
        raise ApiError("bad_request", "Parameters 'api' and 'name' must identify an operation.")
    if match.group(1) not in _workspace_projects(ctx):
        raise ApiError("bad_request", MSG_PROJECT)
    try:
        op = ctx.rest("GET", "%s/v1/%s" % (API_HOSTS[api], name), timeout=15)
    except GcpError as err:
        _raise_upstream(err, "operation")
    done = bool(op.get("done"))
    message = None
    if done:
        cache = getattr(ctx.app, "cache", None)
        if cache is not None:
            cache.clear()
        try:
            _operation_failure(op, "operation")
        except ApiError as err:
            message = err.message
    return {"done": done, "failed": message is not None, "error": message}
