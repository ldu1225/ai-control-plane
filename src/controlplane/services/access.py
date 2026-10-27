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
from ..httpkit import ApiError
from ..security import admin_guard, is_admin_email, uid_for

LOG = logging.getLogger("controlplane.access")

ROLES = ("admin", "viewer")


def _norm_member(value, allow_wildcard=False):
    if not isinstance(value, str) or len(value) > 254:
        raise ApiError("bad_request", "Field 'email' must be an email address or a *@domain pattern.")
    member = value.strip().lower()
    if allow_wildcard and member in ("*", "*@*"):
        return "*"
    if not (config.EMAIL_RE.match(member) or config.ADMIN_PATTERN_RE.match(member)):
        raise ApiError("bad_request", "Field 'email' must be an email address or a *@domain pattern.")
    return member


def _norm_members(body, allow_wildcard=False):
    """Accepts either a single email/pattern, a comma/newline/whitespace-separated string in 'email',
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
    for token in tokens:
        member = _norm_member(token, allow_wildcard=allow_wildcard)
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
    users = []
    seen = set()

    def add(member, role, base_list):
        if member in seen:
            return
        seen.add(member)
        in_config = member in base_admins or member in base_allowed
        is_pattern = member == "*" or member.startswith("*@")
        users.append({"email": member, "role": role, "source": "config" if member in base_list else "ui",
                      "pattern": is_pattern, "lastLogin": last.get(member),
                      "self": member == actor,
                      "canRemove": not in_config and member != actor,
                      "canChangeRole": member != actor and member not in base_admins and member != "*"})

    for member in sec.get("admins") or []:
        add(member, "admin", base_admins)
    for member in sec.get("allowed_users") or []:
        add(member, "viewer", base_allowed)
    users.sort(key=lambda u: (u["role"] != "admin", u["pattern"], u["email"]))
    requests = [r for r in reversed(state.get("requests") or []) if not _approved(r["email"], sec)]
    return {"authMode": ctx.identity.auth_mode, "users": users, "requests": requests,
            "sessionHours": sec.get("session_hours"), "self": actor}


def _save_lists(ctx, admins, allowed):
    ctx.app.settings.update_section("security", {"admins": admins, "allowed_users": allowed}, ctx.identity.email,
                                    check=admin_guard(ctx.identity))
    ctx.app.on_settings_changed()


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
    """POST /api/v1/access/users {email|emails, role}: approve one or more accounts or change their role."""
    role = ctx.body.get("role")
    if role not in ROLES:
        raise ApiError("bad_request", "Field 'role' must be 'admin' or 'viewer'.")
    members = _norm_members(ctx.body, allow_wildcard=(role == "viewer"))
    base = ctx.app.settings.base["security"]
    base_admins = list(base.get("admins") or [])
    base_allowed = list(base.get("allowed_users") or [])
    for member in members:
        if member == ctx.identity.email and role != "admin":
            raise ApiError("conflict", "You cannot remove your own administrator role.")
        if role == "viewer" and member in base_admins:
            raise ApiError("conflict", "This administrator is defined in the deployment configuration and cannot be "
                                       "changed here.")
    admins, allowed = _ui_lists(ctx.app.settings)
    member_set = set(members)
    admins = [m for m in admins if m not in member_set]
    allowed = [m for m in allowed if m not in member_set]
    for member in members:
        if role == "admin" and member not in base_admins:
            admins.append(member)
        elif role == "viewer" and member not in base_allowed:
            allowed.append(member)
    _save_lists(ctx, admins, allowed)
    _drop_requests(ctx, members)
    for member in members:
        LOG.info("access granted member=%s role=%s", uid_for(member), role)
    return summary(ctx)


def delete_user(ctx):
    """POST /api/v1/access/users/delete {email}: remove an account and revoke its sessions."""
    member = _norm_member(ctx.body.get("email"), allow_wildcard=True)
    if member == ctx.identity.email:
        raise ApiError("conflict", "You cannot remove your own access.")
    base = ctx.app.settings.base["security"]
    if member in (base.get("admins") or []) or member in (base.get("allowed_users") or []):
        raise ApiError("conflict", "This account is defined in the deployment configuration (config.yaml or "
                                   "environment) and cannot be removed here.")
    admins, allowed = _ui_lists(ctx.app.settings)
    if member not in admins and member not in allowed:
        raise ApiError("not_found", "Account not found.")
    _save_lists(ctx, [m for m in admins if m != member], [m for m in allowed if m != member])
    if member != "*" and not member.startswith("*@"):
        epoch = int(time.time() * 1000)

        def apply(auth):
            auth["revoked"][member] = epoch
        ctx.app.settings.mutate_auth(apply, ctx.identity.email)
    LOG.info("access removed member=%s", uid_for(member))
    return summary(ctx)


def dismiss_request(ctx):
    """POST /api/v1/access/requests/dismiss {email}."""
    member = _norm_member(ctx.body.get("email"))
    _drop_requests(ctx, [member])
    return summary(ctx)
