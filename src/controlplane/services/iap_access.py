"""Google sign-in through Identity-Aware Proxy (IAP): who IAP admits, kept in step with the approved accounts.

Google sign-in reaches the dashboard only after IAP admits the Google account. IAP sits in front of a Cloud Run service:
the sign-in bridge (auth_mode google without an OAuth client) or the dashboard itself (auth_mode iap, also used when
organization policies block a public entry). Two rules decide whether IAP admits an account, and a refusal shows
Google's IAP page ("You don't have access"), never the dashboard:

1. IAM: roles/iap.httpsResourceAccessor on that service must cover the account (allAuthenticatedUsers,
   domain:<its domain>, user:<email> or a group). Domain restricted sharing (iam.allowedPolicyMemberDomains) rejects
   allAuthenticatedUsers and every account outside the allowed Google Workspace / Cloud Identity customers.
2. OAuth client: Cloud Run IAP uses a Google-managed OAuth client unless a custom one is configured. The Google-managed
   client signs in accounts of the project's own organization only. Accounts of other organizations and personal
   accounts need a custom OAuth client (audience External) on IAP, a Google OAuth client on the dashboard
   (ACCESS_MODE google_oauth), or an ID/password account.

status() reads both, grant() adds principals for approved Google accounts that IAP does not admit yet (one request,
then one principal at a time so a refused account does not block the others), revoke() removes the user: binding of a
removed account. Administrators see the result under Settings > Access control.
"""

from __future__ import annotations

import logging
import re
import threading
import time

from ..gcp import GcpError
from ..httpkit import now_iso
from ..security import resolve_auth_mode

LOG = logging.getLogger("controlplane.iap_access")

ROLE = "roles/iap.httpsResourceAccessor"
API = "https://iap.googleapis.com/v1/"
STATUS_TTL = 60.0
OPEN_MEMBERS = ("allAuthenticatedUsers", "allUsers")
PUBLIC_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com", "icloud.com", "me.com",
    "aol.com", "proton.me", "protonmail.com", "naver.com", "daum.net", "hanmail.net", "kakao.com", "nate.com",
})
_RESOURCE_RE = re.compile(r"^projects/([0-9]+)/iap_web/cloud_run-([a-z0-9-]+)/services/([a-z0-9-]+)$")
_AUDIENCE_RE = re.compile(r"^/projects/([0-9]+)/locations/([a-z0-9-]+)/services/([a-z0-9-]+)$")
_RUN_APP_RE = re.compile(r"^https://([a-z0-9-]+)-([0-9]+)\.([a-z0-9-]+)\.run\.app(?:[:/]|$)")
_DOMAIN_RE = re.compile(r"^(?=.{4,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
_EMAIL_RE = re.compile(r"^[a-z0-9._%+'\-]+@[a-z0-9.-]+\.[a-z]{2,}$")
# Domain restricted sharing refusals: "One or more users named in the policy do not belong to a permitted customer,
# perhaps due to an organization policy."
_POLICY_RE = re.compile(r"permitted customer|allowedPolicyMemberDomains|allowedPolicyMembers|organization polic", re.I)

_cache = {}          # resource -> (monotonic time, status dict)
_last = {}           # approved entry -> latest grant result on this instance (shown next to the account)
_lock = threading.Lock()


def _resource_for(number, region, service):
    return "projects/%s/iap_web/cloud_run-%s/services/%s" % (number, region, service)


def resource(app):
    """IAP resource name of the Cloud Run service that signs Google accounts in, or None when Google sign-in does not
    go through IAP (password-only deployments, a Google OAuth client on the dashboard, local development)."""
    environ = getattr(app, "environ", None) or {}
    auth = getattr(app, "auth", None)
    if auth is not None and auth.oauth_active():
        return None  # an OAuth client signs users in (deployment or connected in Settings); IAP is not on the path
    explicit = str(environ.get("IAP_ACCESS_RESOURCE") or environ.get("SIGN_IN_BRIDGE_IAP_RESOURCE") or "").strip()
    if _RESOURCE_RE.match(explicit):
        return explicit
    sec = (getattr(getattr(app, "settings", None), "base", None) or {}).get("security") or {}
    try:
        mode = resolve_auth_mode(sec.get("auth_mode"), environ)
    except ValueError:
        return None
    if mode == "iap":
        match = _AUDIENCE_RE.match(str(sec.get("iap_audience") or "").strip())
        return _resource_for(*match.groups()) if match else None
    if mode != "google" or auth is None or not auth.bridge_url:
        return None
    match = _RUN_APP_RE.match(auth.bridge_url.strip().lower())
    if not match:
        return None
    service, number, region = match.groups()
    return _resource_for(number, region, service)


def principal_for(entry):
    """user:<email> for an exact email, domain:<domain> for a *@domain pattern; None for '*' and ID accounts."""
    value = str(entry or "").strip().lower()
    if value.startswith("*@"):
        domain = value[2:]
        return "domain:" + domain if _DOMAIN_RE.match(domain) else None
    if _EMAIL_RE.match(value):
        return "user:" + value
    return None


def covers(members, entry):
    """True when the IAP members admit the approved entry (an email, a *@domain pattern or '*')."""
    members = set(members or ())
    if members & set(OPEN_MEMBERS):
        return True
    value = str(entry or "").strip().lower()
    if value.startswith("*@"):
        return ("domain:" + value[2:]) in members
    if "@" not in value:
        return False
    return ("user:" + value) in members or ("domain:" + value.rpartition("@")[2]) in members


def _members(policy):
    out = set()
    for binding in (policy or {}).get("bindings") or []:
        if binding.get("role") == ROLE:
            out.update(m for m in binding.get("members") or [] if isinstance(m, str))
    return out


def _get_policy(app, res, quota_project, timeout=15):
    policy, _ = app.gcp.request("POST", API + res + ":getIamPolicy", quota_project=quota_project,
                                json_body={"options": {"requestedPolicyVersion": 3}}, timeout=timeout)
    return policy or {}


def _with_members(policy, add):
    """The policy with `add` appended to the unconditional IAP accessor binding (other bindings unchanged)."""
    bindings = [dict(b) for b in (policy or {}).get("bindings") or []]
    target = next((b for b in bindings if b.get("role") == ROLE and not b.get("condition")), None)
    if target is None:
        target = {"role": ROLE, "members": []}
        bindings.append(target)
    target["members"] = sorted(set(target.get("members") or []) | set(add))
    out = {"bindings": bindings,
           "version": 3 if any(b.get("condition") for b in bindings) else int((policy or {}).get("version") or 1)}
    if (policy or {}).get("etag"):
        out["etag"] = policy["etag"]
    return out


def _without_members(policy, remove):
    bindings = []
    for binding in (policy or {}).get("bindings") or []:
        binding = dict(binding)
        if binding.get("role") == ROLE and not binding.get("condition"):
            binding["members"] = [m for m in binding.get("members") or [] if m not in remove]
            if not binding["members"]:
                continue
        bindings.append(binding)
    out = {"bindings": bindings,
           "version": 3 if any(b.get("condition") for b in bindings) else int((policy or {}).get("version") or 1)}
    if (policy or {}).get("etag"):
        out["etag"] = policy["etag"]
    return out


def _set_policy(app, res, quota_project, policy):
    app.gcp.request("POST", API + res + ":setIamPolicy", quota_project=quota_project, json_body={"policy": policy},
                    timeout=15)


def classify(err):
    """rejected (organization policy, usually domain restricted sharing), denied (the dashboard's service account
    may not change IAP), invalid (unknown account or domain) or failed."""
    message = getattr(err, "message", "") or str(err)
    if _POLICY_RE.search(message):
        return "rejected"
    kind = getattr(err, "kind", "")
    if kind == "denied":
        return "denied"
    if kind == "invalid":
        return "invalid"
    return "failed"


def _result(entry, principal, outcome, err=None):
    out = {"entry": entry, "principal": principal, "result": outcome, "at": now_iso()}
    if err is not None:
        out["message"] = (getattr(err, "message", "") or str(err))[:300]
    return out


def _invalidate(res):
    with _lock:
        _cache.pop(res, None)


def _remember(results):
    with _lock:
        for item in results:
            if item["result"] == "covered":
                _last.pop(item["entry"], None)
            else:
                _last[item["entry"]] = dict(item)
        while len(_last) > 500:
            _last.pop(next(iter(_last)))


def last_result(entry):
    with _lock:
        item = _last.get(str(entry or "").strip().lower())
        return dict(item) if item else None


def _env_oauth(app):
    found = str((getattr(app, "environ", None) or {}).get("IAP_OAUTH_CLIENT") or "").strip().lower()
    return found if found in ("managed", "custom") else "unknown"


def _oauth_client(app, res, quota_project):
    """custom when a custom OAuth client is set on the service or the project, managed when neither has one, else the
    deployment's finding (IAP_OAUTH_CLIENT) or unknown."""
    number = _RESOURCE_RE.match(res).group(1)
    unreadable = False
    for name in (res, "projects/%s" % number):
        try:
            payload, _ = app.gcp.request("GET", API + name + ":iapSettings", quota_project=quota_project, timeout=6)
        except GcpError as err:
            if err.kind != "not_found":
                unreadable = True
            continue
        oauth = ((payload or {}).get("accessSettings") or {}).get("oauthSettings") or {}
        if oauth.get("clientId"):
            return "custom"
    return _env_oauth(app) if unreadable else "managed"


def org_domain(app):
    """Primary domain of the project's organization as found by the deployment (IAP_ORG_DOMAIN), or ''."""
    value = str((getattr(app, "environ", None) or {}).get("IAP_ORG_DOMAIN") or "").strip().lower()
    return value if _DOMAIN_RE.match(value) else ""


def status(app, quota_project, refresh=False):
    """Who IAP admits for Google sign-in. {"enabled": False} when Google sign-in does not go through IAP. Results,
    failures included, are kept for a minute so the access page never waits on IAP twice."""
    res = resource(app)
    if not res:
        return {"enabled": False}
    now = time.monotonic()
    with _lock:
        hit = _cache.get(res)
        if hit and not refresh and now - hit[0] < STATUS_TTL:
            return dict(hit[1])
    number, region, service = _RESOURCE_RE.match(res).groups()
    out = {"enabled": True, "resource": res, "service": service, "region": region, "projectNumber": number,
           "members": [], "open": False, "domains": [], "users": [], "groups": [], "oauth": "unknown",
           "orgDomain": org_domain(app), "error": None, "checkedAt": now_iso()}
    try:
        members = _members(_get_policy(app, res, quota_project, timeout=8))
        out["members"] = sorted(members)
        out["open"] = bool(members & set(OPEN_MEMBERS))
        out["domains"] = sorted(m[len("domain:"):] for m in members if m.startswith("domain:"))
        out["users"] = sorted(m[len("user:"):] for m in members if m.startswith("user:"))
        out["groups"] = sorted(m[len("group:"):] for m in members if m.startswith("group:"))
    except GcpError as err:
        out["error"] = err.kind
        LOG.warning("IAP policy unreadable resource=%s reason=%s", res, err.kind)
    if out["error"] in ("timeout", "network", "unavailable", "credentials"):
        out["oauth"] = _env_oauth(app)  # IAP is unreachable: do not wait on two more calls
    else:
        out["oauth"] = _oauth_client(app, res, quota_project)
    with _lock:
        _cache[res] = (now, dict(out))
    return out


def coverage(state, entry):
    """covered / missing / unknown for an approved Google entry, None for ID accounts or when IAP is not used."""
    if not state or not state.get("enabled"):
        return None
    value = str(entry or "").strip().lower()
    if value != "*" and "@" not in value:
        return None
    if state.get("error"):
        return "unknown"
    if value == "*":
        return "covered" if state.get("open") else None
    return "covered" if covers(state.get("members"), value) else "missing"


def grant(app, quota_project, entries):
    """Admits approved Google entries at IAP: user:<email> for exact emails, domain:<domain> for *@domain patterns.
    Returns one result per entry: covered (already admitted), added, rejected, denied, invalid or failed."""
    res = resource(app)
    wanted, seen = [], set()
    for entry in entries or ():
        value = str(entry or "").strip().lower()
        principal = principal_for(value)
        if principal and principal not in seen:
            seen.add(principal)
            wanted.append((value, principal))
    if not res or not wanted:
        return []
    results = []
    try:
        policy = _get_policy(app, res, quota_project)
    except GcpError as err:
        results = [_result(e, p, classify(err), err) for e, p in wanted]
        _remember(results)
        return results
    members = _members(policy)
    todo = []
    for entry, principal in wanted:
        if covers(members, entry):
            results.append(_result(entry, principal, "covered"))
        else:
            todo.append((entry, principal))
    if todo:
        try:
            _set_policy(app, res, quota_project, _with_members(policy, [p for _, p in todo]))
            results.extend(_result(e, p, "added") for e, p in todo)
        except GcpError as err:
            if len(todo) == 1:
                results.append(_result(todo[0][0], todo[0][1], classify(err), err))
            else:
                for entry, principal in todo:
                    try:
                        fresh = _get_policy(app, res, quota_project)
                        if principal not in _members(fresh):
                            _set_policy(app, res, quota_project, _with_members(fresh, [principal]))
                        results.append(_result(entry, principal, "added"))
                    except GcpError as one:
                        results.append(_result(entry, principal, classify(one), one))
        _invalidate(res)
    for item in results:
        LOG.info("IAP sign-in access entry=%s result=%s", item["principal"].split(":", 1)[0], item["result"])
    _remember(results)
    return results


def revoke(app, quota_project, entries):
    """Removes the user: bindings of removed accounts. Domain and group bindings stay: they can be the organization
    bindings made at deployment, and the dashboard still refuses accounts that are not approved."""
    res = resource(app)
    remove = {principal_for(e) for e in entries or ()}
    remove = {p for p in remove if p and p.startswith("user:")}
    if not res or not remove:
        return []
    try:
        policy = _get_policy(app, res, quota_project)
        present = remove & _members(policy)
        if present:
            _set_policy(app, res, quota_project, _without_members(policy, present))
            _invalidate(res)
        return sorted(present)
    except GcpError as err:
        LOG.warning("IAP sign-in access not removed reason=%s", err.kind)
        return []
