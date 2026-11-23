"""Agent Gateway (services/agent_gateway.py): inventory and posture, traffic, and the management calls.

The real application runs on 127.0.0.1 against an in-memory model of the Google Cloud APIs it calls (Network
Services agentGateways / authzExtensions / telemetryPolicies / operations, Network Security authzPolicies,
Agent Platform reasoningEngines, Discovery Engine engines, Cloud Logging, Cloud Monitoring). Payloads follow
the documented resource shapes. No network call is made.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane import app as app_module
from controlplane.gcp import GcpError
from controlplane.security import Identity
from controlplane.services import agent_gateway

PROJECT = "demo-project"
LINKED = "linked-project"
REGION = "us-central1"
OTHER_REGION = "asia-northeast3"
RANGE = "start=2026-10-13&end=2026-10-19"
UPSTREAM_SECRET = "upstream detail that must stay in the server log"

CONFIG = """workspace:
  project_id: demo-project
  projects: [linked-project]
  scan_all_projects: false
  audit_dataset: ge_analytics
  timezone: UTC
  agent_engine_regions: [us-central1, asia-northeast3]
security:
  auth_mode: local
storage:
  backend: memory
"""

ADMIN = Identity("admin@example.com", "admin", "google", "user:admin@example.com")
VIEWER = Identity("viewer@example.com", "viewer", "google", "user:viewer@example.com")

RESOURCE_RE = re.compile(r"^/(v1|v1beta1)/projects/([^/]+)/locations/([^/]+)/"
                         r"(agentGateways|authzExtensions|authzPolicies|telemetryPolicies|operations)(?:/([^/]+))?$")


def gateway_name(name, project=PROJECT, location=REGION):
    return "projects/%s/locations/%s/agentGateways/%s" % (project, location, name)


def registry(project=PROJECT, location=REGION):
    return "//agentregistry.googleapis.com/projects/%s/locations/%s" % (project, location)


def engine(engine_id, display, egress=None, ingress=None, created="2026-06-01T00:00:00Z", region=REGION):
    binding = {}
    if egress:
        binding["agentToAnywhereConfig"] = {"agentGateway": egress}
    if ingress:
        binding["clientToAgentConfig"] = {"agentGateway": ingress}
    spec = {"deploymentSpec": {"agentGatewayConfig": binding}} if binding else {"deploymentSpec": {}}
    return {"name": "projects/987654321098/locations/%s/reasoningEngines/%s" % (region, engine_id),
            "displayName": display, "createTime": created, "spec": spec}


def request_entry(ts, gateway, status, url, registered=None, denied_by=None, mcp=None, location=REGION):
    payload = {"agentGatewayInfo": {}}
    if registered:
        payload["agentRegistryResource"] = registered
    payload["authzPolicyInfo"] = {"policies": [{"name": denied_by or "iap", "result": "DENIED" if denied_by else "ALLOWED"}]}
    if mcp:
        payload["mcpInfo"] = mcp
    return {"timestamp": ts, "trace": "projects/%s/traces/%s" % (PROJECT, "a" * 32),
            "httpRequest": {"status": status, "requestUrl": url, "requestMethod": "POST", "latency": "0.200s"},
            "resource": {"type": "networkservices.googleapis.com/Gateway",
                         "labels": {"gateway_name": gateway, "location": location,
                                    "gateway_type": "SECURE_WEB_GATEWAY"}},
            "jsonPayload": payload}


def decision_entry(ts, principal, resource, granted, dry_run=False, unregistered=False):
    return {"timestamp": ts,
            "labels": {"iap.googleapis.com/audited_resource_name": "unregisteredResource"} if unregistered else {},
            "protoPayload": {"serviceName": "iap.googleapis.com",
                             "authenticationInfo": {"principalSubject": principal},
                             "authorizationInfo": [{"resource": resource, "granted": granted,
                                                    "permission": "iap.resources.egressViaIAP"}],
                             "metadata": {"dryRun": True} if dry_run else {}}}


def point(day, value):
    return {"interval": {"startTime": day + "T00:00:00Z", "endTime": day + "T23:59:59Z"}, "value": value}


class Cloud:
    """Fake Google Cloud: an in-memory model that answers by URL and records every call."""

    def __init__(self):
        self.calls = []
        self.audit = []
        self.gateways = {}    # (project, location, id) -> AgentGateway
        self.extensions = {}  # (project, location, id) -> AuthzExtension
        self.policies = {}    # (project, location, id) -> AuthzPolicy
        self.telemetry = {}   # (project, location) -> [TelemetryPolicy]
        self.engines = {}     # (project, region) -> [ReasoningEngine]
        self.apps = {}        # (project, location) -> [Engine]
        self.sinks = {}       # project -> [LogSink]
        self.logs = {"gateway_requests": [], "data_access": [], "semantic-governance-policy": [],
                     "sanitize_operations": []}
        self.series = {}      # metric type -> [TimeSeries]
        self.operations = {}  # name -> {"polls": remaining polls, "error": status or None}
        self.fail = {}        # substring of "METHOD url" -> GcpError
        self.wildcard = True  # the all-locations list (locations/-) is accepted
        self.slow = 0         # polls before a new operation is done
        self.next_error = None
        self.seq = 0

    # -- seeding -------------------------------------------------------------
    def add_gateway(self, name, path="AGENT_TO_ANYWHERE", project=PROJECT, location=REGION, **fields):
        resource = {"name": gateway_name(name, project, location), "protocols": ["MCP"],
                    "googleManaged": {"governedAccessPath": path}, "etag": "etag-%s" % name,
                    "createTime": "2026-09-01T00:00:00Z", "updateTime": "2026-10-01T00:00:00Z"}
        if path == "AGENT_TO_ANYWHERE":
            resource["registries"] = [registry(project, location)]
        resource.update(fields)
        self.gateways[(project, location, name)] = resource
        return resource

    def add_extension(self, name, service="iap.googleapis.com", metadata=None, project=PROJECT, location=REGION,
                      **fields):
        resource = {"name": "projects/%s/locations/%s/authzExtensions/%s" % (project, location, name),
                    "service": service, "failOpen": False, "timeout": "1s", "metadata": dict(metadata or {})}
        resource.update(fields)
        self.extensions[(project, location, name)] = resource

    def add_policy(self, name, gateways, extension=None, profile="REQUEST_AUTHZ", action="CUSTOM", project=PROJECT,
                   location=REGION, **fields):
        resource = {"name": "projects/%s/locations/%s/authzPolicies/%s" % (project, location, name),
                    "target": {"resources": [gateway_name(g, project, location) for g in gateways]},
                    "policyProfile": profile, "action": action}
        if extension:
            resource["customProvider"] = {"authzExtension": {"resources": [
                "projects/%s/locations/%s/authzExtensions/%s" % (project, location, extension)]}}
        resource.update(fields)
        self.policies[(project, location, name)] = resource

    # -- helpers -------------------------------------------------------------
    def find(self, method, needle):
        return [c for c in self.calls if c[0] == method and needle in c[1]]

    def _operation(self, project, location):
        self.seq += 1
        name = "projects/%s/locations/%s/operations/operation-%d" % (project, location, self.seq)
        self.operations[name] = {"polls": self.slow, "error": self.next_error}
        self.next_error = None
        return self._operation_state(name)

    def _operation_state(self, name):
        state = self.operations[name]
        out = {"name": name, "done": state["polls"] <= 0}
        if out["done"] and state["error"]:
            out["error"] = state["error"]
        return out

    def _entries(self, flt):
        for key, entries in self.logs.items():
            if key in flt:
                named = re.search(r'resource\.labels\.gateway_name="([^"]+)"', flt)
                if named:
                    entries = [e for e in entries if e["resource"]["labels"]["gateway_name"] == named.group(1)]
                return sorted(entries, key=lambda e: e["timestamp"], reverse=True)
        return []

    # -- the API -------------------------------------------------------------
    def request(self, method, url, **kwargs):
        params = kwargs.get("params")
        params = dict(params) if params else {}
        body = kwargs.get("json_body")
        self.calls.append((method, url, params, body))
        for needle, err in self.fail.items():
            if needle in "%s %s" % (method, url):
                raise err
        parsed = urllib.parse.urlsplit(url)
        host, path = parsed.hostname, parsed.path
        if path.endswith("/entries:write"):
            self.audit.extend(e["jsonPayload"] for e in body["entries"])
            return {}, {}
        if path.endswith("/entries:list"):
            return {"entries": self._entries(body["filter"])}, {}
        if host == "monitoring.googleapis.com":
            metric = params["filter"].split('metric.type="', 1)[1].split('"', 1)[0]
            return {"timeSeries": self.series.get(metric, []) if "/%s/" % PROJECT in path else []}, {}
        if host == "logging.googleapis.com" and path.endswith("/sinks"):
            return {"sinks": self.sinks.get(path.split("/")[3], [])}, {}
        if host.endswith("-aiplatform.googleapis.com") and path.endswith("/reasoningEngines"):
            parts = path.split("/")
            return {"reasoningEngines": self.engines.get((parts[3], parts[5]), [])}, {}
        if "discoveryengine" in host and path.endswith("/engines"):
            parts = path.split("/")
            return {"engines": self.apps.get((parts[3], parts[5]), [])}, {}
        match = RESOURCE_RE.match(path)
        if not match:
            return {}, {}
        _, project, location, collection, item = match.groups()
        if collection == "operations":
            state = self.operations[path.split("/", 2)[2]]
            state["polls"] -= 1
            return self._operation_state(path.split("/", 2)[2]), {}
        if collection == "telemetryPolicies":
            return {"telemetryPolicies": self.telemetry.get((project, location), [])}, {}
        store = {"agentGateways": self.gateways, "authzExtensions": self.extensions,
                 "authzPolicies": self.policies}[collection]
        if method == "GET" and item is None:
            if location == "-":
                if not self.wildcard:
                    raise GcpError("invalid", status=400, message=UPSTREAM_SECRET)
                found = [v for (p, _, _), v in store.items() if p == project]
            else:
                found = [v for (p, loc, _), v in store.items() if p == project and loc == location]
            return {collection: found}, {}
        if method == "POST":
            item = params["agentGatewayId" if collection == "agentGateways" else
                          "authzExtensionId" if collection == "authzExtensions" else "authzPolicyId"]
            if (project, location, item) in store:
                raise GcpError("conflict", status=409, message=UPSTREAM_SECRET, reason="ALREADY_EXISTS")
            store[(project, location, item)] = dict(
                body, name="projects/%s/locations/%s/%s/%s" % (project, location, collection, item))
            return self._operation(project, location), {}
        if (project, location, item) not in store:
            raise GcpError("not_found", status=404, message=UPSTREAM_SECRET)
        if method == "GET":
            return store[(project, location, item)], {}
        if method == "PATCH":
            for field in params["updateMask"].split(","):
                store[(project, location, item)][field] = body[field]
            return self._operation(project, location), {}
        if method == "DELETE":
            suffix = "/locations/%s/agentGateways/%s" % (location, item)
            if collection == "agentGateways" and any(
                    t.endswith(suffix) for (p, _, _), pol in self.policies.items() if p == project
                    for t in pol["target"]["resources"]):
                raise GcpError("invalid", status=400, message=UPSTREAM_SECRET, reason="FAILED_PRECONDITION")
            del store[(project, location, item)]
            return self._operation(project, location), {}
        raise AssertionError("unexpected call %s %s" % (method, url))


class HelpersTest(unittest.TestCase):
    def test_extension_kinds_and_enforcement_modes(self):
        iap = agent_gateway._extension_summary({"name": "p/x", "service": "iap.googleapis.com",
                                                "metadata": {"iapPolicyVersion": "V2"}})
        self.assertEqual((iap["kind"], iap["mode"], iap["policyModel"]), ("iap", "enforced", "V2"))
        # The console writes ENFORCED when it creates a gateway: anything other than DRY_RUN enforces.
        for value, mode in (("DRY_RUN", "audit"), ("ENFORCED", "enforced")):
            ext = agent_gateway._extension_summary({"service": "iap.googleapis.com",
                                                    "metadata": {"iamEnforcementMode": value}})
            self.assertEqual(ext["mode"], mode)
        armor = agent_gateway._extension_summary({
            "service": "modelarmor.us-central1.rep.googleapis.com", "failOpen": True,
            "metadata": {"model_armor_settings": json.dumps([{
                "request_template_id": "projects/p/locations/us-central1/templates/req",
                "response_template_id": "projects/p/locations/us-central1/templates/resp"}])}})
        self.assertEqual((armor["kind"], armor["failOpen"]), ("model_armor", True))
        self.assertTrue(armor["requestTemplate"].endswith("/req"))
        self.assertTrue(armor["responseTemplate"].endswith("/resp"))
        sgp = agent_gateway._extension_summary({"service": "us-central1.internal.example.com",
                                                "metadata": {"sgpEnforcementMode": "DRY_RUN"}})
        self.assertEqual((sgp["kind"], sgp["mode"]), ("semantic_governance", "audit"))
        custom = agent_gateway._extension_summary({"service": "mycustomauthz.internal.net"})
        self.assertEqual((custom["kind"], custom["mode"]), ("custom", None))
        broken = agent_gateway._extension_summary({"service": "modelarmor.us-central1.rep.googleapis.com",
                                                   "metadata": {"model_armor_settings": "not json"}})
        self.assertEqual((broken["kind"], broken["requestTemplate"]), ("model_armor", None))

    def test_registry_must_be_the_regional_registry_of_the_gateway(self):
        def state(registries, path="AGENT_TO_ANYWHERE", ref=PROJECT):
            return agent_gateway._registry_state({"accessPath": path, "registries": registries, "project": PROJECT,
                                                  "projectRef": ref, "location": REGION})
        self.assertEqual(state([registry()]), "ok")
        self.assertEqual(state([registry() + "/"]), "ok")
        self.assertEqual(state([]), "missing")
        self.assertEqual(state([registry(location="europe-west1")]), "mismatch")
        self.assertEqual(state([registry(project="other-project")]), "mismatch")
        self.assertEqual(state(["//agentregistry.googleapis.com/projects/p"]), "mismatch")
        # A project number cannot be compared with a project ID.
        self.assertEqual(state([registry(project="987654321098")]), "ok")
        self.assertEqual(state([registry(project="111")], ref="987654321098"), "mismatch")
        self.assertEqual(state([], path="CLIENT_TO_AGENT"), "not_used")

    def test_child_resource_ids_stay_within_the_length_limit(self):
        long_name = "g" + "a" * 61 + "z"
        for suffix in ("-iap-authz-ext", "-iap-authzpolicy"):
            child = agent_gateway._child_id(long_name, suffix)
            self.assertLessEqual(len(child), 63)
            self.assertTrue(child.endswith(suffix))
            self.assertRegex(child, agent_gateway.GATEWAY_ID_RE)
        self.assertEqual(agent_gateway._child_id("egress", "-iap-authzpolicy"), "egress-iap-authzpolicy")

    def test_url_parts_drop_the_query_string(self):
        self.assertEqual(agent_gateway._url_parts("https://api.example.com/v1/items?token=secret"),
                         ("api.example.com", "/v1/items"))
        self.assertEqual(agent_gateway._url_parts("api.example.com:443"), ("api.example.com", "/"))
        self.assertEqual(agent_gateway._url_parts(None), (None, None))


class AgentGatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = os.path.join(cls.tmp.name, "config.yaml")
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write(CONFIG)
        with mock.patch.dict(os.environ, {"CONFIG_PATH": cfg}):
            cls.app = app_module.App(environ={"CONFIG_PATH": cfg})
        cls.server = app_module.make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.poll, cls.wait = agent_gateway.POLL_SECONDS, agent_gateway.OPERATION_WAIT_SECONDS
        agent_gateway.POLL_SECONDS = 0

    @classmethod
    def tearDownClass(cls):
        agent_gateway.POLL_SECONDS, agent_gateway.OPERATION_WAIT_SECONDS = cls.poll, cls.wait
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.cloud = Cloud()
        self.app.gcp.request = self.cloud.request
        self.app.cache.clear()
        self.app.limiter = app_module.RateLimiter()
        agent_gateway.OPERATION_WAIT_SECONDS = self.wait

    def call(self, method, path, body=None, identity=None, csrf=True):
        headers = {}
        raw = None
        if method == "POST":
            raw = json.dumps(body or {})
            headers["Content-Type"] = "application/json"
            if csrf:
                token = "t" * 40
                headers.update({"X-CSRF-Token": token, "Cookie": "cp_csrf=%s" % token})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            with mock.patch.object(app_module, "authenticate", return_value=identity or ADMIN):
                conn.request(method, path, body=raw, headers=headers)
                resp = conn.getresponse()
                return resp.status, json.loads(resp.read() or b"{}")
        finally:
            conn.close()

    def data(self, path, identity=None):
        status, payload = self.call("GET", path, identity=identity)
        self.assertEqual(status, 200, payload)
        return payload["data"], {w["code"] for w in payload["meta"]["warnings"]}, payload["meta"]

    def post(self, path, body, **kwargs):
        return self.call("POST", "/api/v1/agent-platform/gateways" + path, body, **kwargs)

    def seed(self):
        """Three gateways: an enforced egress gateway with Model Armor and tracing, an egress gateway in
        audit-only mode, and an ingress gateway without a policy."""
        cloud = self.cloud
        cloud.add_gateway("egress-prod", description="Production egress", labels={"env": "prod"},
                          agentGatewayCard={"mtlsEndpoint": "egress-prod.example.goog",
                                            "rootCertificates": ["cert-a", "cert-b"],
                                            "serviceExtensionsServiceAccount":
                                                "service-1@gcp-sa-dep.iam.gserviceaccount.com"})
        cloud.add_gateway("egress-audit", registries=[registry(location="europe-west1")])
        cloud.add_gateway("ingress", path="CLIENT_TO_AGENT")
        cloud.add_extension("prod-iap", metadata={"iapPolicyVersion": "V2"})
        cloud.add_extension("prod-armor", service="modelarmor.us-central1.rep.googleapis.com", metadata={
            "model_armor_settings": json.dumps([{
                "request_template_id": "projects/demo-project/locations/us-central1/templates/in",
                "response_template_id": "projects/demo-project/locations/us-central1/templates/out"}])})
        cloud.add_extension("audit-iap", metadata={"iapPolicyVersion": "V1", "iamEnforcementMode": "DRY_RUN"},
                            failOpen=True)
        cloud.add_policy("prod-iap-policy", ["egress-prod"], "prod-iap")
        cloud.add_policy("prod-armor-policy", ["egress-prod"], "prod-armor", profile="CONTENT_AUTHZ")
        cloud.add_policy("prod-tools", ["egress-prod"], action="ALLOW", httpRules=[{"to": {"operations": [{"mcp": {
            "baseProtocolMethodsOption": "MATCH_BASE_PROTOCOL_METHODS",
            "methods": [{"name": "tools/list"}, {"name": "tools/call", "params": [{"exact": "get_weather"}]}]}}]}}])
        cloud.add_policy("audit-iap-policy", ["egress-audit"], "audit-iap")
        cloud.telemetry[(PROJECT, REGION)] = [{
            "name": "projects/demo-project/locations/us-central1/telemetryPolicies/trace-prod",
            "telemetryTarget": {"resources": ["//networkservices.googleapis.com/" + gateway_name("egress-prod")]},
            "tracingConfiguration": {"samplingRate": 0.01, "parentBasedSampling": {"enabled": True,
                                                                                   "samplingRate": 1.0}}}]
        cloud.engines[(PROJECT, REGION)] = [
            engine("111", "support-agent", egress=gateway_name("egress-prod"), ingress=gateway_name("ingress")),
            engine("222", "legacy-agent", created="2026-03-01T00:00:00Z"),
            # Bound through the project number: the gateway of the same name and region is the match.
            engine("333", "billing-agent", egress=gateway_name("egress-audit", project="987654321098"))]
        cloud.apps[(PROJECT, "global")] = [
            {"name": "projects/987654321098/locations/global/collections/default_collection/engines/ge-app",
             "displayName": "Company assistant", "appType": "APP_TYPE_INTRANET",
             "agentGatewaySetting": {"defaultEgressAgentGateway": {"name": gateway_name("egress-prod")}}},
            {"name": "projects/987654321098/locations/global/collections/default_collection/engines/ge-open",
             "displayName": "Open assistant", "appType": "APP_TYPE_INTRANET"},
            {"name": "projects/987654321098/locations/global/collections/default_collection/engines/site-search",
             "displayName": "Site search", "solutionType": "SOLUTION_TYPE_SEARCH"}]
        cloud.sinks[PROJECT] = [
            {"name": "gateway-siem", "destination": "pubsub.googleapis.com/projects/demo-project/topics/siem",
             "filter": 'logName:("networkservices.googleapis.com%2Fgateway_requests" OR '
                       '"cloudaudit.googleapis.com%2Fdata_access")'},
            {"name": "unrelated", "destination": "bigquery.googleapis.com/projects/demo-project/datasets/x",
             "filter": 'resource.type="gce_instance"'}]

    # -- inventory -----------------------------------------------------------
    def test_inventory_reads_gateways_policies_and_posture(self):
        self.seed()
        data, codes, meta = self.data("/api/v1/agent-platform/gateways")
        self.assertEqual(codes, set())
        self.assertEqual(data["status"], "ok")
        self.assertEqual([g["name"] for g in data["gateways"]], ["egress-audit", "egress-prod", "ingress"])
        self.assertIn("api:networkservices.agentGateways", meta["sources"])
        self.assertIn("api:networksecurity.authzPolicies", meta["sources"])
        gateways = {g["name"]: g for g in data["gateways"]}

        prod = gateways["egress-prod"]
        self.assertEqual((prod["key"], prod["deployment"], prod["accessPath"]),
                         ("demo-project/us-central1/egress-prod", "google_managed", "AGENT_TO_ANYWHERE"))
        self.assertEqual(prod["access"], {"state": "enforced", "policyModel": "V2", "policy": "prod-iap-policy",
                                          "extension": "prod-iap"})
        self.assertEqual(prod["contentSecurity"]["state"], "configured")
        self.assertTrue(prod["contentSecurity"]["requestTemplate"].endswith("/templates/in"))
        self.assertEqual(prod["registryState"], "ok")
        self.assertEqual(prod["telemetry"]["state"], "configured")
        self.assertEqual((prod["telemetry"]["samplingRate"], prod["telemetry"]["parentBased"]), (0.01, True))
        self.assertEqual(prod["endpoint"], {"mtls": "egress-prod.example.goog", "rootCertificates": 2,
                                            "serviceAccount": "service-1@gcp-sa-dep.iam.gserviceaccount.com"})
        self.assertEqual({p["name"]: p["kind"] for p in prod["policies"]},
                         {"prod-iap-policy": "iap", "prod-armor-policy": "model_armor", "prod-tools": "rule"})
        tools = next(p for p in prod["policies"] if p["name"] == "prod-tools")
        self.assertEqual((tools["action"], tools["mcp"]), ("ALLOW", ["tools/list", "tools/call: get_weather"]))
        self.assertEqual(prod["findings"], [])
        self.assertEqual(prod["severity"], "ok")
        self.assertEqual({(w["displayName"], w["path"]) for w in prod["workloads"]},
                         {("support-agent", "egress"), ("Company assistant", "egress")})

        audit = gateways["egress-audit"]
        self.assertEqual((audit["access"]["state"], audit["access"]["policyModel"]), ("audit", "V1"))
        self.assertEqual({f["code"] for f in audit["findings"]}, {
            "access_audit_only", "legacy_policy_model", "fail_open", "no_content_security", "registry_mismatch",
            "no_tracing"})
        self.assertEqual(audit["severity"], "medium")
        self.assertEqual([w["displayName"] for w in audit["workloads"]], ["billing-agent"])

        ingress = gateways["ingress"]
        self.assertEqual((ingress["accessPath"], ingress["registryState"], ingress["access"]["state"]),
                         ("CLIENT_TO_AGENT", "not_used", "none"))
        self.assertEqual({f["code"] for f in ingress["findings"]}, {"ingress_unscreened", "no_tracing"})
        self.assertEqual([(w["displayName"], w["path"]) for w in ingress["workloads"]],
                         [("support-agent", "ingress")])

        self.assertEqual(data["kpis"], {
            "gateways": 3, "egress": 2, "ingress": 1, "enforced": 1, "audit": 1, "unprotected": 0, "workloads": 5,
            "governed": 3, "findings": {"high": 0, "medium": 4, "low": 2, "info": 2}})
        workloads = {w["displayName"]: w for w in data["workloads"]}
        # A search engine is not a Gemini Enterprise app, so it is not reported as ungoverned.
        self.assertEqual(sorted(workloads), ["Company assistant", "Open assistant", "billing-agent",
                                             "legacy-agent", "support-agent"])
        self.assertEqual(workloads["support-agent"]["egress"]["gateway"], "demo-project/us-central1/egress-prod")
        self.assertEqual(workloads["support-agent"]["ingress"]["gateway"], "demo-project/us-central1/ingress")
        self.assertEqual(workloads["billing-agent"]["egress"]["gateway"], "demo-project/us-central1/egress-audit")
        self.assertIsNone(workloads["legacy-agent"]["egress"])
        # Reasoning engines created before 29 April 2026 cannot be bound to a gateway.
        self.assertIs(workloads["legacy-agent"]["bindable"], False)
        self.assertIs(workloads["support-agent"]["bindable"], True)
        self.assertIsNone(workloads["Open assistant"]["egress"])
        self.assertEqual(data["exports"], [{"project": PROJECT, "name": "gateway-siem",
                                            "service": "pubsub.googleapis.com",
                                            "destination": "projects/demo-project/topics/siem",
                                            "covers": ["gateway_requests", "data_access"], "disabled": False}])
        self.assertEqual(data["scope"]["regions"], [REGION, OTHER_REGION])
        self.assertEqual(data["manage"]["projects"], [PROJECT, LINKED])

    def test_egress_gateway_without_a_policy_is_the_most_severe_finding(self):
        self.cloud.add_gateway("bare", registries=[])
        self.cloud.add_gateway("content-only")
        self.cloud.add_extension("armor", service="modelarmor.us-central1.rep.googleapis.com")
        self.cloud.add_policy("armor-policy", ["content-only"], "armor", profile="CONTENT_AUTHZ")
        data, _, _ = self.data("/api/v1/agent-platform/gateways")
        gateways = {g["name"]: g for g in data["gateways"]}
        bare = gateways["bare"]
        self.assertEqual(bare["findings"][0], {"code": "egress_default_deny", "severity": "high"})
        self.assertIn({"code": "no_registry", "severity": "medium"}, bare["findings"])
        self.assertEqual(bare["severity"], "high")
        # A content policy lifts the default deny, but nothing checks the identity of the agent.
        codes = {f["code"] for f in gateways["content-only"]["findings"]}
        self.assertIn("no_access_authorization", codes)
        self.assertNotIn("egress_default_deny", codes)
        self.assertEqual(data["kpis"]["unprotected"], 2)
        self.assertEqual(data["kpis"]["findings"]["high"], 1)

    def test_shared_policies_dry_run_semantic_governance_and_disabled_logging(self):
        cloud = self.cloud
        cloud.add_gateway("one")
        cloud.add_gateway("two")
        cloud.add_extension("shared-iap", metadata={"iapPolicyVersion": "V2"})
        cloud.add_extension("sgp", service="us-central1.internal.example.com",
                            metadata={"sgpEnforcementMode": "DRY_RUN"})
        cloud.add_policy("shared", ["one", "two"], "shared-iap")
        cloud.add_policy("sgp-policy", ["one"], "sgp", profile="CONTENT_AUTHZ")
        cloud.telemetry[(PROJECT, REGION)] = [{
            "name": "projects/demo-project/locations/us-central1/telemetryPolicies/quiet",
            "telemetryTarget": {"resources": ["//networkservices.googleapis.com/" + gateway_name("one")]},
            "loggingConfiguration": {"enable": False}}]
        data, _, _ = self.data("/api/v1/agent-platform/gateways")
        one, two = data["gateways"]
        self.assertTrue(next(p for p in one["policies"] if p["name"] == "shared")["shared"])
        self.assertEqual(one["semantic"], {"state": "audit", "policy": "sgp-policy"})
        codes = {f["code"] for f in one["findings"]}
        self.assertIn("semantic_audit_only", codes)
        self.assertIn("logging_disabled", codes)
        self.assertEqual(one["telemetry"]["logging"], "disabled")
        self.assertEqual(two["telemetry"]["logging"], "default")
        self.assertEqual(two["access"]["state"], "enforced")

    def test_per_region_lists_are_used_when_the_all_locations_list_is_refused(self):
        self.cloud.wildcard = False
        self.cloud.add_gateway("seoul", location=OTHER_REGION)
        self.cloud.add_gateway("iowa")
        data, codes, _ = self.data("/api/v1/agent-platform/gateways")
        self.assertEqual(codes, set())
        self.assertEqual([(g["name"], g["location"]) for g in data["gateways"]],
                         [("seoul", OTHER_REGION), ("iowa", REGION)])
        listed = [c[1] for c in self.cloud.find("GET", "/projects/%s/locations/" % PROJECT)
                  if c[1].endswith("/agentGateways")]
        self.assertEqual(len(listed), 3)  # all locations, then one list per region

    def test_no_gateways_is_an_empty_list_and_unreadable_is_null(self):
        data, codes, _ = self.data("/api/v1/agent-platform/gateways")
        self.assertEqual((data["status"], data["gateways"], data["exports"]), ("ok", [], []))
        self.assertEqual(data["kpis"]["gateways"], 0)
        self.assertEqual(codes, set())

        self.app.cache.clear()
        self.cloud.fail["/projects/%s/locations/" % PROJECT] = GcpError("denied", status=403, message=UPSTREAM_SECRET)
        status, payload = self.call("GET", "/api/v1/agent-platform/gateways")
        self.assertEqual(status, 200)
        self.assertEqual((payload["data"]["status"], payload["data"]["gateways"]), ("denied", None))
        self.assertEqual({w["code"] for w in payload["meta"]["warnings"]}, {"access_denied"})
        self.assertNotIn(UPSTREAM_SECRET, json.dumps(payload))

    def test_disabled_api_is_reported_as_not_set_up(self):
        self.cloud.fail["/projects/%s/locations/" % PROJECT] = GcpError(
            "denied", status=403, reason="PERMISSION_DENIED",
            message="Network Services API has not been used in project 1 before or it is disabled.")
        data, codes, meta = self.data("/api/v1/agent-platform/gateways")
        self.assertEqual((data["status"], data["gateways"]), ("api_disabled", None))
        self.assertEqual(codes, {"not_configured"})
        self.assertEqual(meta["warnings"][0]["message"], agent_gateway.MSG_API_DISABLED)

    def test_linked_project_failures_are_silent_and_its_gateways_are_listed(self):
        self.cloud.add_gateway("hub")
        data, codes, _ = self.data("/api/v1/agent-platform/gateways")
        self.assertEqual(len(data["gateways"]), 1)
        self.app.cache.clear()
        self.cloud.fail["/projects/%s/locations/" % LINKED] = GcpError("denied", status=403)
        data, codes, _ = self.data("/api/v1/agent-platform/gateways")
        self.assertEqual((data["status"], codes, len(data["gateways"])), ("ok", set(), 1))
        self.app.cache.clear()
        del self.cloud.fail["/projects/%s/locations/" % LINKED]
        self.cloud.add_gateway("spoke", project=LINKED)
        data, _, _ = self.data("/api/v1/agent-platform/gateways")
        self.assertEqual([(g["project"], g["name"]) for g in data["gateways"]], [(PROJECT, "hub"), (LINKED, "spoke")])
        data, _, _ = self.data("/api/v1/agent-platform/gateways?project=%s" % LINKED)
        self.assertEqual([g["name"] for g in data["gateways"]], ["spoke"])

    def test_unreadable_policies_leave_the_posture_unknown(self):
        self.seed()
        self.cloud.fail["/authzPolicies"] = GcpError("denied", status=403, message=UPSTREAM_SECRET)
        status, payload = self.call("GET", "/api/v1/agent-platform/gateways")
        self.assertEqual(status, 200)
        prod = next(g for g in payload["data"]["gateways"] if g["name"] == "egress-prod")
        self.assertIsNone(prod["policies"])
        self.assertEqual((prod["access"]["state"], prod["contentSecurity"]["state"]), ("unknown", "unknown"))
        self.assertNotIn("egress_default_deny", {f["code"] for f in prod["findings"]})
        self.assertIn("api:networksecurity.authzPolicies", {w["source"] for w in payload["meta"]["warnings"]})
        self.assertNotIn(UPSTREAM_SECRET, json.dumps(payload))

    # -- traffic -------------------------------------------------------------
    def seed_traffic(self):
        self.seed()
        registered = "projects/demo-project/locations/us-central1/endpoints/tools"
        self.cloud.logs["gateway_requests"] = [
            request_entry("2026-10-19T03:00:00Z", "egress-prod", 200, "https://tools.example.com/mcp?key=secret",
                          registered=registered, mcp={"method": "tools/call", "param": "get_weather"}),
            request_entry("2026-10-19T03:01:00Z", "egress-prod", 200, "https://tools.example.com/mcp",
                          registered=registered, mcp={"method": "tools/list"}),
            request_entry("2026-10-19T03:02:00Z", "egress-prod", 403, "https://evil.example.net/upload",
                          denied_by="projects/demo-project/locations/us-central1/authzPolicies/prod-iap-policy"),
            request_entry("2026-10-18T03:00:00Z", "egress-prod", 502, "https://tools.example.com/mcp",
                          registered=registered),
            request_entry("2026-10-18T04:00:00Z", "egress-audit", 404, "https://api.example.org/v1/items"),
            request_entry("2026-10-17T04:00:00Z", "ingress", 200, "https://agent.example.com/query")]
        agent = "principal://agents.global.org-1.system.id.goog/resources/aiplatform/projects/1/agents/111"
        self.cloud.logs["data_access"] = [
            decision_entry("2026-10-19T03:00:00Z", agent, "endpoints/tools", True),
            decision_entry("2026-10-19T03:02:00Z", agent, "unregisteredResource", False, unregistered=True),
            decision_entry("2026-10-18T03:00:00Z", agent, "endpoints/crm", False, dry_run=True),
            decision_entry("2026-10-18T05:00:00Z", agent, "endpoints/crm", False, dry_run=True),
            decision_entry("2026-10-17T03:00:00Z", "owner@example.com", "endpoints/tools", True)]
        self.cloud.logs["sanitize_operations"] = [{"timestamp": "2026-10-19T03:00:00Z"}] * 2

    def test_traffic_from_the_request_log_and_access_decisions(self):
        self.seed_traffic()
        data, codes, meta = self.data("/api/v1/agent-platform/gateways/traffic?" + RANGE)
        self.assertEqual(codes, set())
        kpis = data["kpis"]
        self.assertEqual((kpis["requests"], kpis["requestSource"], kpis["denied"], kpis["errors"]),
                         (6, "logs", 1, 1))
        # Registry matching applies to the Agent-to-Anywhere gateways only (the ingress request is not counted).
        self.assertEqual(kpis["unregistered"], 2)
        self.assertEqual((kpis["authorizations"], kpis["authorizationFailures"], kpis["wouldBlock"]), (5, 3, 2))
        self.assertEqual(kpis["failureRate"], 0.6)
        self.assertEqual((kpis["latencyP50"], kpis["latencyP95"]), (200.0, 200.0))
        self.assertEqual((kpis["armorMatches"], kpis["semanticDenials"]), (2, None))
        trend = {s["key"]: s["values"] for s in data["requestTrend"]["series"]}
        self.assertEqual(data["requestTrend"]["buckets"][-3:], ["2026-10-17", "2026-10-18", "2026-10-19"])
        self.assertEqual(trend["allowed"][-3:], [1, 0, 2])
        self.assertEqual(trend["denied"][-3:], [0, 0, 1])
        self.assertEqual(trend["client_error"][-3:], [0, 1, 0])
        self.assertEqual(trend["server_error"][-3:], [0, 1, 0])
        decisions = {s["key"]: s["values"][-3:] for s in data["decisionTrend"]["series"]}
        self.assertEqual(decisions, {"granted": [1, 0, 1], "denied": [0, 0, 1], "would_block": [0, 2, 0]})
        self.assertEqual(data["byGateway"][0], {"key": "demo-project/us-central1/egress-prod", "project": PROJECT,
                                                "location": REGION, "name": "egress-prod", "requests": 4,
                                                "denied": 1, "errors": 1})
        hosts = {d["host"]: d for d in data["destinations"]}
        self.assertEqual(hosts["tools.example.com"], {"host": "tools.example.com", "requests": 3, "denied": 0,
                                                      "registered": True})
        self.assertIs(hosts["evil.example.net"]["registered"], False)
        self.assertIsNone(hosts["agent.example.com"]["registered"])
        self.assertEqual(data["mcp"], [
            {"method": "tools/call", "tool": "get_weather", "requests": 1, "denied": 0},
            {"method": "tools/list", "tool": None, "requests": 1, "denied": 0}])
        self.assertEqual(len(data["deniedRequests"]), 1)
        denied = data["deniedRequests"][0]
        self.assertEqual((denied["host"], denied["path"], denied["status"], denied["policy"], denied["traceId"]),
                         ("evil.example.net", "/upload", 403, "prod-iap-policy", "a" * 32))
        self.assertEqual([(d["resource"], d["denied"], d["wouldBlock"], d["unregistered"])
                          for d in data["decisions"]],
                         [("endpoints/crm", 0, 2, False), ("unregisteredResource", 1, 0, True)])
        self.assertEqual(data["sampled"], {"requests": {"read": 6, "truncated": False},
                                           "decisions": {"read": 5, "truncated": False}})
        # Query strings can carry credentials and are not returned.
        self.assertNotIn("secret", json.dumps(data))
        self.assertIn("logging:networkservices.googleapis.com/gateway_requests", meta["sources"])
        filters = [c[3]["filter"] for c in self.cloud.find("POST", "entries:list")]
        requests = next(f for f in filters if "gateway_requests" in f)
        self.assertIn('NOT httpRequest.requestMethod="CONNECT"', requests)
        self.assertIn('timestamp>="2026-10-13T00:00:00Z" AND timestamp<"2026-10-20T00:00:00Z"', requests)
        self.assertTrue(any('permission:"egressViaIAP"' in f for f in filters))
        # Verdict logs are read only for the controls that a gateway has: Model Armor here, not semantic governance.
        self.assertTrue(any("sanitize_operations" in f for f in filters))
        self.assertFalse(any("semantic-governance-policy" in f for f in filters))

    def test_traffic_of_one_gateway_and_unknown_gateway(self):
        self.seed_traffic()
        data, _, _ = self.data("/api/v1/agent-platform/gateways/traffic?%s&gateway=%s" % (
            RANGE, urllib.parse.quote("demo-project/us-central1/egress-audit", safe="")))
        self.assertEqual(data["scope"]["gateway"], "demo-project/us-central1/egress-audit")
        self.assertEqual((data["kpis"]["requests"], data["kpis"]["denied"]), (1, 0))
        self.assertEqual([d["host"] for d in data["destinations"]], ["api.example.org"])
        flt = next(c[3]["filter"] for c in self.cloud.find("POST", "entries:list") if "gateway_requests" in c[3]["filter"])
        self.assertIn('resource.labels.gateway_name="egress-audit" AND resource.labels.location="us-central1"', flt)
        status, payload = self.call("GET", "/api/v1/agent-platform/gateways/traffic?%s&gateway=%s" % (
            RANGE, urllib.parse.quote('x" OR logName:"', safe="")))
        self.assertEqual((status, payload["error"]["code"]), (400, "bad_request"))

    def test_metrics_replace_log_counts_when_they_exist(self):
        self.seed_traffic()
        labels = {"gateway_name": "egress-prod", "location": REGION}
        self.cloud.series[agent_gateway.REQUEST_COUNT] = [
            {"resource": {"labels": labels}, "metric": {"labels": {"response_code": "200"}},
             "points": [point("2026-10-19", {"int64Value": "900"}), point("2026-10-18", {"int64Value": "100"})]},
            {"resource": {"labels": labels}, "metric": {"labels": {"response_code": "403"}},
             "points": [point("2026-10-19", {"int64Value": "40"})]},
            {"resource": {"labels": labels}, "metric": {"labels": {"response_code": "503"}},
             "points": [point("2026-10-18", {"int64Value": "7"})]}]
        self.cloud.series[agent_gateway.TOTAL_LATENCIES] = [
            {"resource": {"labels": {}}, "metric": {"labels": {}}, "points": [point("2026-10-19", {"doubleValue": 480.5})]}]
        self.cloud.series[agent_gateway.EXTENSION_INVOCATIONS] = [
            {"resource": {"labels": {"resource_name": "prod-iap"}}, "metric": {"labels": {}},
             "points": [point("2026-10-19", {"int64Value": "1000"})]},
            {"resource": {"labels": {"resource_name": "some-load-balancer-extension"}}, "metric": {"labels": {}},
             "points": [point("2026-10-19", {"int64Value": "5"})]}]
        self.cloud.series[agent_gateway.EXTENSION_FAILED_OPEN] = [
            {"resource": {"labels": {"resource_name": "prod-iap"}}, "metric": {"labels": {}},
             "points": [point("2026-10-19", {"int64Value": "3"})]}]
        data, codes, _ = self.data("/api/v1/agent-platform/gateways/traffic?" + RANGE)
        self.assertEqual(codes, set())
        kpis = data["kpis"]
        self.assertEqual((kpis["requests"], kpis["requestSource"], kpis["denied"], kpis["errors"]),
                         (1047, "metrics", 40, 7))
        self.assertEqual((kpis["latencyP50"], kpis["latencyP95"]), (480.5, 480.5))
        trend = {s["key"]: s["values"][-2:] for s in data["requestTrend"]["series"]}
        self.assertEqual(trend, {"allowed": [100, 900], "denied": [0, 40], "client_error": [0, 0],
                                 "server_error": [7, 0]})
        self.assertEqual(data["byGateway"], [{"key": "demo-project/us-central1/egress-prod", "project": PROJECT,
                                              "location": REGION, "name": "egress-prod", "requests": 1047,
                                              "denied": 40, "errors": 7}])
        # Only the extensions attached to the gateways are listed, not other Service Extensions of the project.
        self.assertEqual(data["extensions"], [{"name": "prod-iap", "kind": "iap", "invocations": 1000,
                                               "failedOpen": 3}])
        query = next(c for c in self.cloud.find("GET", "/timeSeries") if agent_gateway.REQUEST_COUNT in c[2]["filter"])
        self.assertIn('resource.type="networkservices.googleapis.com/Gateway"', query[2]["filter"])
        self.assertIn('one_of("egress-audit","egress-prod","ingress")', query[2]["filter"])

    def test_truncated_logs_and_unreadable_sources(self):
        self.seed_traffic()
        with mock.patch.object(agent_gateway, "MAX_REQUEST_ENTRIES", 4):
            original = self.cloud.request

            def paged(method, url, **kwargs):
                payload, headers = original(method, url, **kwargs)
                if url.endswith("entries:list") and "gateway_requests" in kwargs["json_body"]["filter"]:
                    payload = dict(payload, entries=payload["entries"][:4], nextPageToken="more")
                return payload, headers

            self.app.gcp.request = paged
            status, payload = self.call("GET", "/api/v1/agent-platform/gateways/traffic?" + RANGE)
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["sampled"]["requests"], {"read": 4, "truncated": True})
        warning = next(w for w in payload["meta"]["warnings"] if w["code"] == "truncated")
        self.assertEqual(warning["message"], "Log entries were truncated at 4.")

        self.app.gcp.request = self.cloud.request
        self.app.cache.clear()
        self.cloud.fail["entries:list"] = GcpError("denied", status=403, message=UPSTREAM_SECRET)
        self.cloud.fail["/timeSeries"] = GcpError("unavailable", status=503, message=UPSTREAM_SECRET)
        status, payload = self.call("GET", "/api/v1/agent-platform/gateways/traffic?" + RANGE)
        self.assertEqual(status, 200)
        data = payload["data"]
        self.assertIsNone(data["kpis"]["requests"])
        self.assertIsNone(data["requestTrend"])
        self.assertIsNone(data["deniedRequests"])
        self.assertIsNone(data["decisions"])
        self.assertIsNone(data["extensions"])
        self.assertEqual({w["code"] for w in payload["meta"]["warnings"]}, {"access_denied", "upstream_unavailable"})
        self.assertNotIn(UPSTREAM_SECRET, json.dumps(payload))

    def test_traffic_without_gateways_reads_no_logs(self):
        data, codes, _ = self.data("/api/v1/agent-platform/gateways/traffic?" + RANGE)
        self.assertEqual(codes, set())
        self.assertEqual((data["kpis"]["requests"], data["kpis"]["authorizations"]), (0, 0))
        self.assertEqual((data["destinations"], data["deniedRequests"]), ([], []))
        self.assertEqual(len(data["requestTrend"]["buckets"]), 7)
        self.assertEqual(self.cloud.find("POST", "entries:list"), [])
        self.assertEqual(self.cloud.find("GET", "/timeSeries"), [])

    def test_traffic_retries_transient_log_rate_limits_and_surfaces_quota_notice(self):
        self.seed_traffic()
        quota_err = GcpError("unavailable", status=429, reason="RESOURCE_EXHAUSTED", message=UPSTREAM_SECRET)
        original = self.cloud.request
        attempts = {"req": 0}

        def flaky(method, url, **kwargs):
            if url.endswith("entries:list") and "gateway_requests" in kwargs["json_body"]["filter"]:
                attempts["req"] += 1
                if attempts["req"] <= 2:
                    raise quota_err
            return original(method, url, **kwargs)

        with mock.patch.object(agent_gateway, "LOG_RETRY_DELAYS", (0, 0, 0)):
            self.app.gcp.request = flaky
            data, codes, _ = self.data("/api/v1/agent-platform/gateways/traffic?" + RANGE)
            self.assertEqual(codes, set())
            self.assertEqual(data["kpis"]["requests"], 6)
            self.assertEqual(attempts["req"], 3)

            # Persistent 429 surfaces a specific quota notice instead of generic "temporarily unavailable".
            self.app.cache.clear()
            self.app.gcp.request = original
            self.cloud.fail["entries:list"] = quota_err
            status, payload = self.call("GET", "/api/v1/agent-platform/gateways/traffic?" + RANGE)
            self.assertEqual(status, 200)
            req_warn = next(w for w in payload["meta"]["warnings"]
                            if w["source"] == "logging:networkservices.googleapis.com/gateway_requests")
            self.assertEqual(req_warn["code"], "upstream_unavailable")
            self.assertEqual(req_warn["message"], agent_gateway.MSG_LOG_QUOTA)
            self.assertNotIn(UPSTREAM_SECRET, json.dumps(payload))

            # If page 2 hits 429 after page 1 succeeded, page 1 entries are preserved with truncated=True.
            self.cloud.fail.pop("entries:list")
            self.app.cache.clear()

            def page2_rate_limited(method, url, **kwargs):
                if url.endswith("entries:list") and "gateway_requests" in kwargs["json_body"]["filter"]:
                    if kwargs["json_body"].get("pageToken"):
                        raise quota_err
                    res, headers = original(method, url, **kwargs)
                    return dict(res, entries=res["entries"][:4], nextPageToken="page-2"), headers
                return original(method, url, **kwargs)

            self.app.gcp.request = page2_rate_limited
            status, payload = self.call("GET", "/api/v1/agent-platform/gateways/traffic?" + RANGE)
            self.assertEqual(status, 200)
            self.assertEqual(payload["data"]["kpis"]["requests"], 4)
            self.assertEqual(payload["data"]["sampled"]["requests"], {"read": 4, "truncated": True})

    def test_missing_monitoring_metric_descriptors_produce_no_warnings(self):
        self.seed_traffic()
        self.cloud.fail["/timeSeries"] = GcpError("not_found", status=404, reason="NOT_FOUND",
                                                  message=UPSTREAM_SECRET)
        data, codes, _ = self.data("/api/v1/agent-platform/gateways/traffic?" + RANGE)
        self.assertEqual(codes, set())
        self.assertEqual((data["kpis"]["requests"], data["kpis"]["requestSource"]), (6, "logs"))

    def test_unreadable_gateway_inventory_skips_traffic_logs_and_region_retries(self):
        self.cloud.fail["/agentGateways"] = GcpError(
            "denied", status=403,
            message="Network Services API has not been used in project 123 before or it is disabled. SERVICE_DISABLED")
        for _ in range(2):
            status, payload = self.call("GET", "/api/v1/agent-platform/gateways/traffic?" + RANGE)
            self.assertEqual(status, 200)
            self.assertIsNone(payload["data"]["kpis"]["requests"])
            self.assertEqual([(w["source"], w["code"], w["message"]) for w in payload["meta"]["warnings"]],
                             [("api:networkservices.agentGateways", "not_configured", agent_gateway.MSG_API_DISABLED)])
        # API disabled does not fan out into per-region retries, and traffic makes zero log or metric calls.
        self.assertEqual(sorted(c[1] for c in self.cloud.find("GET", "/agentGateways")), [
            "https://networkservices.googleapis.com/v1/projects/demo-project/locations/-/agentGateways",
            "https://networkservices.googleapis.com/v1/projects/linked-project/locations/-/agentGateways"])
        self.assertEqual(self.cloud.find("POST", "entries:list"), [])
        self.assertEqual(self.cloud.find("GET", "/timeSeries"), [])

    # -- create and update ---------------------------------------------------
    def test_create_sends_the_documented_resource(self):
        status, payload = self.post("", {"project": PROJECT, "location": REGION, "name": "new-egress",
                                         "accessPath": "AGENT_TO_ANYWHERE", "description": "Egress for agents",
                                         "labels": {"team": "platform"}})
        self.assertEqual(status, 200, payload)
        self.assertEqual((payload["data"]["status"], payload["data"]["operation"]), ("done", None))
        self.assertEqual(payload["data"]["gateway"]["key"], "demo-project/us-central1/new-egress")
        self.assertIs(payload["data"]["auditLogged"], True)
        call = self.cloud.find("POST", "/agentGateways")[0]
        self.assertEqual(call[1], "https://networkservices.googleapis.com/v1/projects/demo-project/locations/"
                                  "us-central1/agentGateways")
        self.assertEqual(call[2], {"agentGatewayId": "new-egress"})
        self.assertEqual(call[3], {"protocols": ["MCP"], "googleManaged": {"governedAccessPath": "AGENT_TO_ANYWHERE"},
                                   "description": "Egress for agents", "labels": {"team": "platform"},
                                   "registries": [registry()]})
        self.assertEqual(self.cloud.audit, [{"tool": "control-plane", "action": "agent_gateway_create",
                                             "actor": "admin@example.com", "accessPath": "AGENT_TO_ANYWHERE",
                                             "target": "demo-project/us-central1/new-egress", "result": "success"}])
        # The new gateway has no authorization policy yet: it blocks all outbound traffic.
        data, _, _ = self.data("/api/v1/agent-platform/gateways")
        self.assertEqual(data["gateways"][0]["findings"][0]["code"], "egress_default_deny")

    def test_create_ingress_has_no_registry_and_cached_inventory_is_refreshed(self):
        data, _, _ = self.data("/api/v1/agent-platform/gateways")
        self.assertEqual(data["gateways"], [])
        status, payload = self.post("", {"project": LINKED, "location": OTHER_REGION, "name": "front-door",
                                         "accessPath": "CLIENT_TO_AGENT"})
        self.assertEqual(status, 200, payload)
        body = self.cloud.find("POST", "/agentGateways")[0][3]
        self.assertEqual(body, {"protocols": ["MCP"], "googleManaged": {"governedAccessPath": "CLIENT_TO_AGENT"}})
        data, _, _ = self.data("/api/v1/agent-platform/gateways")
        self.assertEqual([(g["project"], g["location"], g["name"]) for g in data["gateways"]],
                         [(LINKED, OTHER_REGION, "front-door")])

    def test_create_waits_within_the_budget_then_reports_the_operation(self):
        self.cloud.slow = 2
        status, payload = self.post("", {"project": PROJECT, "location": REGION, "name": "slow-one",
                                         "accessPath": "CLIENT_TO_AGENT"})
        self.assertEqual((status, payload["data"]["status"]), (200, "done"))
        self.assertEqual(len(self.cloud.find("GET", "/operations/")), 2)

        agent_gateway.OPERATION_WAIT_SECONDS = 0
        self.cloud.slow = 1
        status, payload = self.post("", {"project": PROJECT, "location": REGION, "name": "slow-two",
                                         "accessPath": "CLIENT_TO_AGENT"})
        self.assertEqual((status, payload["data"]["status"]), (200, "pending"))
        operation = payload["data"]["operation"]
        self.assertEqual(operation["api"], "networkservices")
        query = "/api/v1/agent-platform/gateways/operation?api=%s&name=%s" % (
            operation["api"], urllib.parse.quote(operation["name"], safe=""))
        data, _, _ = self.data(query)
        self.assertEqual(data, {"done": True, "failed": False, "error": None})

    def test_create_validation(self):
        valid = {"project": PROJECT, "location": REGION, "name": "ok", "accessPath": "AGENT_TO_ANYWHERE"}
        for change in ({"name": "Bad_Name"}, {"name": "-x"}, {"name": "x" * 64}, {"name": ""},
                       {"project": "someone-elses-project"}, {"location": "global"}, {"location": "us"},
                       {"location": "us-central1-a"}, {"accessPath": "ANYWHERE"}, {"description": "x" * 1025},
                       {"description": "bad\x00"}, {"labels": {"Bad": "x"}}, {"labels": {"k": "UPPER"}},
                       {"labels": ["a"]}):
            self.app.limiter = app_module.RateLimiter()  # management calls are limited to 10 per minute
            status, payload = self.post("", dict(valid, **change))
            self.assertEqual((status, payload.get("error", {}).get("code")), (400, "bad_request"), change)
        self.assertEqual(self.cloud.find("POST", "/agentGateways"), [])
        self.assertEqual(self.cloud.audit, [])

    def test_management_requires_an_administrator_and_csrf(self):
        body = {"project": PROJECT, "location": REGION, "name": "x", "accessPath": "CLIENT_TO_AGENT"}
        for path in ("", "/update", "/delete", "/access"):
            status, payload = self.post(path, body, identity=VIEWER)
            self.assertEqual((status, payload["error"]["code"]), (403, "forbidden"), path)
            status, payload = self.post(path, body, csrf=False)
            self.assertEqual((status, payload["error"]["code"]), (403, "csrf_failed"), path)
        status, payload = self.call("GET", "/api/v1/agent-platform/gateways/operation?api=networkservices&name="
                                    "projects/demo-project/locations/us-central1/operations/x", identity=VIEWER)
        self.assertEqual((status, payload["error"]["code"]), (403, "forbidden"))
        self.assertEqual(self.cloud.calls, [])

    def test_upstream_failures_become_curated_errors_and_are_audited(self):
        body = {"project": PROJECT, "location": REGION, "name": "dup", "accessPath": "CLIENT_TO_AGENT"}
        self.cloud.add_gateway("dup", path="CLIENT_TO_AGENT")
        status, payload = self.post("", body)
        self.assertEqual((status, payload["error"]["code"]), (409, "conflict"))
        self.assertEqual(payload["error"]["message"], "A gateway with this name already exists in this project "
                                                      "and region.")
        self.assertEqual(self.cloud.audit[-1]["result"], "failed")
        self.assertEqual(self.cloud.audit[-1]["error"], "conflict")
        # A permission problem of the service account is not a 403: that status is for the signed-in person.
        self.cloud.fail["POST https://networkservices"] = GcpError("denied", status=403, message=UPSTREAM_SECRET)
        status, payload = self.post("", dict(body, name="other"))
        self.assertEqual((status, payload["error"]["code"]), (502, "upstream_error"))
        self.assertEqual(payload["error"]["message"], agent_gateway.MSG_DENIED)
        self.cloud.fail["POST https://networkservices"] = GcpError("unavailable", status=503, message=UPSTREAM_SECRET)
        status, payload = self.post("", dict(body, name="other"))
        self.assertEqual((status, payload["error"]["message"]), (502, agent_gateway.MSG_UNAVAILABLE))
        self.assertNotIn(UPSTREAM_SECRET, json.dumps(payload))
        self.assertNotIn(UPSTREAM_SECRET, json.dumps(self.cloud.audit))

    def test_failed_operation_is_reported_without_upstream_text(self):
        self.cloud.next_error = {"code": 7, "message": UPSTREAM_SECRET}
        status, payload = self.post("", {"project": PROJECT, "location": REGION, "name": "nope",
                                         "accessPath": "CLIENT_TO_AGENT"})
        self.assertEqual((status, payload["error"]["message"]), (502, agent_gateway.MSG_DENIED))
        self.assertNotIn(UPSTREAM_SECRET, json.dumps(payload))

        agent_gateway.OPERATION_WAIT_SECONDS = 0
        self.cloud.slow, self.cloud.next_error = 1, {"code": 9, "message": UPSTREAM_SECRET}
        status, payload = self.post("", {"project": PROJECT, "location": REGION, "name": "later",
                                         "accessPath": "CLIENT_TO_AGENT"})
        operation = payload["data"]["operation"]
        data, _, _ = self.data("/api/v1/agent-platform/gateways/operation?api=networkservices&name=%s"
                               % urllib.parse.quote(operation["name"], safe=""))
        self.assertEqual((data["done"], data["failed"]), (True, True))
        self.assertNotIn(UPSTREAM_SECRET, data["error"])

    def test_operation_endpoint_validates_its_parameters(self):
        base = "/api/v1/agent-platform/gateways/operation?"
        for query in ("api=compute&name=projects/demo-project/locations/us-central1/operations/x",
                      "api=networkservices&name=projects/demo-project/locations/us-central1/agentGateways/x",
                      "api=networkservices&name=" + urllib.parse.quote("projects/demo-project/locations/u/operations/x/../y"),
                      "api=networkservices&name=projects/someone-elses-project/locations/us-central1/operations/x"):
            status, payload = self.call("GET", base + query)
            self.assertEqual((status, payload["error"]["code"]), (400, "bad_request"), query)
        self.assertEqual(self.cloud.calls, [])

    def test_update_patches_only_the_given_fields(self):
        self.cloud.add_gateway("egress", description="old")
        target = {"project": PROJECT, "location": REGION, "name": "egress"}
        status, payload = self.post("/update", dict(target, description="new text", labels={"env": "prod"},
                                                    etag="etag-egress"))
        self.assertEqual((status, payload["data"]["status"]), (200, "done"))
        call = self.cloud.find("PATCH", "/agentGateways/egress")[0]
        self.assertEqual(call[2], {"updateMask": "description,labels"})
        self.assertEqual(call[3], {"name": gateway_name("egress"), "description": "new text",
                                   "labels": {"env": "prod"}, "etag": "etag-egress"})
        self.assertEqual(self.cloud.audit[-1]["fields"], ["description", "labels"])
        status, payload = self.post("/update", dict(target, registry=False))
        self.assertEqual(status, 200)
        self.assertEqual(self.cloud.find("PATCH", "/agentGateways/egress")[1][3]["registries"], [])
        status, payload = self.post("/update", target)
        self.assertEqual((status, payload["error"]["message"]), (400, "Nothing to change."))
        status, payload = self.post("/update", dict(target, name="missing", description="x"))
        self.assertEqual((status, payload["error"]["code"]), (404, "not_found"))
        self.cloud.fail["PATCH"] = GcpError("conflict", status=409, reason="ABORTED", message=UPSTREAM_SECRET)
        status, payload = self.post("/update", dict(target, description="x"))
        self.assertEqual((status, payload["error"]["message"]),
                         (409, "The gateway changed since it was loaded. Reload and try again."))

    # -- access authorization ------------------------------------------------
    def test_access_creates_the_extension_and_the_policy(self):
        self.cloud.add_gateway("egress")
        target = {"project": PROJECT, "location": REGION, "name": "egress"}
        status, payload = self.post("/access", dict(target, mode="audit"))
        self.assertEqual(status, 200, payload)
        self.assertEqual((payload["data"]["status"], payload["data"]["step"]), ("done", "policy"))
        ext = self.cloud.find("POST", "/authzExtensions")[0]
        self.assertEqual(ext[1], "https://networkservices.googleapis.com/v1/projects/demo-project/locations/"
                                 "us-central1/authzExtensions")
        self.assertEqual(ext[2], {"authzExtensionId": "egress-iap-authz-ext"})
        self.assertEqual(ext[3], {"service": "iap.googleapis.com", "failOpen": False, "timeout": "1s",
                                  "metadata": {"iapPolicyVersion": "V2", "iamEnforcementMode": "DRY_RUN"}})
        policy = self.cloud.find("POST", "/authzPolicies")[0]
        self.assertEqual(policy[1], "https://networksecurity.googleapis.com/v1/projects/demo-project/locations/"
                                    "us-central1/authzPolicies")
        self.assertEqual(policy[2], {"authzPolicyId": "egress-iap-authzpolicy"})
        self.assertEqual(policy[3], {
            "target": {"resources": [gateway_name("egress")]}, "policyProfile": "REQUEST_AUTHZ", "action": "CUSTOM",
            "customProvider": {"authzExtension": {"resources": [
                "projects/demo-project/locations/us-central1/authzExtensions/egress-iap-authz-ext"]}}})
        self.assertEqual(self.cloud.audit[-1]["action"], "agent_gateway_access")
        data, _, _ = self.data("/api/v1/agent-platform/gateways")
        self.assertEqual(data["gateways"][0]["access"]["state"], "audit")

    def test_access_enforce_omits_the_dry_run_key_and_switches_keep_other_metadata(self):
        self.cloud.add_gateway("egress")
        target = {"project": PROJECT, "location": REGION, "name": "egress"}
        self.post("/access", dict(target, mode="enforce"))
        self.assertEqual(self.cloud.find("POST", "/authzExtensions")[0][3]["metadata"], {"iapPolicyVersion": "V2"})
        # A second call in the same mode changes nothing.
        status, payload = self.post("/access", dict(target, mode="enforce"))
        self.assertEqual((payload["data"]["status"], payload["data"]["changed"]), ("done", []))
        self.assertEqual(self.cloud.find("PATCH", "/authzExtensions"), [])
        self.assertEqual(len(self.cloud.find("POST", "/authzPolicies")), 1)

        self.cloud.extensions[(PROJECT, REGION, "egress-iap-authz-ext")]["metadata"]["custom"] = "kept"
        status, payload = self.post("/access", dict(target, mode="audit"))
        self.assertEqual((payload["data"]["step"], payload["data"]["changed"]), ("mode", ["egress-iap-authz-ext"]))
        patch = self.cloud.find("PATCH", "/authzExtensions/egress-iap-authz-ext")[0]
        self.assertEqual(patch[2], {"updateMask": "metadata"})
        self.assertEqual(patch[3], {"metadata": {"iapPolicyVersion": "V2", "custom": "kept",
                                                 "iamEnforcementMode": "DRY_RUN"}})
        # The console writes ENFORCED; the API accepts only DRY_RUN, so enforcing removes the key.
        self.cloud.extensions[(PROJECT, REGION, "egress-iap-authz-ext")]["metadata"]["iamEnforcementMode"] = "DRY_RUN"
        self.post("/access", dict(target, mode="enforce"))
        self.assertEqual(self.cloud.find("PATCH", "/authzExtensions/egress-iap-authz-ext")[1][3],
                         {"metadata": {"iapPolicyVersion": "V2", "custom": "kept"}})

    def test_access_continues_after_a_pending_operation(self):
        self.cloud.add_gateway("egress")
        target = {"project": PROJECT, "location": REGION, "name": "egress", "mode": "audit"}
        agent_gateway.OPERATION_WAIT_SECONDS = 0
        self.cloud.slow = 1
        status, payload = self.post("/access", target)
        self.assertEqual((payload["data"]["status"], payload["data"]["step"]), ("pending", "extension"))
        self.assertEqual(self.cloud.find("POST", "/authzPolicies"), [])
        # The client polls the operation, then repeats the call: the extension exists, so the policy is next.
        agent_gateway.OPERATION_WAIT_SECONDS = self.wait
        self.cloud.slow = 0
        status, payload = self.post("/access", target)
        self.assertEqual((payload["data"]["status"], payload["data"]["step"]), ("done", "policy"))
        self.assertEqual(len(self.cloud.find("POST", "/authzExtensions")), 1)
        self.assertEqual(len(self.cloud.find("POST", "/authzPolicies")), 1)

    def test_access_refusals(self):
        self.cloud.add_gateway("ingress", path="CLIENT_TO_AGENT")
        self.cloud.add_gateway("one")
        self.cloud.add_gateway("two")
        self.cloud.add_extension("shared-iap", metadata={"iapPolicyVersion": "V2"})
        self.cloud.add_policy("shared", ["one", "two"], "shared-iap")
        base = {"project": PROJECT, "location": REGION}
        status, payload = self.post("/access", dict(base, name="ingress", mode="audit"))
        self.assertEqual((status, payload["error"]["message"]),
                         (400, "Access authorization applies to Agent-to-Anywhere gateways only."))
        status, payload = self.post("/access", dict(base, name="one", mode="off"))
        self.assertEqual((status, payload["error"]["code"]), (400, "bad_request"))
        # Changing a shared extension would change the other gateway too.
        status, payload = self.post("/access", dict(base, name="one", mode="audit"))
        self.assertEqual((status, payload["error"]["code"]), (409, "conflict"))
        status, payload = self.post("/access", dict(base, name="missing", mode="audit"))
        self.assertEqual((status, payload["error"]["code"]), (404, "not_found"))
        self.assertEqual(self.cloud.find("PATCH", "/authzExtensions"), [])
        self.assertEqual(self.cloud.find("POST", "/authzExtensions"), [])

    # -- delete --------------------------------------------------------------
    def test_delete_a_gateway_without_policies(self):
        self.cloud.add_gateway("old", path="CLIENT_TO_AGENT")
        status, payload = self.post("/delete", {"project": PROJECT, "location": REGION, "name": "old"})
        self.assertEqual((status, payload["data"]["status"]), (200, "done"))
        call = self.cloud.find("DELETE", "/agentGateways/old")[0]
        self.assertEqual(call[2], {"etag": "etag-old"})
        self.assertEqual(self.cloud.gateways, {})
        self.assertEqual(self.cloud.audit[-1]["action"], "agent_gateway_delete")

    def test_delete_is_refused_while_policies_target_the_gateway(self):
        self.seed()
        target = {"project": PROJECT, "location": REGION, "name": "egress-audit"}
        status, payload = self.post("/delete", target)
        self.assertEqual((status, payload["error"]["code"]), (409, "conflict"))
        self.assertEqual(self.cloud.find("DELETE", "/"), [])
        self.assertIn((PROJECT, REGION, "egress-audit"), self.cloud.gateways)

    def test_delete_never_removes_policies_of_a_gateway_in_use(self):
        self.seed()
        target = {"project": PROJECT, "location": REGION, "name": "egress-audit", "removePolicies": True}
        status, payload = self.post("/delete", target)  # billing-agent is bound to it
        self.assertEqual((status, payload["error"]["message"]), (409, agent_gateway.MSG_IN_USE))
        self.assertEqual(self.cloud.find("DELETE", "/"), [])
        # The agents of the gateway region cannot be listed: nothing is deleted either.
        self.cloud.engines[(PROJECT, REGION)] = []
        self.cloud.fail["/reasoningEngines"] = GcpError("denied", status=403, message=UPSTREAM_SECRET)
        status, payload = self.post("/delete", target)
        self.assertEqual((status, payload["error"]["message"]), (409, agent_gateway.MSG_UNVERIFIED))
        self.assertEqual(self.cloud.find("DELETE", "/"), [])
        self.assertIn((PROJECT, REGION, "audit-iap-policy"), self.cloud.policies)

    def test_delete_with_policies_removes_policies_extensions_then_the_gateway(self):
        self.seed()
        self.cloud.engines[(PROJECT, REGION)] = []
        status, payload = self.post("/delete", {"project": PROJECT, "location": REGION, "name": "egress-audit",
                                                "removePolicies": True})
        self.assertEqual(status, 200, payload)
        self.assertEqual((payload["data"]["status"], payload["data"]["removedPolicies"]),
                         ("done", ["audit-iap-policy"]))
        deleted = [c[1].split("/v1/")[1] for c in self.cloud.find("DELETE", "/")]
        self.assertEqual(deleted, ["projects/demo-project/locations/us-central1/authzPolicies/audit-iap-policy",
                                   "projects/demo-project/locations/us-central1/authzExtensions/audit-iap",
                                   "projects/demo-project/locations/us-central1/agentGateways/egress-audit"])
        self.assertNotIn((PROJECT, REGION, "egress-audit"), self.cloud.gateways)
        # The policies and extensions of the other gateways are untouched.
        self.assertIn((PROJECT, REGION, "prod-iap-policy"), self.cloud.policies)
        self.assertIn((PROJECT, REGION, "prod-iap"), self.cloud.extensions)
        self.assertEqual(self.cloud.audit[-1]["removePolicies"], True)

    def test_delete_keeps_extensions_that_other_policies_use_and_refuses_shared_policies(self):
        cloud = self.cloud
        cloud.add_gateway("one")
        cloud.add_gateway("two")
        cloud.add_extension("common", metadata={"iapPolicyVersion": "V2"})
        cloud.add_policy("one-policy", ["one"], "common")
        cloud.add_policy("two-policy", ["two"], "common")
        status, payload = self.post("/delete", {"project": PROJECT, "location": REGION, "name": "one",
                                                "removePolicies": True})
        self.assertEqual(status, 200, payload)
        self.assertIn((PROJECT, REGION, "common"), cloud.extensions)
        self.assertNotIn((PROJECT, REGION, "one"), cloud.gateways)

        cloud.add_gateway("three")
        cloud.add_policy("both", ["two", "three"], "common")
        status, payload = self.post("/delete", {"project": PROJECT, "location": REGION, "name": "three",
                                                "removePolicies": True})
        self.assertEqual((status, payload["error"]["code"]), (409, "conflict"))
        self.assertIn((PROJECT, REGION, "both"), cloud.policies)

    def test_delete_is_refused_while_a_telemetry_policy_targets_the_gateway(self):
        self.seed()
        self.cloud.engines[(PROJECT, REGION)] = []
        self.cloud.apps[(PROJECT, "global")] = []
        status, payload = self.post("/delete", {"project": PROJECT, "location": REGION, "name": "egress-prod",
                                                "removePolicies": True})
        self.assertEqual((status, payload["error"]["code"]), (409, "conflict"))
        self.assertIn("telemetry policy", payload["error"]["message"])
        self.assertEqual(self.cloud.find("DELETE", "/"), [])

    def test_delete_continues_after_a_pending_operation(self):
        self.seed()
        self.cloud.engines[(PROJECT, REGION)] = []
        body = {"project": PROJECT, "location": REGION, "name": "egress-audit", "removePolicies": True}
        agent_gateway.OPERATION_WAIT_SECONDS = 0
        self.cloud.slow = 1
        status, payload = self.post("/delete", body)
        self.assertEqual((payload["data"]["status"], payload["data"]["step"]), ("pending", "policies"))
        self.assertEqual(payload["data"]["cleanup"], ["audit-iap"])
        self.assertIn((PROJECT, REGION, "audit-iap"), self.cloud.extensions)
        # The policy is gone on the next call, so the client passes the extensions to clean up.
        agent_gateway.OPERATION_WAIT_SECONDS = self.wait
        self.cloud.slow = 0
        status, payload = self.post("/delete", dict(body, cleanup=payload["data"]["cleanup"]))
        self.assertEqual((status, payload["data"]["status"]), (200, "done"))
        self.assertNotIn((PROJECT, REGION, "audit-iap"), self.cloud.extensions)
        self.assertNotIn((PROJECT, REGION, "egress-audit"), self.cloud.gateways)
        status, payload = self.post("/delete", dict(body, cleanup=["../x"]))
        self.assertEqual((status, payload["error"]["code"]), (400, "bad_request"))

    def test_viewers_read_the_inventory_and_traffic(self):
        self.seed_traffic()
        data, _, _ = self.data("/api/v1/agent-platform/gateways", identity=VIEWER)
        self.assertEqual(len(data["gateways"]), 3)
        data, _, _ = self.data("/api/v1/agent-platform/gateways/traffic?" + RANGE, identity=VIEWER)
        self.assertEqual(data["kpis"]["requests"], 6)


if __name__ == "__main__":
    unittest.main()
