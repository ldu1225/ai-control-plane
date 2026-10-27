"""Account access for Google sign-in (administrators only): approved accounts, roles and pending
access requests.

Approved accounts are security.admins (role admin) and security.allowed_users (role viewer), each
an exact email or a *@domain pattern. Entries from config.yaml or the environment ("config") are
fixed; entries added here ("ui") are stored as settings overrides. Removing an account also revokes
its existing sessions.
"""

from __future__ import annotations

import logging
import time

from .. import config
from ..httpkit import ApiError
from ..security import admin_guard, is_admin_email, uid_for

LOG = logging.getLogger("controlplane.access")

ROLES = ("admin", "viewer")


def _norm_member(value):
    if not isinstance(value, str) or len(value) > 254:
        raise ApiError("bad_request", "Field 'email' must be an email address or a *@domain pattern.")
    member = value.strip().lower()
    if not (config.EMAIL_RE.match(member) or config.ADMIN_PATTERN_RE.match(member)):
        raise ApiError("bad_request", "Field 'email' must be an email address or a *@domain pattern.")
    return member


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
        users.append({"email": member, "role": role, "source": "config" if member in base_list else "ui",
                      "pattern": member.startswith("*@"), "lastLogin": last.get(member),
                      "self": member == actor,
                      "canRemove": not in_config and member != actor,
                      "canChangeRole": member != actor and member not in base_admins})

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


def _drop_request(ctx, member):
    def apply(auth):
        auth["requests"] = [r for r in auth["requests"] if r.get("email") != member]
    ctx.app.settings.mutate_auth(apply, ctx.identity.email)


def save_user(ctx):
    """POST /api/v1/access/users {email, role}: approve an account or change its role."""
    member = _norm_member(ctx.body.get("email"))
    role = ctx.body.get("role")
    if role not in ROLES:
        raise ApiError("bad_request", "Field 'role' must be 'admin' or 'viewer'.")
    base = ctx.app.settings.base["security"]
    if member == ctx.identity.email and role != "admin":
        raise ApiError("conflict", "You cannot remove your own administrator role.")
    if role == "viewer" and member in (base.get("admins") or []):
        raise ApiError("conflict", "This administrator is defined in the deployment configuration and cannot be "
                                   "changed here.")
    admins, allowed = _ui_lists(ctx.app.settings)
    admins = [m for m in admins if m != member]
    allowed = [m for m in allowed if m != member]
    if role == "admin" and member not in (base.get("admins") or []):
        admins.append(member)
    if role == "viewer" and member not in (base.get("allowed_users") or []):
        allowed.append(member)
    _save_lists(ctx, admins, allowed)
    if not member.startswith("*@"):
        _drop_request(ctx, member)
    LOG.info("access granted member=%s role=%s", uid_for(member), role)
    return summary(ctx)


def delete_user(ctx):
    """POST /api/v1/access/users/delete {email}: remove an account and revoke its sessions."""
    member = _norm_member(ctx.body.get("email"))
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
    if not member.startswith("*@"):
        epoch = int(time.time() * 1000)

        def apply(auth):
            auth["revoked"][member] = epoch
        ctx.app.settings.mutate_auth(apply, ctx.identity.email)
    LOG.info("access removed member=%s", uid_for(member))
    return summary(ctx)


def dismiss_request(ctx):
    """POST /api/v1/access/requests/dismiss {email}."""
    member = _norm_member(ctx.body.get("email"))
    _drop_request(ctx, member)
    return summary(ctx)
