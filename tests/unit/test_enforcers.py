"""IAM deny policy enforcement (services/enforcers.py).

The IAM v2 API is an in-memory fake behind ctx.rest (one policy per attachment point and id, etags that change on
every write, operations that are done at once or after some polls, injected errors), so no test reaches Google Cloud.
"""

import copy
import re
import unittest
import urllib.parse

import support

from controlplane.gcp import GcpError
from controlplane.services import enforcers
from controlplane.services.enforcers import AttachmentState, DenyState

IAM = "https://iam.googleapis.com/v2"
PID = "ai-control-plane-caps"
CRM = "cloudresourcemanager.googleapis.com"
AP_DEMO = CRM + "/projects/" + support.PROJECT
AP_B = CRM + "/projects/proj-b"
AP_C = CRM + "/projects/proj-c"
ALICE = "principal://goog/subject/alice@example.com"
BOB = "principal://goog/subject/bob@example.com"
CAROL = "principal://goog/subject/carol@example.com"
DANA = "principal://iam.googleapis.com/locations/global/workforcePools/corp-pool/subject/Dana.Kim"
AGY = ["businessaicode.googleapis.com/locations.generateContent"]
BOTH = ["businessaicode.googleapis.com/locations.generateContent", "discoveryengine.googleapis.com/assistants.assist"]
LOGGER = "controlplane.enforcers"


def denied(principals, permissions=None, **extra):
    """A deny policy body with one rule (extra keys go into the denyRule)."""
    rule = {"deniedPrincipals": list(principals), "deniedPermissions": list(permissions or AGY)}
    rule.update(extra)
    return {"displayName": enforcers.DISPLAY_NAME, "rules": [{"denyRule": rule}]}


def people(count):
    return {"principal://goog/subject/user%04d@example.com" % i for i in range(count)}


class FakeIam:
    """IAM v2 deny policies behind ctx.rest.

    policies: (attachment point, policy id) -> stored policy. Every write gets a new etag. op_polls 0: operations are
    done in the write response, else after op_polls polls. errors: (method, attachment point or "operation") -> errors
    raised in order before the call is served. op_errors: (method, attachment point) -> google.rpc.Status of an
    operation that fails (the write does not happen)."""

    def __init__(self, op_polls=0):
        self.policies = {}
        self.op_polls = op_polls
        self.ops = {}
        self.errors = {}
        self.op_errors = {}
        self.calls = []
        self.seq = 0
        # Principals IAM does not find: a write that lists one fails like the live API (400 INVALID_ARGUMENT).
        self.unknown = set()

    def _next(self):
        self.seq += 1
        return self.seq

    def put(self, ap, policy, pid=PID):
        """Stores a policy as if someone wrote it (new etag) and returns a copy."""
        old = self.policies.get((ap, pid)) or {}
        stored = copy.deepcopy(policy)
        stored.update(name="policies/%s/denypolicies/%s" % (urllib.parse.quote(ap, safe=""), pid),
                      uid=old.get("uid") or "uid-%d" % self._next(), kind="DenyPolicy", etag="etag-%d" % self._next(),
                      createTime="2026-11-01T00:00:00Z", updateTime="2026-11-02T00:00:00Z")
        self.policies[(ap, pid)] = stored
        return copy.deepcopy(stored)

    def fail(self, method, ap, *errors):
        self.errors.setdefault((method, ap), []).extend(errors)

    def principals(self, ap, pid=PID):
        policy = self.policies.get((ap, pid))
        return None if policy is None else policy["rules"][0]["denyRule"]["deniedPrincipals"]

    def writes(self, ap=None):
        return [c for c in self.calls if c[0] in ("POST", "PUT", "DELETE")
                and (ap is None or c[1].startswith("%s/policies/%s/" % (IAM, urllib.parse.quote(ap, safe=""))))]

    def rest(self, method, url, json_body=None, params=None, timeout=None, **_kwargs):
        self.calls.append((method, url, dict(params or {}), copy.deepcopy(json_body)))
        if not url.startswith(IAM + "/policies/"):
            raise AssertionError("unexpected URL %s" % url)
        path = url[len(IAM) + 1:]
        if "/operations/" in path:
            return self._poll(path)
        m = re.fullmatch(r"policies/([^/]+)/denypolicies(?:/([a-z0-9-]+))?", path)
        if not m:
            raise AssertionError("unexpected deny policy URL %s" % url)
        ap = urllib.parse.unquote(m.group(1))
        pid = m.group(2) or (params or {}).get("policyId")
        queued = self.errors.get((method, ap))
        if queued:
            raise queued.pop(0)
        if method in ("POST", "PUT"):
            listed = [p for rule in (json_body or {}).get("rules") or []
                      for p in (rule.get("denyRule") or {}).get("deniedPrincipals") or []]
            ghost = next((p for p in listed if p in self.unknown), None)
            if ghost is not None:
                raise GcpError("invalid", 400, "The principal specified by member (%s) was not found." % ghost,
                               "INVALID_ARGUMENT")
        current = self.policies.get((ap, pid))
        if method == "GET":
            if current is None:
                raise GcpError("not_found", 404, "Policy not found.", "NOT_FOUND")
            return copy.deepcopy(current)
        if method == "POST":
            if m.group(2) is not None:
                raise AssertionError("create posts to the collection")
            if current is not None:
                raise GcpError("conflict", 409, "Policy already exists.", "ALREADY_EXISTS")
            return self._operation(method, ap, pid, json_body)
        if current is None:
            raise GcpError("not_found", 404, "Policy not found.", "NOT_FOUND")
        etag = (json_body or {}).get("etag") if method == "PUT" else (params or {}).get("etag")
        if method == "PUT" and not etag:
            raise GcpError("invalid", 400, "The etag is required.", "INVALID_ARGUMENT")
        if etag and etag != current["etag"]:
            raise GcpError("conflict", 409, "The etag does not match.", "ABORTED")
        return self._operation(method, ap, pid, json_body)

    def _operation(self, method, ap, pid, body):
        name = "policies/%s/denypolicies/%s/operations/op-%d" % (urllib.parse.quote(ap, safe=""), pid, self._next())
        status = self.op_errors.pop((method, ap), None)
        if status is not None:
            final = {"name": name, "done": True, "error": status}
        elif method == "DELETE":
            final = {"name": name, "done": True, "response": self.policies.pop((ap, pid))}
        else:
            final = {"name": name, "done": True,
                     "response": self.put(ap, {"displayName": body["displayName"], "rules": body["rules"]}, pid)}
        if not self.op_polls:
            return final
        self.ops[name] = [self.op_polls, final]
        return {"name": name, "metadata": {"@type": "type.googleapis.com/google.iam.v2.PolicyOperationMetadata"}}

    def _poll(self, name):
        queued = self.errors.get(("GET", "operation"))
        if queued:
            raise queued.pop(0)
        entry = self.ops[name]
        entry[0] -= 1
        return copy.deepcopy(entry[1]) if entry[0] <= 0 else {"name": name}


def make_ctx(fake=None, workspace=None, **governance):
    ctx = support.FakeCtx(settings=support.make_settings(workspace=dict(workspace or {}), governance=governance))
    if fake is not None:
        ctx.rest = fake.rest
    return ctx


def ours(ap, principals, permissions=AGY, etag="etag-1"):
    return AttachmentState(ap, exists=True, principals=frozenset(principals), permissions=tuple(permissions),
                           etag=etag)


class SettingsTest(unittest.TestCase):
    def test_denied_permissions_follow_deny_ge_app(self):
        self.assertEqual(enforcers.ANTIGRAVITY_PERMISSION, AGY[0])
        self.assertEqual(enforcers.GE_ASSIST_PERMISSION, BOTH[1])
        self.assertEqual(enforcers.denied_permissions(support.make_settings()), AGY)
        self.assertEqual(enforcers.denied_permissions(support.make_settings(governance={"deny_ge_app": True})), BOTH)
        self.assertEqual(enforcers.denied_permissions(support.make_settings(governance={"deny_ge_app": "yes"})), AGY)
        self.assertEqual(enforcers.denied_permissions({}), AGY)

    def test_denied_permissions_follow_deny_agent_platform(self):
        # Antigravity signed in with ADC or pay-as-you-go calls Agent Platform directly (seen live): opt-in deny.
        predict = "aiplatform.googleapis.com/endpoints.predict"
        self.assertEqual(enforcers.AGENT_PLATFORM_PERMISSION, predict)
        self.assertIn(predict, enforcers.KNOWN_PERMISSIONS)
        on = support.make_settings(governance={"deny_agent_platform": True})
        self.assertEqual(enforcers.denied_permissions(on), sorted([AGY[0], predict]))
        both = support.make_settings(governance={"deny_agent_platform": True, "deny_ge_app": True})
        self.assertEqual(enforcers.denied_permissions(both), sorted([AGY[0], BOTH[1], predict]))
        for value in ("yes", 1, None, False):
            loose = support.make_settings(governance={"deny_agent_platform": value})
            self.assertEqual(enforcers.denied_permissions(loose), AGY, value)
        # The settings validator accepts booleans only, and the default is off.
        from controlplane import config
        self.assertIs(config.DEFAULTS["governance"]["antigravity"]["deny_agent_platform"], False)
        self.assertIn("governance.antigravity.deny_agent_platform", config.VALIDATORS)

    def test_policy_id_falls_back_to_the_default_when_invalid(self):
        self.assertEqual(enforcers.policy_id(support.make_settings()), PID)
        team = support.make_settings(governance={"deny_policy_id": "ai-control-plane-team-a"})
        self.assertEqual(enforcers.policy_id(team), "ai-control-plane-team-a")
        for bad in ("custom-caps", "ai-control-plane-", "AI-CONTROL-PLANE-X", "ai-control-plane-caps\n", 7, None):
            self.assertEqual(enforcers.policy_id(support.make_settings(governance={"deny_policy_id": bad})), PID, bad)


class AttachmentPointsTest(unittest.TestCase):
    def test_projects_give_one_point_per_valid_project_in_order(self):
        ctx = make_ctx(workspace={"projects": ["proj-b", "Bad_Project", "proj-b", "proj-c\n", "proj-c"]})
        self.assertEqual(enforcers.attachment_points(ctx), [AP_DEMO, AP_B, AP_C])

    def test_scope_attachment_uses_the_folders_or_the_organization(self):
        ctx = make_ctx(workspace={"scope_type": "folder", "scope_ids": ["1234", "x9", "5678", "1234", " 42 "]},
                       deny_attachment="scope")
        self.assertEqual(enforcers.attachment_points(ctx),
                         [CRM + "/folders/1234", CRM + "/folders/5678", CRM + "/folders/42"])
        ctx = make_ctx(workspace={"scope_type": "organization", "scope_ids": ["777"]}, deny_attachment="scope")
        self.assertEqual(enforcers.attachment_points(ctx), [CRM + "/organizations/777"])

    def test_scope_attachment_without_a_usable_scope_falls_back_to_projects(self):
        for ws in ({"scope_type": "folder", "scope_ids": ["abc", "", "-1"]},
                   {"scope_type": "projects", "scope_ids": ["1234"]}):
            ctx = make_ctx(workspace=dict(ws, projects=["proj-b"]), deny_attachment="scope")
            self.assertEqual(enforcers.attachment_points(ctx), [AP_DEMO, AP_B], ws)

    def test_projects_attachment_ignores_a_folder_scope(self):
        ctx = make_ctx(workspace={"scope_type": "folder", "scope_ids": ["1234"], "projects": ["proj-b"]})
        self.assertEqual(enforcers.attachment_points(ctx), [AP_DEMO, AP_B])


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeIam()
        self.ctx = make_ctx(self.fake)
        self.sleeps = []
        self.client = enforcers.DenyPolicyClient(self.ctx, sleep=self.sleeps.append, polls=3)

    def test_urls_encode_the_whole_attachment_point(self):
        folder = CRM + "/folders/1234"
        self.assertIsNone(self.client.get(folder, PID))
        op = self.client.create(folder, PID, enforcers.build_policy([ALICE], AGY))
        self.assertTrue(op["done"])
        base = IAM + "/policies/cloudresourcemanager.googleapis.com%2Ffolders%2F1234/denypolicies"
        self.assertEqual(self.fake.calls[0][:3], ("GET", base + "/" + PID, {}))
        self.assertEqual(self.fake.calls[1][:3], ("POST", base, {"policyId": PID}))
        self.assertEqual(self.fake.calls[1][3], enforcers.build_policy([ALICE], AGY))
        domain_project = CRM + "/projects/example.com:proj-x"
        self.client.get(domain_project, PID)
        self.assertEqual(self.fake.calls[-1][1], IAM + "/policies/cloudresourcemanager.googleapis.com%2Fprojects%2F"
                                                       "example.com%3Aproj-x/denypolicies/" + PID)

    def test_get_update_and_delete_carry_the_etag(self):
        created = self.fake.put(AP_DEMO, denied([ALICE]))
        policy = self.client.get(AP_DEMO, PID)
        self.assertEqual(policy["etag"], created["etag"])
        op = self.client.update(AP_DEMO, PID, enforcers.build_policy([ALICE, BOB], AGY, etag=policy["etag"]))
        self.assertEqual((op["done"], op["error"]), (True, None))
        self.assertEqual(op["response"]["rules"][0]["denyRule"]["deniedPrincipals"], [ALICE, BOB])
        self.assertEqual(self.fake.calls[-1][3]["etag"], policy["etag"])
        updated = self.client.get(AP_DEMO, PID)
        self.assertNotEqual(updated["etag"], policy["etag"])
        op = self.client.delete(AP_DEMO, PID, etag=updated["etag"])
        self.assertTrue(op["done"])
        self.assertEqual(self.fake.calls[-1][:3], ("DELETE", IAM + "/policies/cloudresourcemanager.googleapis.com"
                                                            "%2Fprojects%2Fdemo-project/denypolicies/" + PID,
                                                   {"etag": updated["etag"]}))
        self.assertIsNone(self.client.get(AP_DEMO, PID))

    def test_operations_are_polled_until_done(self):
        self.fake.op_polls = 2
        op = self.client.create(AP_DEMO, PID, enforcers.build_policy([ALICE], AGY))
        self.assertEqual((op["done"], op["error"]), (True, None))
        self.assertEqual(self.sleeps, [1, 1])
        self.assertRegex(self.fake.calls[-1][1], r"^https://iam\.googleapis\.com/v2/policies/cloudresourcemanager\."
                                                 r"googleapis\.com%2Fprojects%2Fdemo-project/denypolicies/"
                                                 r"ai-control-plane-caps/operations/op-\d+$")
        self.assertEqual(self.fake.calls[-1][0], "GET")

    def test_an_operation_that_is_not_done_in_time_is_pending(self):
        self.fake.op_polls = 5
        op = self.client.create(AP_DEMO, PID, enforcers.build_policy([ALICE], AGY))
        self.assertEqual((op["done"], op["error"], op["response"]), (False, None, None))
        self.assertIn("/operations/", op["name"])
        self.assertEqual(self.sleeps, [1, 1, 1])

    def test_transient_poll_failures_are_polled_again_and_others_stop_the_wait(self):
        self.fake.op_polls = 2
        self.fake.fail("GET", "operation", GcpError("unavailable", 503))
        op = self.client.create(AP_DEMO, PID, enforcers.build_policy([ALICE], AGY))
        self.assertTrue(op["done"])
        self.assertEqual(len(self.sleeps), 3)
        self.fake.fail("GET", "operation", GcpError("denied", 403))
        policy = self.client.get(AP_DEMO, PID)
        with self.assertLogs(LOGGER, level="WARNING"):
            op = self.client.update(AP_DEMO, PID, enforcers.build_policy([BOB], AGY, etag=policy["etag"]))
        self.assertFalse(op["done"])
        self.assertEqual(len(self.sleeps), 4)

    def test_failed_operations_report_their_error(self):
        self.fake.op_errors[("POST", AP_DEMO)] = {"code": 7, "message": "Permission denied."}
        op = self.client.create(AP_DEMO, PID, enforcers.build_policy([ALICE], AGY))
        self.assertEqual((op["done"], op["error"]["code"]), (True, 7))
        self.assertIsNone(self.client.get(AP_DEMO, PID))

    def test_unexpected_operation_names_are_never_polled(self):
        for name in ("policies/../../projects/x/operations/1", "https://elsewhere.example.com/operations/1",
                     "policies/a/b/c/operations/1?alt=media", "policies/a/./c/operations/1",
                     "policies/%2e%2e/b/c/operations/1", "policies/a/b/c/operations/1#x", None, 7):
            with self.assertLogs(LOGGER, level="WARNING"):
                result = self.client.wait({"name": name})
            self.assertFalse(result["done"], name)
        self.assertEqual((self.fake.calls, self.sleeps), ([], []))
        with self.assertLogs(LOGGER, level="WARNING"):
            result = self.client.wait(None)
        self.assertEqual(result, {"name": None, "done": False, "error": None, "response": None})

    def test_only_prefixed_policy_ids_and_attachment_points_are_accepted(self):
        with self.assertRaises(ValueError):
            self.client.get(AP_DEMO, "other-policy")
        with self.assertRaises(ValueError):
            self.client.create(AP_DEMO, "ai-control-plane-caps/../x", {})
        with self.assertRaises(ValueError):
            self.client.delete("projects/demo-project", PID)
        with self.assertRaises(ValueError):
            self.client.update(AP_DEMO + "/../other", PID, {})
        self.assertEqual(self.fake.calls, [])


class ReadStateTest(unittest.TestCase):
    def test_reads_every_point_and_keeps_going_when_one_is_denied(self):
        fake = FakeIam()
        ctx = make_ctx(fake, workspace={"projects": ["proj-b", "proj-c"]})
        fake.put(AP_DEMO, denied([ALICE, BOB]))
        fake.put(AP_C, denied([ALICE]))
        fake.fail("GET", AP_B, GcpError("denied", 403, "Permission iam.denypolicies.get denied."))
        with self.assertLogs(LOGGER, level="WARNING"):
            state = enforcers.read_state(ctx)
        self.assertEqual([st.attachment for st in state.attachments], [AP_DEMO, AP_B, AP_C])
        demo, b, _ = state.attachments
        self.assertEqual((demo.exists, demo.principals, demo.permissions, demo.error, demo.foreign),
                         (True, frozenset({ALICE, BOB}), tuple(AGY), None, False))
        self.assertEqual(demo.etag, fake.policies[(AP_DEMO, PID)]["etag"])
        self.assertEqual((b.exists, b.principals, b.error), (False, frozenset(), "denied"))
        self.assertFalse(state.readable)
        self.assertEqual(state.principals, frozenset({ALICE}))
        self.assertEqual(state.partial, frozenset({BOB}))
        self.assertEqual(list(state.by_attachment), [AP_DEMO, AP_B, AP_C])
        self.assertIn(enforcers.SOURCE, ctx.sources)

    def test_absent_policies_are_readable_and_deny_nobody(self):
        fake = FakeIam()
        ctx = make_ctx(fake, workspace={"projects": ["proj-b"]})
        fake.put(AP_DEMO, denied([ALICE]))
        state = enforcers.read_state(ctx)
        self.assertTrue(state.readable)
        self.assertEqual((state.attachments[1].exists, state.attachments[1].error), (False, None))
        self.assertEqual(state.principals, frozenset())
        self.assertEqual(state.partial, frozenset({ALICE}))

    def test_without_attachment_points_nothing_is_readable(self):
        self.assertFalse(DenyState([]).readable)
        self.assertEqual((DenyState([]).principals, DenyState([]).partial), (frozenset(), frozenset()))

    def test_only_the_configured_policy_id_is_read(self):
        fake = FakeIam()
        ctx = make_ctx(fake, deny_policy_id="ai-control-plane-team-a")
        fake.put(AP_DEMO, denied([ALICE]))
        fake.put(AP_DEMO, denied([BOB]), pid="ai-control-plane-team-a")
        state = enforcers.read_state(ctx)
        self.assertEqual(state.principals, frozenset({BOB}))
        self.assertEqual([c[1].rsplit("/", 1)[1] for c in fake.calls], ["ai-control-plane-team-a"])

    def test_never_raises(self):
        class NoProjects(support.FakeCtx):
            @property
            def all_projects(self):
                raise RuntimeError("discovery failed")

        with self.assertLogs(LOGGER, level="ERROR"):
            self.assertEqual(enforcers.read_state(NoProjects()).attachments, [])

        def broken(*_args, **_kwargs):
            raise RuntimeError("transport failed")

        ctx = make_ctx()
        ctx.rest = broken
        with self.assertLogs(LOGGER, level="WARNING"):
            state = enforcers.read_state(ctx)
        self.assertEqual(state.attachments[0].error, "internal_error")
        self.assertFalse(state.readable)

    def test_policies_in_shapes_we_never_write_are_foreign(self):
        rule = denied([ALICE])["rules"][0]
        shapes = {
            "two rules": {"rules": [rule, denied([BOB])["rules"][0]]},
            "exception principals": denied([ALICE], exceptionPrincipals=[BOB]),
            "exception permissions": denied([ALICE], BOTH, exceptionPermissions=[BOTH[1]]),
            "denial condition": denied([ALICE], denialCondition={"expression": "resource.name.startsWith('x')"}),
            "other permission": denied([ALICE], ["iam.googleapis.com/roles.delete"]),
            "group": denied(["principalSet://goog/group/eng@example.com"]),
            "service account": denied(["principal://goog/subject/robot@demo-project.iam.gserviceaccount.com"]),
            "allow policy member": denied(["user:alice@example.com"]),
            "not lower case": denied(["principal://goog/subject/Alice@Example.com"]),
            "unknown rule kind": {"rules": [{"allowRule": {"members": [ALICE]}}]},
            "unknown rule field": {"rules": [dict(rule, priority=1)]},
            "rules not a list": {"rules": {"denyRule": rule["denyRule"]}},
        }
        for label, policy in shapes.items():
            fake = FakeIam()
            ctx = make_ctx(fake)
            fake.put(AP_DEMO, policy)
            state = enforcers.read_state(ctx)
            st = state.attachments[0]
            self.assertEqual((st.exists, st.foreign, st.error), (True, True, None), label)
            self.assertFalse(state.readable, label)
            self.assertEqual(state.principals, frozenset(), label)

    def test_shapes_we_write_are_ours(self):
        shapes = (denied([ALICE, DANA]), denied([ALICE], BOTH), denied([ALICE], exceptionPrincipals=[]),
                  {"displayName": "renamed", "rules": [{"description": "note", "denyRule": denied([ALICE])["rules"][0]
                                                        ["denyRule"]}]},
                  {"rules": []})
        for policy in shapes:
            fake = FakeIam()
            ctx = make_ctx(fake)
            fake.put(AP_DEMO, policy)
            state = enforcers.read_state(ctx)
            self.assertFalse(state.attachments[0].foreign, policy)
            self.assertTrue(state.readable, policy)


class BuildAndPlanTest(unittest.TestCase):
    def test_build_policy_is_deterministic(self):
        self.assertEqual(enforcers.build_policy([BOB, ALICE, BOB], AGY + AGY), {
            "displayName": "AI Control Plane caps (managed)",
            "rules": [{"denyRule": {"deniedPrincipals": [ALICE, BOB], "deniedPermissions": AGY}}]})
        self.assertEqual(enforcers.build_policy({ALICE}, AGY, etag="etag-7")["etag"], "etag-7")
        self.assertNotIn("etag", enforcers.build_policy({ALICE}, AGY))

    def test_create_update_and_none(self):
        state = DenyState([AttachmentState(AP_DEMO), ours(AP_B, {ALICE, CAROL}), ours(AP_C, {ALICE, BOB})])
        items = enforcers.plan(state, {ALICE, CAROL}, AGY)
        self.assertEqual([(i["attachment"], i["op"], i["add"], i["remove"], i["count"]) for i in items], [
            (AP_DEMO, "create", [ALICE, CAROL], [], 2), (AP_B, "none", [], [], 2), (AP_C, "update", [CAROL], [BOB], 2)])
        self.assertEqual({(i["foreign"], i["error"]) for i in items}, {(False, None)})

    def test_empty_desired_deletes_existing_policies_only(self):
        state = DenyState([AttachmentState(AP_DEMO), ours(AP_B, {BOB, ALICE})])
        items = enforcers.plan(state, set(), AGY)
        self.assertEqual([(i["op"], i["add"], i["remove"], i["count"]) for i in items],
                         [("none", [], [], 0), ("delete", [], [ALICE, BOB], 0)])

    def test_a_permission_change_alone_is_an_update(self):
        items = enforcers.plan(DenyState([ours(AP_DEMO, {ALICE})]), {ALICE}, BOTH)
        self.assertEqual([(i["op"], i["add"], i["remove"], i["count"]) for i in items], [("update", [], [], 1)])
        items = enforcers.plan(DenyState([ours(AP_DEMO, {ALICE}, BOTH)]), {ALICE}, AGY)
        self.assertEqual(items[0]["op"], "update")

    def test_unreadable_and_foreign_points_are_never_changed(self):
        foreign = AttachmentState(AP_C, exists=True, principals=frozenset({BOB}), permissions=tuple(AGY), etag="e",
                                  foreign=True)
        items = enforcers.plan(DenyState([AttachmentState(AP_B, error="denied"), foreign]), {ALICE}, AGY)
        self.assertEqual(items, [
            {"attachment": AP_B, "op": "none", "add": [], "remove": [], "count": 0, "foreign": False,
             "error": "denied"},
            {"attachment": AP_C, "op": "none", "add": [], "remove": [], "count": 1, "foreign": True,
             "error": "foreign_policy"}])
        self.assertEqual([i["op"] for i in enforcers.plan(DenyState([foreign]), set(), AGY)], ["none"])


class InvariantsTest(unittest.TestCase):
    def setUp(self):
        self.settings = support.make_settings()
        self.state = DenyState([AttachmentState(AP_DEMO)])

    def check(self, desired, permissions=None, settings=None, attachments=(AP_DEMO,), items=None):
        settings = settings or self.settings
        permissions = enforcers.denied_permissions(settings) if permissions is None else permissions
        items = enforcers.plan(self.state, desired, permissions) if items is None else items
        return enforcers.check_invariants(settings, items, desired, permissions, list(attachments))

    def test_a_safe_plan_has_no_violations(self):
        self.assertEqual(self.check({ALICE, DANA}), [])
        self.assertEqual(self.check(set()), [])

    def test_principals_must_be_people_in_deny_form(self):
        for bad in ("group:eng@example.com", "serviceAccount:robot@demo-project.iam.gserviceaccount.com",
                    "domain:example.com", "user:alice@example.com", "alice@example.com",
                    "principalSet://goog/group/eng@example.com", "principal://goog/subject/Alice@Example.com",
                    "principal://goog/subject/robot@demo-project.iam.gserviceaccount.com", "allUsers"):
            violations = self.check({ALICE, bad})
            self.assertEqual(len(violations), 1, bad)
            self.assertIn("deny policy form", violations[0])
            self.assertIn(bad, violations[0])

    def test_permissions_must_be_the_allow_list(self):
        self.assertIn("allow list", self.check({ALICE}, permissions=["iam.googleapis.com/roles.delete"])[0])
        self.assertTrue(self.check({ALICE}, permissions=BOTH))
        self.assertTrue(self.check({ALICE}, permissions=AGY + AGY))
        self.assertTrue(self.check({ALICE}, permissions=[]))
        ge = support.make_settings(governance={"deny_ge_app": True})
        self.assertTrue(self.check({ALICE}, permissions=AGY, settings=ge))
        self.assertEqual(self.check({ALICE}, settings=ge), [])

    def test_policy_id_must_keep_the_prefix(self):
        for bad in ("custom-caps", "ai-control-plane", "ai-control-plane-UPPER"):
            settings = support.make_settings(governance={"deny_policy_id": bad})
            violations = self.check({ALICE}, settings=settings)
            self.assertEqual(len(violations), 1, bad)
            self.assertIn("ai-control-plane-", violations[0])
        self.assertEqual(self.check({ALICE}, settings=support.make_settings(governance={"deny_policy_id": ""})), [])

    def test_principal_limit(self):
        violations = self.check(people(enforcers.DENY_PRINCIPAL_LIMIT + 1))
        self.assertEqual(len(violations), 2)
        self.assertTrue(all("2500" in v for v in violations))
        self.assertEqual(self.check(people(enforcers.DENY_PRINCIPAL_LIMIT)), [])

    def test_attachment_points_must_be_configured_and_planned_once(self):
        self.assertIn("outside the configured list", self.check({ALICE}, attachments=(AP_B,))[0])
        self.assertIn("outside the configured list", self.check({ALICE}, attachments=())[0])
        twice = enforcers.plan(DenyState([AttachmentState(AP_DEMO), AttachmentState(AP_DEMO)]), {ALICE}, AGY)
        self.assertIn("more than once", self.check({ALICE}, items=twice)[0])
        malformed = [dict(enforcers.plan(self.state, {ALICE}, AGY)[0], attachment="projects/demo-project")]
        self.assertTrue(self.check({ALICE}, items=malformed, attachments=("projects/demo-project",)))

    def test_foreign_policies_and_unknown_operations(self):
        item = {"attachment": AP_DEMO, "op": "update", "add": [ALICE], "remove": [], "count": 1, "foreign": True}
        self.assertIn("Foreign", self.check({ALICE}, items=[item])[0])
        self.assertIn("Unknown operation", self.check({ALICE}, items=[dict(item, foreign=False, op="replace")])[0])
        self.assertEqual(self.check({ALICE}, items=[dict(item, op="none")]), [])
        self.assertIn("More than", self.check({ALICE}, items=[dict(item, foreign=False, count="many")])[0])


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeIam()
        self.ctx = make_ctx(self.fake, workspace={"projects": ["proj-b"]})
        self.sleeps = []
        self.client = enforcers.DenyPolicyClient(self.ctx, sleep=self.sleeps.append, polls=3)

    def apply(self, desired, state=None, **kwargs):
        state = enforcers.read_state(self.ctx) if state is None else state
        return enforcers.apply(self.ctx, state, desired, client=self.client, **kwargs)

    @staticmethod
    def summary(result, *keys):
        return [tuple(item[k] for k in keys) for item in result["items"]]

    def test_create_update_none_and_delete(self):
        result = self.apply({BOB, ALICE})
        self.assertEqual((result["applied"], result["violations"]), (True, []))
        self.assertEqual(self.summary(result, "attachment", "op", "add", "count", "status", "error"),
                         [(AP_DEMO, "create", [ALICE, BOB], 2, "applied", None),
                          (AP_B, "create", [ALICE, BOB], 2, "applied", None)])
        stored = self.fake.policies[(AP_DEMO, PID)]
        self.assertEqual((stored["displayName"], stored["rules"]), (enforcers.DISPLAY_NAME, [
            {"denyRule": {"deniedPrincipals": [ALICE, BOB], "deniedPermissions": AGY}}]))

        etag = self.fake.policies[(AP_B, PID)]["etag"]
        result = self.apply({ALICE})
        self.assertEqual(self.summary(result, "op", "add", "remove", "count", "status"),
                         [("update", [], [BOB], 1, "applied")] * 2)
        self.assertEqual(self.fake.writes(AP_B)[-1][3]["etag"], etag)
        self.assertEqual(self.fake.principals(AP_B), [ALICE])

        result = self.apply({ALICE})
        self.assertEqual(self.summary(result, "op", "status", "error"), [("none", "skipped", None)] * 2)
        self.assertFalse(result["applied"])

        etag = self.fake.policies[(AP_DEMO, PID)]["etag"]
        result = self.apply(set())
        self.assertEqual(self.summary(result, "op", "remove", "count", "status"),
                         [("delete", [ALICE], 0, "applied")] * 2)
        self.assertEqual(self.fake.writes(AP_DEMO)[-1][:3][::2], ("DELETE", {"etag": etag}))
        self.assertEqual(self.fake.policies, {})
        result = self.apply(set())
        self.assertEqual((self.summary(result, "op", "status"), result["applied"]), ([("none", "skipped")] * 2, False))

    def test_a_permission_change_updates_every_point(self):
        self.fake.put(AP_DEMO, denied([ALICE]))
        self.fake.put(AP_B, denied([ALICE]))
        self.ctx.settings["governance"]["antigravity"]["deny_ge_app"] = True
        result = self.apply({ALICE})
        self.assertEqual(self.summary(result, "op", "add", "remove", "status"), [("update", [], [], "applied")] * 2)
        for ap in (AP_DEMO, AP_B):
            self.assertEqual(self.fake.policies[(ap, PID)]["rules"][0]["denyRule"]["deniedPermissions"], BOTH)

    def test_an_etag_conflict_rereads_that_point_and_retries_once(self):
        self.fake.put(AP_DEMO, denied([ALICE]))
        self.fake.put(AP_B, denied([ALICE]))
        state = enforcers.read_state(self.ctx)
        self.fake.put(AP_B, denied([ALICE, CAROL]))     # written by someone else after our read
        with self.assertLogs(LOGGER, level="WARNING"):
            result = self.apply({ALICE, BOB}, state=state)
        self.assertEqual(self.summary(result, "op", "add", "remove", "status"),
                         [("update", [BOB], [], "applied"), ("update", [BOB], [CAROL], "applied")])
        self.assertEqual(self.fake.principals(AP_B), [ALICE, BOB])
        puts = [c for c in self.fake.writes(AP_B) if c[0] == "PUT"]
        self.assertEqual(len(puts), 2)
        self.assertEqual(puts[0][3]["etag"], state.by_attachment[AP_B].etag)
        self.assertNotEqual(puts[1][3]["etag"], puts[0][3]["etag"])
        self.assertEqual(state.by_attachment[AP_B].principals, frozenset({ALICE}))    # the input state is unchanged

    def test_a_create_that_meets_an_existing_policy_becomes_an_update(self):
        state = enforcers.read_state(self.ctx)
        self.fake.put(AP_DEMO, denied([CAROL]))
        with self.assertLogs(LOGGER, level="WARNING"):
            result = self.apply({ALICE}, state=state)
        self.assertEqual(self.summary(result, "op", "add", "remove", "status"),
                         [("update", [ALICE], [CAROL], "applied"), ("create", [ALICE], [], "applied")])
        self.assertEqual(self.fake.principals(AP_DEMO), [ALICE])

    def test_a_second_conflict_fails_that_point_only(self):
        self.fake.put(AP_DEMO, denied([ALICE]))
        self.fake.put(AP_B, denied([ALICE]))
        self.fake.fail("PUT", AP_DEMO, GcpError("conflict", 409, reason="ABORTED"), GcpError("precondition", 412))
        with self.assertLogs(LOGGER, level="WARNING"):
            result = self.apply({ALICE, BOB})
        self.assertEqual(self.summary(result, "status", "error"), [("failed", "precondition"), ("applied", None)])
        self.assertTrue(result["applied"])
        self.assertEqual((self.fake.principals(AP_DEMO), self.fake.principals(AP_B)), ([ALICE], [ALICE, BOB]))

    def test_a_precondition_failure_on_delete_is_retried(self):
        self.fake.put(AP_DEMO, denied([ALICE]))
        self.fake.fail("DELETE", AP_DEMO, GcpError("precondition", 412))
        with self.assertLogs(LOGGER, level="WARNING"):
            result = self.apply(set())
        self.assertEqual(self.summary(result, "op", "status"), [("delete", "applied"), ("none", "skipped")])
        self.assertEqual(self.fake.policies, {})

    def test_a_policy_removed_meanwhile_is_skipped_or_created_again(self):
        self.fake.put(AP_DEMO, denied([ALICE]))
        self.fake.put(AP_B, denied([ALICE]))
        state = enforcers.read_state(self.ctx)
        self.fake.policies.clear()
        with self.assertLogs(LOGGER, level="WARNING"):
            result = self.apply(set(), state=state)
        self.assertEqual(self.summary(result, "op", "status", "error"), [("none", "skipped", None)] * 2)
        self.assertFalse(result["applied"])
        with self.assertLogs(LOGGER, level="WARNING"):
            result = self.apply({ALICE, BOB}, state=state)
        self.assertEqual(self.summary(result, "op", "status"), [("create", "applied")] * 2)
        self.assertEqual(self.fake.principals(AP_B), [ALICE, BOB])

    def test_an_operation_that_is_not_done_is_pending(self):
        self.fake.op_polls = 10
        result = self.apply({ALICE})
        self.assertEqual(self.summary(result, "status", "error"), [("pending", None)] * 2)
        self.assertTrue(result["applied"])
        self.assertEqual(self.sleeps, [1] * 6)

    def test_a_failed_operation_fails_that_point(self):
        self.fake.op_errors[("POST", AP_B)] = {"code": 7, "message": "Permission denied for bob@example.com."}
        self.fake.op_errors[("POST", AP_DEMO)] = {"code": 1, "message": "Cancelled."}
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            result = self.apply({ALICE})
        self.assertEqual(self.summary(result, "status", "error"), [("failed", "operation_failed"),
                                                                   ("failed", "denied")])
        self.assertFalse(result["applied"])
        self.assertEqual(self.fake.policies, {})
        self.assertNotIn("bob@example.com", "\n".join(logs.output))

    def test_an_aborted_operation_is_retried(self):
        self.fake.put(AP_DEMO, denied([ALICE]))
        self.fake.op_errors[("PUT", AP_DEMO)] = {"code": 10, "message": "Aborted."}
        with self.assertLogs(LOGGER, level="WARNING"):
            result = self.apply({BOB})
        self.assertEqual(self.summary(result, "op", "status"), [("update", "applied"), ("create", "applied")])
        self.assertEqual(self.fake.principals(AP_DEMO), [BOB])

    def test_one_failing_point_does_not_stop_the_others(self):
        self.fake.fail("POST", AP_DEMO, GcpError("denied", 403, "Permission denied for alice@example.com."))
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            result = self.apply({ALICE})
        self.assertEqual(self.summary(result, "op", "status", "error"),
                         [("create", "failed", "denied"), ("create", "applied", None)])
        self.assertTrue(result["applied"])
        self.assertNotIn("alice@example.com", "\n".join(logs.output))

    def test_unreadable_points_fail_without_a_write(self):
        self.fake.fail("GET", AP_B, *[GcpError("unavailable", 503)] * (enforcers.READ_RETRIES + 1))
        with self.assertLogs(LOGGER, level="WARNING"):
            result = self.apply({ALICE})
        self.assertEqual(self.summary(result, "op", "status", "error"),
                         [("create", "applied", None), ("none", "failed", "unavailable")])
        self.assertEqual(self.fake.writes(AP_B), [])

    def test_a_transient_read_failure_is_read_again(self):
        # Seen live: one of several parallel reads answered 429/503 once.
        self.fake.fail("GET", AP_B, GcpError("unavailable", 503))
        state = enforcers.read_state(self.ctx, client=self.client)
        self.assertTrue(state.readable)
        self.assertEqual(self.sleeps, [enforcers.READ_RETRY_SECONDS])
        self.fake.fail("GET", AP_B, GcpError("denied", 403, "Permission denied."))
        with self.assertLogs(LOGGER, level="WARNING"):
            state = enforcers.read_state(self.ctx, client=self.client)
        self.assertEqual(state.by_attachment[AP_B].error, "denied")   # not transient: no second read
        self.assertEqual(self.sleeps, [enforcers.READ_RETRY_SECONDS])

    def test_the_job_waits_out_the_policy_read_quota(self):
        # Seen live: a scheduled job run read in the same minute as the dashboard and got 429 on every short retry.
        quota = GcpError("unavailable", 429, "Quota exceeded for quota metric 'Policy Read Requests'.",
                         "RESOURCE_EXHAUSTED")
        self.ctx.deny_quota_patience = True
        patient = enforcers.DenyPolicyClient(self.ctx, sleep=self.sleeps.append, polls=3)
        self.assertEqual(patient.quota_waits, enforcers.QUOTA_WAIT_SECONDS)
        self.fake.fail("GET", AP_B, quota, quota)
        state = enforcers.read_state(self.ctx, client=patient)
        self.assertTrue(state.readable)
        self.assertEqual(self.sleeps, list(enforcers.QUOTA_WAIT_SECONDS))
        # After the quota waits, the short retries still apply; then the read fails (gate G1 stops the run).
        del self.sleeps[:]
        self.fake.fail("GET", AP_B, *[quota] * (len(enforcers.QUOTA_WAIT_SECONDS) + enforcers.READ_RETRIES + 1))
        with self.assertLogs(LOGGER, level="WARNING"):
            state = enforcers.read_state(self.ctx, client=patient)
        self.assertEqual(state.by_attachment[AP_B].error, "unavailable")
        self.assertEqual(self.sleeps, list(enforcers.QUOTA_WAIT_SECONDS) + [enforcers.READ_RETRY_SECONDS * (i + 1)
                                                                            for i in range(enforcers.READ_RETRIES)])

    def test_the_dashboard_never_waits_long_for_the_quota(self):
        quota = GcpError("unavailable", 429, "Quota exceeded.", "RESOURCE_EXHAUSTED")
        self.assertEqual(self.client.quota_waits, ())   # no deny_quota_patience on a dashboard request
        self.fake.fail("GET", AP_B, *[quota] * (enforcers.READ_RETRIES + 1))
        with self.assertLogs(LOGGER, level="WARNING"):
            state = enforcers.read_state(self.ctx, client=self.client)
        self.assertEqual(state.by_attachment[AP_B].error, "unavailable")
        self.assertEqual(self.sleeps, [enforcers.READ_RETRY_SECONDS * (i + 1) for i in range(enforcers.READ_RETRIES)])

    def test_an_account_iam_does_not_find_is_left_out(self):
        # Seen live: IAM rejects the whole write when one listed account does not exist.
        ghost = "principal://goog/subject/departed@example.com"
        self.fake.unknown.add(ghost)
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            result = self.apply({ALICE, ghost})
        self.assertEqual(self.summary(result, "op", "add", "status", "error"),
                         [("create", [ALICE], "applied", None)] * 2)
        self.assertEqual(result["missing"], [ghost])
        self.assertEqual(self.fake.principals(AP_DEMO), [ALICE])
        self.assertNotIn("departed@example.com", "\n".join(logs.output))
        # Already listed and then deleted: an update leaves it out too; nobody left means no write at all.
        self.fake.unknown.add(ALICE)
        with self.assertLogs(LOGGER, level="WARNING"):
            result = self.apply({ALICE, BOB})
        self.assertEqual(self.summary(result, "op", "add", "remove", "status"),
                         [("update", [BOB], [ALICE], "applied")] * 2)
        self.assertEqual(result["missing"], [ALICE])
        with self.assertLogs(LOGGER, level="WARNING"):
            result = enforcers.apply(self.ctx, DenyState([AttachmentState(AP_DEMO)]), {ghost}, client=self.client,
                                     attachments=[AP_DEMO])
        self.assertEqual(self.summary(result, "op", "status"), [("none", "skipped")])
        self.assertEqual(result["missing"], [ghost])

    def test_only_a_listed_principal_counts_as_missing(self):
        self.fake.fail("POST", AP_DEMO, GcpError(
            "invalid", 400, "The principal specified by member (principal://goog/subject/x@example.com) was not "
                            "found.", "INVALID_ARGUMENT"))
        with self.assertLogs(LOGGER, level="WARNING"):
            result = self.apply({ALICE})
        self.assertEqual(self.summary(result, "status", "error"), [("failed", "invalid"), ("applied", None)])
        self.assertEqual(result["missing"], [])

    def test_a_dry_run_writes_nothing(self):
        self.fake.put(AP_B, denied([BOB]))
        result = self.apply({ALICE}, dry_run=True)
        self.assertEqual(self.summary(result, "op", "add", "remove", "status", "error"),
                         [("create", [ALICE], [], "skipped", None), ("update", [ALICE], [BOB], "skipped", None)])
        self.assertEqual((result["applied"], result["violations"], self.fake.writes()), (False, [], []))

    def test_violations_refuse_every_write(self):
        with self.assertLogs(LOGGER, level="WARNING"):
            result = self.apply({ALICE, "group:eng@example.com"})
        self.assertEqual(len(result["violations"]), 1)
        self.assertEqual(self.summary(result, "op", "status", "error"),
                         [("create", "skipped", "invariant_violation")] * 2)
        self.assertEqual((result["applied"], self.fake.writes()), (False, []))
        self.ctx.settings["governance"]["antigravity"]["deny_policy_id"] = "custom-caps"
        with self.assertLogs(LOGGER, level="WARNING"):
            result = self.apply({ALICE})
        self.assertTrue(result["violations"])
        with self.assertLogs(LOGGER, level="WARNING"):
            result = self.apply(people(enforcers.DENY_PRINCIPAL_LIMIT + 1))
        self.assertTrue(result["violations"])
        self.assertEqual(self.fake.writes(), [])

    def test_a_state_read_for_other_attachment_points_is_refused(self):
        state = enforcers.read_state(self.ctx)
        with self.assertLogs(LOGGER, level="WARNING"):
            result = self.apply({ALICE}, state=state, attachments=[AP_DEMO])
        self.assertIn("outside the configured list", result["violations"][0])
        self.assertEqual(self.fake.writes(), [])

    def test_foreign_policies_are_reported_and_never_modified(self):
        self.fake.put(AP_B, denied([BOB], exceptionPrincipals=[CAROL]))
        before = copy.deepcopy(self.fake.policies[(AP_B, PID)])
        for desired in ({ALICE}, set()):
            result = self.apply(desired)
            self.assertEqual(result["violations"], [])
            self.assertEqual(self.summary(result, "op", "foreign", "status", "error")[1],
                             ("none", True, "failed", "foreign_policy"))
        self.assertEqual(self.fake.policies[(AP_B, PID)], before)
        self.assertEqual(self.fake.writes(AP_B), [])

    def test_a_retry_that_finds_a_foreign_policy_stops(self):
        self.fake.put(AP_DEMO, denied([ALICE]))
        state = enforcers.read_state(self.ctx)
        self.fake.put(AP_DEMO, denied([ALICE], denialCondition={"expression": "true"}))
        with self.assertLogs(LOGGER, level="WARNING"):
            result = self.apply({BOB}, state=state)
        self.assertEqual(self.summary(result, "foreign", "status", "error")[0], (True, "failed", "foreign_policy"))
        self.assertEqual(len(self.fake.writes(AP_DEMO)), 1)
        self.assertEqual(self.fake.principals(AP_DEMO), [ALICE])

    def test_a_policy_without_an_etag_is_never_written(self):
        state = DenyState([ours(AP_DEMO, {ALICE}, etag=None)])
        result = self.apply({BOB}, state=state)
        self.assertEqual(self.summary(result, "op", "status", "error"), [("update", "failed", "missing_etag")])
        self.assertEqual(self.fake.writes(), [])

    def test_points_that_are_no_longer_configured_can_be_cleaned_up(self):
        old = CRM + "/folders/1234"
        self.fake.put(old, denied([ALICE]))
        state = enforcers.read_state(self.ctx, attachments=[old])
        with self.assertLogs(LOGGER, level="WARNING"):
            self.assertTrue(self.apply(set(), state=state)["violations"])
        result = self.apply(set(), state=state, attachments=[old])
        self.assertEqual(self.summary(result, "attachment", "op", "status"), [(old, "delete", "applied")])
        self.assertEqual(self.fake.policies, {})

    def test_points_are_written_in_batches(self):
        projects = ["proj-%02d" % i for i in range(1, 10)]
        self.ctx.settings["workspace"]["projects"] = projects
        runs = []
        run = self.ctx.run
        self.ctx.run = lambda tasks: runs.append(len(tasks)) or run(tasks)
        result = self.apply({ALICE})
        self.assertEqual(runs, [10, enforcers.APPLY_BATCH, 10 - enforcers.APPLY_BATCH])
        self.assertEqual([i["attachment"] for i in result["items"]],
                         [AP_DEMO] + [CRM + "/projects/" + p for p in projects])
        self.assertEqual({i["status"] for i in result["items"]}, {"applied"})


class CapacityTest(unittest.TestCase):
    def test_count_and_ratio_per_point(self):
        foreign = AttachmentState(AP_B, exists=True, principals=frozenset({ALICE, BOB}), permissions=tuple(AGY),
                                  etag="e", foreign=True)
        state = DenyState([AttachmentState(AP_DEMO), foreign, ours(AP_C, {CAROL})])
        self.assertEqual(enforcers.capacity(state, people(2100)), [
            {"attachment": AP_DEMO, "count": 2100, "limit": 2500, "ratio": 0.84},
            {"attachment": AP_B, "count": 2, "limit": 2500, "ratio": 0.0008},
            {"attachment": AP_C, "count": 2100, "limit": 2500, "ratio": 0.84}])
        self.assertGreaterEqual(2100, enforcers.DENY_WARN_AT)
        self.assertEqual(enforcers.capacity(state, set())[0]["count"], 0)
        self.assertEqual(enforcers.capacity(DenyState([]), {ALICE}), [])


if __name__ == "__main__":
    unittest.main()
