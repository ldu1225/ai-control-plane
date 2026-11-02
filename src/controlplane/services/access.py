"""Account access for Google sign-in (administrators only): approved accounts, roles and pending
access requests.

Approved accounts are security.admins (role admin) and security.allowed_users (role viewer), each
an exact email or a *@domain pattern. Entries from config.yaml or the environment ("config") are
fixed; entries added here ("ui") are stored as settings overrides. Removing an account also revokes
its existing sessions.
"""

from __future__ import annotations

import logging
import re
import time

from .. import config
from ..auth import hash_password
from ..httpkit import ApiError, now_iso
from ..security import admin_guard, is_admin_email, uid_for
from ..settings_store import ACCOUNT_ID_RE

LOG = logging.getLogger("controlplane.access")

ROLES = ("admin", "viewer")


def _norm_member(value, allow_wildcard=False, allow_account_id=False):
    if not isinstance(value, str) or len(value) > 254:
        raise ApiError("bad_request", "Field 'email' must be an email address or a *@domain pattern.")
    member = value.strip().lower()
    if allow_wildcard and member in ("*", "*@*"):
        return "*"
    if config.EMAIL_RE.match(member) or config.ADMIN_PATTERN_RE.match(member):
        return member
    if allow_account_id and ACCOUNT_ID_RE.match(member):
        return member
    raise ApiError("bad_request", "Field 'email' must be an email address or a *@domain pattern.")


def _norm_members(body, allow_wildcard=False, allow_account_id=False, existing_accounts=()):
    """Accepts either a single email/pattern/ID, a comma/newline/whitespace-separated string in 'email',
    or a list in 'emails'. Returns a deduplicated list of validated members."""
    raw_list = body.get("emails")
    if isinstance(raw_list, list):
        tokens = [str(x).strip() for x in raw_list if str(x).strip()]
    else:
        raw = body.get("email")
        if not isinstance(raw, str) or not raw.strip():
            raise ApiError("bad_request", "Field 'email' must be an email address or a *@domain pattern.")
        tokens = [t.strip() for t in re.split(r"[,;\s]+", raw) if t.strip()]
    if not tokens or len(tokens) > 200:
        raise ApiError("bad_request", "Provide between 1 and 200 email addresses or *@domain patterns.")
    out = []
    seen = set()
    existing = set(existing_accounts or ())
    for token in tokens:
        t_low = token.lower()
        can_id = allow_account_id or t_low == "admin" or t_low in existing
        member = _norm_member(token, allow_wildcard=allow_wildcard, allow_account_id=can_id)
        if member not in seen:
            seen.add(member)
            out.append(member)
    return out


def _ui_lists(mgr):
    sec = mgr.overrides().get("security") or {}
    return list(sec.get("admins") or []), list(sec.get("allowed_users") or [])


def _approved(email, sec):
    return is_admin_email(email, sec.get("admins")) or is_admin_email(email, sec.get("allowed_users"))


def summary(ctx):
    mgr = ctx.app.settings
    effective, _ = mgr.snapshot()
    sec = effective["security"]
    base = mgr.base["security"]
    base_admins = list(base.get("admins") or [])
    base_allowed = list(base.get("allowed_users") or [])
    actor = ctx.identity.email
    state = mgr.auth_state()
    last = state.get("lastLogin") or {}
    accounts = state.get("accounts") or {}
    users = []
    seen = set()

    # 1. Built-in default administrator account ('admin')
    admin_meta = accounts.get("admin") or {}
    seen.add("admin")
    users.append({
        "email": "admin",
        "role": "admin",
        "source": "builtin",
        "pattern": False,
        "hasPassword": True,
        "defaultPassword": not bool(admin_meta.get("passwordHash")),
        "lastLogin": last.get("admin"),
        "self": actor == "admin",
        "canRemove": False,
        "canChangeRole": False,
    })

    def add(member, role, base_list):
        if member in seen:
            return
        seen.add(member)
        in_config = member in base_admins or member in base_allowed
        is_pattern = member == "*" or member.startswith("*@")
        acct_meta = accounts.get(member) or {}
        users.append({"email": member, "role": role, "source": "config" if member in base_list else "ui",
                      "pattern": is_pattern, "hasPassword": bool(acct_meta.get("passwordHash")),
                      "defaultPassword": False, "lastLogin": last.get(member),
                      "self": member == actor,
                      "canRemove": not in_config and member != actor,
                      "canChangeRole": member != actor and member not in base_admins and member != "*"})

    for member in sec.get("admins") or []:
        add(member, "admin", base_admins)
    for member in sec.get("allowed_users") or []:
        add(member, "viewer", base_allowed)

    # 2. Additional ID/Password accounts stored in auth["accounts"]
    for acct_id, meta in accounts.items():
        if acct_id in seen:
            continue
        seen.add(acct_id)
        role = "admin" if meta.get("role") == "admin" else "viewer"
        users.append({
            "email": acct_id,
            "role": role,
            "source": "ui",
            "pattern": False,
            "hasPassword": bool(meta.get("passwordHash")),
            "defaultPassword": False,
            "lastLogin": last.get(acct_id),
            "self": acct_id == actor,
            "canRemove": acct_id != actor,
            "canChangeRole": acct_id != actor,
        })

    users.sort(key=lambda u: (u["email"] != "admin", u["role"] != "admin", u["pattern"], u["email"]))
    requests = [r for r in reversed(state.get("requests") or []) if not _approved(r["email"], sec)]
    return {"authMode": ctx.identity.auth_mode, "users": users, "requests": requests,
            "sessionHours": sec.get("session_hours"), "self": actor}


_PUBLIC_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com",
    "naver.com", "daum.net", "hanmail.net", "kakao.com",
})
_RUN_APP_BRIDGE_RE = re.compile(r"^https://([a-z0-9-]+)-([0-9]+)\.([a-z0-9-]+)\.run\.app(?:[:/]|$)")


def _iap_bridge_resource(ctx):
    auth = getattr(ctx.app, "auth", None)
    bridge_url = getattr(auth, "bridge_url", "") if auth else ""
    if not bridge_url:
        return None
    explicit = (ctx.app.environ.get("SIGN_IN_BRIDGE_IAP_RESOURCE") or "").strip()
    if explicit:
        return explicit
    m = _RUN_APP_BRIDGE_RE.match(bridge_url.strip().lower())
    if m:
        service, project_num, region = m.group(1), m.group(2), m.group(3)
        return "projects/%s/iap_web/cloud_run-%s/services/%s" % (project_num, region, service)
    return None


def _principals_for_entries(entries):
    out = set()
    for raw in entries or ():
        token = str(raw or "").strip().lower()
        if not token or token == "*" or "@" not in token:
            continue
        domain = token.rpartition("@")[2]
        if token.startswith("*@"):
            if domain and domain not in _PUBLIC_EMAIL_DOMAINS:
                out.add("domain:" + domain)
        else:
            out.add("user:" + token)
            if domain and domain not in _PUBLIC_EMAIL_DOMAINS:
                out.add("domain:" + domain)
    return out


def _sync_iap_bridge(ctx, removed_member=None):
    """Best-effort sync of approved users/domains to the Cloud Run IAP sign-in bridge IAM policy."""
    resource = _iap_bridge_resource(ctx)
    if not resource or not getattr(ctx.app, "gcp", None):
        return
    try:
        effective, _ = ctx.app.settings.snapshot()
        quota_project = (effective.get("workspace") or {}).get("project_id") or ""
        sec = effective.get("security") or {}
        desired = _principals_for_entries(list(sec.get("admins") or []) + list(sec.get("allowed_users") or []))
        desired.add("allAuthenticatedUsers")
        removed = set()
        if removed_member:
            rm = str(removed_member).strip().lower()
            if rm.startswith("*@"):
                removed.add("domain:" + rm[2:])
            elif "@" in rm:
                removed.add("user:" + rm)
        base_url = "https://iap.googleapis.com/v1/" + resource
        policy, _ = ctx.app.gcp.request("POST", base_url + ":getIamPolicy", quota_project=quota_project,
                                        json_body={}, timeout=10)
        policy = policy or {}
        bindings = list(policy.get("bindings") or [])
        role = "roles/iap.httpsResourceAccessor"
        target = None
        for b in bindings:
            if b.get("role") == role:
                target = b
                break
        if target is None:
            target = {"role": role, "members": []}
            bindings.append(target)
        current = set(target.get("members") or [])
        updated = (current - removed) | (desired - removed)
        if updated == current:
            return
        # Try bulk update first; if a domain/user is rejected by IAM, add new members individually.
        target["members"] = sorted(updated)
        new_policy = {"version": policy.get("version", 1), "bindings": bindings}
        if policy.get("etag"):
            new_policy["etag"] = policy["etag"]
        try:
            ctx.app.gcp.request("POST", base_url + ":setIamPolicy", quota_project=quota_project,
                                json_body={"policy": new_policy}, timeout=10)
            return
        except Exception:  # pylint: disable=broad-except
            pass
        kept = current - removed
        for cand in sorted(desired - kept):
            trial = sorted(kept | {cand})
            target["members"] = trial
            try:
                pol, _ = ctx.app.gcp.request("POST", base_url + ":getIamPolicy", quota_project=quota_project,
                                             json_body={}, timeout=10)
                pol = pol or {}
                if pol.get("etag"):
                    new_policy["etag"] = pol["etag"]
                ctx.app.gcp.request("POST", base_url + ":setIamPolicy", quota_project=quota_project,
                                    json_body={"policy": new_policy}, timeout=10)
                kept.add(cand)
            except Exception as exc:  # pylint: disable=broad-except
                LOG.warning("iap bridge binding skipped member=%s reason=%s", cand, type(exc).__name__)
    except Exception as exc:  # pylint: disable=broad-except
        LOG.warning("iap bridge policy sync skipped reason=%s", type(exc).__name__)


def _save_lists(ctx, admins, allowed, removed_member=None):
    ctx.app.settings.update_section("security", {"admins": admins, "allowed_users": allowed}, ctx.identity.email,
                                    check=admin_guard(ctx.identity))
    ctx.app.on_settings_changed()
    _sync_iap_bridge(ctx, removed_member=removed_member)


def _drop_requests(ctx, members):
    wildcard = "*" in members
    exact = {m for m in members if m != "*" and not m.startswith("*@")}
    domains = {m[2:] for m in members if m.startswith("*@")}
    if not wildcard and not exact and not domains:
        return

    def apply(auth):
        if wildcard:
            auth["requests"] = []
            return
        kept = []
        for r in auth["requests"]:
            em = (r.get("email") or "").strip().lower()
            dom = em.rpartition("@")[2] if "@" in em else ""
            if em in exact or (dom and dom in domains):
                continue
            kept.append(r)
        auth["requests"] = kept
    ctx.app.settings.mutate_auth(apply, ctx.identity.email)


def save_user(ctx):
    """POST /api/v1/access/users {email|emails, role, password?}: approve accounts, create ID/Password accounts,
    change roles, or update passwords."""
    role = ctx.body.get("role")
    if role not in ROLES:
        raise ApiError("bad_request", "Field 'role' must be 'admin' or 'viewer'.")
    raw_password = ctx.body.get("password")
    has_password = isinstance(raw_password, str) and bool(raw_password.strip())
    pw_hash = hash_password(raw_password) if has_password else None
    existing_accounts = (ctx.app.settings.auth_state() or {}).get("accounts") or {}
    members = _norm_members(ctx.body, allow_wildcard=(role == "viewer" and not has_password),
                            allow_account_id=has_password, existing_accounts=existing_accounts.keys())
    base = ctx.app.settings.base["security"]
    base_admins = list(base.get("admins") or [])
    base_allowed = list(base.get("allowed_users") or [])
    for member in members:
        if member == "admin" and role != "admin":
            raise ApiError("conflict", "The built-in 'admin' account must remain an administrator.")
        if member == ctx.identity.email and role != "admin":
            raise ApiError("conflict", "You cannot remove your own administrator role.")
        if role == "viewer" and member in base_admins:
            raise ApiError("conflict", "This administrator is defined in the deployment configuration and cannot be "
                                       "changed here.")
        if has_password and (member == "*" or member.startswith("*@")):
            raise ApiError("bad_request", "Passwords can only be set on individual accounts, not domain wildcards.")

    email_members = [m for m in members if m == "*" or "@" in m]
    if email_members:
        admins, allowed = _ui_lists(ctx.app.settings)
        member_set = set(email_members)
        admins = [m for m in admins if m not in member_set]
        allowed = [m for m in allowed if m not in member_set]
        for member in email_members:
            if role == "admin" and member not in base_admins:
                admins.append(member)
            elif role == "viewer" and member not in base_allowed:
                allowed.append(member)
        _save_lists(ctx, admins, allowed)
        _drop_requests(ctx, email_members)

    id_or_pw_members = [m for m in members if has_password or "@" not in m or m in existing_accounts]
    if id_or_pw_members:
        when = now_iso()

        def apply_accounts(auth):
            accts = auth.setdefault("accounts", {})
            for member in id_or_pw_members:
                if member == "*" or member.startswith("*@"):
                    continue
                cur = dict(accts.get(member) or {})
                cur["role"] = "admin" if member == "admin" else role
                if pw_hash:
                    cur["passwordHash"] = pw_hash
                cur["updatedAt"] = when
                accts[member] = cur
        ctx.app.settings.mutate_auth(apply_accounts, ctx.identity.email)

    for member in members:
        LOG.info("access granted member=%s role=%s pw=%s", uid_for(member), role, bool(pw_hash))
    return summary(ctx)


def delete_user(ctx):
    """POST /api/v1/access/users/delete {email}: remove an account and revoke its sessions."""
    existing_accounts = (ctx.app.settings.auth_state() or {}).get("accounts") or {}
    raw_email = str(ctx.body.get("email") or "").strip().lower()
    if raw_email == "admin":
        raise ApiError("conflict", "The built-in 'admin' account cannot be deleted. You can change its password instead.")
    member = _norm_member(ctx.body.get("email"), allow_wildcard=True,
                          allow_account_id=(raw_email in existing_accounts))
    if member == ctx.identity.email:
        raise ApiError("conflict", "You cannot remove your own access.")
    base = ctx.app.settings.base["security"]
    if member in (base.get("admins") or []) or member in (base.get("allowed_users") or []):
        raise ApiError("conflict", "This account is defined in the deployment configuration (config.yaml or "
                                   "environment) and cannot be removed here.")
    admins, allowed = _ui_lists(ctx.app.settings)
    in_lists = member in admins or member in allowed
    in_accounts = member in existing_accounts
    if not in_lists and not in_accounts:
        raise ApiError("not_found", "Account not found.")
    if in_lists:
        _save_lists(ctx, [m for m in admins if m != member], [m for m in allowed if m != member],
                    removed_member=member)
    if member != "*" and not member.startswith("*@"):
        epoch = int(time.time() * 1000)

        def apply(auth):
            auth.get("accounts", {}).pop(member, None)
            auth["revoked"][member] = epoch
        ctx.app.settings.mutate_auth(apply, ctx.identity.email)
    LOG.info("access removed member=%s", uid_for(member))
    return summary(ctx)


def dismiss_request(ctx):
    """POST /api/v1/access/requests/dismiss {email}."""
    member = _norm_member(ctx.body.get("email"))
    _drop_requests(ctx, [member])
    return summary(ctx)
