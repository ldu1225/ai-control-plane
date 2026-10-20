"""Antigravity governance: usage, IAM 6-state classification, safety gates and caps.

Safety rules (from agy-admin):
- Only direct, unconditional `user:` bindings are ever modified.
- NOT_ENFORCEABLE and UNKNOWN users are never modified (no override).
- All changes of one cycle are written in a single atomic read-modify-write guarded by the
  policy etag; a concurrent modification (409/ABORTED) triggers one retry with a fresh plan.
- Gates G1/G2/G4 abort the cycle; G3 withholds restores; G5 only warns.

Per-user caps over a settlement period:
- A user is over the cap when tokens, requests or the estimated cost (tokens x configured unit
  prices) reach the cap within the period (daily / weekly / monthly calendar periods reset at
  local midnight in governance.antigravity.reset_timezone).
- enforcement_mode=auto: the scheduled enforcement run suspends over-cap users until the period
  resets. enforcement_mode=manual: over-cap users wait in a review queue; an administrator
  suspends them or allows them for the rest of the period.
- Exception rules per user (both modes): allow (never suspended), block (always disallowed) and
  limit (individual caps). Suspensions end at the period reset even without telemetry, because
  the usage window itself restarts.
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
import math
import threading
import urllib.parse
from dataclasses import dataclass, field

from .. import config, timeutil
from ..gcp import GcpError
from ..httpkit import ApiError, now_iso
from ..schema import expr, quote_ident
from ..security import is_human, uid_for
from ..settings_store import EMPTY_GOVERNANCE
from .base import AGY_SINK, iso, parse_rfc3339, to_int
from .pricing import price_table

LOG = logging.getLogger("controlplane.governance")

CRM_V3 = "https://cloudresourcemanager.googleapis.com/v3"
CRM_V1 = "https://cloudresourcemanager.googleapis.com/v1"
IAM_V1 = "https://iam.googleapis.com/v1"
LOGGING_V2 = "https://logging.googleapis.com/v2"

EA = "ENFORCEABLE_ALLOWED"
CAPPED = "CAPPED_BY_TOOL"
BLOCKED = "BLOCKED_BY_ADMIN"
NE = "NOT_ENFORCEABLE"
NO_ACCESS = "NO_ACCESS"
UNKNOWN = "UNKNOWN"
STATES = (EA, CAPPED, BLOCKED, NE, NO_ACCESS, UNKNOWN)

REMEDIATION = {
    "PRIVILEGED_ROLE": ("The user holds a privileged role (for example owner, editor or Discovery Engine admin) "
                        "that grants access independently of individual bindings. Remove or reduce the "
                        "privileged role, then re-evaluate the user."),
    "OTHER_ACCESS_ROLE": ("Access is granted through an additional access role configured in the governance "
                          "settings. Remove that role binding before using per-user controls."),
    "GROUP_GRANT_PRESENT": ("Access is granted through a Google Group or domain binding on the access role. Remove "
                            "the user from the group, or replace the group grant with individual user bindings."),
    "INHERITED_BINDING": ("Access is inherited from a folder or organization IAM policy. Remove the inherited "
                          "binding at the ancestor level."),
    "CONFLICTING_BINDINGS": ("The user has a conditional binding, or holds both the access role and a marker role. "
                             "Resolve the bindings manually so that exactly one unconditional binding remains."),
    "POLICY_UNREADABLE": ("The project IAM policy could not be read. Grant resourcemanager.projects.getIamPolicy "
                          "to the service identity and retry."),
    "ANCESTOR_UNCHECKED": ("Folder or organization IAM policies could not be read, so inherited access cannot be "
                           "ruled out. Grant read access to the ancestor policies or review them manually."),
}
NON_USER_PREFIXES = ("group:", "domain:", "principalset:", "principal:")
NON_USER_EXACT = ("allusers", "allauthenticatedusers")
REQUIRED_PERMISSIONS = ("resourcemanager.projects.getIamPolicy", "resourcemanager.projects.setIamPolicy",
                        "logging.logEntries.create", "iam.roles.create")
WEBHOOK_HOSTS = ("hooks.slack.com", "chat.googleapis.com")
_WRITE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Configuration and policy index (pure)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GovConfig:
    project: str
    access_role: str
    cap_marker: str
    admin_marker: str
    legacy_markers: tuple
    privileged_roles: tuple
    extra_access_roles: tuple
    cap_marker_id: str = ""
    admin_marker_id: str = ""
    change_budget_min: int = 10
    change_budget_ratio: float = 0.2

    @property
    def markers(self):
        return {self.cap_marker, self.admin_marker, *self.legacy_markers}


def marker_name(project, role_id):
    return "projects/%s/roles/%s" % (project, role_id)


def gov_config(settings):
    ag = settings["governance"]["antigravity"]
    project = settings["workspace"]["project_id"]
    return GovConfig(
        project=project, access_role=ag["access_role"],
        cap_marker=marker_name(project, ag["cap_marker_role"]),
        admin_marker=marker_name(project, ag["admin_marker_role"]),
        legacy_markers=tuple(marker_name(project, r) for r in ag.get("legacy_marker_roles") or []),
        privileged_roles=tuple(ag.get("privileged_roles") or []),
        extra_access_roles=tuple(ag.get("extra_access_roles") or []),
        cap_marker_id=ag["cap_marker_role"], admin_marker_id=ag["admin_marker_role"],
        change_budget_min=int(ag.get("change_budget_min", 10)),
        change_budget_ratio=float(ag.get("change_budget_ratio", 0.2)))


@dataclass(frozen=True)
class Pricing:
    """Prices per million tokens used to estimate per-user cost (billing exports have no user
    dimension, so per-user cost is estimated from each user's tokens per model).

    source "auto": prices from services.pricing (what the billing account paid per token, then the
    public Google price list) in the cap currency. source "manual": the entered default and model
    prices. Entered model prices override automatic prices in both modes."""
    currency: str = "USD"
    input_per_million: float = 0.0
    output_per_million: float = 0.0
    models: tuple = ()  # ((model id or prefix, input, output), ...), longest first
    source: str = "manual"
    table: object = None  # services.pricing.PriceTable when source == "auto"

    @property
    def has_default(self):
        return self.source == "manual" and (self.input_per_million > 0 or self.output_per_million > 0)

    @property
    def configured(self):
        if any(pin > 0 or pout > 0 for _, pin, pout in self.models):
            return True
        if self.source == "auto":
            return bool(self.table is not None and self.table.prices)
        return self.has_default

    def price(self, model):
        """(input, output, priced) prices per million tokens for one model id. priced is False when
        a direction has no price (that part adds nothing to the estimate)."""
        key = str(model or "").lower()
        for prefix, pin, pout in self.models:
            if key and (key == prefix or key.startswith(prefix)):
                return pin, pout, True
        if self.source == "auto":
            hit = self.table.lookup(model) if self.table is not None and model else None
            if not hit:
                return 0.0, 0.0, False
            pin, pout = hit.get("input"), hit.get("output")
            return pin or 0.0, pout or 0.0, bool(pin and pout)
        return self.input_per_million, self.output_per_million, self.has_default

    def lookup(self, model):
        """Public price entry for one model id, or None when unpriced."""
        pin, pout, priced = self.price(model)
        if not (pin or pout):
            return None
        key = str(model or "").lower()
        source = "manual"
        if self.source == "auto" and not any(key and (key == p or key.startswith(p)) for p, _, _ in self.models):
            source = (self.table.lookup(model) or {}).get("source", "catalog")
        return {"input": round(pin, 6), "output": round(pout, 6), "source": source, "complete": priced}

    def public(self, used_models=()):
        out = {"configured": self.configured, "currency": self.currency, "source": self.source,
               "inputPerMillion": self.input_per_million, "outputPerMillion": self.output_per_million,
               "models": [{"model": m, "input": pin, "output": pout} for m, pin, pout in sorted(self.models)],
               "fx": None, "counts": None, "used": []}
        if self.source == "auto" and self.table is not None:
            out["fx"] = self.table.fx
            out["counts"] = dict(self.table.counts)
        for model in sorted({str(m) for m in used_models if m and m != "unknown"}):
            out["used"].append({"model": model, "price": self.lookup(model)})
        return out


def _manual_models(ag):
    models = []
    for item in ag.get("model_prices") or []:
        if isinstance(item, dict) and item.get("model"):
            models.append((str(item["model"]).lower(), float(item.get("input") or 0), float(item.get("output") or 0)))
    models.sort(key=lambda m: (-len(m[0]), m[0]))
    return tuple(models)


def pricing_from(ag, table=None):
    """Pricing for the saved policy. table is the automatic price table (services.pricing) or None."""
    source = ag.get("pricing_source") or "auto"
    currency = ag.get("currency") or ""
    if source == "auto" and table is not None:
        currency = table.currency
    return Pricing(currency=currency or "USD",
                   input_per_million=float(ag.get("input_price_per_million") or 0),
                   output_per_million=float(ag.get("output_price_per_million") or 0), models=_manual_models(ag),
                   source=source, table=table if source == "auto" else None)


def estimate_cost(rec, pricing):
    """(cost, partial) of one user's usage in the pricing currency: prompt tokens at the input
    price and every other token (candidates and thinking) at the output price, per model.
    cost is None when no price is available (or none of the user's tokens could be priced);
    partial is True when some tokens belong to a model without a price (those tokens add nothing)."""
    if not pricing.configured:
        return None, False
    parts = list((rec.get("byModel") or {}).items()) or [(None, rec)]
    total = 0.0
    partial = False
    priced_tokens = 0
    used_tokens = 0
    for model, agg in parts:
        tokens = to_int(agg.get("tokens"))
        prompt = max(0, to_int(agg.get("promptTokens")))
        output = max(tokens - prompt, to_int(agg.get("candidateTokens")), 0)
        pin, pout, priced = pricing.price(model)
        used_tokens += tokens
        if pin or pout:
            priced_tokens += tokens
        if not priced and tokens > 0:
            partial = True
        total += prompt * pin / 1e6 + output * pout / 1e6
    if used_tokens > 0 and priced_tokens == 0:
        return None, True
    return round(total, 6), partial


class PolicyIndex:
    """Read-only view over an IAM policy (or an unreadable placeholder)."""

    def __init__(self, policy, readable=True):
        self.readable = readable and isinstance(policy, dict)
        self.policy = policy if isinstance(policy, dict) else {}
        self.bindings = [b for b in self.policy.get("bindings") or [] if isinstance(b, dict)]

    def user_bindings(self, email):
        member = "user:%s" % (email or "").lower()
        out = []
        for binding in self.bindings:
            members = [str(m).lower() for m in binding.get("members") or []]
            if member in members:
                out.append((binding.get("role"), bool(binding.get("condition"))))
        return out

    def non_user_members(self, role):
        out = []
        for binding in self.bindings:
            if binding.get("role") != role:
                continue
            for m in binding.get("members") or []:
                low = str(m).lower()
                if low.startswith(NON_USER_PREFIXES) or low in NON_USER_EXACT:
                    out.append(low)
        return out

    def direct_users(self, roles):
        users = set()
        for binding in self.bindings:
            if binding.get("role") not in roles:
                continue
            for m in binding.get("members") or []:
                low = str(m).lower()
                if low.startswith("user:"):
                    users.add(low[5:])
        return users


def grants_via_non_user(members, email):
    domain = (email or "").rpartition("@")[2].lower()
    for m in members:
        if m.startswith(("group:", "principalset:")) or m in NON_USER_EXACT:
            return True
        if m == "domain:%s" % domain:
            return True
    return False


def classify(email, has_usage, idx, cfg, ancestor_check=None):
    """Classifies one user into (state, reason) following the agy-admin rules."""
    if not idx.readable:
        return UNKNOWN, "POLICY_UNREADABLE"
    direct = idx.user_bindings(email)
    roles = {role for role, _ in direct}
    if roles & set(cfg.privileged_roles):
        return NE, "PRIVILEGED_ROLE"
    if roles & set(cfg.extra_access_roles):
        return NE, "OTHER_ACCESS_ROLE"
    markers = cfg.markers
    has_access = cfg.access_role in roles
    has_marker = bool(roles & markers)
    conditional = any(cond for role, cond in direct if role == cfg.access_role or role in markers)
    if (has_access and has_marker) or conditional:
        return NE, "CONFLICTING_BINDINGS"
    if cfg.admin_marker in roles or roles & set(cfg.legacy_markers):
        return BLOCKED, None
    if cfg.cap_marker in roles:
        return CAPPED, None
    if has_access:
        return EA, None
    if has_usage:
        if grants_via_non_user(idx.non_user_members(cfg.access_role), email):
            return NE, "GROUP_GRANT_PRESENT"
        result = ancestor_check(email) if ancestor_check is not None else None
        if result is True:
            return NE, "INHERITED_BINDING"
        if result is None:
            return UNKNOWN, "ANCESTOR_UNCHECKED"
        return NO_ACCESS, None
    return NO_ACCESS, None


def cap_check(tokens, requests, token_cap, request_cap, cost=None, cost_cap=0.0):
    """(over, usage_ratio, reasons) of one user's period usage. Caps of 0 are disabled; the cost
    cap is evaluated only when an estimated cost is available. usage_ratio is None when the cost
    cap is the only enabled cap and no cost could be estimated (unknown, not 0)."""
    reasons = []
    ratios = [0.0]
    if token_cap > 0:
        ratios.append(tokens / token_cap)
        if tokens >= token_cap:
            reasons.append("tokens")
    if request_cap > 0:
        ratios.append(requests / request_cap)
        if requests >= request_cap:
            reasons.append("requests")
    if cost_cap and cost_cap > 0:
        if cost is not None:
            ratios.append(cost / cost_cap)
            if cost >= cost_cap:
                reasons.append("cost")
        elif token_cap <= 0 and request_cap <= 0:
            return False, None, []
    return bool(reasons), round(max(ratios), 4), reasons


def over_cap(tokens, requests, token_cap, request_cap):
    over, usage, _ = cap_check(tokens, requests, token_cap, request_cap)
    return over, usage


def suspension_ended(suspension, now):
    """True when a suspension recorded by the control plane has reached its period end."""
    until = parse_rfc3339((suspension or {}).get("until"))
    return until is not None and now >= until


def evaluate(users, policy_readable, telemetry_ok, telemetry_records, non_user_on_access, cfg, access_role,
             mode="auto", now=None):
    """Computes gates and the plan. `users` items need uid, state and overCap; optional
    `exception` ({"type": allow|block|limit}) and `suspension` ({"until": iso}) refine the plan:
    - block exception: users with access (EA or CAPPED) are disallowed in every cycle.
    - allow exception: never capped; CAPPED users are restored (an explicit administrator decision,
      so not gated by G3).
    - a suspension whose period has ended is restored regardless of telemetry (the usage window
      restarted), unless usage is known to be over the cap again.
    - mode "manual": over-cap users go to the review queue instead of the block list."""
    now = now or dt.datetime.now(dt.timezone.utc)
    population = len(users)
    determinate = sum(1 for u in users if u["state"] != UNKNOWN)
    gates = []
    gates.append({"id": "G1_POLICY_READABLE", "status": "pass" if policy_readable else "fail",
                  "detail": "Project IAM policy read (version 3)." if policy_readable
                  else "The project IAM policy could not be read."})
    g2_ok = population == 0 or determinate > 0
    gates.append({"id": "G2_ANY_DETERMINATE", "status": "pass" if g2_ok else "fail",
                  "detail": "%d of %d users have a determinate state." % (determinate, population) if population
                  else "No users are in scope."})
    g3_ok = bool(telemetry_ok) and telemetry_records > 0
    gates.append({"id": "G3_TELEMETRY_HEALTHY", "status": "pass" if g3_ok else "warn",
                  "detail": ("%d inference log records in the window." % telemetry_records) if g3_ok else
                  ("The usage query failed; automatic restores are withheld." if not telemetry_ok else
                   "No inference log records in the window; automatic restores are withheld.")})
    block, review, restore, disallow, manual = [], [], [], [], []
    withheld = False
    for u in users:
        uid, state, over = u["uid"], u["state"], u["overCap"]
        kind = (u.get("exception") or {}).get("type")
        if state in (NE, UNKNOWN):
            if over or kind == "block":
                manual.append(uid)
            continue
        if kind == "block":
            if state in (EA, CAPPED):
                disallow.append(uid)
            continue
        if kind == "allow":
            if state == CAPPED:
                restore.append(uid)
            continue
        if state == EA and over:
            (review if mode == "manual" else block).append(uid)
        elif state == CAPPED and over is not True:
            # Restores require known (non-null) usage below the caps and healthy telemetry (G3),
            # or a suspension whose settlement period has ended.
            if suspension_ended(u.get("suspension"), now) or (over is False and g3_ok):
                restore.append(uid)
            elif not g3_ok:
                withheld = True
    budget = max(cfg.change_budget_min, int(math.ceil(cfg.change_budget_ratio * population)))
    planned = len(block) + len(restore) + len(disallow)
    g4_ok = planned <= budget
    gates.append({"id": "G4_CHANGE_BUDGET", "status": "pass" if g4_ok else "fail",
                  "detail": "%d planned changes; budget is %d." % (planned, budget)})
    gates.append({"id": "G5_NON_USER_GRANT_ABSENT", "status": "pass" if not non_user_on_access else "warn",
                  "detail": "No group or domain grants on %s." % access_role if not non_user_on_access else
                  "%d group or domain grant(s) on %s; affected users are not enforceable."
                  % (len(non_user_on_access), access_role)})
    failed = [g for g in gates if g["status"] == "fail"]
    plan = {"block": block, "restore": restore, "disallow": disallow, "review": review, "manual": manual,
            "withheldRestores": withheld, "aborted": bool(failed),
            "abortReason": "; ".join("%s: %s" % (g["id"], g["detail"]) for g in failed) if failed else None}
    return gates, plan, budget


# ---------------------------------------------------------------------------
# Policy mutation (pure)
# ---------------------------------------------------------------------------

def _remove_member(policy, role, member):
    keep = []
    for binding in policy.get("bindings") or []:
        if binding.get("role") == role and not binding.get("condition"):
            members = [m for m in binding.get("members") or [] if str(m).lower() != member]
            if not members:
                continue
            binding = dict(binding, members=members)
        keep.append(binding)
    policy["bindings"] = keep


def _add_member(policy, role, member):
    for binding in policy.get("bindings") or []:
        if binding.get("role") == role and not binding.get("condition"):
            if member not in [str(m).lower() for m in binding.get("members") or []]:
                binding["members"] = list(binding.get("members") or []) + [member]
            return
    policy.setdefault("bindings", []).append({"role": role, "members": [member]})


def apply_changes(policy, changes, cfg):
    """Returns a new policy with the (email, action) changes applied. Only unconditional
    bindings are touched; every other binding (including conditional ones) is preserved."""
    new = copy.deepcopy(policy)
    new["bindings"] = [dict(b) for b in new.get("bindings") or []]
    for email, action in changes:
        member = "user:%s" % email.lower()
        if action == "block":
            _remove_member(new, cfg.access_role, member)
            _add_member(new, cfg.cap_marker, member)
        elif action == "restore":
            _remove_member(new, cfg.cap_marker, member)
            _add_member(new, cfg.access_role, member)
        elif action == "disallow":
            _remove_member(new, cfg.access_role, member)
            _remove_member(new, cfg.cap_marker, member)
            _add_member(new, cfg.admin_marker, member)
        elif action == "allow":
            for role in (cfg.cap_marker, cfg.admin_marker) + tuple(cfg.legacy_markers):
                _remove_member(new, role, member)
            _add_member(new, cfg.access_role, member)
        else:
            raise ValueError("unknown action")
    new["version"] = 3
    return new


ALLOWED_TRANSITIONS = {"block": {EA}, "restore": {CAPPED}, "disallow": {EA, CAPPED},
                       "allow": {BLOCKED, CAPPED, NO_ACCESS}}
TARGET_STATE = {"block": CAPPED, "restore": EA, "disallow": BLOCKED, "allow": EA}


def check_transition(state, action):
    """Raises ValueError unless the action is allowed from the state. Never for NE/UNKNOWN."""
    if state in (NE, UNKNOWN):
        raise ValueError("not enforceable")
    if state not in ALLOWED_TRANSITIONS.get(action, set()):
        raise ValueError("transition not allowed")


# ---------------------------------------------------------------------------
# Upstream calls
# ---------------------------------------------------------------------------

def get_policy(ctx, project=None):
    project = project or ctx.project
    url = "%s/projects/%s:getIamPolicy" % (CRM_V3, project)
    return ctx.rest("POST", url, json_body={"options": {"requestedPolicyVersion": 3}})


def set_policy(ctx, policy):
    url = "%s/projects/%s:setIamPolicy" % (CRM_V3, ctx.project)
    return ctx.rest("POST", url, json_body={"policy": policy, "updateMask": "bindings,etag"})


def make_ancestor_check(ctx, cfg):
    cache = ctx.app.cache
    key = ("ancestors", ctx.project)

    def load():
        policies = []
        unreadable = False
        try:
            data = ctx.rest("POST", "%s/projects/%s:getAncestry" % (CRM_V1, ctx.project), json_body={})
        except GcpError:
            return {"policies": [], "unreadable": True}
        for anc in data.get("ancestor") or []:
            rid = (anc or {}).get("resourceId") or {}
            rtype, rid_value = rid.get("type"), str(rid.get("id") or "")
            if rtype not in ("folder", "organization") or not rid_value.isdigit():
                continue
            kind = "folders" if rtype == "folder" else "organizations"
            try:
                pol = ctx.rest("POST", "%s/%s/%s:getIamPolicy" % (CRM_V3, kind, rid_value),
                               json_body={"options": {"requestedPolicyVersion": 3}})
                policies.append(pol)
            except GcpError:
                unreadable = True
        return {"policies": policies, "unreadable": unreadable}

    state = {"value": None}

    def check(email):
        if state["value"] is None:
            state["value"], _ = cache.get_or_compute(key, 600, load)
        value = state["value"]
        roles = {cfg.access_role, *cfg.privileged_roles, *cfg.extra_access_roles}
        member = "user:%s" % email.lower()
        for pol in value["policies"]:
            for binding in pol.get("bindings") or []:
                if binding.get("role") not in roles:
                    continue
                members = [str(m).lower() for m in binding.get("members") or []]
                if member in members or grants_via_non_user(members, email):
                    return True
        return None if value["unreadable"] else False
    return check


def marker_status(ctx, cfg):
    out = []
    for role_id in (cfg.cap_marker_id, cfg.admin_marker_id):
        url = "%s/projects/%s/roles/%s" % (IAM_V1, ctx.project, urllib.parse.quote(role_id, safe=""))
        try:
            data = ctx.rest("GET", url)
            out.append({"role": role_id, "exists": not bool(data.get("deleted"))})
        except GcpError as err:
            out.append({"role": role_id, "exists": False if err.kind == "not_found" else None})
    return out


def permission_status(ctx):
    url = "%s/projects/%s:testIamPermissions" % (CRM_V3, ctx.project)
    try:
        data = ctx.rest("POST", url, json_body={"permissions": list(REQUIRED_PERMISSIONS)})
    except GcpError:
        return {p: None for p in REQUIRED_PERMISSIONS}
    granted = set(data.get("permissions") or [])
    return {p: p in granted for p in REQUIRED_PERMISSIONS}


# ---------------------------------------------------------------------------
# Usage (BigQuery sink or Cloud Logging)
# ---------------------------------------------------------------------------

def agy_fields(src, alias="t"):
    s = src.schema
    return {
        "user": expr(alias, s, "labels.user_id", cast="STRING"),
        "client": expr(alias, s, "labels.client_name", cast="STRING"),
        "model": expr(alias, s, "labels.model_id", "labels.model", cast="STRING"),
        "total": expr(alias, s, "jsonPayload.metadata.totalTokenCount", cast="INT64"),
        "prompt": expr(alias, s, "jsonPayload.metadata.promptTokenCount", cast="INT64"),
        "candidates": expr(alias, s, "jsonPayload.metadata.candidatesTokenCount", cast="INT64"),
        "ts": "%s.%s" % (alias, quote_ident(src.time_column())),
    }


class Usage:
    def __init__(self, source):
        self.source = source
        self.ok = False
        self.records = 0
        self.truncated = False
        self.users = {}
        self.by_model = {}
        self.by_client = {}
        self.daily = {}

    def user(self, email):
        return self.users.setdefault(email, {"requests": 0, "tokens": 0, "promptTokens": 0, "candidateTokens": 0,
                                             "clients": set(), "models": set(), "last": None, "byModel": {}})

    @staticmethod
    def add_model(rec, model, requests, tokens, prompt, cand):
        agg = rec["byModel"].setdefault(model or "unknown", {"requests": 0, "tokens": 0, "promptTokens": 0,
                                                             "candidateTokens": 0})
        agg["requests"] += requests
        agg["tokens"] += tokens
        agg["promptTokens"] += prompt
        agg["candidateTokens"] += cand


def _bq_usage(ctx, src, start, end, email=None, rng=None):
    f = agy_fields(src)
    if not f["user"]:
        raise GcpError("invalid", message="sink table lacks labels.user_id")
    null = "CAST(NULL AS STRING)"
    where = "%s IS NOT NULL" % f["user"]
    params = {"w_start": start, "w_end": end}
    lo = (start - dt.timedelta(days=1)).strftime("%Y%m%d")
    hi = (end + dt.timedelta(days=1)).strftime("%Y%m%d")
    params.update({"sfx_start": lo, "sfx_end": hi})
    if email:
        where += " AND LOWER(%s) = @email" % f["user"]
        params["email"] = email
    base = ("WITH %s,\nu AS (SELECT %s AS ts, LOWER(%s) AS email, %s AS client, %s AS model, IFNULL(%s, 0) AS total, "
            "IFNULL(%s, 0) AS prompt, IFNULL(%s, 0) AS cand FROM s t WHERE %s)"
            % (src.cte("s", start="w_start", end="w_end"), f["ts"], f["user"], f["client"] or null,
               f["model"] or null, f["total"] or "0", f["prompt"] or "0", f["candidates"] or "0", where))
    usage = Usage("bigquery")
    rows = ctx.query(base + "\nSELECT email, model, COUNT(*) AS requests, SUM(total) AS tokens, SUM(prompt) AS prompt_tokens, "
                     "SUM(cand) AS candidate_tokens, ARRAY_AGG(DISTINCT client IGNORE NULLS LIMIT 20) AS clients, "
                     "MAX(ts) AS last_ts\nFROM u GROUP BY email, model", params, "antigravity")
    for row in rows:
        if not row.get("email"):
            continue
        u = usage.user(row["email"])
        requests, tokens = to_int(row.get("requests")), to_int(row.get("tokens"))
        prompt, cand = to_int(row.get("prompt_tokens")), to_int(row.get("candidate_tokens"))
        u["requests"] += requests
        u["tokens"] += tokens
        u["promptTokens"] += prompt
        u["candidateTokens"] += cand
        u["clients"] |= set(row.get("clients") or [])
        if row.get("model"):
            u["models"].add(row["model"])
        Usage.add_model(u, row.get("model"), requests, tokens, prompt, cand)
        last = row.get("last_ts")
        if last is not None and (u["last"] is None or last > u["last"]):
            u["last"] = last
        usage.records += requests
    if email:
        for dim, target in (("model", usage.by_model), ("client", usage.by_client)):
            sub = ctx.query(base + "\nSELECT %s AS k, COUNT(*) AS requests, SUM(total) AS tokens, SUM(prompt) AS p, "
                            "SUM(cand) AS c FROM u GROUP BY k" % dim, params, "antigravity")
            for row in sub:
                target[row.get("k") or "unknown"] = {"requests": to_int(row.get("requests")),
                                                     "tokens": to_int(row.get("tokens")),
                                                     "promptTokens": to_int(row.get("p")),
                                                     "candidateTokens": to_int(row.get("c"))}
    if rng is not None:
        params.update({"tz": rng.tz_name, "start_date": rng.start, "bucket_days": rng.bucket_days})
        from .base import bucket_sql
        sub = ctx.query(base + "\nSELECT %s AS bucket, email, COUNT(*) AS requests, SUM(total) AS tokens "
                        "FROM u GROUP BY bucket, email" % bucket_sql("ts"), params, "antigravity")
        for row in sub:
            usage.daily[(row.get("email"), row.get("bucket"))] = (to_int(row.get("requests")), to_int(row.get("tokens")))
    usage.ok = True
    return usage


def _logging_usage(ctx, start, end, email=None, rng=None):
    ag = ctx.settings["governance"]["antigravity"]
    log_id = ag["inference_log"]
    if not config.LOG_NAME_RE.match(log_id):
        raise GcpError("invalid", message="invalid log id")
    flt = 'logName="projects/%s/logs/%s" AND timestamp>="%s" AND timestamp<"%s"' % (
        ctx.project, log_id, iso(start), iso(end))
    if email:
        if not config.EMAIL_RE.match(email):
            raise GcpError("invalid", message="invalid email")
        flt += ' AND labels.user_id="%s"' % email
    limit = int(ag.get("max_log_entries") or 20000)
    usage = Usage("logging")
    tz = timeutil.get_zone(ctx.tz_name)
    token = None
    fetched = 0
    while True:
        body = {"resourceNames": ["projects/%s" % ctx.project], "filter": flt, "orderBy": "timestamp desc",
                "pageSize": min(1000, max(1, limit - fetched))}
        if token:
            body["pageToken"] = token
        data = ctx.rest("POST", "%s/entries:list" % LOGGING_V2, json_body=body, timeout=45)
        entries = data.get("entries") or []
        for entry in entries:
            labels = entry.get("labels") or {}
            user = (labels.get("user_id") or "").strip().lower()
            if not user:
                continue
            meta = ((entry.get("jsonPayload") or {}).get("metadata") or {})
            total = to_int(meta.get("totalTokenCount"))
            prompt = to_int(meta.get("promptTokenCount"))
            cand = to_int(meta.get("candidatesTokenCount"))
            client = labels.get("client_name")
            model = labels.get("model_id") or labels.get("model")
            ts = parse_rfc3339(entry.get("timestamp"))
            u = usage.user(user)
            u["requests"] += 1
            u["tokens"] += total
            u["promptTokens"] += prompt
            u["candidateTokens"] += cand
            if client:
                u["clients"].add(client)
            if model:
                u["models"].add(model)
            Usage.add_model(u, model, 1, total, prompt, cand)
            if ts and (u["last"] is None or ts > u["last"]):
                u["last"] = ts
            usage.records += 1
            if email:
                for key, target in ((model or "unknown", usage.by_model), (client or "unknown", usage.by_client)):
                    agg = target.setdefault(key, {"requests": 0, "tokens": 0, "promptTokens": 0, "candidateTokens": 0})
                    agg["requests"] += 1
                    agg["tokens"] += total
                    agg["promptTokens"] += prompt
                    agg["candidateTokens"] += cand
            if rng is not None and ts is not None:
                bucket_idx = rng.bucket_index(ts.astimezone(tz).date())
                if bucket_idx is not None:
                    bucket = rng.buckets()[bucket_idx]
                    req, tok = usage.daily.get((user, bucket), (0, 0))
                    usage.daily[(user, bucket)] = (req + 1, tok + total)
        fetched += len(entries)
        token = data.get("nextPageToken")
        if not token:
            break
        if fetched >= limit:
            usage.truncated = True
            break
    usage.ok = True
    return usage


def antigravity_usage(ctx, start, end, email=None, rng=None):
    """Returns a Usage. Never raises for upstream failures (usage.ok=False + warning)."""
    ag = ctx.settings["governance"]["antigravity"]
    mode = ag.get("usage_source", "auto")
    src = None
    if mode in ("auto", "bigquery"):
        src = ctx.audit_source(AGY_SINK, optional=(mode == "auto"))
    log_label = "logging:%s" % urllib.parse.unquote(ag["inference_log"])
    if src is not None:
        label = src.label
        try:
            usage = _bq_usage(ctx, src, start, end, email=email, rng=rng)
            ctx.add_source(label)
            return usage
        except GcpError as err:
            ctx.warn_error(label, err, "The Antigravity inference log table")
            usage = Usage("bigquery")
            return usage
    if mode == "bigquery":
        return Usage("unavailable")
    cache_key = ("agy_logging", ctx.project, iso(start), iso(end), email or "", rng)

    def load():
        return _logging_usage(ctx, start, end, email=email, rng=rng)
    try:
        usage, _ = ctx.app.cache.get_or_compute(cache_key, 300, load)
    except GcpError as err:
        ctx.warn_error(log_label, err, "Cloud Logging inference logs")
        return Usage("logging")
    ctx.add_source(log_label)
    if usage.truncated:
        ctx.warn(log_label, "truncated", "Inference log entries were truncated at max_log_entries.")
    return usage


# ---------------------------------------------------------------------------
# Exception rules and suspension records (persistent governance state)
# ---------------------------------------------------------------------------

EXCEPTION_TYPES = ("allow", "block", "limit")


def _state_store(ctx):
    store = getattr(ctx.app, "settings", None)
    return store if store is not None and hasattr(store, "mutate_governance") else None


def governance_state(ctx):
    store = _state_store(ctx)
    return store.governance_state() if store is not None else copy.deepcopy(EMPTY_GOVERNANCE)


def mutate_state(ctx, fn, required=False):
    """Applies fn(state) to the persistent governance state. Without a store (unit tests) the
    change is skipped unless required."""
    store = _state_store(ctx)
    if store is None:
        if required:
            raise ApiError("upstream_error", "The governance state store is unavailable.")
        return None
    return store.mutate_governance(fn, ctx.identity.email)


def _expired(item, now):
    until = parse_rfc3339(item.get("until")) if item.get("until") else None
    return until is not None and until <= now


def active_exceptions(state, now):
    return {e["email"]: e for e in state.get("exceptions") or [] if not _expired(e, now)}


def effective_caps(exc, token_cap, request_cap, cost_cap):
    """Caps that apply to one user: an individual limit replaces each cap it sets."""
    if exc and exc.get("type") == "limit":
        return (token_cap if exc.get("tokenCap") is None else exc["tokenCap"],
                request_cap if exc.get("requestCap") is None else exc["requestCap"],
                cost_cap if exc.get("costCap") is None else exc["costCap"])
    return token_cap, request_cap, cost_cap


def public_exception(item):
    if not item:
        return None
    return {"type": item["type"], "scope": "period" if item.get("until") else "permanent",
            "until": item.get("until"), "tokenCap": item.get("tokenCap"), "requestCap": item.get("requestCap"),
            "costCap": item.get("costCap"), "note": item.get("note") or "",
            "createdBy": item.get("createdBy") or None, "createdAt": item.get("createdAt")}


def public_suspension(item):
    if not item:
        return None
    return {"since": item.get("since"), "until": item.get("until"), "trigger": item.get("trigger"),
            "by": item.get("by") or None, "reasons": list(item.get("reasons") or []),
            "tokens": item.get("tokens"), "requests": item.get("requests"), "cost": item.get("cost")}


def restore_at(state, suspension, over, next_reset, now):
    """When a suspended user regains access: the recorded period end, else the next reset while the
    user is over the cap. None when not suspended, already due, or without a fixed reset."""
    if state != CAPPED:
        return None
    until = parse_rfc3339((suspension or {}).get("until"))
    if until is not None and until > now:
        return iso(until)
    if over and next_reset is not None:
        return iso(next_reset)
    return None


def scheduler_status(ctx, state):
    ag = ctx.settings["governance"]["antigravity"]
    invokers = list(ag.get("scheduler_invokers") or [])
    base = (ctx.settings.get("security") or {}).get("public_base_url") or ""
    return {"configured": bool(invokers and base), "lastRun": state.get("lastScheduledRun")}


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def policy_params(ctx, source):
    ag = ctx.settings["governance"]["antigravity"]
    period = source.get("period") or ag["period"]
    window = source.get("window") or ag["window"]
    if period not in timeutil.PERIODS:
        raise ApiError("bad_request", "Parameter 'period' must be one of daily, weekly, monthly, all.")
    if window not in timeutil.WINDOWS:
        raise ApiError("bad_request", "Parameter 'window' must be 'calendar' or 'rolling'.")
    caps = []
    for name, default in (("tokenCap", ag["token_cap"]), ("requestCap", ag["request_cap"])):
        value = source.get(name, default)
        if isinstance(value, str) and value.isdigit():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 10 ** 12:
            raise ApiError("bad_request", "Field '%s' must be a non-negative integer." % name)
        caps.append(value)
    return period, window, caps[0], caps[1]


_EMPTY_USAGE = {"requests": 0, "tokens": 0, "promptTokens": 0, "candidateTokens": 0, "clients": set(),
                "models": set(), "byModel": {}}


def compute_status(ctx, period, window, token_cap, request_cap, cost_cap=None, mode=None, now=None):
    """Computes the full governance status. Returns (public_payload, internal).

    cost_cap and mode default to the saved policy. mode "auto" plans blocks for over-cap users;
    "manual" puts them in the review queue."""
    cfg = gov_config(ctx.settings)
    ag = ctx.settings["governance"]["antigravity"]
    cost_cap = float(ag.get("cost_cap") or 0) if cost_cap is None else float(cost_cap)
    mode = mode or ag.get("enforcement_mode") or "manual"
    tz = ag.get("reset_timezone") or "UTC"
    now = now or dt.datetime.now(dt.timezone.utc)
    start, end = timeutil.governance_window(period, window, now=now, tz=tz)
    next_reset = timeutil.governance_next_reset(period, window, now=now, tz=tz)
    gstate = governance_state(ctx)
    exceptions = active_exceptions(gstate, now)
    suspensions = {s["email"]: s for s in gstate.get("suspensions") or []}
    tasks = {
        "usage": lambda: antigravity_usage(ctx, start, end),
        "policy": lambda: get_policy(ctx),
        "markers": lambda: marker_status(ctx, cfg),
        "perms": lambda: permission_status(ctx),
    }
    if (ag.get("pricing_source") or "auto") == "auto":
        tasks["prices"] = lambda: price_table(ctx, ag.get("currency") or "")
    res = ctx.run(tasks)
    table = None
    if "prices" in res:
        ok, value = res["prices"]
        if ok:
            table = value
        elif isinstance(value, ApiError):
            ctx.warn("pricing:auto", "pricing_unavailable", "Automatic token prices could not be loaded.")
        else:
            ctx.warn_error("pricing:auto", value, "Automatic token prices")
    pricing = pricing_from(ag, table)
    ok, usage = res["usage"]
    if not ok:
        usage = Usage("unavailable")
    ok, policy = res["policy"]
    readable = ok
    if not ok:
        ctx.warn_error("api:cloudresourcemanager.projects.getIamPolicy", policy, "The project IAM policy")
        policy = None
    else:
        ctx.add_source("api:cloudresourcemanager.projects.getIamPolicy")
    idx = PolicyIndex(policy, readable=readable)
    ancestor = make_ancestor_check(ctx, cfg)
    population = {e for e in usage.users if is_human(e)}
    population |= idx.direct_users({cfg.access_role, *cfg.markers})
    users = []
    for email in sorted(population):
        u = usage.users.get(email) or _EMPTY_USAGE
        has_usage = u["requests"] > 0
        state, reason = classify(email, has_usage, idx, cfg, ancestor)
        exc = exceptions.get(email)
        tcap, rcap, ccap = effective_caps(exc, token_cap, request_cap, cost_cap)
        susp = suspensions.get(email) if state == CAPPED else None
        row = {"uid": uid_for(email), "email": email, "state": state, "reason": reason, "plannedAction": "none",
               "exception": public_exception(exc), "suspension": public_suspension(susp),
               "caps": {"tokenCap": tcap, "requestCap": rcap, "costCap": ccap}}
        if usage.ok:
            cost, partial = estimate_cost(u, pricing)
            over, cap_usage, reasons = cap_check(u["tokens"], u["requests"], tcap, rcap, cost, ccap)
            row.update({"requests": u["requests"], "tokens": u["tokens"], "promptTokens": u["promptTokens"],
                        "candidateTokens": u["candidateTokens"], "clients": sorted(u["clients"]),
                        "models": sorted(u["models"]), "estimatedCost": cost, "costPartial": partial,
                        "overCap": over, "capUsage": cap_usage, "overCapReasons": reasons})
        else:
            # Usage telemetry is unavailable: usage-derived fields are unknown (null), never zero.
            row.update({"requests": None, "tokens": None, "promptTokens": None, "candidateTokens": None,
                        "clients": None, "models": None, "estimatedCost": None, "costPartial": False,
                        "overCap": None, "capUsage": None, "overCapReasons": None})
        row["restoreAt"] = restore_at(state, susp, row["overCap"], next_reset, now)
        users.append(row)
    non_user = idx.non_user_members(cfg.access_role) if readable else []
    gates, plan, budget = evaluate(users, readable, usage.ok, usage.records, non_user, cfg, cfg.access_role,
                                   mode=mode, now=now)
    planned = {}
    for action in ("block", "review", "restore", "disallow", "manual"):
        for uid in plan[action]:
            planned[uid] = action
    by_state = {s: 0 for s in STATES}
    manual = []
    review_queue = []
    for u in users:
        u["plannedAction"] = planned.get(u["uid"], "none")
        by_state[u["state"]] += 1
        if u["plannedAction"] == "manual":
            manual.append({"uid": u["uid"], "email": u["email"], "state": u["state"], "reason": u["reason"],
                           "remediation": REMEDIATION.get(u["reason"])})
        elif u["plannedAction"] == "review":
            review_queue.append({"uid": u["uid"], "email": u["email"], "requests": u["requests"],
                                 "tokens": u["tokens"], "estimatedCost": u["estimatedCost"],
                                 "capUsage": u["capUsage"], "overCapReasons": u["overCapReasons"],
                                 "caps": u["caps"], "exception": u["exception"]})
    users.sort(key=lambda u: (-(u["tokens"] or 0), -(u["requests"] or 0), u["email"]))
    review_queue.sort(key=lambda r: (-(r["capUsage"] or 0), r["email"]))
    controllable = by_state[EA] + by_state[CAPPED] + by_state[BLOCKED]
    denominator = len(users) - by_state[NO_ACCESS]
    # With neither usage telemetry nor a readable policy the population itself is unknown.
    population_known = usage.ok or readable
    by_email = {u["email"]: u for u in users}
    exception_rows = []
    for email, item in sorted(exceptions.items()):
        row = by_email.get(email)
        if row is not None:
            state = row["state"]
        else:
            state = classify(email, False, idx, cfg)[0] if readable else UNKNOWN
        exception_rows.append(dict(public_exception(item), uid=uid_for(email), email=email, state=state))
    ok, markers = res["markers"]
    if not ok:
        markers = [{"role": cfg.cap_marker_id, "exists": None}, {"role": cfg.admin_marker_id, "exists": None}]
    ok, perms = res["perms"]
    if not ok:
        perms = {p: None for p in REQUIRED_PERMISSIONS}
    usage_source = usage.source if usage.ok else "unavailable"
    if cost_cap > 0 and not pricing.configured:
        ctx.warn("policy:governance.antigravity.cost_cap", "pricing_not_configured",
                 "A cost cap is set but no token prices were found, so estimated cost is unavailable and the cost "
                 "cap is not applied." if pricing.source == "auto" else
                 "A cost cap is set but no token prices are entered, so estimated cost is unavailable and the cost "
                 "cap is not applied.")
    if any(u.get("costPartial") for u in users):
        ctx.warn("policy:governance.antigravity.model_prices", "pricing_incomplete",
                 "Some tokens were used with models that have no price; their estimated cost is incomplete.")
    costs = [u["estimatedCost"] for u in users if u.get("estimatedCost") is not None]
    used_models = {m for rec in usage.users.values() for m in (rec.get("byModel") or {})} if usage.ok else set()
    store = _state_store(ctx)
    payload = {
        "policy": {"tokenCap": token_cap, "requestCap": request_cap, "costCap": cost_cap,
                   "currency": pricing.currency, "currencySetting": ag.get("currency") or "",
                   "period": period, "window": window,
                   "windowStart": iso(start), "windowEnd": iso(end), "nextReset": iso(next_reset),
                   "resetTimezone": tz, "changeBudget": budget, "enforcementMode": mode,
                   "pricing": pricing.public(used_models)},
        "usageSource": usage_source, "gates": gates,
        "population": {"total": len(users) if population_known else None,
                       "byState": by_state if population_known else None,
                       "enforceableCoverage": round(controllable / denominator, 4)
                       if (readable and denominator > 0) else None},
        "users": users if population_known else None, "plan": plan,
        "manualActions": manual if (readable and usage.ok) else None,
        "reviewQueue": review_queue if (readable and usage.ok) else None,
        "exceptions": exception_rows,
        "totals": {"requests": sum(u["requests"] or 0 for u in users) if usage.ok else None,
                   "tokens": sum(u["tokens"] or 0 for u in users) if usage.ok else None,
                   "estimatedCost": round(sum(costs), 6) if (usage.ok and pricing.configured) else None,
                   "overCap": sum(1 for u in users if u["overCap"]) if usage.ok else None,
                   "suspended": by_state[CAPPED] if readable else None,
                   "review": len(plan["review"]), "exceptions": len(exception_rows)},
        "enforcement": {"mode": mode, "lastRun": gstate.get("lastRun"),
                        "scheduler": scheduler_status(ctx, gstate),
                        "durable": store.governance_durable() if store is not None else None},
        "prerequisites": {"markerRoles": markers, "permissions": perms,
                          "telemetryDetected": bool(usage.ok and usage.records > 0)},
    }
    internal = {"policy": policy, "readable": readable, "cfg": cfg, "usage": usage, "idx": idx, "now": now,
                "nextReset": next_reset, "tz": tz, "mode": mode, "state": gstate,
                "periodEnd": timeutil.governance_period_end(period, window, now=now, tz=tz)}
    return payload, internal


def status(ctx):
    period, window, token_cap, request_cap = policy_params(ctx, ctx.params)
    payload, _ = compute_status(ctx, period, window, token_cap, request_cap)
    return payload


def default_status(ctx):
    ag = ctx.settings["governance"]["antigravity"]
    payload, internal = compute_status(ctx, ag["period"], ag["window"], ag["token_cap"], ag["request_cap"])
    return payload, internal


# ---------------------------------------------------------------------------
# Audit log and webhook
# ---------------------------------------------------------------------------

def write_audit(ctx, action, payload):
    ag = ctx.settings["governance"]["antigravity"]
    entry = {"tool": "control-plane", "action": action, "actor": ctx.identity.email}
    entry.update(payload)
    body = {"logName": "projects/%s/logs/%s" % (ctx.project, ag["audit_log_name"]),
            "resource": {"type": "global", "labels": {"project_id": ctx.project}},
            "entries": [{"severity": "NOTICE", "jsonPayload": entry}]}
    try:
        ctx.rest("POST", "%s/entries:write" % LOGGING_V2, json_body=body)
        return True
    except GcpError as err:
        LOG.warning("audit log write failed kind=%s", err.kind)
        return False


def send_webhook(ctx, text):
    url = ctx.settings["governance"]["antigravity"].get("webhook_url") or ""
    if not url or not config.valid_webhook(url):
        return False
    try:
        import requests
        resp = requests.post(url, json={"text": text}, timeout=5, allow_redirects=False)
        return 200 <= resp.status_code < 300
    except Exception as err:  # pylint: disable=broad-except
        LOG.warning("webhook delivery failed: %s", type(err).__name__)
        return False


# ---------------------------------------------------------------------------
# Enforcement cycle: POST /api/v1/governance/antigravity/cap (administrator, ad hoc caps, every
# over-cap user) and POST /api/v1/governance/antigravity/enforce (saved policy and enforcement
# mode; Cloud Scheduler or "Run now")
# ---------------------------------------------------------------------------

CYCLE_ACTIONS = ("block", "restore", "disallow")
MAX_BATCH = 100


def _marker_for(action, cfg):
    return {"block": cfg.cap_marker_id, "disallow": cfg.admin_marker_id}.get(action)


def _result_of(mode, plan, changes):
    if mode == "dry_run":
        return "dry_run"
    if plan["aborted"]:
        return "aborted"
    if changes and all(c["status"] == "applied" for c in changes):
        return "success"
    if any(c["status"] == "applied" for c in changes):
        return "partial"
    if any(c["status"] == "failed" for c in changes):
        return "failed"
    return "success"


def _applied(changes, action):
    return sum(1 for c in changes if c["action"] == action and c["status"] == "applied")


STATUS_PATH = "/api/v1/governance/antigravity/status"


def _refresh_status_cache(ctx):
    """Drops only the cached governance status (it carries the last-run summary)."""
    cache = getattr(ctx.app, "cache", None)
    if cache is not None and hasattr(cache, "clear_matching"):
        cache.clear_matching(lambda k: isinstance(k, tuple) and len(k) > 1 and k[0] == "route" and k[1] == STATUS_PATH)


def _suspension_record(ctx, user, until, trigger, now):
    return {"email": user["email"], "since": iso(now), "until": iso(until), "trigger": trigger,
            "by": ctx.identity.email, "reasons": list(user.get("overCapReasons") or []),
            "tokens": user.get("tokens"), "requests": user.get("requests"), "cost": user.get("estimatedCost")}


def _record_changes(state, ctx, users_by_email, applied, internal, trigger):
    """Updates the suspension records in `state` for applied (email, action) pairs: a block starts a
    suspension until the period reset; any other change ends it."""
    susp = {s["email"]: s for s in state.get("suspensions") or []}
    for email, action in applied:
        if action == "block":
            user = users_by_email.get(email) or {"email": email}
            susp[email] = _suspension_record(ctx, user, internal["nextReset"], trigger, internal["now"])
        else:
            susp.pop(email, None)
    state["suspensions"] = list(susp.values())


def _save_cycle(ctx, payload, internal, changes, result, eval_mode, trigger, suspension_trigger):
    """Persists suspension records, prunes stale records and expired exception rules, and records the
    run. Returns the users that entered the review queue in this period and were not announced yet."""
    users = payload["users"] or []
    by_email = {u["email"]: u for u in users}
    applied = [(c["email"], c["action"]) for c in changes if c["status"] == "applied"]
    blocked_now = {email for email, action in applied if action == "block"}
    now = internal["now"]
    window_start = payload["policy"]["windowStart"]
    review = [u["email"] for u in users if u["plannedAction"] == "review"]
    run = {"at": iso(now), "trigger": trigger, "actor": ctx.identity.email, "mode": eval_mode, "result": result,
           "blocked": _applied(changes, "block"), "restored": _applied(changes, "restore"),
           "disallowed": _applied(changes, "disallow"), "review": len(review),
           "failed": sum(1 for c in changes if c["status"] == "failed")}
    fresh = []

    def apply(state):
        _record_changes(state, ctx, by_email, applied, internal, suspension_trigger)
        if internal["readable"]:
            # Records of users who are no longer suspended (restored outside this cycle) are dropped.
            state["suspensions"] = [s for s in state["suspensions"] if s["email"] in blocked_now
                                    or (by_email.get(s["email"]) or {}).get("state") == CAPPED]
        state["exceptions"] = [e for e in state.get("exceptions") or [] if not _expired(e, now)]
        notified = {k: v for k, v in (state.get("notified") or {}).items() if v == window_start and k in review}
        del fresh[:]
        for email in review:
            if email not in notified:
                fresh.append(email)
                notified[email] = window_start
        state["notified"] = notified
        state["lastRun"] = run
        if trigger == "scheduler":
            state["lastScheduledRun"] = run
    try:
        mutate_state(ctx, apply)
    except ApiError as err:
        LOG.warning("governance state was not saved: %s", err.code)
    return list(fresh)


def _run(ctx, mode, period, window, token_cap, request_cap, eval_mode, trigger, suspension_trigger, kind):
    """One enforcement cycle (mode dry_run | apply). eval_mode "auto" blocks over-cap users;
    "manual" leaves them in the review queue while restores and exception rules still apply."""
    previous = (governance_state(ctx).get("lastRun") or {}).get("result")
    with _WRITE_LOCK:
        for attempt in (1, 2):
            payload, internal = compute_status(ctx, period, window, token_cap, request_cap, mode=eval_mode)
            plan, gates = payload["plan"], payload["gates"]
            users = {u["uid"]: u for u in payload["users"] or []}
            cfg = internal["cfg"]
            changes = []
            executed = False
            if mode == "dry_run" or plan["aborted"]:
                message = "Dry run: no IAM change was made." if mode == "dry_run" else \
                    "Aborted by safety gates: no IAM change was made."
                for action in CYCLE_ACTIONS:
                    for uid in plan[action]:
                        changes.append({"uid": uid, "email": users[uid]["email"], "action": action,
                                        "status": "skipped", "message": message})
                break
            markers = {m["role"]: m["exists"] for m in payload["prerequisites"]["markerRoles"]}
            mutations = []
            for action in CYCLE_ACTIONS:
                for uid in plan[action]:
                    user = users[uid]
                    try:
                        check_transition(user["state"], action)
                    except ValueError:
                        changes.append({"uid": uid, "email": user["email"], "action": action, "status": "skipped",
                                        "message": "The user state changed; no IAM change was made."})
                        continue
                    marker = _marker_for(action, cfg)
                    if marker and markers.get(marker) is not True:
                        changes.append({"uid": uid, "email": user["email"], "action": action, "status": "failed",
                                        "message": "Marker role %s does not exist. Run setup first." % marker})
                        continue
                    mutations.append((uid, user["email"], action))
            if not mutations:
                break
            new_policy = apply_changes(internal["policy"], [(e, a) for _, e, a in mutations], cfg)
            try:
                set_policy(ctx, new_policy)
                executed = True
                for uid, email, action in mutations:
                    changes.append({"uid": uid, "email": email, "action": action, "status": "applied",
                                    "message": "IAM policy updated."})
                break
            except GcpError as err:
                if err.kind == "conflict" and attempt == 1:
                    LOG.info("setIamPolicy etag conflict; retrying with a fresh plan")
                    continue
                LOG.warning("setIamPolicy failed kind=%s", err.kind)
                for uid, email, action in mutations:
                    changes.append({"uid": uid, "email": email, "action": action, "status": "failed",
                                    "message": "The IAM policy update failed (%s)." % err.kind})
                break
        result = _result_of(mode, plan, changes)
        new_review = []
        if mode == "apply":
            new_review = _save_cycle(ctx, payload, internal, changes, result, eval_mode, trigger, suspension_trigger)
        audit_action = ("cap_dry_run" if mode == "dry_run" else "cap_apply") if kind == "cap" else \
            ("enforce_dry_run" if mode == "dry_run" else "enforce")
        audit_logged = write_audit(ctx, audit_action, {
            "mode": mode, "result": result, "target": "%d users" % len(changes), "gates": gates,
            "changes": [{"email": c["email"], "action": c["action"], "status": c["status"]} for c in changes],
            "period": period, "window": window, "tokenCap": token_cap, "requestCap": request_cap,
            "costCap": payload["policy"]["costCap"], "enforcementMode": eval_mode, "trigger": trigger,
            "review": len(plan["review"])})
        webhook = False
        counts = (_applied(changes, "block"), _applied(changes, "restore"), _applied(changes, "disallow"))
        if mode == "apply" and kind == "cap" and (executed or plan["aborted"] or plan["manual"]):
            webhook = send_webhook(ctx, "AI Control Plane cap %s by %s: %d blocked, %d restored, %d manual action "
                                        "item(s)%s." % (result, ctx.identity.email, counts[0], counts[1],
                                                        len(plan["manual"]),
                                                        "; aborted by safety gates" if plan["aborted"] else ""))
        elif mode == "apply" and kind == "enforce" and (executed or new_review or
                                                        (plan["aborted"] and previous != "aborted")):
            parts = ["AI Control Plane %s enforcement (%s): %d suspended until the period reset, %d restored, "
                     "%d blocked by exception rules" % (eval_mode, result, counts[0], counts[1], counts[2])]
            if new_review:
                parts.append("%d user(s) over the cap await review: %s" % (len(new_review), ", ".join(new_review[:10])))
            if plan["aborted"]:
                parts.append("aborted by safety gates")
            webhook = send_webhook(ctx, "; ".join(parts) + ".")
    if executed:
        ctx.app.invalidate_governance()
    elif mode == "apply":
        _refresh_status_cache(ctx)
    out = {"mode": mode, "executed": executed, "plan": plan, "gates": gates, "changes": changes,
           "auditLogged": audit_logged, "webhookSent": webhook}
    if kind == "enforce":
        out.update({"enforcementMode": eval_mode, "trigger": trigger, "newReview": new_review})
    return out


def cap(ctx):
    """Administrator run with ad hoc caps: every over-cap user is suspended (bulk approval), even in
    manual enforcement mode."""
    mode = ctx.body.get("mode")
    if mode not in ("dry_run", "apply"):
        raise ApiError("bad_request", "Field 'mode' must be 'dry_run' or 'apply'.")
    period, window, token_cap, request_cap = policy_params(ctx, ctx.body)
    return _run(ctx, mode, period, window, token_cap, request_cap, eval_mode="auto", trigger="admin",
                suspension_trigger="bulk", kind="cap")


def enforce(ctx):
    """Saved policy in the configured enforcement mode. Called by Cloud Scheduler (OIDC) or by an
    administrator ("Run now")."""
    dry = ctx.body.get("dryRun", False)
    if not isinstance(dry, bool):
        raise ApiError("bad_request", "Field 'dryRun' must be true or false.")
    ag = ctx.settings["governance"]["antigravity"]
    trigger = "scheduler" if ctx.identity.auth_mode == "scheduler" else "admin"
    return _run(ctx, "dry_run" if dry else "apply", ag["period"], ag["window"], ag["token_cap"],
                ag["request_cap"], eval_mode=ag.get("enforcement_mode") or "manual", trigger=trigger,
                suspension_trigger="auto", kind="enforce")


# ---------------------------------------------------------------------------
# POST /api/v1/governance/antigravity/review (manual mode decisions)
# ---------------------------------------------------------------------------

def _uids(value):
    if not isinstance(value, list) or not value or len(value) > MAX_BATCH:
        raise ApiError("bad_request", "Field 'uids' must be a list of 1 to %d user ids." % MAX_BATCH)
    out = []
    for uid in value:
        if not isinstance(uid, str) or len(uid) != 16 or any(c not in "0123456789abcdef" for c in uid):
            raise ApiError("bad_request", "Field 'uids' contains an invalid user id.")
        if uid not in out:
            out.append(uid)
    return out


def _note(value):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > 200 or config.CONTROL_RE.search(value):
        raise ApiError("bad_request", "Field 'note' must be text of at most 200 characters.")
    return value.strip()


def _exception_item(ctx, email, kind, until, note, caps=None):
    caps = caps or {}
    return {"email": email, "type": kind, "until": iso(until) if until else None,
            "tokenCap": caps.get("tokenCap"), "requestCap": caps.get("requestCap"), "costCap": caps.get("costCap"),
            "note": note, "createdBy": ctx.identity.email, "createdAt": now_iso()}


def _put_exception(state, item):
    state["exceptions"] = [e for e in state.get("exceptions") or [] if e["email"] != item["email"]] + [item]


def review(ctx):
    """suspend: over-cap users with access are suspended until the period reset.
    allow: users are allowed for the rest of the period (suspended users are restored now)."""
    decision = ctx.body.get("decision")
    if decision not in ("suspend", "allow"):
        raise ApiError("bad_request", "Field 'decision' must be 'suspend' or 'allow'.")
    uids = _uids(ctx.body.get("uids"))
    note = _note(ctx.body.get("note"))
    ag = ctx.settings["governance"]["antigravity"]
    executed = False
    with _WRITE_LOCK:
        for attempt in (1, 2):
            payload, internal = compute_status(ctx, ag["period"], ag["window"], ag["token_cap"], ag["request_cap"],
                                               mode="manual")
            users = {u["uid"]: u for u in payload["users"] or []}
            cfg = internal["cfg"]
            markers = {m["role"]: m["exists"] for m in payload["prerequisites"]["markerRoles"]}
            results, mutations, allow_only = [], [], []
            for uid in uids:
                user = users.get(uid)
                if user is None:
                    results.append({"uid": uid, "email": None, "status": "skipped", "message": "User not found."})
                    continue
                skip = None
                if decision == "suspend":
                    if user["state"] != EA:
                        skip = "The user has no access that can be suspended."
                    elif user["overCap"] is not True:
                        skip = "The user is not over the cap."
                    elif (user["exception"] or {}).get("type") == "allow":
                        skip = "An allow exception applies to this user."
                    elif markers.get(cfg.cap_marker_id) is not True:
                        results.append({"uid": uid, "email": user["email"], "status": "failed",
                                        "message": "Marker role %s does not exist. Run setup first."
                                        % cfg.cap_marker_id})
                        continue
                    else:
                        mutations.append((uid, user["email"], "block"))
                elif user["state"] == CAPPED:
                    mutations.append((uid, user["email"], "restore"))
                elif user["state"] == EA:
                    allow_only.append((uid, user["email"]))
                else:
                    skip = "The user cannot be allowed in its current state."
                if skip:
                    results.append({"uid": uid, "email": user["email"], "status": "skipped", "message": skip})
            if not mutations:
                break
            new_policy = apply_changes(internal["policy"], [(e, a) for _, e, a in mutations], cfg)
            try:
                set_policy(ctx, new_policy)
                executed = True
                for uid, email, action in mutations:
                    results.append({"uid": uid, "email": email, "status": "applied",
                                    "message": "Suspended until the period reset." if action == "block"
                                    else "Restored for the rest of the period."})
                break
            except GcpError as err:
                if err.kind == "conflict" and attempt == 1:
                    continue
                LOG.warning("setIamPolicy failed kind=%s", err.kind)
                for uid, email, _ in mutations:
                    results.append({"uid": uid, "email": email, "status": "failed",
                                    "message": "The IAM policy update failed (%s)." % err.kind})
                mutations = []
                break
        applied = [(e, a) for _, e, a in mutations] if executed else []
        allowed = [e for e, a in applied if a == "restore"] + [e for _, e in allow_only]
        by_email = {u["email"]: u for u in payload["users"] or []}
        if applied or allowed:
            def apply(state):
                _record_changes(state, ctx, by_email, applied, internal, "review")
                for email in allowed:
                    _put_exception(state, _exception_item(
                        ctx, email, "allow", internal["periodEnd"],
                        note or "Allowed for the rest of the period in the review queue."))
            mutate_state(ctx, apply, required=bool(allowed))
        for uid, email in allow_only:
            results.append({"uid": uid, "email": email, "status": "applied",
                            "message": "Allowed for the rest of the period."})
        audit_logged = write_audit(ctx, "review_%s" % decision, {
            "result": "success" if all(r["status"] == "applied" for r in results) else
            ("partial" if any(r["status"] == "applied" for r in results) else "failed"),
            "target": "%d users" % len(results),
            "changes": [{"email": r["email"], "status": r["status"]} for r in results]})
    if executed or allowed:
        ctx.app.invalidate_governance()
    return {"decision": decision, "results": results, "auditLogged": audit_logged}


# ---------------------------------------------------------------------------
# POST /api/v1/governance/antigravity/exceptions and /exceptions/delete
# ---------------------------------------------------------------------------

def _optional_cap(body, name, integer, hi):
    value = body.get(name)
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value < 0 or value > hi \
            or (integer and not float(value).is_integer()):
        raise ApiError("bad_request", "Field '%s' must be a non-negative %s." % (name, "integer" if integer else "number"))
    return int(value) if integer else round(float(value), 6)


def _target_email(ctx):
    email = ctx.body.get("email")
    if email is not None:
        email = email.strip().lower() if isinstance(email, str) else ""
        if not config.EMAIL_RE.match(email) or not is_human(email):
            raise ApiError("bad_request", "Field 'email' must be the email address of a user.")
        return email, None
    uid = ctx.body.get("uid")
    if not isinstance(uid, str) or len(uid) != 16 or any(c not in "0123456789abcdef" for c in uid):
        raise ApiError("bad_request", "Field 'email' or 'uid' is required.")
    return None, uid


def _decide_block(state):
    return None if state in (BLOCKED, NO_ACCESS) else "disallow"


def _decide_allow(state):
    return {CAPPED: "restore", BLOCKED: "allow"}.get(state)


def _decide_unblock(state):
    return "allow" if state == BLOCKED else None


def exception_save(ctx):
    """Creates or replaces one user's exception rule. block disallows the user now (admin marker);
    allow restores a suspended or blocked user now; limit applies individual caps from the next
    evaluation."""
    body = ctx.body
    kind = body.get("type")
    if kind not in EXCEPTION_TYPES:
        raise ApiError("bad_request", "Field 'type' must be 'allow', 'block' or 'limit'.")
    scope = body.get("scope") or "permanent"
    if scope not in ("period", "permanent"):
        raise ApiError("bad_request", "Field 'scope' must be 'period' or 'permanent'.")
    if kind == "block":
        scope = "permanent"
    note = _note(body.get("note"))
    caps = {}
    if kind == "limit":
        caps = {"tokenCap": _optional_cap(body, "tokenCap", True, 10 ** 12),
                "requestCap": _optional_cap(body, "requestCap", True, 10 ** 9),
                "costCap": _optional_cap(body, "costCap", False, 1e9)}
        if all(v is None for v in caps.values()):
            raise ApiError("bad_request", "Set at least one individual cap.")
    email, uid = _target_email(ctx)
    decide = {"block": _decide_block, "allow": _decide_allow}.get(kind, lambda state: None)
    email, previous, new_state, action = _change_user(ctx, decide, uid=uid, email=email)
    ag = ctx.settings["governance"]["antigravity"]
    until = None
    if scope == "period":
        until = timeutil.governance_period_end(ag["period"], ag["window"], tz=ag.get("reset_timezone") or "UTC")
    item = _exception_item(ctx, email, kind, until, note, caps)

    def apply(state):
        _put_exception(state, item)
        if action is not None:
            state["suspensions"] = [s for s in state.get("suspensions") or [] if s["email"] != email]
    mutate_state(ctx, apply, required=True)
    audit_logged = write_audit(ctx, "exception_save", {
        "result": "success", "target": email, "type": kind, "scope": scope, "until": item["until"],
        "caps": caps or None, "iamAction": action, "previousState": previous, "newState": new_state})
    ctx.app.invalidate_governance()
    return {"exception": dict(public_exception(item), email=email, uid=uid_for(email), state=new_state),
            "change": {"action": action, "previousState": previous, "newState": new_state} if action else None,
            "auditLogged": audit_logged}


def exception_delete(ctx):
    """Removes one user's exception rule. Removing a block rule restores access (unless
    restoreAccess is false); other rules return the user to the default policy."""
    email, _ = _target_email(ctx)
    if not email:
        raise ApiError("bad_request", "Field 'email' is required.")
    restore_access = ctx.body.get("restoreAccess", True)
    if not isinstance(restore_access, bool):
        raise ApiError("bad_request", "Field 'restoreAccess' must be true or false.")
    current = next((e for e in governance_state(ctx).get("exceptions") or [] if e["email"] == email), None)
    if current is None:
        raise ApiError("not_found", "No exception rule exists for this user.")
    change = None
    if current["type"] == "block" and restore_access:
        _, previous, new_state, action = _change_user(ctx, _decide_unblock, email=email)
        if action:
            change = {"action": action, "previousState": previous, "newState": new_state}

    def apply(state):
        state["exceptions"] = [e for e in state.get("exceptions") or [] if e["email"] != email]
    mutate_state(ctx, apply, required=True)
    audit_logged = write_audit(ctx, "exception_delete", {"result": "success", "target": email,
                                                         "type": current["type"],
                                                         "iamAction": change["action"] if change else None})
    ctx.app.invalidate_governance()
    return {"email": email, "removed": True, "change": change, "auditLogged": audit_logged}


# ---------------------------------------------------------------------------
# POST /api/v1/governance/antigravity/user-action
# ---------------------------------------------------------------------------

def _resolve_uid(ctx, uid, policy_idx, cfg, usage):
    for email in usage.users:
        if uid_for(email) == uid:
            return email
    for email in policy_idx.direct_users({cfg.access_role, *cfg.markers, *cfg.privileged_roles}):
        if uid_for(email) == uid:
            return email
    try:
        from .users import user_table
        rng = timeutil.parse_range({}, ctx.tz_name, ctx.ws["default_range_days"])
        table = ctx.memo("user_table", rng, lambda c: user_table(c, rng))
    except (GcpError, ApiError):
        return None
    for email in table["users"]:
        if uid_for(email) == uid:
            return email
    return None


def _change_user(ctx, decide, uid=None, email=None):
    """One user's IAM transition as an etag-guarded read-modify-write (one retry on conflict).
    decide is an action or callable(state) -> action | None. Returns (email, previous_state,
    new_state, action); action None means that no change was needed."""
    cfg = gov_config(ctx.settings)
    ag = ctx.settings["governance"]["antigravity"]
    start, end = timeutil.governance_window(ag["period"], ag["window"], tz=ag.get("reset_timezone") or "UTC")
    with _WRITE_LOCK:
        usage = antigravity_usage(ctx, start, end)
        for attempt in (1, 2):
            try:
                policy = get_policy(ctx)
            except GcpError as err:
                LOG.warning("getIamPolicy failed kind=%s", err.kind)
                raise ApiError("upstream_error", "The project IAM policy could not be read.")
            idx = PolicyIndex(policy)
            target = email or _resolve_uid(ctx, uid, idx, cfg, usage)
            if not target:
                raise ApiError("not_found", "User not found.")
            has_usage = (usage.users.get(target) or {}).get("requests", 0) > 0
            state, reason = classify(target, has_usage, idx, cfg, make_ancestor_check(ctx, cfg))
            action = decide(state) if callable(decide) else decide
            if action is None:
                return target, state, state, None
            if state in (NE, UNKNOWN):
                raise ApiError("conflict", REMEDIATION.get(reason) or "This user cannot be managed per user.")
            try:
                check_transition(state, action)
            except ValueError:
                if action == "allow":
                    raise ApiError("conflict", "The user already has access.")
                raise ApiError("conflict", "The user does not currently have access that can be disallowed.")
            marker = _marker_for(action, cfg)
            if marker:
                markers = {m["role"]: m["exists"] for m in marker_status(ctx, cfg)}
                if markers.get(marker) is not True:
                    raise ApiError("conflict", "Marker role %s does not exist. Run setup first." % marker)
            new_policy = apply_changes(policy, [(target, action)], cfg)
            try:
                set_policy(ctx, new_policy)
                break
            except GcpError as err:
                if err.kind == "conflict" and attempt == 1:
                    continue
                LOG.warning("setIamPolicy failed kind=%s", err.kind)
                raise ApiError("upstream_error", "The IAM policy update failed.")
    return target, state, TARGET_STATE[action], action


def _sync_after_action(ctx, email, previous, action):
    """Keeps exception rules consistent with drawer actions: disallow is an always-block rule;
    allowing a suspended user allows them for the rest of the period (otherwise the next cycle
    would suspend them again); allowing a blocked user removes the block rule."""
    ag = ctx.settings["governance"]["antigravity"]
    item = None
    if action == "disallow":
        item = _exception_item(ctx, email, "block", None, "Disallowed by an administrator.")
    elif action == "allow" and previous == CAPPED:
        until = timeutil.governance_period_end(ag["period"], ag["window"], tz=ag.get("reset_timezone") or "UTC")
        item = _exception_item(ctx, email, "allow", until, "Restored early by an administrator.")

    def apply(state):
        state["suspensions"] = [s for s in state.get("suspensions") or [] if s["email"] != email]
        if item is not None:
            _put_exception(state, item)
        elif action == "allow":
            state["exceptions"] = [e for e in state.get("exceptions") or []
                                   if not (e["email"] == email and e["type"] == "block")]
    try:
        mutate_state(ctx, apply)
    except ApiError as err:
        LOG.warning("governance state was not saved after %s: %s", action, err.code)
        return None
    return public_exception(item)


def user_action(ctx):
    uid = ctx.body.get("uid")
    action = ctx.body.get("action")
    if action not in ("allow", "disallow"):
        raise ApiError("bad_request", "Field 'action' must be 'allow' or 'disallow'.")
    if not isinstance(uid, str) or len(uid) != 16 or any(c not in "0123456789abcdef" for c in uid):
        raise ApiError("bad_request", "Field 'uid' is invalid.")
    email, state, new_state, _ = _change_user(ctx, action, uid=uid)
    exception = _sync_after_action(ctx, email, state, action)
    audit_logged = write_audit(ctx, action, {"result": "success", "target": email, "previousState": state,
                                             "newState": new_state, "uid": uid})
    ctx.app.invalidate_governance()
    return {"uid": uid, "email": email, "action": action, "previousState": state, "newState": new_state,
            "auditLogged": audit_logged, "exception": exception}


# ---------------------------------------------------------------------------
# POST /api/v1/governance/antigravity/setup
# ---------------------------------------------------------------------------

def setup(ctx):
    cfg = gov_config(ctx.settings)
    base_role = cfg.access_role
    results = []
    try:
        role = ctx.rest("GET", "%s/%s" % (IAM_V1, base_role))
        permissions = [p for p in role.get("includedPermissions") or [] if not p.startswith("businessaicode.")]
    except GcpError as err:
        LOG.warning("could not read %s kind=%s", base_role, err.kind)
        permissions = None
    titles = {cfg.cap_marker_id: ("Antigravity Cap Blocked",
                                  "Assigned by AI Control Plane when a user exceeds the Antigravity cap. "
                                  "Same as the access role without businessaicode permissions."),
              cfg.admin_marker_id: ("Antigravity Admin Blocked",
                                    "Assigned by an administrator to disallow Antigravity. "
                                    "Same as the access role without businessaicode permissions.")}
    with _WRITE_LOCK:
        for role_id in (cfg.cap_marker_id, cfg.admin_marker_id):
            url = "%s/projects/%s/roles/%s" % (IAM_V1, ctx.project, urllib.parse.quote(role_id, safe=""))
            try:
                existing = ctx.rest("GET", url)
                if existing.get("deleted"):
                    results.append({"role": role_id, "status": "failed",
                                    "message": "The role exists in a deleted state; undelete it in IAM."})
                else:
                    results.append({"role": role_id, "status": "exists", "message": "The role already exists."})
                continue
            except GcpError as err:
                if err.kind != "not_found":
                    results.append({"role": role_id, "status": "failed",
                                    "message": "The role could not be read (%s)." % err.kind})
                    continue
            if not permissions:
                results.append({"role": role_id, "status": "failed",
                                "message": "The permissions of %s could not be read." % base_role})
                continue
            title, description = titles[role_id]
            perms = list(permissions)
            status = None
            for _ in range(2):
                try:
                    ctx.rest("POST", "%s/projects/%s/roles" % (IAM_V1, ctx.project),
                             json_body={"roleId": role_id, "role": {"title": title, "description": description,
                                                                    "includedPermissions": perms, "stage": "GA"}})
                    status = {"role": role_id, "status": "created",
                              "message": "Created with %d permissions (businessaicode.* excluded)." % len(perms)}
                    break
                except GcpError as err:
                    bad = [p for p in perms if p in (err.message or "")]
                    if err.kind == "invalid" and bad:
                        perms = [p for p in perms if p not in bad]
                        continue
                    status = {"role": role_id, "status": "failed",
                              "message": "The role could not be created (%s)." % err.kind}
                    break
            results.append(status or {"role": role_id, "status": "failed", "message": "The role could not be created."})
        write_audit(ctx, "setup", {"result": "success" if all(r["status"] != "failed" for r in results) else "partial",
                                   "target": "marker roles", "results": results})
    ctx.app.invalidate_governance()
    return {"results": results}


# ---------------------------------------------------------------------------
# GET /api/v1/governance/audit-log
# ---------------------------------------------------------------------------

_RESULT_MAP = {"ok": "success", "success": "success", "applied": "success", "succeeded": "success",
               "partial": "partial", "error": "failed", "failed": "failed", "failure": "failed",
               "aborted": "aborted", "abort": "aborted", "dry_run": "dry_run", "dry-run": "dry_run",
               "dryrun": "dry_run"}
_KNOWN_KEYS = {"tool", "action", "actor", "target", "result"}


def _normalize_entry(entry):
    payload = entry.get("jsonPayload") or {}
    if not isinstance(payload, dict):
        payload = {}
    text = entry.get("textPayload")
    action = payload.get("action") or payload.get("event") or payload.get("command") or (
        text[:80] if isinstance(text, str) else None)
    actor = payload.get("actor") or payload.get("operator") or payload.get("admin") or payload.get("user")
    target = payload.get("target") or payload.get("email") or payload.get("user_email")
    if target is None and isinstance(payload.get("changes"), list):
        target = "%d users" % len(payload["changes"])
    raw_result = str(payload.get("result") or payload.get("status") or "").lower()
    if payload.get("dry_run") is True or payload.get("mode") == "dry_run":
        raw_result = raw_result or "dry_run"
    result = _RESULT_MAP.get(raw_result)
    if result is None:
        result = "failed" if str(entry.get("severity") or "").upper() in ("ERROR", "CRITICAL", "ALERT") else "success"
    tool = payload.get("tool") or "agy-admin"
    details = {k: v for k, v in payload.items() if k not in _KNOWN_KEYS}
    return {"time": iso(parse_rfc3339(entry.get("timestamp"))) or entry.get("timestamp"),
            "actor": actor if isinstance(actor, str) else None, "action": action if isinstance(action, str) else None,
            "target": target if isinstance(target, str) else None, "result": result,
            "tool": "control-plane" if tool == "control-plane" else "agy-admin", "details": details}


def audit_entries(ctx, limit):
    ag = ctx.settings["governance"]["antigravity"]
    log_name = ag["audit_log_name"]
    body = {"resourceNames": ["projects/%s" % ctx.project],
            "filter": 'logName="projects/%s/logs/%s"' % (ctx.project, log_name),
            "orderBy": "timestamp desc", "pageSize": limit}
    data = ctx.rest("POST", "%s/entries:list" % LOGGING_V2, json_body=body, timeout=30)
    return [_normalize_entry(e) for e in data.get("entries") or [] if isinstance(e, dict)][:limit]


def audit_log(ctx):
    try:
        limit = int(ctx.params.get("limit") or 50)
    except ValueError:
        raise ApiError("bad_request", "Parameter 'limit' must be an integer.")
    limit = max(1, min(limit, 200))
    label = "logging:%s" % urllib.parse.unquote(ctx.settings["governance"]["antigravity"]["audit_log_name"])
    try:
        items = audit_entries(ctx, limit)
    except GcpError as err:
        ctx.warn_error(label, err, "The governance audit log")
        return {"items": None}
    ctx.add_source(label)
    return {"items": items}
