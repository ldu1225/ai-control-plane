"""Suspension enforcement with IAM deny policies (governance.antigravity.enforcement_backend = iam_deny).

The control plane manages one deny policy with a fixed id (policy_id: governance.antigravity.deny_policy_id, always
with the ai-control-plane- prefix) at every attachment point (attachment_points): each workspace project, or the
folders / organization of a folder or organization scope when deny_attachment is "scope" (a deny policy is inherited
by every project below its attachment point). The policy has one rule that denies the allow-listed permissions
(denied_permissions: Antigravity generateContent, plus the Gemini Enterprise assistant with deny_ge_app and Agent
Platform model calls with deny_agent_platform) to the suspended people, written as principal://goog/subject/EMAIL
(Google accounts) or as the workforce principal URI
(identity.deny_principal). Unlike the allow policy swap, a deny policy also stops access that comes from groups,
inheritance and workforce pools.

IAM v2 behavior this module relies on (IAM "Deny access to resources" and the REST v2 policies reference):
- A policy lives at policies/{URL-encoded attachment point}/denypolicies/{policy id}; the attachment point is
  cloudresourcemanager.googleapis.com/{organizations|folders|projects}/ID.
- Create, update and delete return a long-running operation; updates and deletes carry the etag that was read.
  Operations are polled a bounded number of times: one that is not done in time is reported as pending.
- A resource holds at most 500 deny policies, 500 deny rules and 2,500 denied principals across its deny policies.
  Changes are eventually consistent.
- Deny Admin (roles/iam.denyAdmin, writes) and Deny Reviewer (roles/iam.denyReviewer, reads) can only be granted on
  the organization.

Safety rules:
- Only the policy with the configured id is read or written; other deny policies are never touched.
- A policy with our id in a shape this module never writes (several rules, exception principals or permissions, a
  denial condition, other permissions, principals that are not people in deny form) is foreign: it is reported and
  never modified.
- apply() checks the invariants first (check_invariants) and writes nothing when one is violated. A write that meets
  a concurrent change (conflict / precondition) re-reads that attachment point once and retries with a fresh plan;
  one failing attachment point does not stop the others.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field

from .. import config, identity
from ..gcp import GcpError, log_safe

LOG = logging.getLogger("controlplane.enforcers")

IAM_V2 = "https://iam.googleapis.com/v2"
CRM_SERVICE = "cloudresourcemanager.googleapis.com"
DENY_PRINCIPAL_LIMIT = 2500     # denied principals across the deny policies of one resource (IAM limit)
DENY_WARN_AT = 2000             # gate G6 warns from this many principals at an attachment point
ANTIGRAVITY_PERMISSION = "businessaicode.googleapis.com/locations.generateContent"
GE_ASSIST_PERMISSION = "discoveryengine.googleapis.com/assistants.assist"
# Antigravity signed in with Application Default Credentials (AGY_ADC_AUTH) or pay-as-you-go calls Agent Platform models
# directly (PredictionService.StreamGenerateContent on aiplatform.googleapis.com, seen live), so the Antigravity permission
# does not stop it. This one does, for every Agent Platform model call of the person at the attachment point.
AGENT_PLATFORM_PERMISSION = "aiplatform.googleapis.com/endpoints.predict"
KNOWN_PERMISSIONS = frozenset((ANTIGRAVITY_PERMISSION, GE_ASSIST_PERMISSION, AGENT_PLATFORM_PERMISSION))
DISPLAY_NAME = "AI Control Plane caps (managed)"
POLICY_PREFIX = "ai-control-plane-"
DEFAULT_POLICY_ID = "ai-control-plane-caps"
OPS = ("create", "update", "delete", "none")
SOURCE = "api:iam.denypolicies"
POLL_SECONDS = 1
DEFAULT_POLLS = 5               # operation polls per write; the change is eventually consistent anyway
# Default IAM quota of the policy API (deny policies), per project of the caller's quota project: 10 "Policy Read
# Requests" and 10 "Policy Write Requests" per minute (Service Usage, iam.googleapis.com, read_policies and
# write_policies). Every run reads the policy at every attachment point, so more points than QUOTA_WARN_POINTS leave
# no room for writes and retries: attach at a folder or organization (deny_attachment scope) or raise the quota.
POLICY_API_QUOTA_PER_MINUTE = 10
QUOTA_WARN_POINTS = 5
REQUEST_TIMEOUT = 30
APPLY_BATCH = 8                 # attachment points written concurrently
RETRY_KINDS = frozenset(("conflict", "precondition"))
TRANSIENT_KINDS = frozenset(("unavailable", "timeout", "network"))
READ_RETRIES = 2
READ_RETRY_SECONDS = 1
# Seen live: a scheduled job run read the policies in the same minute as the dashboard and got 429 RESOURCE_EXHAUSTED
# (the 10 reads per minute above) on every retry, so it stopped at gate G1. The enforcement job sets
# ctx.deny_quota_patience and then waits these seconds after a 429 read before reading again, which carries the read
# into the next quota minute; the dashboard keeps the short retries and never waits that long.
QUOTA_WAIT_SECONDS = (20, 40)
# IAM rejects a whole write with 400 INVALID_ARGUMENT "The principal specified by member (principal://...) was not
# found." when one listed account does not exist (seen live for a deleted or never created account). Such principals
# are left out, at most MAX_MISSING_PRINCIPALS per write.
MISSING_PRINCIPAL_RE = re.compile(r"principal specified by member \((principal://[^)\s]+)\) was not found")
MAX_MISSING_PRINCIPALS = 25
SCOPE_COLLECTIONS = {"folder": "folders", "organization": "organizations"}
# google.rpc.Code of a failed operation -> the GcpError kind of the same failure over HTTP.
RPC_KINDS = {3: "invalid", 4: "timeout", 5: "not_found", 6: "conflict", 7: "denied", 8: "unavailable",
             9: "precondition", 10: "conflict", 13: "unavailable", 14: "unavailable", 16: "denied"}

_ATTACHMENT_RE = re.compile(r"cloudresourcemanager\.googleapis\.com/(?:organizations/[0-9]{1,30}"
                            r"|folders/[0-9]{1,30}|projects/[a-z0-9][a-z0-9.:\-]{4,99})")
_SEGMENT = r"[A-Za-z0-9._~%:\-]+"
# Operation names: policies/{attachment point}/denypolicies/{policy id}/operations/{id} (the point stays encoded).
_OPERATION_RE = re.compile(r"policies/%s/%s/%s/operations/%s" % ((_SEGMENT,) * 4))
_RULE_KEYS = frozenset(("denyRule", "description"))
_DENY_LISTS = ("deniedPrincipals", "deniedPermissions")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _antigravity(settings):
    return ((settings or {}).get("governance") or {}).get("antigravity") or {}


def denied_permissions(settings):
    """Permissions the managed rule denies, sorted: Antigravity always, the Gemini Enterprise assistant with
    governance.antigravity.deny_ge_app, and Agent Platform model calls with governance.antigravity.deny_agent_platform."""
    ag = _antigravity(settings)
    perms = {ANTIGRAVITY_PERMISSION}
    if ag.get("deny_ge_app") is True:
        perms.add(GE_ASSIST_PERMISSION)
    if ag.get("deny_agent_platform") is True:
        perms.add(AGENT_PLATFORM_PERMISSION)
    return sorted(perms)


def policy_id(settings):
    """governance.antigravity.deny_policy_id, or ai-control-plane-caps when it is not a valid prefixed id."""
    value = _antigravity(settings).get("deny_policy_id")
    if isinstance(value, str) and config.DENY_POLICY_ID_RE.fullmatch(value):
        return value
    return DEFAULT_POLICY_ID


def _scope_ids(raw):
    """Valid numeric folder or organization ids of workspace.scope_ids, in order and without duplicates."""
    if isinstance(raw, str):
        raw = raw.split(",")
    ids = (str(value).strip() for value in raw or ())
    return list(dict.fromkeys(value for value in ids if config.SCOPE_ID_RE.fullmatch(value)))


def attachment_points(ctx):
    """Where our deny policy is attached, ordered and without duplicates.

    deny_attachment "scope" with a folder or organization scope and valid scope_ids: each folder or organization
    (inherited by every project below; primary or configured projects outside the scope are not covered). Otherwise
    one point per workspace project (ctx.all_projects) with a valid project id."""
    settings = ctx.settings
    ws = settings.get("workspace") or {}
    collection = SCOPE_COLLECTIONS.get(ws.get("scope_type"))
    if _antigravity(settings).get("deny_attachment") == "scope" and collection:
        ids = _scope_ids(ws.get("scope_ids"))
        if ids:
            return ["%s/%s/%s" % (CRM_SERVICE, collection, value) for value in ids]
    out, seen = [], set()
    for pid in ctx.all_projects or ():
        if not (isinstance(pid, str) and config.valid_project(pid)):
            continue
        point = "%s/projects/%s" % (CRM_SERVICE, pid)
        if _ATTACHMENT_RE.fullmatch(point) and point not in seen:
            seen.add(point)
            out.append(point)
    return out


# ---------------------------------------------------------------------------
# IAM v2 client
# ---------------------------------------------------------------------------

def _checked_id(pid):
    if not (isinstance(pid, str) and config.DENY_POLICY_ID_RE.fullmatch(pid)):
        raise ValueError("deny policy ids must start with %s" % POLICY_PREFIX)
    return pid


def _collection_url(ap):
    if not (isinstance(ap, str) and _ATTACHMENT_RE.fullmatch(ap)):
        raise ValueError("not a deny policy attachment point")
    return "%s/policies/%s/denypolicies" % (IAM_V2, urllib.parse.quote(ap, safe=""))


def _policy_url(ap, pid):
    return "%s/%s" % (_collection_url(ap), _checked_id(pid))


def _valid_operation(name):
    """True for an operation name that can be appended to IAM_V2 safely (no dot segments, query or fragment)."""
    if not (isinstance(name, str) and len(name) <= 1024 and _OPERATION_RE.fullmatch(name)):
        return False
    return ".." not in name and "%2e" not in name.lower() and "." not in name.split("/")


def _summary(op, name):
    error, response = op.get("error"), op.get("response")
    return {"name": name, "done": op.get("done") is True, "error": error if isinstance(error, dict) else None,
            "response": response if isinstance(response, dict) else None}


class DenyPolicyClient:
    """IAM v2 deny policy calls through ctx.rest; writes wait for their long-running operation (wait).

    Only well-formed attachment points and policy ids with the control plane prefix are accepted (ValueError
    otherwise), so the client cannot reach any other deny policy. `sleep` and `polls` bound the wait: at most `polls`
    polls POLL_SECONDS apart (tests pass a no-op sleep)."""

    def __init__(self, ctx, sleep=time.sleep, polls=DEFAULT_POLLS, quota_waits=None):
        self.ctx = ctx
        self.sleep = sleep
        self.polls = max(0, int(polls))
        # Waits after a 429 read: QUOTA_WAIT_SECONDS in the enforcement job (ctx.deny_quota_patience), none elsewhere.
        if quota_waits is None:
            quota_waits = QUOTA_WAIT_SECONDS if getattr(ctx, "deny_quota_patience", False) is True else ()
        self.quota_waits = tuple(quota_waits)

    def get(self, ap, pid):
        """The policy with id `pid` at `ap`, or None when there is none (404). A 429 (policy read quota) is first read
        again after each of `quota_waits`; then a transient failure (429, 5xx, timeout, network) is read again up to
        READ_RETRIES times, READ_RETRY_SECONDS x attempt apart: reads are idempotent, and one failed read stops the
        whole run at gate G1."""
        url = _policy_url(ap, pid)
        quota_tries = transient_tries = 0
        while True:
            try:
                payload = self.ctx.rest("GET", url, timeout=REQUEST_TIMEOUT)
            except GcpError as err:
                if err.kind == "not_found":
                    return None
                if err.status == 429 and quota_tries < len(self.quota_waits):
                    self.sleep(self.quota_waits[quota_tries])
                    quota_tries += 1
                    continue
                if err.kind in TRANSIENT_KINDS and transient_tries < READ_RETRIES:
                    transient_tries += 1
                    self.sleep(READ_RETRY_SECONDS * transient_tries)
                    continue
                raise
            return payload if isinstance(payload, dict) else {}

    def create(self, ap, pid, policy):
        """Creates the policy (POST ?policyId=); returns the final operation (see wait)."""
        op = self.ctx.rest("POST", _collection_url(ap), json_body=policy, params={"policyId": _checked_id(pid)},
                           timeout=REQUEST_TIMEOUT)
        return self.wait(op)

    def update(self, ap, pid, policy):
        """Replaces the policy (PUT; `policy` carries the etag that was read); returns the final operation."""
        return self.wait(self.ctx.rest("PUT", _policy_url(ap, pid), json_body=policy, timeout=REQUEST_TIMEOUT))

    def delete(self, ap, pid, etag=None):
        """Deletes the policy, guarded by `etag` when given; returns the final operation."""
        params = {"etag": etag} if etag else None
        return self.wait(self.ctx.rest("DELETE", _policy_url(ap, pid), params=params, timeout=REQUEST_TIMEOUT))

    def wait(self, op):
        """{"name", "done", "error", "response"} of a long-running operation, polled until done at most `polls`
        times. done False: the change is still in progress (pending). Transient poll failures are polled again;
        other failures and names that are not operation names stop the wait."""
        current = op if isinstance(op, dict) else {}
        name = current.get("name") if isinstance(current.get("name"), str) else None
        polls = 0
        while current.get("done") is not True and current.get("error") is None and polls < self.polls:
            if not _valid_operation(name):
                LOG.warning("deny policy operation cannot be polled: unexpected operation name")
                break
            self.sleep(POLL_SECONDS)
            polls += 1
            try:
                fresh = self.ctx.rest("GET", "%s/%s" % (IAM_V2, name), timeout=REQUEST_TIMEOUT)
            except GcpError as err:
                if err.kind in TRANSIENT_KINDS:
                    continue
                LOG.warning("deny policy operation poll failed kind=%s", err.kind)
                break
            if isinstance(fresh, dict):
                current = fresh
        return _summary(current, name)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AttachmentState:
    """Our deny policy at one attachment point as read. principals and permissions are empty when the policy does
    not exist or could not be read (error: the GcpError kind, "invalid" or "internal_error"). foreign: a policy with
    our id in a shape we never write; it is reported and never modified."""

    attachment: str
    exists: bool = False
    principals: frozenset = frozenset()
    permissions: tuple = ()
    etag: str | None = None
    error: str | None = None
    foreign: bool = False


@dataclass
class DenyState:
    """Our deny policy at every attachment point (read_state)."""

    attachments: list = field(default_factory=list)

    def _usable(self):
        return [st for st in self.attachments if st.error is None and not st.foreign]

    @property
    def readable(self):
        """True when there is at least one attachment point and every one was read and is ours (gate G1)."""
        return bool(self.attachments) and all(st.error is None and not st.foreign for st in self.attachments)

    @property
    def principals(self):
        """Principals denied at every readable attachment point that is ours."""
        usable = self._usable()
        return frozenset.intersection(*(st.principals for st in usable)) if usable else frozenset()

    @property
    def partial(self):
        """Principals denied at some but not all readable attachment points that are ours."""
        usable = self._usable()
        return frozenset().union(*(st.principals for st in usable)) - self.principals

    @property
    def by_attachment(self):
        return {st.attachment: st for st in self.attachments}


def _deny_form(value):
    """True for a person written exactly as deny policies name them (identity.deny_principal)."""
    if not isinstance(value, str):
        return False
    found = identity.parse(value)
    return bool(found is not None and found.is_person and identity.deny_principal(value) == value)


def _state_of(ap, policy):
    """AttachmentState of an existing policy; foreign when its shape is not one this module writes."""
    etag = policy.get("etag") if isinstance(policy.get("etag"), str) else None
    rules = policy.get("rules") or []
    foreign = not isinstance(rules, list) or len(rules) > 1
    principals, permissions = set(), set()
    for rule in rules if isinstance(rules, list) else ():
        deny = rule.get("denyRule") if isinstance(rule, dict) else None
        if not isinstance(deny, dict) or any(value for key, value in rule.items() if key not in _RULE_KEYS):
            foreign = True
            continue
        for key, value in deny.items():
            if key not in _DENY_LISTS:
                foreign = foreign or bool(value)   # exceptionPrincipals, exceptionPermissions, denialCondition, ...
            elif isinstance(value, list) and all(isinstance(item, str) for item in value):
                (principals if key == "deniedPrincipals" else permissions).update(value)
            elif value:
                foreign = True
    if not permissions <= KNOWN_PERMISSIONS or not all(_deny_form(p) for p in principals):
        foreign = True
    return AttachmentState(ap, exists=True, principals=frozenset(principals), permissions=tuple(sorted(permissions)),
                           etag=etag, foreign=foreign)


def _error_kind(err):
    if isinstance(err, GcpError):
        return err.kind
    if isinstance(err, ValueError):
        return "invalid"
    LOG.warning("deny policy call failed unexpectedly: %s", type(err).__name__)
    return "internal_error"


def _read_one(client, ap, pid):
    try:
        policy = client.get(ap, pid)
    except (GcpError, ValueError) as err:
        if isinstance(err, GcpError):
            LOG.warning("deny policy read failed kind=%s status=%s", err.kind, err.status)
        return AttachmentState(ap, error=_error_kind(err))
    return AttachmentState(ap) if policy is None else _state_of(ap, policy)


def read_state(ctx, *, attachments=None, client=None):
    """Our deny policy at every attachment point (default attachment_points(ctx)), read in parallel with ctx.run.

    Never raises: an attachment point that cannot be read carries the error kind; when the points themselves cannot
    be resolved the state is empty (not readable)."""
    try:
        points = list(dict.fromkeys(attachments if attachments is not None else attachment_points(ctx)))
        client = client or DenyPolicyClient(ctx)
        pid = policy_id(ctx.settings)
        tasks = {ap: (lambda ap=ap: _read_one(client, ap, pid)) for ap in points}
        results = ctx.run(tasks) if tasks else {}
    except Exception:  # pylint: disable=broad-except
        LOG.exception("deny policy state could not be read")
        return DenyState([])
    states = []
    for ap in points:
        ok, value = results.get(ap, (False, None))
        if not (ok and isinstance(value, AttachmentState)):
            value = AttachmentState(ap, error=_error_kind(value))
        states.append(value)
    add_source = getattr(ctx, "add_source", None)
    if callable(add_source) and any(st.error is None for st in states):
        add_source(SOURCE)
    return DenyState(states)


# ---------------------------------------------------------------------------
# Plan, invariants and apply
# ---------------------------------------------------------------------------

def build_policy(principals, permissions, etag=None):
    """The policy body we write: DISPLAY_NAME and one rule denying `permissions` to `principals` (both sorted, without
    duplicates); the etag only when given (updates)."""
    policy = {"displayName": DISPLAY_NAME,
              "rules": [{"denyRule": {"deniedPrincipals": sorted(set(principals)),
                                      "deniedPermissions": sorted(set(permissions))}}]}
    if etag:
        policy["etag"] = etag
    return policy


def plan(state, desired, permissions):
    """One item per attachment point of `state`: {"attachment", "op", "add", "remove", "count", "foreign", "error"}.

    op: create (no policy yet), update (principals or permissions differ), delete (nobody to deny: an existing policy
    is removed rather than left empty) or none. add / remove: sorted principal changes. count: principals in our
    policy afterwards. A point that could not be read or holds a foreign policy is never changed: op none, error the
    read error or "foreign_policy", count the principals found there."""
    desired = frozenset(desired or ())
    wanted = sorted(set(permissions or ()), key=str)
    items = []
    for st in state.attachments:
        item = {"attachment": st.attachment, "op": "none", "add": [], "remove": [], "count": len(st.principals),
                "foreign": bool(st.foreign), "error": st.error or ("foreign_policy" if st.foreign else None)}
        if item["error"] is None:
            add = sorted(desired - st.principals, key=str)
            remove = sorted(st.principals - desired, key=str)
            if not desired:
                item.update(op="delete" if st.exists else "none", remove=remove, count=0)
            elif not st.exists:
                item.update(op="create", add=add, count=len(desired))
            else:
                changed = bool(add or remove) or wanted != sorted(set(st.permissions), key=str)
                item.update(op="update" if changed else "none", add=add, remove=remove, count=len(desired))
        items.append(item)
    return items


def _examples(values, limit=3):
    values = list(values)
    more = len(values) - limit
    return ", ".join(values[:limit]) + (" and %d more" % more if more > 0 else "")


def _count(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return DENY_PRINCIPAL_LIMIT + 1   # an unknown count never passes the limit check


def check_invariants(settings, items, desired, permissions, attachments):
    """Reasons (English sentences) not to apply `items`; [] when they are safe to apply.

    Checked: the configured policy id keeps the fixed prefix; `permissions` are exactly denied_permissions(settings);
    every principal written is a person in deny form (identity.deny_principal); every item names one of the
    configured `attachments`, at most once, with a known op; no policy gets more than DENY_PRINCIPAL_LIMIT
    principals; no foreign policy is changed."""
    out = []
    raw = _antigravity(settings).get("deny_policy_id")
    if raw not in (None, "") and not (isinstance(raw, str) and config.DENY_POLICY_ID_RE.fullmatch(raw)):
        out.append("The configured deny policy id %s is not an id with the %s prefix." % (str(raw)[:80], POLICY_PREFIX))
    pid = policy_id(settings)
    if not (pid.startswith(POLICY_PREFIX) and config.DENY_POLICY_ID_RE.fullmatch(pid)):
        out.append("The deny policy id %s does not have the %s prefix." % (pid, POLICY_PREFIX))
    expected = denied_permissions(settings)
    given = sorted(str(p) for p in permissions or ())
    if given != expected:
        out.append("The denied permissions (%s) differ from the allow list (%s)."
                   % (", ".join(given) or "none", ", ".join(expected)))
    items = list(items or ())
    written = list(desired or ()) + [p for item in items for p in item.get("add") or ()]
    bad = sorted({str(p) for p in written if not _deny_form(p)})
    if bad:
        out.append("%d principal(s) are not people in deny policy form (principal://goog/subject/EMAIL or a "
                   "workforce principal): %s." % (len(bad), _examples(bad)))
    total = len({str(p) for p in desired or ()})
    if total > DENY_PRINCIPAL_LIMIT:
        out.append("%d principals exceed the limit of %d denied principals per resource."
                   % (total, DENY_PRINCIPAL_LIMIT))
    allowed = {a for a in attachments or () if isinstance(a, str)}
    unknown, repeated, bad_ops, foreign, over, seen = [], [], [], [], [], set()
    for item in items:
        ap, op = item.get("attachment"), item.get("op")
        name = str(ap)
        if not (isinstance(ap, str) and ap in allowed and _ATTACHMENT_RE.fullmatch(ap)):
            unknown.append(name)
        if name in seen:
            repeated.append(name)
        seen.add(name)
        if op not in OPS:
            bad_ops.append(name)
        elif op != "none" and item.get("foreign"):
            foreign.append(name)
        if op in ("create", "update") and _count(item.get("count")) > DENY_PRINCIPAL_LIMIT:
            over.append(name)
    if unknown:
        out.append("Attachment points outside the configured list: %s." % _examples(unknown))
    if repeated:
        out.append("Attachment points planned more than once: %s." % _examples(repeated))
    if bad_ops:
        out.append("Unknown operations planned at: %s." % _examples(bad_ops))
    if foreign:
        out.append("Foreign deny policies would be modified at: %s." % _examples(foreign))
    if over:
        out.append("More than %d principals would be written at: %s." % (DENY_PRINCIPAL_LIMIT, _examples(over)))
    return out


def _done(item, status, error=None, missing=None):
    out = dict(item)
    out["status"] = status
    out["error"] = error
    if missing:
        out["missing"] = sorted(missing)
    return out


def _operation_error_kind(error):
    try:
        code = int(error.get("code"))
    except (AttributeError, TypeError, ValueError):
        code = None
    return RPC_KINDS.get(code, "operation_failed")


def _write(client, pid, st, op, desired, permissions):
    if op == "create":
        return client.create(st.attachment, pid, build_policy(desired, permissions))
    if op == "update":
        return client.update(st.attachment, pid, build_policy(desired, permissions, etag=st.etag))
    return client.delete(st.attachment, pid, etag=st.etag)


def _missing_principal(kind, op, message, desired):
    """The principal of `desired` that IAM reports as not found (a deleted account), else None."""
    if kind != "invalid" or op not in ("create", "update"):
        return None
    found = MISSING_PRINCIPAL_RE.search(message or "")
    return found.group(1) if found and found.group(1) in desired else None


def _execute(client, settings, pid, st, item, desired, permissions, allowed):
    """Runs one planned write. A concurrent change (conflict / precondition, or a policy that disappeared before an
    update or delete) re-reads the attachment point once and retries with a fresh, re-checked plan. A principal that
    IAM does not find (the account was deleted, so it cannot use anything) is left out and the write retried, at most
    MAX_MISSING_PRINCIPALS times; the item lists them in "missing"."""
    missing = []
    attempt = 1
    while True:
        op = item["op"]
        if op != "create" and not st.etag:
            return _done(item, "failed", "missing_etag", missing)
        try:
            result = _write(client, pid, st, op, desired, permissions)
        except GcpError as err:
            kind, message = err.kind, err.message
            LOG.warning("deny policy %s failed kind=%s status=%s message=%s", op, kind, err.status,
                        log_safe(err.message))
        else:
            if result["error"] is None:
                return _done(item, "applied" if result["done"] else "pending", None, missing)
            kind, message = _operation_error_kind(result["error"]), result["error"].get("message")
            LOG.warning("deny policy %s operation failed kind=%s message=%s", op, kind, log_safe(message))
        gone = _missing_principal(kind, op, message, desired)
        if gone is not None and len(missing) < MAX_MISSING_PRINCIPALS:
            LOG.warning("deny policy %s: IAM does not find one principal; writing the policy without it", op)
            missing.append(gone)
            desired = desired - {gone}
            item = plan(DenyState([st]), desired, permissions)[0]   # nothing was written: the state is unchanged
        elif attempt == 2 or not (kind in RETRY_KINDS or (kind == "not_found" and op != "create")):
            return _done(item, "failed", kind, missing)
        else:
            attempt += 1
            LOG.info("deny policy %s met a concurrent change (%s); re-reading the attachment point", op, kind)
            st = _read_one(client, st.attachment, pid)
            item = plan(DenyState([st]), desired, permissions)[0]
        if check_invariants(settings, [item], desired, permissions, allowed):
            return _done(item, "failed", "invariant_violation", missing)
        if item["error"] or item["op"] == "none":
            return _done(item, "failed" if item["error"] else "skipped", item["error"], missing)


def apply(ctx, state, desired, *, dry_run=False, client=None, attachments=None):
    """Brings our deny policy at every attachment point of `state` to `desired` (deny principal identifiers, see
    identity.deny_principal) with denied_permissions(ctx.settings). Points are written APPLY_BATCH at a time.

    Returns {"items": [plan item + "status" + "error"], "applied": bool, "violations": [...], "missing": [...]}. status:
    applied (written, operation done), pending (written, operation not done after the polls; eventually consistent),
    failed (error: a GcpError kind, operation_failed, missing_etag, invariant_violation, foreign_policy or the read
    error of that point) or skipped (nothing to change, dry run, or refused). applied: something was written (applied
    or pending). missing: principals that IAM does not find (deleted accounts), left out of the written policies.
    With any violation nothing is written. `attachments` replaces the allowed attachment points (default
    attachment_points(ctx)), for example to clean up points that are no longer configured."""
    settings = ctx.settings
    desired = frozenset(desired or ())
    permissions = denied_permissions(settings)
    allowed = list(attachments) if attachments is not None else attachment_points(ctx)
    items = plan(state, desired, permissions)
    violations = check_invariants(settings, items, desired, permissions, allowed)
    if violations:
        LOG.warning("deny policy apply refused: %d invariant violation(s)", len(violations))
        refused = [_done(item, "skipped", item["error"] or ("invariant_violation" if item["op"] != "none" else None))
                   for item in items]
        return {"items": refused, "applied": False, "violations": violations, "missing": []}
    if dry_run:
        return {"items": [_done(item, "skipped", item["error"]) for item in items], "applied": False, "violations": [],
                "missing": []}
    client = client or DenyPolicyClient(ctx)
    pid = policy_id(settings)
    by_attachment = state.by_attachment

    def run(index):
        item = items[index]
        return _execute(client, settings, pid, by_attachment[item["attachment"]], item, desired, permissions, allowed)

    results = [None] * len(items)
    writes = []
    for index, item in enumerate(items):
        if item["op"] == "none":
            results[index] = _done(item, "failed" if item["error"] else "skipped", item["error"])
        else:
            writes.append(index)
    for start in range(0, len(writes), APPLY_BATCH):
        chunk = writes[start:start + APPLY_BATCH]
        outcome = ctx.run({index: (lambda index=index: run(index)) for index in chunk})
        for index in chunk:
            ok, value = outcome.get(index, (False, None))
            results[index] = value if ok and isinstance(value, dict) else \
                _done(items[index], "failed", _error_kind(value))
    statuses = [item["status"] for item in results]
    missing = sorted({p for item in results for p in item.get("missing") or ()})
    LOG.info("deny policy apply attachments=%d applied=%d pending=%d failed=%d skipped=%d missing=%d", len(results),
             statuses.count("applied"), statuses.count("pending"), statuses.count("failed"), statuses.count("skipped"),
             len(missing))
    return {"items": results, "applied": any(s in ("applied", "pending") for s in statuses), "violations": [],
            "missing": missing}


def capacity(state, desired):
    """Principals our policy holds after applying `desired`, per attachment point, against DENY_PRINCIPAL_LIMIT (gate
    G6 warns from DENY_WARN_AT and holds new suspensions beyond the limit). A foreign policy keeps its own principals.
    Other deny policies at the same point count toward the same IAM limit and are not read here."""
    wanted = len(frozenset(desired or ()))
    out = []
    for st in state.attachments:
        count = len(st.principals) if st.foreign else wanted
        out.append({"attachment": st.attachment, "count": count, "limit": DENY_PRINCIPAL_LIMIT,
                    "ratio": round(count / float(DENY_PRINCIPAL_LIMIT), 4)})
    return out
