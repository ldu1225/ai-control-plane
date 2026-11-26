"""Antigravity governance: usage, IAM 6-state classification, safety gates and caps.

Safety rules (from agy-admin):
- Only direct, unconditional `user:` bindings are ever modified.
- NOT_ENFORCEABLE and UNKNOWN users are never modified (no override).
- All changes of one cycle are written in a single atomic read-modify-write guarded by the
  policy etag; a concurrent modification (409/ABORTED) triggers one retry with a fresh plan.
- Gates G1/G2/G4 abort the cycle; G3 withholds restores; G5 only warns.

Enforcement backends (governance.antigravity.enforcement_backend):
- iam_allow_swap (default): the rules above; direct user: bindings of the primary project are swapped for marker roles.
- iam_deny: suspended people are listed in one managed IAM deny policy per attachment point (services/enforcers.py),
  which also stops access through groups, inheritance and workforce pools. The deny policy is the state: a person in
  it is suspended (blocked when a block rule applies). Administrator decisions are recorded in the governance state
  and applied by an enforcement run, by the enforcement job when one is configured (services/jobs.py). Extra gates:
  G6 (denied principals per attachment point against the IAM limit) and G7 (surge guard).
- notify_only: the plan and its notifications only; nobody loses access.

Per-user caps over a settlement period:
- A user is over the cap when tokens, requests or the estimated cost (tokens x configured unit
  prices) reach the cap within the period. Calendar periods start at governance.antigravity.reset_time in
  reset_timezone (default: the workspace time zone): daily every day, weekly on reset_weekday, monthly on reset_day of
  the month (the last day in shorter months) and custom every cycle_days days counted from cycle_start.
- enforcement_mode=auto: an over-cap user is suspended until the period resets, by the enforcement run that a usage
  event starts as soon as the cap is reached (services/usage_events.py) or else by the next scheduled run.
  enforcement_mode=manual: over-cap users wait in a review queue; an administrator suspends them or allows them for the
  rest of the period.
- Exception rules per user (both modes): allow (never suspended), block (always disallowed) and
  limit (individual caps). Suspensions end at the period reset even without telemetry, because
  the usage window itself restarts.
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
import math
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

from .. import config, identity, timeutil
from ..gcp import GcpError
from ..httpkit import ApiError
from ..schema import expr, quote_ident
from ..security import is_admin_email, is_human, uid_for
from ..settings_store import EMPTY_GOVERNANCE
from . import credit_ledger, enforcers, jobs
from .base import AGY_SINK, discovery_host, discovery_locations, iso, parse_rfc3339, to_int
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
    "WORKFORCE_IDENTITY": ("The user signs in through Workforce Identity Federation (an external identity "
                           "provider). Usage and cost are counted, but per-user caps swap direct user: bindings "
                           "and cannot act on workforce principals. Restrict access through the identity provider "
                           "or the workforce pool bindings."),
    "DENY_POLICY_UNREADABLE": ("The managed deny policy could not be read at every attachment point, so suspensions "
                               "are unknown. Grant Deny Reviewer (roles/iam.denyReviewer) on the organization to the "
                               "dashboard service account, and Deny Admin (roles/iam.denyAdmin) to the identity that "
                               "applies suspensions."),
}
NON_USER_PREFIXES = ("group:", "domain:", "principalset:", "principal:")
NON_USER_EXACT = ("allusers", "allauthenticatedusers")
REQUIRED_PERMISSIONS = ("resourcemanager.projects.getIamPolicy", "resourcemanager.projects.setIamPolicy",
                        "logging.logEntries.create", "iam.roles.create")
WEBHOOK_HOSTS = ("hooks.slack.com", "chat.googleapis.com")
_WRITE_LOCK = threading.Lock()

BACKENDS = ("iam_allow_swap", "iam_deny", "notify_only")
NOTIFY_ONLY_MESSAGE = "Notify only: no access was changed."
NOTIFY_ONLY_REFUSED = ("The enforcement method is notify only, so the dashboard does not change access. Change it in "
                       "Settings > Governance policy.")
MISSING_ACCOUNT_MESSAGE = ("IAM does not find this account (deleted, or not a Google account), so it cannot be listed in "
                           "a deny policy. Remove its rule or check the address.")


def enforcement_backend(settings):
    """governance.antigravity.enforcement_backend: iam_allow_swap (default), iam_deny or notify_only."""
    ag = ((settings or {}).get("governance") or {}).get("antigravity") or {}
    value = ag.get("enforcement_backend")
    return value if value in BACKENDS else "iam_allow_swap"


# ---------------------------------------------------------------------------
# Configuration and policy index (pure)
# ---------------------------------------------------------------------------

# In Google Cloud IAM, roles/discoveryengine.user ("Discovery Engine User") has the exact same 135 permissions as
# roles/discoveryengine.agentspaceUser ("Gemini Enterprise User"), including every businessaicode.* permission.
# Administrators often grant one or both in the console. roles/businessaicode.user (9 permissions) is a strict subset
# whose non-businessaicode.* permissions are also in the marker roles, whereas roles/discoveryengine.podcastApiUser
# adds podcast permissions that a marker role would drop.
EQUIVALENT_ACCESS_ROLES = ("roles/discoveryengine.agentspaceUser", "roles/discoveryengine.user")
SUBSET_ACCESS_ROLES = ("roles/businessaicode.user",)
BUILTIN_EXTRA_ACCESS_ROLES = ("roles/discoveryengine.podcastApiUser",)


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

    @property
    def access_roles(self):
        if self.access_role in EQUIVALENT_ACCESS_ROLES:
            return (self.access_role, *(r for r in EQUIVALENT_ACCESS_ROLES if r != self.access_role))
        return (self.access_role,)

    @property
    def replaceable_roles(self):
        if self.access_role in EQUIVALENT_ACCESS_ROLES:
            return (*self.access_roles, *SUBSET_ACCESS_ROLES)
        return self.access_roles

    @property
    def all_extra_access_roles(self):
        own = set(self.access_roles) | set(self.privileged_roles) | self.markers
        out = list(self.extra_access_roles)
        for role in BUILTIN_EXTRA_ACCESS_ROLES:
            if role not in own and role not in out:
                out.append(role)
        return tuple(out)


def utcnow():
    """The current time (aware UTC). The caps drill (controlplane/caps_drill.py) replaces it to run a month of
    scheduled enforcement in seconds; nothing else does."""
    return dt.datetime.now(dt.timezone.utc)


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


def period_anchor(settings):
    """Keyword arguments that locate calendar period boundaries: the reset time zone (empty = the
    workspace time zone), the weekly reset weekday, the reset time, the monthly start day and the length and first day
    of custom periods."""
    ag = settings["governance"]["antigravity"]
    tz = ag.get("reset_timezone") or (settings.get("workspace") or {}).get("timezone") or "UTC"
    return {"tz": tz, "weekday": ag.get("reset_weekday", 0) or 0, "at": ag.get("reset_time") or "00:00",
            "day": timeutil.reset_day_of_month(ag.get("reset_day", 1)),
            "cycle_days": timeutil.cycle_length(ag.get("cycle_days", timeutil.CYCLE_DAYS_DEFAULT)),
            "cycle_start": str(ag.get("cycle_start") or "")}


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
    cached_models: tuple = ()  # ((model id or prefix, cached-token price), ...), longest first

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

    def cached_price(self, model, input_price):
        """(price, known) per million prompt tokens served from the prompt cache. Entered cached prices
        first, then the automatic price table. Without one the input price applies (known False), so
        cached tokens are never estimated below what they can cost."""
        key = str(model or "").lower()
        for prefix, pcached in self.cached_models:
            if key and (key == prefix or key.startswith(prefix)):
                return pcached, True
        if any(key and (key == prefix or key.startswith(prefix)) for prefix, _, _ in self.models):
            return input_price, False
        if self.source == "auto" and self.table is not None and model:
            hit = self.table.lookup(model) or {}
            if hit.get("cached") is not None:
                return float(hit["cached"]), True
        return input_price, False

    def lookup(self, model):
        """Public price entry for one model id, or None when unpriced."""
        pin, pout, priced = self.price(model)
        if not (pin or pout):
            return None
        key = str(model or "").lower()
        source = "manual"
        if self.source == "auto" and not any(key and (key == p or key.startswith(p)) for p, _, _ in self.models):
            source = (self.table.lookup(model) or {}).get("source", "catalog")
        out = {"input": round(pin, 6), "output": round(pout, 6), "source": source, "complete": priced}
        pcached, known = self.cached_price(model, pin)
        if known:
            out["cached"] = round(pcached, 6)
        return out

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


def _manual_cached(ag):
    out = []
    for item in ag.get("model_prices") or []:
        if isinstance(item, dict) and item.get("model") and item.get("cached") not in (None, ""):
            out.append((str(item["model"]).lower(), float(item["cached"])))
    out.sort(key=lambda m: (-len(m[0]), m[0]))
    return tuple(out)


def pricing_from(ag, table=None, currency=None):
    """Pricing for the saved policy. table is the automatic price table (services.pricing) or None. currency overrides
    the policy currency (credit caps are always in USD, like the credit itself)."""
    source = ag.get("pricing_source") or "auto"
    currency = currency or ag.get("currency") or ""
    if source == "auto" and table is not None:
        currency = table.currency
    return Pricing(currency=currency or "USD",
                   input_per_million=float(ag.get("input_price_per_million") or 0),
                   output_per_million=float(ag.get("output_price_per_million") or 0), models=_manual_models(ag),
                   source=source, table=table if source == "auto" else None, cached_models=_manual_cached(ag))


def estimate_cost(rec, pricing):
    """(cost, partial) of one user's usage in the pricing currency, per model: prompt tokens served from
    the prompt cache (cachedContentTokenCount, a subset of promptTokenCount) at the cached-token price,
    the other prompt tokens at the input price, and every other token (candidates and thinking) at the
    output price. This follows how the calls are billed; cache writes are not in the log and count as
    input. cost is None when no price is available (or none of the user's tokens could be priced);
    partial is True when some tokens belong to a model without a price (those tokens add nothing)."""
    if not pricing.configured:
        return None, False
    from . import models as models_service
    parts = list((rec.get("byModel") or {}).items()) or [(None, rec)]
    total = 0.0
    partial = False
    priced_tokens = 0
    used_tokens = 0
    for model, agg in parts:
        tokens = to_int(agg.get("tokens"))
        if model and models_service.is_non_token_model(model):
            continue
        prompt = max(0, to_int(agg.get("promptTokens")))
        cached = min(max(0, to_int(agg.get("cachedTokens"))), prompt)
        output = max(tokens - prompt, to_int(agg.get("candidateTokens")), 0)
        pin, pout, priced = pricing.price(model)
        pcached, _known = pricing.cached_price(model, pin)
        used_tokens += tokens
        if pin or pout:
            priced_tokens += tokens
        if not priced and tokens > 0:
            partial = True
        total += (prompt - cached) * pin / 1e6 + cached * pcached / 1e6 + output * pout / 1e6
    if used_tokens > 0 and priced_tokens == 0:
        return None, True
    return round(total, 6), partial


# ---------------------------------------------------------------------------
# Credit caps: weekly free credit (Gemini models) and a monthly paid budget, in USD (services/credit_ledger.py)
# ---------------------------------------------------------------------------

CREDIT_MODEL = "credit_budget"
PERIOD_MODEL = "period"


PERIOD_CAP_KEYS = ("cost_cap", "token_cap", "request_cap")
CREDIT_BUDGET_KEYS = ("paid_budget_month", "org_paid_budget_month")


def _positive(value):
    try:
        return float(value or 0) > 0
    except (TypeError, ValueError):
        return False


def credit_mode(ag):
    """True when caps follow the Antigravity credit (cap_model "credit_budget"); False for the fixed caps per period
    (cap_model "period"), where a person who reaches a cap stays suspended until the configured period ends (for
    example the end of the month) and nothing resets weekly. When cap_model was never chosen (empty) the fixed period
    caps apply (the default), except that an installation of an earlier version that relied on credit caps (a paid
    budget set and no fixed cap) keeps them: an upgrade must never drop caps a customer relies on."""
    model = ag.get("cap_model") or ""
    if model:
        return model == CREDIT_MODEL
    if any(_positive(ag.get(key)) for key in PERIOD_CAP_KEYS):
        return False
    return any(usd_budget(ag.get(key)) is not None for key in CREDIT_BUDGET_KEYS)


def usd_budget(value):
    """A USD budget from settings or a rule: None (no limit) for a negative or missing value, 0 = credits only."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return None if amount != amount or amount < 0 else amount


def credit_cycle(settings):
    anchor = period_anchor(settings)
    return credit_ledger.Cycle(tz=anchor["tz"], weekday=int(anchor["weekday"] or 0), at=anchor["at"])


def credit_prefixes(ag):
    return tuple(p for p in (ag.get("credit_model_prefixes") or credit_ledger.CREDIT_MODEL_PREFIXES) if p)


def credit_terms(ag, exc, tier_credit=None):
    """(monthly credit, monthly paid budget or None) for one person: the credit of their license tier when known
    (tier_credit), else credit_per_user_month; an individual limit rule replaces both."""
    credit = float(ag.get("credit_per_user_month") or 0) if tier_credit is None else float(tier_credit)
    budget = usd_budget(ag.get("paid_budget_month"))
    if exc and exc.get("type") == "limit":
        if exc.get("creditMonth") is not None:
            credit = float(exc["creditMonth"])
        if exc.get("paidBudget") is not None:
            budget = usd_budget(exc["paidBudget"])
    return max(0.0, credit), budget


def credit_source_of(exc, tier):
    """Where a person's monthly credit comes from: an individual rule, their license tier, or the fixed default."""
    if exc and exc.get("type") == "limit" and exc.get("creditMonth") is not None:
        return "rule"
    return "license" if tier else "fixed"


def license_credits(ctx):
    """{person: (tier, monthly credit in USD)} from assigned Gemini Enterprise licenses whose subscription tier is in
    credit_by_tier, when credit_source is "license". Empty when the source is "fixed" or licenses cannot be read:
    everyone then gets credit_per_user_month. A person with licenses of several tiers gets the largest credit."""
    ag = ctx.settings["governance"]["antigravity"]
    if (ag.get("credit_source") or "license") != "license":
        return {}
    amounts = config.tier_amounts(ag.get("credit_by_tier"))
    if not amounts:
        return {}
    try:
        from . import gemini_enterprise
        index = gemini_enterprise.license_index(ctx)
    except Exception as err:  # pylint: disable=broad-except
        LOG.warning("license tiers unavailable: %s", type(err).__name__)
        return {}
    out = {}
    for person, items in index.people.items():
        best = None
        for item in items:
            tier = item.get("tier")
            if tier in amounts and (best is None or amounts[tier] > best[1]):
                best = (tier, amounts[tier])
        if best is not None:
            out[person] = best
    return out


def split_cost(rec, pricing, prefixes=credit_ledger.CREDIT_MODEL_PREFIXES):
    """(gemini_cost, other_cost, known, partial) of one usage record in the pricing currency. Gemini models draw on
    the included credit; every other model (Claude) is paid. known is False when tokens were used but none of a part
    could be priced (its cost then counts as 0 and the caps do not act on it)."""
    gemini, other = {}, {}
    for model, agg in (rec.get("byModel") or {}).items():
        (gemini if credit_ledger.credit_eligible(model, prefixes) else other)[model] = agg
    costs, known, partial = [], True, False
    for part in (gemini, other):
        if not part:
            costs.append(0.0)
            continue
        cost, part_partial = estimate_cost({"byModel": part}, pricing)
        partial = partial or part_partial
        if cost is None:
            known = False
            cost = 0.0
        costs.append(cost)
    return costs[0], costs[1], known, partial


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
    """Classifies one user into (state, reason) following the agy-admin rules. `email` is a person key: a Google
    Account e-mail, or a workforce identity principal (never enforceable through direct user: bindings)."""
    if not idx.readable:
        return UNKNOWN, "POLICY_UNREADABLE"
    if identity.is_workforce_key(email):
        return NE, "WORKFORCE_IDENTITY"
    direct = idx.user_bindings(email)
    roles = {role for role, _ in direct}
    if roles & set(cfg.privileged_roles):
        return NE, "PRIVILEGED_ROLE"
    if roles & set(cfg.all_extra_access_roles):
        return NE, "OTHER_ACCESS_ROLE"
    markers = cfg.markers
    access_set = set(cfg.access_roles)
    subset_only = SUBSET_ACCESS_ROLES if cfg.access_role in EQUIVALENT_ACCESS_ROLES else ()
    has_access = bool(roles & access_set)
    has_subset = bool(roles & set(subset_only))
    has_marker = bool(roles & markers)
    managed = access_set | set(subset_only) | markers
    conditional = any(cond for role, cond in direct if role in managed)
    if ((has_access or has_subset) and has_marker) or conditional:
        return NE, "CONFLICTING_BINDINGS"
    if has_subset and not has_access:
        return NE, "OTHER_ACCESS_ROLE"
    if cfg.admin_marker in roles or roles & set(cfg.legacy_markers):
        return BLOCKED, None
    if cfg.cap_marker in roles:
        return CAPPED, None
    if has_access:
        return EA, None
    if has_usage:
        non_user_roles = (*cfg.access_roles, *subset_only, *cfg.all_extra_access_roles)
        if any(grants_via_non_user(idx.non_user_members(r), email) for r in non_user_roles):
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
             mode="auto", now=None, telemetry_truncated=False, backend="iam_allow_swap", deny=None):
    """Computes gates and the plan. `users` items need uid, state and overCap; optional
    `exception` ({"type": allow|block|limit}) and `suspension` ({"until": iso}) refine the plan:
    - block exception: users with access (EA or CAPPED) are disallowed in every cycle.
    - allow exception: never capped; CAPPED users are restored (an explicit administrator decision,
      so not gated by G3).
    - a suspension whose period has ended is restored regardless of telemetry (the usage window
      restarted), unless usage is known to be over the cap again.
    - mode "manual": over-cap users go to the review queue instead of the block list.
    - telemetry_truncated: usage was read only partly (log read cap), so counts are lower bounds. Over-cap
      results stay valid, but usage-based restores are withheld (G3 warns).
    - approved (iam_deny): an administrator approved the suspension of this over-cap user (review decision), so it
      is planned even in manual mode. forceReview: the user is over only because of an organization unit budget with
      the review action, so the user goes to the review queue even in automatic mode. hold: a suspended user stays
      suspended until the recorded suspension ends (no usage-based restore).
    backend iam_deny adds G6 (denied principals per attachment point) and G7 (surge guard) from `deny`:
    {"attachments": [attachment points], "unreadable": n, "foreign": n, "current": people denied now,
     "surgeMin": n, "surgeRatio": r}."""
    now = now or utcnow()
    population = len(users)
    determinate = sum(1 for u in users if u["state"] != UNKNOWN)
    deny = deny or {}
    gates = []
    gates.append({"id": "G1_POLICY_READABLE", "status": "pass" if policy_readable else "fail",
                  "detail": _g1_detail(backend, policy_readable, deny)})
    g2_ok = population == 0 or determinate > 0
    gates.append({"id": "G2_ANY_DETERMINATE", "status": "pass" if g2_ok else "fail",
                  "detail": "%d of %d users have a determinate state." % (determinate, population) if population
                  else "No users are in scope."})
    g3_ok = bool(telemetry_ok) and telemetry_records > 0 and not telemetry_truncated
    if g3_ok:
        g3_detail = "%d inference log records in the window." % telemetry_records
    elif not telemetry_ok:
        g3_detail = "The usage query failed; automatic restores are withheld."
    elif telemetry_truncated:
        g3_detail = ("Inference log entries were truncated at max_log_entries, so usage is a lower bound; "
                     "automatic restores are withheld. Route the log to BigQuery for complete counts.")
    else:
        g3_detail = "No inference log records in the window; automatic restores are withheld."
    gates.append({"id": "G3_TELEMETRY_HEALTHY", "status": "pass" if g3_ok else "warn", "detail": g3_detail})
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
            if u.get("approved"):
                block.append(uid)
            elif mode == "manual" or u.get("forceReview"):
                review.append(uid)
            else:
                block.append(uid)
        elif state == CAPPED and over is not True:
            # Restores require known (non-null) usage below the caps and healthy telemetry (G3),
            # or a suspension whose settlement period has ended.
            if suspension_ended(u.get("suspension"), now) or (over is False and g3_ok and not u.get("hold")):
                restore.append(uid)
            elif not g3_ok:
                withheld = True
    severity = {u["uid"]: u.get("capUsage") or 0 for u in users}
    approved = {u["uid"] for u in users if u.get("approved")}
    # G7 (iam_deny): a deny policy stops access however it is granted, so a sudden wave of automatic suspensions (a
    # telemetry or pricing glitch) is held for review instead of applied. Approved suspensions and block rules are
    # administrator decisions and are not held.
    g7 = None
    if backend == "iam_deny":
        smin = max(1, to_int(deny.get("surgeMin") or 10))
        sratio = float(deny.get("surgeRatio") or 0.5)
        auto = [uid for uid in block if uid not in approved]
        if auto and len(auto) >= smin and len(auto) >= sratio * population:
            review.extend(auto)
            block = [uid for uid in block if uid in approved]
            g7 = {"id": "G7_SURGE_GUARD", "status": "warn",
                  "detail": "%d new suspensions of %d people reach the surge guard (%d people and %s%%), so nobody is "
                            "suspended in this run." % (len(auto), population, smin, _pct(sratio))}
        else:
            g7 = {"id": "G7_SURGE_GUARD", "status": "pass",
                  "detail": "%d new suspensions of %d people; the surge guard starts at %d people and %s%%."
                            % (len(auto), population, smin, _pct(sratio))}
    # The change budget limits how many people one run suspends: a circuit breaker against a telemetry or pricing
    # glitch suspending everyone at once. Suspensions above it are deferred to the next run (block rules first, then
    # the people furthest over their limits), so a real wave, such as an organization budget being spent or a new
    # credits-only policy, is still applied within a few runs. Restores give access back and are never limited: at a
    # weekly reset every suspended person is restored in the same run, however many there are.
    budget = max(cfg.change_budget_min, int(math.ceil(cfg.change_budget_ratio * population)))
    planned = len(block) + len(disallow)
    g4_ok = planned <= budget
    deferred = []
    if not g4_ok:
        # stable: approved suspensions first, then the people furthest over; equal severity keeps the population order
        block.sort(key=lambda uid: (uid not in approved, -severity.get(uid, 0)))
        keep = disallow[:budget]
        room = budget - len(keep)
        deferred = disallow[len(keep):] + block[room:]
        disallow, block = keep, block[:room]
    gates.append({"id": "G4_CHANGE_BUDGET", "status": "pass" if g4_ok else "warn",
                  "detail": "%d planned suspensions; budget is %d per run." % (planned, budget) if g4_ok else
                  "%d planned suspensions; budget is %d per run, so %d wait for the next run."
                  % (planned, budget, len(deferred))})
    if backend == "iam_deny":
        gates.append({"id": "G5_NON_USER_GRANT_ABSENT", "status": "pass",
                      "detail": "Deny policies also stop access granted through groups, domains and inherited roles."})
    elif backend == "notify_only":
        gates.append({"id": "G5_NON_USER_GRANT_ABSENT", "status": "pass", "detail": "Notify only: nobody loses access."})
    else:
        gates.append({"id": "G5_NON_USER_GRANT_ABSENT", "status": "pass" if not non_user_on_access else "warn",
                      "detail": "No group or domain grants on %s." % access_role if not non_user_on_access else
                      "%d group or domain grant(s) on %s; affected users are not enforceable."
                      % (len(non_user_on_access), access_role)})
    # G6 (iam_deny): one resource holds at most DENY_PRINCIPAL_LIMIT denied principals. Every attachment point lists
    # the same people, so the count after this run is the people denied now, minus restores, plus new suspensions.
    # New suspensions beyond the limit wait (the least severe first); restores always go through.
    held = []
    if backend == "iam_deny":
        limit, warn_at = enforcers.DENY_PRINCIPAL_LIMIT, enforcers.DENY_WARN_AT
        states = {u["uid"]: u["state"] for u in users}
        fresh = [uid for uid in disallow + block if states.get(uid) == EA]
        after = max(0, to_int(deny.get("current")) - len(restore)) + len(fresh)
        where = _attachment_label((deny.get("attachments") or [""])[0])
        if after > limit and fresh:
            order = sorted((uid for uid in block if states.get(uid) == EA),
                           key=lambda uid: (uid in approved, severity.get(uid, 0)))
            order += [uid for uid in reversed(disallow) if states.get(uid) == EA]
            held = order[:min(len(fresh), after - limit)]
            block = [uid for uid in block if uid not in held]
            disallow = [uid for uid in disallow if uid not in held]
            gates.append({"id": "G6_DENY_CAPACITY", "status": "warn",
                          "detail": "%d new suspensions wait because %s would go over the limit of %d people."
                                    % (len(held), where, limit)})
        elif after >= warn_at:
            gates.append({"id": "G6_DENY_CAPACITY", "status": "warn",
                          "detail": "%d people at %s; a warning starts at %d and the limit is %d."
                                    % (after, where, warn_at, limit)})
        else:
            gates.append({"id": "G6_DENY_CAPACITY", "status": "pass",
                          "detail": "At most %d people per attachment point; the limit is %d." % (after, limit)})
    if g7 is not None:
        gates.append(g7)
    failed = [g for g in gates if g["status"] == "fail"]
    plan = {"block": block, "restore": restore, "disallow": disallow, "review": review, "manual": manual,
            "withheldRestores": withheld, "deferredBlocks": deferred, "changeBudget": budget,
            "capacityHeld": held, "aborted": bool(failed),
            "abortReason": "; ".join("%s: %s" % (g["id"], g["detail"]) for g in failed) if failed else None}
    return gates, plan, budget


def _pct(ratio):
    """A ratio as a percentage without trailing zeros: 0.5 -> "50", 0.125 -> "12.5"."""
    return "%g" % round(float(ratio) * 100, 1)


def _attachment_label(attachment):
    """An attachment point without the service prefix (projects/p, folders/1, organizations/1)."""
    text = str(attachment or "")
    return text.split("googleapis.com/", 1)[-1] or "-"


def _g1_detail(backend, readable, deny):
    if backend == "notify_only":
        return "Notify only: no IAM policy is read or changed."
    if backend != "iam_deny":
        return "Project IAM policy read (version 3)." if readable else "The project IAM policy could not be read."
    total = len(deny.get("attachments") or [])
    if readable:
        return "Deny policies read at %d attachment point(s)." % total
    if not total:
        return "No deny policy attachment point is configured: the workspace has no valid project or scope."
    if deny.get("unreadable"):
        return "The deny policy could not be read at %d of %d attachment points." % (deny["unreadable"], total)
    return ("The deny policy at %d of %d attachment points was changed outside the dashboard, so the dashboard does "
            "not change it." % (deny.get("foreign") or 0, total))


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
            for role in cfg.replaceable_roles:
                _remove_member(new, role, member)
            _add_member(new, cfg.cap_marker, member)
        elif action == "restore":
            _remove_member(new, cfg.cap_marker, member)
            _add_member(new, cfg.access_role, member)
        elif action == "disallow":
            for role in (*cfg.replaceable_roles, cfg.cap_marker, *cfg.legacy_markers):
                _remove_member(new, role, member)
            _add_member(new, cfg.admin_marker, member)
        elif action == "allow":
            for role in (*cfg.replaceable_roles, cfg.cap_marker, cfg.admin_marker, *cfg.legacy_markers):
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


def make_ancestor_check(ctx, cfg, extra_prefixes=()):
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
        roles = {*cfg.replaceable_roles, *cfg.privileged_roles, *cfg.all_extra_access_roles}
        member = "user:%s" % email.lower()
        for pol in value["policies"]:
            for binding in pol.get("bindings") or []:
                b_role = str(binding.get("role") or "")
                if b_role not in roles and not (extra_prefixes and b_role.startswith(extra_prefixes)):
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


# A person is stopped by replacing the access role with a marker role: a custom role with the permissions of the
# access role except businessaicode.* (Antigravity), so the Gemini Enterprise app keeps working. Which marker a person
# holds also records why they are stopped: the cap marker (over a limit, restored at the reset) or the admin marker
# (blocked by an administrator until unblocked). The deployment creates both roles. When an administrator's action
# needs one that is missing or deleted, the helpers below create or restore it instead of refusing the action.

MARKER_CREATE_ATTEMPTS = 5  # every attempt drops the permissions Google Cloud rejects in custom roles (as deploy.sh)


def _marker_meta(cfg):
    return {cfg.cap_marker_id: ("Antigravity Cap Blocked",
                                "Assigned by AI Control Plane when a user exceeds the Antigravity cap. "
                                "Same as the access role without businessaicode permissions."),
            cfg.admin_marker_id: ("Antigravity Admin Blocked",
                                  "Assigned by an administrator to disallow Antigravity. "
                                  "Same as the access role without businessaicode permissions.")}


def _marker_result(role_id, status, message):
    return {"role": role_id, "status": status, "message": message}


def _marker_failed(role_id, message, kind, reason=None):
    """A failed result. kind is the upstream error kind; reason is what marker_unavailable() explains: denied,
    deleted, reserved, base_role or error."""
    return {"role": role_id, "status": "failed", "message": message, "kind": kind,
            "reason": reason or ("denied" if kind == "denied" else "error")}


def _marker_permissions(ctx, cfg):
    """Permissions of a marker role: those of the access role without businessaicode.* (Antigravity). None when
    the access role cannot be read."""
    try:
        role = ctx.rest("GET", "%s/%s" % (IAM_V1, cfg.access_role))
    except GcpError as err:
        LOG.warning("could not read %s kind=%s", cfg.access_role, err.kind)
        return None
    return [p for p in role.get("includedPermissions") or [] if not p.startswith("businessaicode.")]


def _create_marker(ctx, role_id, meta, permissions):
    """Creates one marker role. Returns (result, taken); taken is True when Google Cloud answers that the role ID
    is in use, which _ensure_marker resolves by reading the role again."""
    title, description = meta
    perms = list(permissions)
    for _ in range(MARKER_CREATE_ATTEMPTS):
        try:
            ctx.rest("POST", "%s/projects/%s/roles" % (IAM_V1, ctx.project),
                     json_body={"roleId": role_id, "role": {"title": title, "description": description,
                                                            "includedPermissions": perms, "stage": "GA"}})
            return _marker_result(role_id, "created",
                                  "Created with %d permissions (businessaicode.* excluded)." % len(perms)), False
        except GcpError as err:
            bad = [p for p in perms if p in (err.message or "")]
            if err.kind == "invalid" and bad:
                perms = [p for p in perms if p not in bad]
                continue
            return (_marker_failed(role_id, "The role could not be created (%s)." % err.kind, err.kind),
                    err.kind == "conflict")
    return _marker_failed(role_id, "The role could not be created.", "invalid"), False


def _ensure_marker(ctx, cfg, role_id, base_permissions):
    url = "%s/projects/%s/roles/%s" % (IAM_V1, ctx.project, urllib.parse.quote(role_id, safe=""))
    for _ in (1, 2):
        try:
            existing = ctx.rest("GET", url)
        except GcpError as err:
            if err.kind != "not_found":
                return _marker_failed(role_id, "The role could not be read (%s)." % err.kind, err.kind)
            existing = None
        if existing is not None and not existing.get("deleted"):
            return _marker_result(role_id, "exists", "The role already exists.")
        if existing is not None:
            # Deleted in the last 7 days: Google Cloud still restores it, and the bindings on it work again.
            body = {"etag": existing["etag"]} if existing.get("etag") else {}
            try:
                ctx.rest("POST", url + ":undelete", json_body=body)
            except GcpError as err:
                return _marker_failed(role_id, "The deleted role could not be restored (%s)." % err.kind, err.kind,
                                      reason="denied" if err.kind == "denied" else "deleted")
            return _marker_result(role_id, "restored", "The deleted role was restored.")
        permissions = base_permissions()
        if not permissions:
            return _marker_failed(role_id, "The permissions of %s could not be read." % cfg.access_role, "unreadable",
                                  reason="base_role")
        result, taken = _create_marker(ctx, role_id, _marker_meta(cfg)[role_id], permissions)
        if not taken:
            return result
        # Taken: created by someone else a moment ago (the next read finds it) or the ID of a deleted role.
    # The ID is in use although no role can be read. Google Cloud keeps the ID of a deleted role unusable while it
    # removes the role for good, up to 44 days after the deletion.
    return _marker_failed(role_id, "The role ID is still reserved by a deleted role.", "conflict", reason="reserved")


def ensure_marker_roles(ctx, cfg, role_ids=None):
    """Makes the marker roles usable (both by default): an existing role is left as it is, a role deleted in the
    last 7 days is restored, a missing role is created. One result per role: {"role", "status", "message"} with
    status exists | restored | created | failed; failed results also carry "kind" and "reason" (_marker_failed).
    The caller holds _WRITE_LOCK."""
    loaded = {}

    def base_permissions():
        # Read once, and only when a role has to be created.
        if "value" not in loaded:
            loaded["value"] = _marker_permissions(ctx, cfg)
        return loaded["value"]
    return [_ensure_marker(ctx, cfg, role_id, base_permissions)
            for role_id in (role_ids or (cfg.cap_marker_id, cfg.admin_marker_id))]


def marker_unavailable(result, cfg):
    """What an administrator reads when an action needs a marker role that could not be made usable: what the role
    is for, why it is not there and what to do about it."""
    lead = "Role %s is needed to stop Antigravity for a person, but " % result["role"]
    reason, kind = result.get("reason"), result.get("kind")
    if reason == "denied":
        return lead + ("the dashboard service account is not allowed to read or create custom roles in this project. "
                       "Grant it roles/iam.roleAdmin and try again.")
    if reason == "deleted":
        return lead + ("it is deleted and could not be restored (%s). Restore it in IAM > Roles, or enter another "
                       "role ID in Settings > Governance policy." % kind)
    if reason == "reserved":
        return lead + ("Google Cloud does not accept this role ID right now, which happens for up to 44 days after a "
                       "role with the same ID was deleted. Enter another role ID in Settings > Governance policy.")
    if reason == "base_role":
        return lead + "it could not be created because the permissions of %s could not be read." % cfg.access_role
    return lead + "it could not be prepared (%s). Try again in a moment." % kind


def _setup_markers(ctx, cfg, trigger):
    """Creates or restores both marker roles and records it in the audit log. Returns (results, audit_logged)."""
    results = ensure_marker_roles(ctx, cfg)
    logged = write_audit(ctx, "setup", {
        "result": "success" if all(r["status"] != "failed" for r in results) else "partial",
        "target": "marker roles", "results": results, "trigger": trigger})
    return results, logged


def prepare_markers(ctx, cfg, needed, known):
    """Before an administrator's action assigns marker roles: {role id: why it cannot be assigned} for the roles in
    `needed` that are unusable (empty when all can be assigned). `known` is {role id: exists} as read by
    marker_status. When a needed role is not known to exist, both marker roles are created or restored exactly as
    setup() does. The caller holds _WRITE_LOCK."""
    if all(known.get(role_id) is True for role_id in needed):
        return {}
    results, _ = _setup_markers(ctx, cfg, "on_demand")
    if any(r["status"] in ("created", "restored") for r in results):
        ctx.app.invalidate_governance()  # the status page lists the roles as present from now on
    return {r["role"]: marker_unavailable(r, cfg) for r in results if r["status"] == "failed" and r["role"] in needed}


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

# Antigravity inference logs name the caller in labels.user_id as an IAM principal: "user:alice@example.com" (the
# documented format; older entries carried the bare email) or, for Workforce Identity Federation users, the workforce
# principal URI. Usage is keyed by identity.person_key: the plain lowercase email, the same key as the direct "user:"
# bindings of the IAM policy (PolicyIndex.direct_users), so caps, the user list and IAM states line up instead of
# showing "user:..." duplicates that never match a binding; workforce users by their lowercase principal URI.
USER_PRINCIPAL_PREFIX_RE = r"(?i)^user:"


def agy_user_sql(user_expr):
    """BigQuery expression: the person key behind labels.user_id (lower-cased, a leading "user:" removed)."""
    return identity.sql_person_key(user_expr)


def usage_user(principal):
    """The person behind labels.user_id (identity.person_key), or None: service accounts ("serviceAccount:..." or a
    .gserviceaccount.com email) and other principal types get no per-user usage or caps. Workforce identities are
    people: their usage and cost are counted, while caps report them as not enforceable (WORKFORCE_IDENTITY)."""
    return identity.person_key(principal)


AGY_PAYLOAD_COLUMN = "jsonpayload_v1_inferenceresponselog"


def payload_paths(schema, path):
    """`path` under every payload column of a Log Router table.

    Log Router names the payload column of an entry whose jsonPayload carries an @type after that type, and lower-cases
    the field names: Antigravity's InferenceResponseLog lands in jsonpayload_v1_inferenceresponselog.metadata.
    prompttokencount, not jsonPayload.metadata.promptTokenCount (seen in a real sink table). Lookups are
    case-insensitive, so the documented camelCase path finds the lower-cased columns."""
    typed = sorted(node.name for name, node in ((schema.tree or {}).items() if schema else ())
                   if name.startswith("jsonpayload_"))
    typed.sort(key=lambda n: n.lower() != AGY_PAYLOAD_COLUMN)
    return ["%s.%s" % (col, path) for col in typed] + ["jsonPayload.%s" % path]


def agy_fields(src, alias="t"):
    s = src.schema
    user = expr(alias, s, "labels.user_id", cast="STRING")

    def tokens(name):
        return expr(alias, s, *payload_paths(s, "metadata.%s" % name), cast="INT64")
    return {
        "user": agy_user_sql(user) if user else None,
        # The label as logged: workforce subjects are case-sensitive, the key above is lower-cased (_note_principal).
        "principal": user,
        "client": expr(alias, s, "labels.client_name", cast="STRING"),
        "model": expr(alias, s, "labels.model_id", "labels.model", cast="STRING"),
        "total": tokens("totalTokenCount"),
        "prompt": tokens("promptTokenCount"),
        "candidates": tokens("candidatesTokenCount"),
        # Optional: the column exists only after the first call served from the prompt cache.
        "cached": tokens("cachedContentTokenCount"),
        "ts": "%s.%s" % (alias, quote_ident(src.time_column())),
    }


USAGE_COUNTERS = ("requests", "tokens", "promptTokens", "candidateTokens", "cachedTokens")


def _note_principal(usage, person, raw):
    """Keeps the workforce principal as logged (subject case intact) next to its lower-case key. Google documents
    google.subject, and so the workforce principal identifier, as case-sensitive: a deny policy entry built from the
    key would not match a subject with upper-case letters (_apply_deny). E-mail addresses are case-insensitive, so
    Google accounts need nothing."""
    if person in usage.principals or not isinstance(raw, str) or not identity.is_workforce_key(person):
        return
    if identity.person_key(raw) == person:
        usage.principals[person] = raw.strip()


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
        # {project: newest entry timestamp} of a BigQuery sink table read (see _logging_tail_ranges).
        self.project_last = {}
        # {workforce person key: principal as logged} (see _note_principal).
        self.principals = {}

    def user(self, email):
        return self.users.setdefault(email, {"requests": 0, "tokens": 0, "promptTokens": 0, "candidateTokens": 0,
                                             "cachedTokens": 0, "clients": set(), "models": set(), "last": None,
                                             "byModel": {}, "byProject": {}})

    @staticmethod
    def add_model(rec, model, requests, tokens, prompt, cand, cached=0):
        agg = rec["byModel"].setdefault(model or "unknown", {"requests": 0, "tokens": 0, "promptTokens": 0,
                                                             "candidateTokens": 0, "cachedTokens": 0})
        agg["requests"] += requests
        agg["tokens"] += tokens
        agg["promptTokens"] += prompt
        agg["candidateTokens"] += cand
        agg["cachedTokens"] = agg.get("cachedTokens", 0) + cached

    @staticmethod
    def add_project(rec, project, model, requests, tokens, prompt, cand, cached=0):
        """Adds one model's usage in one project to rec["byProject"] (Google bills per project)."""
        prec = rec.setdefault("byProject", {}).setdefault(project or "unknown", {
            "requests": 0, "tokens": 0, "promptTokens": 0, "candidateTokens": 0, "cachedTokens": 0, "byModel": {}})
        prec["requests"] += requests
        prec["tokens"] += tokens
        prec["promptTokens"] += prompt
        prec["candidateTokens"] += cand
        prec["cachedTokens"] += cached
        Usage.add_model(prec, model, requests, tokens, prompt, cand, cached)

    def merge(self, other):
        """Adds `other`, which must hold different log entries (Cloud Logging entries the sink table never received)."""
        self.records += other.records
        self.truncated = self.truncated or other.truncated
        for email, rec in other.users.items():
            u = self.user(email)
            for key in USAGE_COUNTERS:
                u[key] += rec.get(key) or 0
            u["clients"] |= set(rec.get("clients") or ())
            u["models"] |= set(rec.get("models") or ())
            if rec.get("last") is not None and (u["last"] is None or rec["last"] > u["last"]):
                u["last"] = rec["last"]
            for model, agg in (rec.get("byModel") or {}).items():
                Usage.add_model(u, model, agg.get("requests") or 0, agg.get("tokens") or 0,
                                agg.get("promptTokens") or 0, agg.get("candidateTokens") or 0,
                                agg.get("cachedTokens") or 0)
            for project, prec in (rec.get("byProject") or {}).items():
                for model, agg in (prec.get("byModel") or {}).items():
                    Usage.add_project(u, project, model, agg.get("requests") or 0, agg.get("tokens") or 0,
                                      agg.get("promptTokens") or 0, agg.get("candidateTokens") or 0,
                                      agg.get("cachedTokens") or 0)
        for mine, theirs in ((self.by_model, other.by_model), (self.by_client, other.by_client)):
            for key, agg in theirs.items():
                cur = mine.setdefault(key, {k: 0 for k in USAGE_COUNTERS})
                for k in USAGE_COUNTERS:
                    cur[k] = (cur.get(k) or 0) + (agg.get(k) or 0)
        for (person, bucket), (req, tok) in other.daily.items():
            if isinstance(bucket, str):
                bucket = dt.date.fromisoformat(bucket[:10])
            r0, t0 = self.daily.get((person, bucket), (0, 0))
            self.daily[(person, bucket)] = (r0 + req, t0 + tok)
        for person, raw in (getattr(other, "principals", None) or {}).items():
            self.principals.setdefault(person, raw)


_LOGNAME_PROJECT_RE = re.compile(r"^projects/([^/]+)/")


def entry_project(log_name, default=None):
    """The project id in a log entry name (projects/<id>/logs/<log>), or `default`."""
    m = _LOGNAME_PROJECT_RE.match(log_name or "") if isinstance(log_name, str) else None
    return m.group(1) if m else default


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
        where += " AND %s = @email" % f["user"]  # matches "user:<email>" and the bare email alike
        params["email"] = email
    lname = expr("t", src.schema, "logName", cast="STRING")
    project_sql = "REGEXP_EXTRACT(%s, r'^projects/([^/]+)/')" % lname if lname else null
    base = ("WITH %s,\nu AS (SELECT %s AS ts, %s AS email, %s AS principal, %s AS client, %s AS model, "
            "IFNULL(%s, 0) AS total, IFNULL(%s, 0) AS prompt, IFNULL(%s, 0) AS cand, IFNULL(%s, 0) AS cached, "
            "%s AS project FROM s t WHERE %s)"
            % (src.cte("s", start="w_start", end="w_end"), f["ts"], f["user"], f["principal"] or null,
               f["client"] or null, f["model"] or null, f["total"] or "0", f["prompt"] or "0", f["candidates"] or "0",
               f["cached"] or "0", project_sql, where))
    usage = Usage("bigquery")
    main_sql = (base + "\nSELECT email, model, project, COUNT(*) AS requests, SUM(total) AS tokens, "
                "SUM(prompt) AS prompt_tokens, SUM(cand) AS candidate_tokens, SUM(cached) AS cached_tokens, "
                "ARRAY_AGG(DISTINCT client IGNORE NULLS LIMIT 20) AS clients, "
                "ANY_VALUE(principal) AS principal, MAX(ts) AS last_ts\nFROM u GROUP BY email, model, project")
    tasks = {"main": lambda: ctx.query(main_sql, params, "antigravity")}
    if email:
        client_sql = (base + "\nSELECT client AS k, COUNT(*) AS requests, SUM(total) AS tokens, SUM(prompt) AS p, "
                      "SUM(cand) AS c, SUM(cached) AS ca FROM u GROUP BY k")
        tasks["client"] = lambda: ctx.query(client_sql, params, "antigravity")
    if rng is not None:
        from .base import bucket_sql
        bucket_params = dict(params, tz=rng.tz_name, start_date=rng.start, bucket_days=rng.bucket_days)
        bucket_query = (base + "\nSELECT %s AS bucket, email, COUNT(*) AS requests, SUM(total) AS tokens "
                        "FROM u GROUP BY bucket, email" % bucket_sql("ts"))
        tasks["bucket"] = lambda: ctx.query(bucket_query, bucket_params, "antigravity")
    # The per-person rows, the per-client totals and the per-day buckets read the same rows independently, so they
    # run together: one person's details used to wait for four jobs in a row. The main query's failure wins.
    if len(tasks) == 1:
        res = {"main": (True, tasks["main"]())}
    else:
        res = ctx.run(tasks)
        for name in tasks:
            ok, value = res[name]
            if not ok:
                raise value
    rows = res["main"][1]
    for row in rows:
        if not row.get("email"):
            continue
        requests, tokens = to_int(row.get("requests")), to_int(row.get("tokens"))
        prompt, cand = to_int(row.get("prompt_tokens")), to_int(row.get("candidate_tokens"))
        cached = to_int(row.get("cached_tokens"))
        usage.records += requests
        row_project, row_last = row.get("project"), row.get("last_ts")
        if isinstance(row_project, str) and isinstance(row_last, dt.datetime):
            seen = usage.project_last.get(row_project)
            if seen is None or row_last > seen:
                usage.project_last[row_project] = row_last
        person = usage_user(row["email"])
        if person is None:
            continue  # a service account or another non-user principal: telemetry, but no per-user usage
        _note_principal(usage, person, row.get("principal"))
        u = usage.user(person)
        u["requests"] += requests
        u["tokens"] += tokens
        u["promptTokens"] += prompt
        u["candidateTokens"] += cand
        u["cachedTokens"] += cached
        u["clients"] |= set(row.get("clients") or [])
        if row.get("model"):
            u["models"].add(row["model"])
        Usage.add_model(u, row.get("model"), requests, tokens, prompt, cand, cached)
        project = row.get("project") if isinstance(row.get("project"), str) else None
        Usage.add_project(u, project, row.get("model"), requests, tokens, prompt, cand, cached)
        last = row.get("last_ts")
        if last is not None and (u["last"] is None or last > u["last"]):
            u["last"] = last
    if email:
        # Per-model totals of the one person: the main rows already group by model (and project), so they are summed
        # here instead of a separate job.
        for row in rows:
            agg = usage.by_model.setdefault(row.get("model") or "unknown", {
                "requests": 0, "tokens": 0, "promptTokens": 0, "candidateTokens": 0, "cachedTokens": 0})
            agg["requests"] += to_int(row.get("requests"))
            agg["tokens"] += to_int(row.get("tokens"))
            agg["promptTokens"] += to_int(row.get("prompt_tokens"))
            agg["candidateTokens"] += to_int(row.get("candidate_tokens"))
            agg["cachedTokens"] += to_int(row.get("cached_tokens"))
        for row in res["client"][1]:
            usage.by_client[row.get("k") or "unknown"] = {"requests": to_int(row.get("requests")),
                                                         "tokens": to_int(row.get("tokens")),
                                                         "promptTokens": to_int(row.get("p")),
                                                         "candidateTokens": to_int(row.get("c")),
                                                         "cachedTokens": to_int(row.get("ca"))}
    if rng is not None:
        for row in res["bucket"][1]:
            person = usage_user(row.get("email"))
            if person is not None:
                usage.daily[(person, row.get("bucket"))] = (to_int(row.get("requests")), to_int(row.get("tokens")))
    usage.ok = True
    return usage


def _user_label_regex(email):
    """RE2 pattern for labels.user_id of one person: optional "user:" prefix, case-insensitive, exact email."""
    parts = []
    for ch in email.lower():
        parts.append(ch if (ch.isalnum() or ch in "@_") else "[%s]" % ch)
    return "(?i)^(user:)?%s$" % "".join(parts)


_LABEL_SAFE_PUNCT = frozenset(".-:/+=~!$&'()*,;%")


def _principal_label_regex(key):
    """RE2 pattern for labels.user_id of one workforce identity: the exact principal, case-insensitive. Symbols become
    character classes (no escapes); a principal with other symbols cannot be filtered on."""
    parts = []
    for ch in key:
        if ch.isalnum() or ch in "@_":
            parts.append(ch)
        elif ch in _LABEL_SAFE_PUNCT:
            parts.append("[%s]" % ch)
        else:
            raise GcpError("invalid", message="unsupported principal")
    return "(?i)^%s$" % "".join(parts)


def _log_ts(value):
    """A Cloud Logging filter timestamp: RFC 3339 UTC, with microseconds when the value has them (a tail read starts
    just after a sink table's newest entry, stored to the microsecond)."""
    value = value.astimezone(dt.timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if value.microsecond else iso(value)


def _logging_usage_project(ctx, project, log_id, start, end, email=None, rng=None):
    flt = 'logName="projects/%s/logs/%s" AND timestamp>="%s" AND timestamp<"%s"' % (
        project, log_id, _log_ts(start), _log_ts(end))
    if email:
        if identity.is_workforce_key(email):
            flt += ' AND labels.user_id=~"%s"' % _principal_label_regex(email)
        elif not config.EMAIL_RE.match(email):
            raise GcpError("invalid", message="invalid email")
        else:
            # labels.user_id is "user:<email>" (documented format); older entries carried the bare email. The match
            # is case-insensitive like IAM principals; dots and other symbols become character classes (no escapes).
            flt += ' AND labels.user_id=~"%s"' % _user_label_regex(email)
    ag = ctx.settings["governance"]["antigravity"]
    limit = int(ag.get("max_log_entries") or 20000)
    tz = timeutil.get_zone(ctx.tz_name)
    token = None
    fetched = 0
    entries_out = []
    truncated = False
    while True:
        body = {"resourceNames": ["projects/%s" % project], "filter": flt, "orderBy": "timestamp desc",
                "pageSize": min(1000, max(1, limit - fetched))}
        if token:
            body["pageToken"] = token
        data = ctx.rest("POST", "%s/entries:list" % LOGGING_V2, json_body=body, timeout=45)
        entries = data.get("entries") or []
        entries_out.extend(entries)
        fetched += len(entries)
        token = data.get("nextPageToken")
        if not token:
            break
        if fetched >= limit:
            truncated = True
            break
    return entries_out, truncated, tz


def _logging_usage(ctx, start, end, email=None, rng=None, ranges=None):
    """Usage from Cloud Logging. `ranges` ({project: (from, to)}) limits the read to those projects and time ranges."""
    ag = ctx.settings["governance"]["antigravity"]
    log_id = ag["inference_log"]
    if not config.LOG_NAME_RE.match(log_id):
        raise GcpError("invalid", message="invalid log id")
    projects = list(getattr(ctx, "projects", None) or [ctx.project])
    if ranges is not None:
        projects = [p for p in projects if p in ranges]
    else:
        ranges = {p: (start, end) for p in projects}
    usage = Usage("logging")
    tz = timeutil.get_zone(ctx.tz_name)
    all_entries = []
    if not projects:
        usage.ok = True
        return usage
    if len(projects) <= 1:
        proj = projects[0]
        lo, hi = ranges[proj]
        entries, truncated, tz = _logging_usage_project(ctx, proj, log_id, lo, hi, email=email, rng=rng)
        all_entries.extend((entry, proj) for entry in entries)
        usage.truncated = truncated
    else:
        tasks = {p: (lambda pr=p: _logging_usage_project(ctx, pr, log_id, ranges[pr][0], ranges[pr][1],
                                                         email=email, rng=rng))
                 for p in projects}
        res = ctx.run(tasks)
        for p in projects:
            ok, val = res[p]
            if not ok:
                if p == ctx.project:
                    raise val
                continue
            entries, truncated, _ = val
            all_entries.extend((entry, p) for entry in entries)
            if truncated:
                usage.truncated = True
    for entry, read_project in all_entries:
        labels = entry.get("labels") or {}
        principal = labels.get("user_id")
        if not isinstance(principal, str) or not principal.strip():
            continue
        usage.records += 1
        user = usage_user(principal)  # "user:alice@example.com" -> "alice@example.com"
        if user is None:
            continue  # a service account or another non-user principal: telemetry, but no per-user usage
        _note_principal(usage, user, principal)
        meta = ((entry.get("jsonPayload") or {}).get("metadata") or {})
        total = to_int(meta.get("totalTokenCount"))
        prompt = to_int(meta.get("promptTokenCount"))
        cand = to_int(meta.get("candidatesTokenCount"))
        cached = to_int(meta.get("cachedContentTokenCount"))
        client = labels.get("client_name")
        model = labels.get("model_id") or labels.get("model")
        ts = parse_rfc3339(entry.get("timestamp"))
        u = usage.user(user)
        u["requests"] += 1
        u["tokens"] += total
        u["promptTokens"] += prompt
        u["candidateTokens"] += cand
        u["cachedTokens"] += cached
        if client:
            u["clients"].add(client)
        if model:
            u["models"].add(model)
        Usage.add_model(u, model, 1, total, prompt, cand, cached)
        Usage.add_project(u, entry_project(entry.get("logName"), read_project), model, 1, total, prompt, cand,
                          cached)
        if ts and (u["last"] is None or ts > u["last"]):
            u["last"] = ts
        if email:
            for key, target in ((model or "unknown", usage.by_model), (client or "unknown", usage.by_client)):
                agg = target.setdefault(key, {"requests": 0, "tokens": 0, "promptTokens": 0, "candidateTokens": 0,
                                              "cachedTokens": 0})
                agg["requests"] += 1
                agg["tokens"] += total
                agg["promptTokens"] += prompt
                agg["candidateTokens"] += cand
                agg["cachedTokens"] += cached
        if rng is not None and ts is not None:
            bucket_idx = rng.bucket_index(ts.astimezone(tz).date())
            if bucket_idx is not None:
                bucket = rng.buckets()[bucket_idx]
                req, tok = usage.daily.get((user, bucket), (0, 0))
                usage.daily[(user, bucket)] = (req + 1, tok + total)
    usage.ok = True
    return usage


def _bq_first_seen(ctx, src, start, end):
    """{project: earliest inference log entry in [start, end)} in the sink table(s), from logName."""
    lname = expr("t", src.schema, "logName", cast="STRING")
    if not lname:
        return {}
    params = {"w_start": start, "w_end": end,
              "sfx_start": (start - dt.timedelta(days=1)).strftime("%Y%m%d"),
              "sfx_end": (end + dt.timedelta(days=1)).strftime("%Y%m%d")}
    sql = ("WITH %s\nSELECT REGEXP_EXTRACT(%s, r'^projects/([^/]+)/') AS project, MIN(%s) AS first_ts FROM s t "
           "GROUP BY project" % (src.cte("s", start="w_start", end="w_end"), lname, agy_fields(src)["ts"]))
    return {r["project"]: r["first_ts"] for r in ctx.query(sql, params, "antigravity")
            if r.get("project") and r.get("first_ts") is not None}


FIRST_SEEN_TTL = 6 * 3600


def _first_seen(ctx, src, start, late, end):
    """_bq_first_seen with a cache. The earliest entry at or after `start` never changes once it exists (later entries
    are newer), so a result that has every late project is kept for FIRST_SEEN_TTL whatever the window end; a project
    without an entry yet is read again next time."""
    cache = getattr(getattr(ctx, "app", None), "cache", None)
    key = ("agy_first_seen", getattr(src, "label", ""), tuple(sorted(late)), iso(start))
    if cache is not None:
        found, hit = cache.get(key)
        if found:
            return hit
    first = _bq_first_seen(ctx, src, start, end)
    if cache is not None and all(p in first for p in late):
        cache.set(key, first, FIRST_SEEN_TTL)
    return first


def logging_gaps(ctx, src, start, end):
    """{project: (from, to)}: the parts of [start, end) whose inference logs exist only in Cloud Logging.

    Log Router copies only entries received after a sink was created, and creates the table with the first one. So a
    project whose sink table was created after `start` has its earlier entries only in Cloud Logging (for example
    Metadata logging was turned on before the dashboard was installed), and a scanned project without a sink table
    has all of them there. Empty when every sink table predates `start` (the normal case after the first period)."""
    projects = list(getattr(ctx, "projects", None) or [ctx.project])
    central = getattr(ctx, "central_audit_project", "")
    if isinstance(central, str) and central:
        # One table fed by an organization or folder aggregated sink holds every project's entries.
        created = {p: getattr(src, "created", None) for p in projects}
    else:
        created = {s.project: getattr(s, "created", None) for s in (getattr(src, "sources", None) or [src])}
    gaps, late = {}, []
    for p in projects:
        if p not in created:
            gaps[p] = (start, end)
        elif created[p] is not None and created[p] > start:
            late.append(p)
    if late:
        first = _first_seen(ctx, src, start, late, end)
        for p in late:
            stop = min(x for x in (first.get(p), created[p], end) if x is not None)
            if stop > start:
                gaps[p] = (start, stop)
    return gaps


def _read_logging_gaps(ctx, src, start, end, email=None, rng=None, fresh=False):
    """Reads the Cloud Logging entries the sink table never received (see logging_gaps) without touching ctx notices:
    (extra Usage or None, failure or None), failure = (stage, GcpError). Never raises GcpError, so it can run while
    the table is queried. Readings are kept 5 minutes; fresh (enforcement runs) reads anew and replaces the kept one."""
    try:
        gaps = logging_gaps(ctx, src, start, end)
    except GcpError as err:
        return None, ("first", err)
    if not gaps:
        return None, None
    key = ("agy_logging_gap", tuple(sorted((p, iso(a), iso(b)) for p, (a, b) in gaps.items())), email or "", rng)

    def load():
        return _logging_usage(ctx, start, end, email=email, rng=rng, ranges=gaps)
    try:
        if fresh:
            extra = load()
            ctx.app.cache.set(key, extra, 300)
        else:
            extra, _ = ctx.app.cache.get_or_compute(key, 300, load)
    except GcpError as err:
        return None, ("logging", err)
    return extra, None


def _apply_logging_gaps(ctx, src, usage, extra, failure, log_label):
    if failure is not None:
        stage, err = failure
        if stage == "first":
            ctx.warn_error(src.label, err, "The first entries of the Antigravity inference log table")
        else:
            ctx.warn_error(log_label, err, "Cloud Logging inference logs older than the sink table")
        return
    if extra is None:
        return
    if extra.records:
        ctx.add_source(log_label)
    if extra.truncated:
        ctx.warn(log_label, "truncated", "Inference log entries were truncated at max_log_entries.")
    usage.merge(extra)


def _logging_tail_ranges(ctx, src, usage, start, end):
    """{project: (from, to)}: for every scanned project, the part of [start, end) that neither the sink table holds
    (entries after its newest one, or the whole window when it has none in it) nor the gap read covers (logging_gaps:
    projects without a table, and the time before a table was created). Empty when the gaps cannot be determined, so
    nothing is ever counted twice."""
    try:
        gaps = logging_gaps(ctx, src, start, end)
    except GcpError:
        return {}
    out = {}
    for project in list(getattr(ctx, "projects", None) or [ctx.project]):
        lo = start
        if project in gaps:
            if gaps[project][1] >= end:
                continue   # the gap read covers the whole window of this project
            lo = max(lo, gaps[project][1])
        last = (getattr(usage, "project_last", None) or {}).get(project)
        if last is not None:
            lo = max(lo, last + dt.timedelta(microseconds=1))
        if lo < end:
            out[project] = (lo, end)
    return out


def _apply_logging_tail(ctx, src, usage, start, end, email, rng, log_label):
    """Adds the Cloud Logging entries that Log Router has not streamed to the sink table yet (usually seconds behind,
    sometimes minutes). Enforcement runs only (fresh reads): a run that a usage event starts must see the entry that
    reached the cap. A failure leaves the table's usage as it is and warns."""
    ranges = _logging_tail_ranges(ctx, src, usage, start, end)
    if not ranges:
        return
    try:
        extra = _logging_usage(ctx, start, end, email=email, rng=rng, ranges=ranges)
    except GcpError as err:
        ctx.warn_error(log_label, err, "Cloud Logging inference logs newer than the sink table")
        return
    if extra.records:
        ctx.add_source(log_label)
    if extra.truncated:
        ctx.warn(log_label, "truncated", "Inference log entries were truncated at max_log_entries.")
    usage.merge(extra)


def antigravity_usage(ctx, start, end, email=None, rng=None, fresh=False):
    """Returns a Usage. Never raises for upstream failures (usage.ok=False + warning). fresh skips the 5-minute cache of
    Cloud Logging reads and also reads the entries newer than the sink table from Cloud Logging (enforcement runs decide
    on the latest entries)."""
    ag = ctx.settings["governance"]["antigravity"]
    mode = ag.get("usage_source", "auto")
    src = None
    if mode in ("auto", "bigquery"):
        src = ctx.audit_source(AGY_SINK, optional=(mode == "auto"))
    log_label = "logging:%s" % urllib.parse.unquote(ag["inference_log"])
    if src is not None:
        label = src.label
        tasks = {"bq": lambda: _bq_usage(ctx, src, start, end, email=email, rng=rng)}
        if mode == "auto":
            # Entries older than the sink table are read from Cloud Logging while the table is queried, not after it.
            tasks["gaps"] = lambda: _read_logging_gaps(ctx, src, start, end, email=email, rng=rng, fresh=fresh)
        if len(tasks) == 1:
            try:
                res = {"bq": (True, tasks["bq"]())}
            except GcpError as err:
                res = {"bq": (False, err)}
        else:
            res = ctx.run(tasks)
        ok, usage = res["bq"]
        if not ok:
            if not isinstance(usage, GcpError):
                raise usage
            ctx.warn_error(label, usage, "The Antigravity inference log table")
            return Usage("bigquery")
        ctx.add_source(label)
        if "gaps" in res:
            ok, value = res["gaps"]
            if not ok:
                raise value
            _apply_logging_gaps(ctx, src, usage, value[0], value[1], log_label)
        if fresh and mode == "auto":
            _apply_logging_tail(ctx, src, usage, start, end, email, rng, log_label)
        return usage
    if mode == "bigquery":
        return Usage("unavailable")
    projects = tuple(getattr(ctx, "projects", None) or [ctx.project])
    cache_key = ("agy_logging", projects, iso(start), iso(end), email or "", rng)

    def load():
        return _logging_usage(ctx, start, end, email=email, rng=rng)
    try:
        if fresh:
            usage = load()
            ctx.app.cache.set(cache_key, usage, 300)
        else:
            usage, _ = ctx.app.cache.get_or_compute(cache_key, 300, load)
    except GcpError as err:
        ctx.warn_error(log_label, err, "Cloud Logging inference logs")
        return Usage("logging")
    ctx.add_source(log_label)
    if usage.truncated:
        ctx.warn(log_label, "truncated", "Inference log entries were truncated at max_log_entries.")
    return usage


TELEMETRY_LOOKBACK = dt.timedelta(days=30)  # Cloud Logging keeps _Default entries for 30 days by default


def telemetry_seen(ctx, usage, now):
    """True when an Antigravity inference log entry exists in the last 30 days.

    The cap window alone is not enough: right after every reset it is empty although Metadata logging is on, and the
    page would tell the administrator to turn on a setting that is already on."""
    if not usage.ok:
        return False
    if usage.records > 0:
        return True
    ag = ctx.settings["governance"]["antigravity"]
    mode = ag.get("usage_source", "auto")
    log_id = ag["inference_log"]
    projects = tuple(getattr(ctx, "projects", None) or [ctx.project])

    def probe():
        start, end = now - TELEMETRY_LOOKBACK, now + dt.timedelta(minutes=5)
        if mode in ("auto", "bigquery"):
            src = ctx.audit_source(AGY_SINK, optional=True)
            if src is not None:
                params = {"w_start": start, "w_end": end,
                          "sfx_start": (start - dt.timedelta(days=1)).strftime("%Y%m%d"),
                          "sfx_end": (end + dt.timedelta(days=1)).strftime("%Y%m%d")}
                rows = ctx.query("WITH %s\nSELECT COUNT(*) AS n FROM (SELECT 1 FROM s LIMIT 1)"
                                 % src.cte("s", start="w_start", end="w_end"), params, "antigravity")
                if rows and to_int(rows[0].get("n")):
                    return True
        if mode == "bigquery" or not config.LOG_NAME_RE.match(log_id):
            return False
        for proj in projects:
            body = {"resourceNames": ["projects/%s" % proj], "orderBy": "timestamp desc", "pageSize": 1,
                    "filter": 'logName="projects/%s/logs/%s" AND timestamp>="%s"' % (proj, log_id, iso(start))}
            try:
                data = ctx.rest("POST", "%s/entries:list" % LOGGING_V2, json_body=body, timeout=30)
            except (GcpError, ValueError):
                continue
            if (data or {}).get("entries"):
                return True
        return False

    try:
        seen, _ = ctx.app.cache.get_or_compute(("agy_telemetry_seen", projects, mode), 600, probe)
    except (GcpError, ValueError):
        return False  # a failed probe must not break the status page; the usage notices already say what failed
    return bool(seen)


DEV_TOOLS_LOCATIONS = ("global", "us", "eu")
DEV_TOOLS_CACHE_TTL = 300


def _call_safe(fn):
    try:
        return True, fn()
    except (GcpError, ValueError) as err:
        return False, err


def dev_tools_logging_status(ctx):
    """Reads Gemini Enterprise AI developer tools compliance settings across multi-region locations (global, us, eu)
    to detect active locations where Metadata logging is still off.

    Gemini Enterprise configures AI developer tools and Metadata logging separately per location, while Antigravity
    routes Gemini 4 and preview models to `global`. Read-only; failures never break the status page."""
    raw_projects = getattr(ctx, "projects", None) or [getattr(ctx, "project", None)]
    projects = tuple(p for p in raw_projects if config.valid_project(p))
    locs = list(DEV_TOOLS_LOCATIONS)
    if hasattr(ctx, "ws"):
        for loc in discovery_locations(ctx):
            if loc not in locs and config.LOCATION_RE.match(loc or ""):
                locs.append(loc)
    locations = tuple(locs)
    empty = {"checked": False, "locations": [], "enabledLocations": [], "missingLocations": [], "allEnabled": None}
    if not projects or not locations:
        return empty

    def probe():
        tasks = {}
        for proj in projects:
            for loc in locations:
                url = "%s/v1alpha/projects/%s/locations/%s/devToolsConfig" % (discovery_host(loc), proj, loc)
                tasks[(proj, loc)] = (lambda u=url: ctx.rest("GET", u, timeout=15))
        results = ctx.run(tasks) if hasattr(ctx, "run") else {k: _call_safe(fn) for k, fn in tasks.items()}
        checked = False
        items, enabled, missing = [], [], []
        for proj in projects:
            for loc in locations:
                ok, val = results.get((proj, loc), (False, None))
                if not ok or not isinstance(val, dict):
                    continue
                checked = True
                if not val.get("devToolsEnabled"):
                    continue
                log_cfg = val.get("loggingConfig") if isinstance(val.get("loggingConfig"), dict) else {}
                meta_on = bool(log_cfg.get("metadataLoggingEnabled"))
                items.append({"project": proj, "location": loc, "devToolsEnabled": True,
                              "metadataLoggingEnabled": meta_on})
                (enabled if meta_on else missing).append({"project": proj, "location": loc})
        all_on = (bool(enabled) and not missing) if (enabled or missing) else None
        return {"checked": checked, "locations": items, "enabledLocations": enabled,
                "missingLocations": missing, "allEnabled": all_on}

    cache = getattr(getattr(ctx, "app", None), "cache", None)
    if cache is None:
        return probe()
    try:
        val, _ = cache.get_or_compute(("agy_dev_tools_logging", projects, locations), DEV_TOOLS_CACHE_TTL, probe)
        return val
    except (GcpError, ValueError):
        return empty


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
            "costCap": item.get("costCap"), "paidBudget": item.get("paidBudget"),
            "creditMonth": item.get("creditMonth"), "note": item.get("note") or "",
            "createdBy": item.get("createdBy") or None, "createdAt": item.get("createdAt")}


def public_suspension(item):
    if not item:
        return None
    return {"since": item.get("since"), "until": item.get("until"), "trigger": item.get("trigger"),
            "by": item.get("by") or None, "reasons": list(item.get("reasons") or []),
            "tokens": item.get("tokens"), "requests": item.get("requests"), "cost": item.get("cost")}


def restore_at(state, suspension, over, next_reset, now):
    """When a suspended user regains access: while the user is over the cap, the end of the current period (or the
    recorded end when that is later), because a suspension recorded under an earlier, shorter period (for example
    weekly before the switch to monthly caps) does not lift while the usage of the current period is over the cap;
    otherwise the recorded period end. None when not suspended, already due, or without a fixed reset."""
    if state != CAPPED:
        return None
    until = parse_rfc3339((suspension or {}).get("until"))
    if over and next_reset is not None:
        return iso(max(until, next_reset) if until is not None else next_reset)
    if until is not None and until > now:
        return iso(until)
    return None


# The default schedule runs every 5 minutes; two hours without a scheduled run means it stopped, and suspended
# users would then not be restored at the period reset.
SCHEDULER_STALE_AFTER = dt.timedelta(hours=2)


def scheduler_status(ctx, state, now=None):
    """Scheduled enforcement: configured (Cloud Scheduler can call the enforcement endpoint, or runs the enforcement
    job), the last scheduled run and stale (configured, has run before, but not within SCHEDULER_STALE_AFTER)."""
    ag = ctx.settings["governance"]["antigravity"]
    invokers = list(ag.get("scheduler_invokers") or [])
    base = (ctx.settings.get("security") or {}).get("public_base_url") or ""
    job = jobs.job_name(ctx.settings) if enforcement_backend(ctx.settings) == "iam_deny" else ""
    configured = bool((invokers and base) or job)
    last = state.get("lastScheduledRun")
    last_at = parse_rfc3339(last.get("at")) if isinstance(last, dict) else None
    now = now or utcnow()
    stale = bool(configured and last_at is not None and now - last_at > SCHEDULER_STALE_AFTER)
    return {"configured": configured, "lastRun": last, "stale": stale, "job": job or None}


def realtime_status(ctx, state, now=None):  # pylint: disable=unused-argument
    """Real-time enforcement (services/usage_events.py): configured (a Pub/Sub push subscription posts every inference
    log entry to the usage-event endpoint), when the last event arrived and when an event last started an enforcement
    run (counts and times only)."""
    ag = ctx.settings["governance"]["antigravity"]
    rec = state.get("usageEvents") if isinstance(state.get("usageEvents"), dict) else {}
    return {"configured": bool(ag.get("usage_events_subscription")),
            "lastEventAt": rec.get("lastEventAt"), "lastTriggerAt": rec.get("lastTriggerAt"),
            "lastTriggerVia": rec.get("lastTriggerVia"), "triggers": to_int(rec.get("triggers"))}


# A run request older than this is not the reason of the job execution that finds it (the scheduler started that one).
RUN_REQUEST_TTL = dt.timedelta(minutes=5)


def note_run_request(ctx, trigger):
    """Records why the dashboard starts an enforcement job execution (usage or admin); the execution records it as its
    trigger (claim_run_request). A failure only costs the label."""
    stamp = {"trigger": trigger, "at": iso(utcnow())}
    try:
        mutate_state(ctx, lambda state: state.__setitem__("runRequest", stamp))
    except ApiError as err:
        LOG.warning("enforcement run request not recorded: %s", err.code)


def claim_run_request(ctx, default="scheduler", now=None):
    """The trigger of this enforcement job execution: the run request the dashboard recorded just before starting it
    (at most RUN_REQUEST_TTL old), which is consumed, else `default` (Cloud Scheduler started it)."""
    req = governance_state(ctx).get("runRequest")
    if not isinstance(req, dict) or not req.get("at"):
        return default
    claimed = []

    def take(state):
        cur = state.get("runRequest")
        if isinstance(cur, dict) and cur.get("at") == req.get("at"):
            state.pop("runRequest", None)
            claimed.append(cur.get("trigger"))
    try:
        mutate_state(ctx, take)
    except ApiError as err:
        LOG.warning("enforcement run request not claimed: %s", err.code)
        return default
    at = parse_rfc3339(req.get("at"))
    if claimed and claimed[0] in ("usage", "admin") and at is not None and (now or utcnow()) - at <= RUN_REQUEST_TTL:
        return claimed[0]
    return default


def request_enforcement(ctx, trigger="admin"):
    """Starts an enforcement run of the saved policy right away, after a change that can suspend or restore someone: an
    administrator's policy or rule change, or a usage event that reached a cap (trigger "usage"). The enforcement job
    runs it when one is configured (its service account alone writes deny policies), else a cycle in this process;
    notify_only changes nobody's access and runs nothing. Never raises.

    Returns {"via": "job" | "run" | None, "started": bool, "error": code or None, "execution": name or None,
    "suspended": n, "restored": n} (counts only for a cycle that ran here)."""
    out = {"via": None, "started": False, "error": None, "execution": None, "suspended": 0, "restored": 0}
    backend = enforcement_backend(ctx.settings)
    if backend == "notify_only":
        return out
    if backend == "iam_deny" and jobs.job_name(ctx.settings):
        out["via"] = "job"
        note_run_request(ctx, trigger)
        try:
            started = jobs.trigger(ctx)
        except GcpError as err:
            LOG.warning("enforcement job run refused kind=%s", err.kind)
            out["error"] = err.kind
            return out
        write_audit(ctx, "enforce_job_run", {"result": "success", "target": started["job"], "trigger": trigger,
                                             "execution": started["execution"]})
        _refresh_status_cache(ctx)
        out.update(started=True, execution=started.get("execution"))
        return out
    out["via"] = "run"
    try:
        result = enforce_cycle(ctx, trigger=trigger)
    except (ApiError, GcpError) as err:
        LOG.warning("enforcement run failed code=%s", getattr(err, "code", None) or getattr(err, "kind", "error"))
        out["error"] = getattr(err, "code", None) or getattr(err, "kind", "error")
        return out
    changes = result.get("changes") or []
    out.update(started=True, suspended=_applied(changes, "block"), restored=_applied(changes, "restore"))
    return out


# Settings whose change can suspend or restore someone: saving one in automatic mode applies the policy at once.
POLICY_KEYS = frozenset({
    "cap_model", "period", "window", "reset_timezone", "reset_weekday", "reset_time", "reset_day", "cycle_days",
    "cycle_start", "token_cap", "request_cap", "cost_cap", "currency", "pricing_source", "model_prices",
    "input_price_per_million", "output_price_per_million", "credit_per_user_month", "paid_budget_month",
    "org_paid_budget_month", "credit_model_prefixes", "credit_source", "credit_by_tier", "enforcement_mode"})


def after_policy_change(ctx, keys):
    """After an administrator saved or reset governance settings (`keys`, the setting names): in automatic mode an
    enforcement run applies the new policy at once (people now over a cap are suspended, people no longer over one are
    restored) instead of at the next scheduled run. `ctx` must carry the saved settings. Returns request_enforcement's
    result, or None when nothing needs to run. Never raises."""
    if not (set(keys or ()) & POLICY_KEYS):
        return None
    ag = ctx.settings["governance"]["antigravity"]
    if (ag.get("enforcement_mode") or "manual") != "auto" or not ctx.settings["modules"].get("antigravity", False):
        return None
    return request_enforcement(ctx, trigger="admin")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def policy_params(ctx, source):
    ag = ctx.settings["governance"]["antigravity"]
    period = source.get("period") or ag["period"]
    window = source.get("window") or ag["window"]
    if period not in timeutil.PERIODS:
        raise ApiError("bad_request", "Parameter 'period' must be one of daily, weekly, monthly, custom, all.")
    if window not in timeutil.WINDOWS:
        raise ApiError("bad_request", "Parameter 'window' must be 'calendar' or 'rolling'.")
    caps = []
    for name, default in (("tokenCap", ag["token_cap"]), ("requestCap", ag["request_cap"])):
        value = source.get(name, default)
        if isinstance(value, str) and value.isascii() and value.isdigit():   # isdigit() alone takes superscripts
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 10 ** 12:
            raise ApiError("bad_request", "Field '%s' must be a non-negative integer." % name)
        caps.append(value)
    return period, window, caps[0], caps[1]


_EMPTY_USAGE = {"requests": 0, "tokens": 0, "promptTokens": 0, "candidateTokens": 0, "cachedTokens": 0,
                "clients": set(), "models": set(), "byModel": {}}


def _usage_tasks(ctx, tasks, credit, segs, start, end, fresh, now=None):
    if credit:
        horizon = (now or utcnow()) - dt.timedelta(minutes=15)
        for i, seg in enumerate(segs):
            # Closed segments no longer change (log entries arrive within minutes): only the open one is read fresh
            # during enforcement, the others may come from the 5-minute cache.
            seg_fresh = bool(fresh and seg.end > horizon)
            tasks[("usage", i)] = (lambda a=seg.start, b=seg.end, f=seg_fresh: antigravity_usage(ctx, a, b, fresh=f))
    else:
        # Cached reads round `end` to the minute so cache keys stay stable; fresh enforcement runs must see entries
        # written during the current minute (for example the usage event that just reached a cap).
        query_end = max(end, (now or utcnow()).astimezone(dt.timezone.utc)) if fresh else end
        tasks["usage"] = lambda: antigravity_usage(ctx, start, query_end, fresh=fresh)


def _segment_usages(res, segs):
    """(all usage, current-cycle usage, evaluated-month usage, per-segment usages) from the per-segment results. Any
    failed segment makes usage unavailable: the ledger must never decide on part of the month."""
    parts, healthy = [], True
    for i in range(len(segs)):
        ok, value = res[("usage", i)]
        if not ok or not getattr(value, "ok", False):
            healthy = False
            value = value if ok else Usage("unavailable")
        parts.append(value)
    source = next((u.source for u in parts if u.ok), "unavailable") if healthy else "unavailable"
    every, week, month = Usage(source), Usage(source), Usage(source)
    if healthy:
        every.ok = week.ok = month.ok = True
        for seg, part in zip(segs, parts):
            every.merge(part)
            if seg.in_week:
                week.merge(part)
            if seg.in_month:
                month.merge(part)
    return every, week, month, parts


def _credit_ledgers(ag, emails, exceptions, segs, parts, pricing, tiers=None, terms=None):
    """{email: (ledger, budget, known, partial, week_cost)} for every person in `emails`. tiers ({person: (tier,
    monthly credit)}, see license_credits) sets each person's credit by license tier; terms, when a dict, receives
    {person: {"creditMonth", "creditSource", "tier"}}."""
    prefixes = credit_prefixes(ag)
    tiers = tiers or {}
    out = {}
    for email in emails:
        tier, tier_credit = tiers.get(email) or (None, None)
        exc = exceptions.get(email)
        credit_month, budget = credit_terms(ag, exc, tier_credit)
        if terms is not None:
            terms[email] = {"creditMonth": round(credit_month, 6), "creditSource": credit_source_of(exc, tier),
                            "tier": tier}
        costs, known, partial, week_cost = [], True, False, 0.0
        for seg, part in zip(segs, parts):
            rec = part.users.get(email)
            if not rec:
                costs.append((0.0, 0.0))
                continue
            gemini, other, ok, part_partial = split_cost(rec, pricing, prefixes)
            known, partial = known and ok, partial or part_partial
            costs.append((gemini, other))
            if seg.in_week:
                week_cost += gemini + other
        led = credit_ledger.run(segs, costs, credit_month / 4.0)
        out[email] = (led, budget, known and pricing.configured, partial, week_cost)
    return out


def _credit_row(led, budget, known, org_used_up):
    used_up = credit_ledger.budget_used_up(led, budget, org_used_up)
    return {"creditWeek": round(led.credit_week, 6), "freeUsedWeek": round(led.free_used_week, 6),
            "freeLeftWeek": round(led.free_left_week, 6), "paidWeek": round(led.paid_week, 6),
            "paidMonth": round(led.paid_month, 6), "freeMonth": round(led.free_month, 6),
            "costMonth": round(led.cost_month, 6), "geminiMonth": round(led.gemini_month, 6),
            "otherMonth": round(led.other_month, 6), "budget": budget,
            "budgetLeft": None if budget is None else round(max(0.0, budget - led.paid_month), 6),
            "budgetUsedUp": used_up, "costKnown": known}


def _credit_ratio(led, budget):
    if budget is not None and budget > 0:
        return led.paid_month / budget
    if budget == 0 and led.credit_week > 0:
        return led.free_used_week / led.credit_week + (1.0 if led.paid_month > credit_ledger.EPS else 0.0)
    return 0.0


# ---------------------------------------------------------------------------
# Enforcement backends: deny policy state and organization unit budgets
# ---------------------------------------------------------------------------

def _deny_people(dstate):
    """{person key: deny principal} of the people our deny policy lists at any readable attachment point (a person
    listed at only some points is suspended too; the next run lists them everywhere)."""
    out = {}
    for principal in sorted(dstate.principals | dstate.partial):
        key = identity.person_key(principal)
        if key and identity.is_person_key(key):
            out[key] = principal
    return out


def _deny_state(email, deny_people, exceptions):
    """(state, reason) of one person with the iam_deny backend: listed in the deny policy = suspended (blocked when a
    block rule applies), otherwise enforceable (a deny policy also stops group, inherited and workforce access)."""
    if email in deny_people:
        return (BLOCKED if (exceptions.get(email) or {}).get("type") == "block" else CAPPED), None
    return EA, None


def _deny_warnings(ctx, dstate):
    errors = sorted({st.error for st in dstate.attachments if st.error is not None})
    if errors:
        failed = sum(1 for st in dstate.attachments if st.error is not None)
        ctx.warn(enforcers.SOURCE, "partial_data", "The deny policy could not be read at %d of %d attachment points "
                                                   "(%s)." % (failed, len(dstate.attachments), ", ".join(errors)))
    if len(dstate.attachments) > enforcers.QUOTA_WARN_POINTS:
        ctx.warn(enforcers.SOURCE, "quota", "%d deny policy attachment points: every run reads each of them, and the "
                                            "IAM policy API allows %d policy reads and %d writes per minute per project "
                                            "by default. Choose the folders or organization of the workspace scope as "
                                            "the deny policy attachment, or ask for a higher quota."
                 % (len(dstate.attachments), enforcers.POLICY_API_QUOTA_PER_MINUTE,
                    enforcers.POLICY_API_QUOTA_PER_MINUTE))


def _unit_budgets(ctx, credit, ledgers, month_usage, pricing, people):
    """Organization unit budgets of the running month (services/budgets.py). Returns ({person: {"suspend": [unit ids],
    "review": [unit ids], "paid": bool}}, BudgetStatus or None) for the people whose usage is attributed to a unit,
    or a unit above it, whose budget is used up; attribution through a project label counts for the BudgetStatus only,
    never for these decisions. With credit caps the facts come from the same month ledgers as enforcement (fresh);
    with period caps from the chargeback report of the month (kept for 5 minutes). Never raises: a failure is a
    warning, and then no unit budget applies."""
    from . import budgets, chargeback, org_model   # budgets imports chargeback, which imports this module
    try:
        tree = budgets.load(ctx)
        if not tree.units and not tree.projects:
            return {}, None
        org = org_model.load(ctx)
        cb = ctx.settings.get("chargeback") or {}
        if credit:
            mode = cb.get("allocation_mode") or "credit_share"
            prefixes = credit_prefixes(ctx.settings["governance"]["antigravity"])
            facts = []
            for email in people:
                item = ledgers.get(email)
                rec = month_usage.users.get(email) if month_usage is not None else None
                if item is None or not rec:
                    continue
                led, _budget, known, _partial, _week = item
                row = {"email": email, "cost": led.cost_month if known else None,
                       "chargeable": led.paid_month if known else None,
                       "byProject": project_split(rec, pricing, prefixes, led.free_month, "gemini", known)}
                facts.extend(chargeback.usage_facts(row, mode))
            label_key = cb.get("project_unit_label") or ""
            labels = chargeback._project_labels(ctx) if label_key else {}   # pylint: disable=protected-access
            for fact in facts:
                fact["unit"], fact["via"] = org.unit_for(fact["person"], fact["project"],
                                                         labels.get(fact["project"]) or {}, label_key)
        else:
            facts, currency = budgets._facts(ctx, chargeback.current_month(ctx))  # pylint: disable=protected-access
            if currency != "USD":
                ctx.warn(budgets.SOURCE, "currency_mismatch", "Budgets are in USD, so they are not measured against "
                                                              "a chargeback report in %s." % currency)
                return {}, None
        status = budgets.evaluate(tree, org, facts)
        # Project labels are set by project owners, not administrators, so spend attributed through a label counts for
        # the status (report and notify alerts) but never decides a suspension or a review: a label must not be able
        # to spend another unit's budget away and get its members suspended.
        decided = [fact for fact in facts if fact.get("via") != "label"]
        decide = status if len(decided) == len(facts) else budgets.evaluate(tree, org, decided)
    except ApiError as err:
        LOG.warning("unit budgets unavailable: %s", err.code)
        ctx.warn(budgets.SOURCE, "status_unavailable", "The budget status is unavailable: %s" % err.message)
        return {}, None
    except GcpError as err:
        ctx.warn_error(budgets.SOURCE, err, "The chargeback figures of the budget status")
        return {}, None
    except Exception:  # pylint: disable=broad-except
        LOG.exception("unit budgets failed")
        ctx.warn(budgets.SOURCE, "internal_error", "The budget status could not be computed.")
        return {}, None
    units, paid = {}, set()
    for fact in decided:
        units.setdefault(fact.get("person"), set()).add(fact.get("unit"))
        if (fact.get("chargeable") or 0) > 0:
            paid.add(fact.get("person"))
    flags = {}
    for person, unit_ids in units.items():
        ordered = sorted(u for u in unit_ids if isinstance(u, str))
        suspend = decide.suspend_nodes_for(ordered)
        review = decide.review_nodes_for(ordered)
        if suspend or review:
            flags[person] = {"suspend": [n["unitId"] for n in suspend], "review": [n["unitId"] for n in review],
                             "paid": person in paid}
    return flags, status


def compute_status(ctx, period, window, token_cap, request_cap, cost_cap=None, mode=None, now=None, fresh=False):
    """Computes the full governance status. Returns (public_payload, internal).

    cost_cap and mode default to the saved policy. mode "auto" plans blocks for over-cap users;
    "manual" puts them in the review queue. With fixed caps per period (cap_model period, the default) a person who
    reaches a cap stays over it until the configured period ends (for example the 1st of the next month), so the
    suspension lasts until then. With credit caps (cap_model credit_budget) the usage of the
    month so far is read per weekly cycle and each person's weekly free credit and monthly paid budget decide
    (services/credit_ledger.py); token and request caps then apply per weekly cycle. fresh skips cached log reads.

    The enforcement backend decides where the state comes from: the primary project's allow policy (iam_allow_swap),
    the managed deny policy (iam_deny: listed = suspended), or nowhere (notify_only: everyone keeps access and over-cap
    people go to the review queue, which is announced). Used-up organization unit budgets (services/budgets.py) act
    like the organization budget for the people whose usage is attributed to the unit: they are suspended (action
    suspend) or reviewed (action review) while nothing is free for them."""
    cfg = gov_config(ctx.settings)
    ag = ctx.settings["governance"]["antigravity"]
    backend = enforcement_backend(ctx.settings)
    credit = credit_mode(ag)
    cost_cap = float(ag.get("cost_cap") or 0) if cost_cap is None else float(cost_cap)
    if credit:
        cost_cap = 0.0
    mode = mode or ag.get("enforcement_mode") or "manual"
    anchor = period_anchor(ctx.settings)
    tz = anchor["tz"]
    now = now or utcnow()
    segs, month_start, month_end = None, None, None
    if credit:
        period, window = "weekly", "calendar"
        segs, (month_start, month_end), (start, next_reset) = credit_ledger.segments(credit_cycle(ctx.settings), now)
        end = now.astimezone(dt.timezone.utc).replace(second=0, microsecond=0)
    else:
        start, end = timeutil.governance_window(period, window, now=now, **anchor)
        next_reset = timeutil.governance_next_reset(period, window, now=now, **anchor)
    gstate = governance_state(ctx)
    exceptions = active_exceptions(gstate, now)
    suspensions = {s["email"]: s for s in gstate.get("suspensions") or []}
    tasks = {}
    if backend == "iam_allow_swap":
        tasks.update({"policy": lambda: get_policy(ctx), "markers": lambda: marker_status(ctx, cfg),
                      "perms": lambda: permission_status(ctx)})
    elif backend == "iam_deny":
        tasks["deny"] = lambda: enforcers.read_state(ctx)
    _usage_tasks(ctx, tasks, credit, segs, start, end, fresh, now)
    if (ag.get("pricing_source") or "auto") == "auto":
        tasks["prices"] = lambda: price_table(ctx, "USD" if credit else (ag.get("currency") or ""))
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
    pricing = pricing_from(ag, table, currency="USD" if credit else None)
    parts, month_usage = [], None
    if credit:
        usage, window_usage, month_usage, parts = _segment_usages(res, segs)
    else:
        ok, usage = res["usage"]
        if not ok:
            usage = Usage("unavailable")
        window_usage = usage
    policy, dstate, deny_people = None, None, {}
    if backend == "iam_allow_swap":
        ok, policy = res["policy"]
        readable = ok
        if not ok:
            ctx.warn_error("api:cloudresourcemanager.projects.getIamPolicy", policy, "The project IAM policy")
            policy = None
        else:
            ctx.add_source("api:cloudresourcemanager.projects.getIamPolicy")
    elif backend == "iam_deny":
        ok, dstate = res["deny"]
        if not ok or not isinstance(dstate, enforcers.DenyState):
            dstate = enforcers.DenyState([])
        readable = dstate.readable
        deny_people = _deny_people(dstate) if readable else {}
        _deny_warnings(ctx, dstate)
    else:
        readable = True   # notify_only reads no IAM policy
    idx = PolicyIndex(policy, readable=readable and backend == "iam_allow_swap")
    ancestor = make_ancestor_check(ctx, cfg) if backend == "iam_allow_swap" else None
    population = {e for e in usage.users if identity.is_person_key(e)}
    if backend == "iam_allow_swap":
        population |= idx.direct_users({*cfg.access_roles, *cfg.markers})
    else:
        # Everyone the deny policy lists, everyone a block rule denies (with or without usage) and every approved
        # suspension is in scope, so that the plan covers the whole policy.
        population |= set(deny_people)
        population |= {e for e, x in exceptions.items() if x.get("type") == "block" and identity.is_person_key(e)}
        population |= {e for e, s in suspensions.items() if s.get("approved") and identity.is_person_key(e)}
    ledgers, org_paid, org_budget, org_used_up = {}, None, None, False
    terms = {}
    if credit and usage.ok:
        ledgers = _credit_ledgers(ag, sorted(population), exceptions, segs, parts, pricing,
                                  tiers=license_credits(ctx), terms=terms)
        org_paid = round(sum(item[0].paid_month for item in ledgers.values()), 6)
        org_budget = usd_budget(ag.get("org_paid_budget_month"))
        org_used_up = org_budget is not None and org_paid >= org_budget - credit_ledger.EPS
    unit_flags, budget_status = {}, None
    if usage.ok:
        unit_flags, budget_status = _unit_budgets(ctx, credit, ledgers, month_usage, pricing, sorted(population))
    # Unit budgets are calendar months (chargeback months start on the 1st), whatever day the cap period starts.
    month_reset = None if credit else timeutil.governance_next_reset("monthly", "calendar", now=now,
                                                                     **dict(anchor, day=1))
    prefixes = credit_prefixes(ag)
    users = []
    for email in sorted(population):
        u = window_usage.users.get(email) or _EMPTY_USAGE
        has_usage = (usage.users.get(email) or _EMPTY_USAGE)["requests"] > 0
        if backend == "iam_allow_swap":
            state, reason = classify(email, has_usage, idx, cfg, ancestor)
        elif backend == "iam_deny":
            state, reason = _deny_state(email, deny_people, exceptions) if readable else \
                (UNKNOWN, "DENY_POLICY_UNREADABLE")
        else:
            state, reason = EA, None
        exc = exceptions.get(email)
        tcap, rcap, ccap = effective_caps(exc, token_cap, request_cap, cost_cap)
        if credit:
            ccap = 0.0
        record = suspensions.get(email)
        susp = record if state == CAPPED else None
        flags = unit_flags.get(email) or {}
        unit_suspend, unit_review = bool(flags.get("suspend")), bool(flags.get("review"))
        force_review = False
        row = {"uid": uid_for(email), "email": email, "state": state, "reason": reason, "plannedAction": "none",
               "exception": public_exception(exc), "suspension": public_suspension(susp),
               "caps": {"tokenCap": tcap, "requestCap": rcap, "costCap": ccap}}
        if backend == "iam_deny" and record and not suspension_ended(record, now):
            if state == EA and record.get("approved"):
                row["approved"] = True   # an administrator approved this suspension (review queue)
            elif state == CAPPED and record.get("hold"):
                row["hold"] = True       # stays suspended until the recorded end
        reset_at = next_reset
        if usage.ok and credit:
            led, budget, known, partial, week_cost = ledgers[email]
            row["caps"]["paidBudget"] = budget
            row["caps"]["creditWeek"] = round(led.credit_week, 6)
            over_t, ratio_t, reasons_t = cap_check(u["tokens"], u["requests"], tcap, rcap)
            # A used-up unit budget acts like the organization budget: suspended while nothing is free right now.
            org_or_unit = org_used_up or unit_suspend or unit_review
            limited = budget is not None or org_or_unit
            if known:
                over_l, reasons_l = credit_ledger.decide(led, budget, org_or_unit)
                if over_l and (unit_suspend or unit_review) and \
                        not credit_ledger.budget_used_up(led, budget, org_used_up):
                    reasons_l = ["unit_budget"] + [r for r in reasons_l if r != "budget"]
                    force_review = not unit_suspend and not over_t
            else:
                over_l, reasons_l = (None if limited else False), []
            over = True if over_t else over_l
            reasons = reasons_l + reasons_t
            times = []
            if over_l:
                times.append(credit_ledger.restore_time(led, budget, now, next_reset, month_end, org_or_unit))
            if over_t:
                times.append(next_reset)
            reset_at = None if (not times or None in times) else max(times)
            row.update({"requests": u["requests"], "tokens": u["tokens"], "promptTokens": u["promptTokens"],
                        "candidateTokens": u["candidateTokens"], "clients": sorted(u["clients"]),
                        "models": sorted(u["models"]), "byModel": _model_rows(u, pricing, prefixes),
                        "estimatedCost": round(week_cost, 6) if known else None,
                        "costPartial": partial, "overCap": over,
                        "capUsage": round(max(ratio_t or 0.0, _credit_ratio(led, budget)), 4) if known else ratio_t,
                        "overCapReasons": reasons,
                        "credit": dict(_credit_row(led, budget, known, org_or_unit), **(terms.get(email) or {}))})
        elif usage.ok:
            cost, partial = estimate_cost(u, pricing)
            over, cap_usage, reasons = cap_check(u["tokens"], u["requests"], tcap, rcap, cost, ccap)
            if (unit_suspend or unit_review) and flags.get("paid"):
                # Period caps: a used-up unit budget stops paid usage (this month's chargeable amount) until the month
                # starts again.
                force_review = not unit_suspend and not over
                over, reasons = True, list(reasons or []) + ["unit_budget"]
                if month_reset is not None:
                    reset_at = max(reset_at, month_reset) if reset_at is not None else month_reset
            row.update({"requests": u["requests"], "tokens": u["tokens"], "promptTokens": u["promptTokens"],
                        "candidateTokens": u["candidateTokens"], "clients": sorted(u["clients"]),
                        "models": sorted(u["models"]), "byModel": _model_rows(u, pricing, prefixes),
                        "estimatedCost": cost, "costPartial": partial,
                        "overCap": over, "capUsage": cap_usage, "overCapReasons": reasons})
        else:
            # Usage telemetry is unavailable: usage-derived fields are unknown (null), never zero.
            row.update({"requests": None, "tokens": None, "promptTokens": None, "candidateTokens": None,
                        "clients": None, "models": None, "byModel": None, "estimatedCost": None,
                        "costPartial": False, "overCap": None, "capUsage": None, "overCapReasons": None})
            if credit:
                row["credit"] = None
        if force_review:
            row["forceReview"] = True
        if flags:
            row["unitBudget"] = {"suspend": list(flags.get("suspend") or []), "review": list(flags.get("review") or [])}
        # resetAt: when this person's caps lift without further usage (the suspension end of a block planned now).
        row["resetAt"] = iso(reset_at) if (row["overCap"] and reset_at is not None) else None
        row["restoreAt"] = restore_at(state, susp, row["overCap"], reset_at, now)
        users.append(row)
    non_user = list(dict.fromkeys(m for r in cfg.access_roles for m in idx.non_user_members(r))) \
        if (readable and backend == "iam_allow_swap") else []
    deny_info = None
    if backend == "iam_deny":
        deny_info = {"attachments": [st.attachment for st in dstate.attachments],
                     "unreadable": sum(1 for st in dstate.attachments if st.error is not None),
                     "foreign": sum(1 for st in dstate.attachments if st.error is None and st.foreign),
                     "current": len(deny_people),
                     "surgeMin": to_int(ag.get("surge_guard_min")) or 10,
                     "surgeRatio": float(ag.get("surge_guard_ratio") or 0.5)}
    # Notify only changes nobody's access, so over-cap people go to the review queue, which is announced once per
    # period, whatever the enforcement mode.
    gates, plan, budget_n = evaluate(users, readable, usage.ok, usage.records, non_user, cfg, cfg.access_role,
                                     mode="manual" if backend == "notify_only" else mode, now=now,
                                     telemetry_truncated=usage.truncated, backend=backend, deny=deny_info)
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
                                 "caps": u["caps"], "exception": u["exception"], "credit": u.get("credit")})
    if credit:
        users.sort(key=lambda u: (-(((u.get("credit") or {}).get("costMonth")) or 0), -(u["tokens"] or 0), u["email"]))
    else:
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
        elif backend == "iam_allow_swap":
            state = classify(email, False, idx, cfg)[0] if readable else UNKNOWN
        elif backend == "iam_deny":
            state = _deny_state(email, deny_people, exceptions)[0] if readable else UNKNOWN
        else:
            state = EA
        exception_rows.append(dict(public_exception(item), uid=uid_for(email), email=email, state=state))
    if backend == "iam_allow_swap":
        ok, markers = res["markers"]
        if not ok:
            markers = [{"role": cfg.cap_marker_id, "exists": None}, {"role": cfg.admin_marker_id, "exists": None}]
        ok, perms = res["perms"]
        if not ok:
            perms = {p: None for p in REQUIRED_PERMISSIONS}
    else:
        markers, perms = [], {}   # marker roles and allow-policy permissions are only used by the allow swap
    usage_source = usage.source if usage.ok else "unavailable"
    paid_limits = credit and (usd_budget(ag.get("paid_budget_month")) is not None or org_budget is not None
                              or any((e.get("paidBudget") is not None) for e in exceptions.values()))
    if (cost_cap > 0 or paid_limits) and not pricing.configured:
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
    # When a decision "for this period" ends (an allow rule's expiry): the next reset, or the end of a rolling window.
    period_end = next_reset if credit else timeutil.governance_period_end(period, window, now=now, **anchor)
    policy_out = {"tokenCap": token_cap, "requestCap": request_cap, "costCap": cost_cap,
                  "currency": pricing.currency, "currencySetting": ag.get("currency") or "",
                  "period": period, "window": window,
                  # The saved fixed-cap period: credit caps always evaluate their weekly cycle (period above), so the
                  # policy editor starts fixed caps from these and never carries the weekly credit reset over.
                  "periodSetting": ag.get("period") or "monthly", "windowSetting": ag.get("window") or "calendar",
                  "windowStart": iso(start), "windowEnd": iso(end), "nextReset": iso(next_reset),
                  "periodEnd": iso(period_end),
                  "resetTimezone": tz, "resetWeekday": anchor["weekday"], "resetTime": anchor["at"],
                  "resetDay": anchor["day"], "cycleDays": anchor["cycle_days"],
                  "cycleStart": timeutil.cycle_start_date(anchor["cycle_start"]).isoformat(),
                  "creditPerUserMonth": float(ag.get("credit_per_user_month") or 0),
                  "changeBudget": budget_n, "enforcementMode": mode,
                  "capModel": CREDIT_MODEL if credit else PERIOD_MODEL,
                  "pricing": pricing.public(used_models)}
    totals = {"requests": sum(u["requests"] or 0 for u in users) if usage.ok else None,
              "tokens": sum(u["tokens"] or 0 for u in users) if usage.ok else None,
              "estimatedCost": round(sum(costs), 6) if (usage.ok and pricing.configured) else None,
              "overCap": sum(1 for u in users if u["overCap"]) if usage.ok else None,
              "suspended": by_state[CAPPED] if readable else None,
              "review": len(plan["review"]), "exceptions": len(exception_rows)}
    if credit:
        rows = [u.get("credit") for u in users if u.get("credit")]
        policy_out.update({"creditPerWeek": round(float(ag.get("credit_per_user_month") or 0) / 4.0, 6),
                           "paidBudgetMonth": usd_budget(ag.get("paid_budget_month")),
                           "orgPaidBudgetMonth": org_budget if usage.ok else usd_budget(ag.get("org_paid_budget_month")),
                           "orgPaidMonth": org_paid, "orgBudgetUsedUp": org_used_up,
                           "monthStart": iso(month_start), "monthEnd": iso(month_end),
                           "creditModels": list(credit_prefixes(ag))})
        totals.update({"paidMonth": org_paid,
                       "freeMonth": round(sum(r["freeMonth"] for r in rows), 6) if usage.ok else None,
                       "costMonth": round(sum(r["costMonth"] for r in rows), 6) if usage.ok else None,
                       "budgetUsedUp": sum(1 for r in rows if r["budgetUsedUp"]) if usage.ok else None})
    payload = {
        "policy": policy_out,
        "usageSource": usage_source, "gates": gates,
        "population": {"total": len(users) if population_known else None,
                       "byState": by_state if population_known else None,
                       "enforceableCoverage": round(controllable / denominator, 4)
                       if (readable and denominator > 0) else None},
        "users": users if population_known else None, "plan": plan,
        "manualActions": manual if (readable and usage.ok) else None,
        "reviewQueue": review_queue if (readable and usage.ok) else None,
        "exceptions": exception_rows,
        "totals": totals,
        "enforcement": {"mode": mode, "backend": backend, "lastRun": gstate.get("lastRun"),
                        "scheduler": scheduler_status(ctx, gstate, now),
                        "realtime": realtime_status(ctx, gstate, now),
                        "durable": store.governance_durable() if store is not None else None},
        "prerequisites": {"markerRoles": markers, "permissions": perms,
                          "telemetryDetected": telemetry_seen(ctx, usage, now),
                          "inferenceLog": urllib.parse.unquote(ag.get("inference_log") or "")},
    }
    policy_out["enforcementBackend"] = backend
    if backend == "iam_deny":
        payload["enforcement"]["deny"] = {
            "readable": readable, "policyId": enforcers.policy_id(ctx.settings),
            "attachments": len(dstate.attachments), "people": len(deny_people), "partial": len(dstate.partial),
            "limit": enforcers.DENY_PRINCIPAL_LIMIT}
    if budget_status is not None:
        payload["budgets"] = budget_status.public()
    internal = {"policy": policy, "readable": readable, "cfg": cfg, "usage": usage, "idx": idx, "now": now,
                "nextReset": next_reset, "tz": tz, "mode": mode, "state": gstate, "backend": backend,
                "deny": dstate, "denyPeople": deny_people, "budgetStatus": budget_status,
                "periodEnd": period_end, "pricing": pricing, "credit": credit, "orgUsedUp": org_used_up}
    return payload, internal


def current_period_end(ctx, now=None):
    """End of the current settlement period: the weekly credit cycle with credit caps, else the policy period."""
    ag = ctx.settings["governance"]["antigravity"]
    if credit_mode(ag):
        return credit_cycle(ctx.settings).week(now or utcnow())[1]
    return timeutil.governance_period_end(ag["period"], ag["window"], now=now, **period_anchor(ctx.settings))


# ---------------------------------------------------------------------------
# GET /api/v1/governance/antigravity/period-preview
# ---------------------------------------------------------------------------

def _preview_int(params, name, default, lo, hi):
    raw = params.get(name)
    if raw in (None, ""):
        return default
    # ASCII digits only: isdigit() alone also takes superscripts, which int() rejects (a 500 instead of a 400).
    if not (isinstance(raw, str) and raw.isascii() and raw.isdigit() and lo <= int(raw) <= hi):
        raise ApiError("bad_request", "Parameter '%s' must be a whole number from %d to %d." % (name, lo, hi))
    return int(raw)


def period_preview(ctx):
    """The current cap period and the next two for period settings that are being edited (not saved yet), computed by
    the same rules as enforcement: {"periods": [{"start", "end"}], "rolling", "timezone", "now"}. Parameters default to
    the saved policy: period, window, tz, weekday (0 = Monday), at (HH:MM), day (1-31), cycleDays (1-92) and
    cycleStart (YYYY-MM-DD)."""
    params = ctx.params
    ag = ctx.settings["governance"]["antigravity"]
    saved = period_anchor(ctx.settings)
    period = params.get("period") or ag["period"]
    window = params.get("window") or ag["window"]
    if period not in timeutil.PERIODS:
        raise ApiError("bad_request", "Parameter 'period' must be one of daily, weekly, monthly, custom, all.")
    if window not in timeutil.WINDOWS:
        raise ApiError("bad_request", "Parameter 'window' must be 'calendar' or 'rolling'.")
    tz = params.get("tz") or saved["tz"]
    if not timeutil.valid_timezone(tz):
        raise ApiError("bad_request", "Parameter 'tz' must be an IANA time zone.")
    at = params.get("at") or saved["at"]
    if not config.TIME_OF_DAY_RE.match(str(at)):
        raise ApiError("bad_request", "Parameter 'at' must be a time as HH:MM.")
    cycle_start = params.get("cycleStart")
    if cycle_start in (None, ""):
        cycle_start = saved["cycle_start"]
    else:
        try:
            cycle_start = config.VALIDATORS["governance.antigravity.cycle_start"](cycle_start)
        except config.Invalid:
            raise ApiError("bad_request", "Parameter 'cycleStart' must be a date as YYYY-MM-DD.")
    anchor = {"tz": tz, "at": at, "cycle_start": cycle_start,
              "weekday": _preview_int(params, "weekday", int(saved["weekday"] or 0), 0, 6),
              "day": _preview_int(params, "day", saved["day"], 1, 31),
              "cycle_days": _preview_int(params, "cycleDays", saved["cycle_days"], 1, timeutil.CYCLE_DAYS_MAX)}
    now = utcnow()
    periods = timeutil.governance_periods(period, window, now=now, count=3, **anchor)
    return {"period": period, "window": window, "timezone": tz, "now": iso(now),
            "rolling": period == "all" or window == "rolling",
            "cycleStart": timeutil.cycle_start_date(cycle_start).isoformat(),
            "periods": [{"start": iso(start), "end": iso(end)} for start, end in periods]}


def status(ctx):
    period, window, token_cap, request_cap = policy_params(ctx, ctx.params)
    payload, _ = compute_status(ctx, period, window, token_cap, request_cap)
    payload.setdefault("prerequisites", {})["metadataLogging"] = dev_tools_logging_status(ctx)
    return payload


def default_status(ctx):
    ag = ctx.settings["governance"]["antigravity"]
    payload, internal = compute_status(ctx, ag["period"], ag["window"], ag["token_cap"], ag["request_cap"])
    return payload, internal


# ---------------------------------------------------------------------------
# GET /api/v1/governance/antigravity/billing-report?month=YYYY-MM
# ---------------------------------------------------------------------------

REPORT_MONTH_RE = re.compile(r"([0-9]{4})-([0-9]{1,2})")   # fullmatch; ASCII digits only (isdigit takes superscripts)


def _report_month(ctx, tz, now):
    """(start, end) aware local datetimes of the requested calendar month (default: the current month)."""
    raw = str(ctx.params.get("month") or "").strip()
    local_now = now.astimezone(tz)
    if raw:
        found = REPORT_MONTH_RE.fullmatch(raw)
        if not found:
            raise ApiError("bad_request", "Parameter 'month' must be YYYY-MM.")
        year, month = int(found.group(1)), int(found.group(2))
        if not 1 <= month <= 12 or year < 2020:
            raise ApiError("bad_request", "Parameter 'month' must be YYYY-MM.")
        start = local_now.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start > local_now:
        raise ApiError("bad_request", "Parameter 'month' must not be in the future.")
    end = (start + dt.timedelta(days=32)).replace(day=1)
    return start, end


def _to_currency_from_usd(amount_usd, currency, table):
    if amount_usd is None:
        return None
    if currency == "USD":
        return float(amount_usd)
    fx = getattr(table, "fx", None) or {}
    if fx.get("from") == "USD" and fx.get("to") == currency and fx.get("rate"):
        return float(amount_usd) * float(fx["rate"])
    return None


def _token_columns(rec):
    prompt = to_int(rec.get("promptTokens"))
    return {"requests": to_int(rec.get("requests")), "tokens": to_int(rec.get("tokens")), "promptTokens": prompt,
            "cachedTokens": to_int(rec.get("cachedTokens")),
            "outputTokens": max(to_int(rec.get("tokens")) - prompt, to_int(rec.get("candidateTokens")), 0)}


def _model_rows(rec, pricing, prefixes):
    out = []
    for model, agg in sorted((rec.get("byModel") or {}).items()):
        mcost, mpartial = estimate_cost({"tokens": agg.get("tokens"), "byModel": {model: agg}}, pricing)
        out.append(dict(_token_columns(agg), model=model, cost=mcost, costPartial=mpartial,
                        credit=credit_ledger.credit_eligible(model, prefixes)))
    total_cost = sum(m["cost"] for m in out if m["cost"] is not None)
    for m in out:
        m["costShare"] = round(m["cost"] / total_cost, 4) if (m["cost"] is not None and total_cost > 0) else None
    out.sort(key=lambda m: (-(m["cost"] if m["cost"] is not None else -1.0),
                            -to_int(m.get("tokens")), -to_int(m.get("requests")), m.get("model") or ""))
    return out


def project_split(rec, pricing, prefixes, credit, weight="gemini", known=True):
    """[{project, requests, tokens, cost, credit, chargeable}] of one person's usage. Google bills per project, so
    each project gets the list cost of the usage in it; the credit the person used (`credit`) is spread over the
    projects in proportion to their credit-eligible (Gemini) cost, or to their whole cost with weight="cost" (period
    caps), and the rest is charged back. Amounts are None when the person's cost is unknown."""
    items = []
    for project, prec in sorted((rec.get("byProject") or {}).items()):
        gemini, other, _ok, _partial = split_cost(prec, pricing, prefixes)
        items.append((project, prec, gemini, other))
    basis = sum((g if weight == "gemini" else g + o) for _, _, g, o in items)
    out = []
    for project, prec, gemini, other in items:
        row = {"project": project, "requests": to_int(prec.get("requests")), "tokens": to_int(prec.get("tokens")),
               "cost": None, "credit": None, "chargeable": None}
        if known:
            cost = gemini + other
            part = gemini if weight == "gemini" else cost
            share = (credit or 0.0) * part / basis if basis > 0 else 0.0
            row.update(cost=round(cost, 6), credit=round(share, 6), chargeable=round(cost - share, 6))
        out.append(row)
    return out


def _credit_billing_report(ctx):
    """billing_report with credit caps: the same ledger as enforcement. Per person, Gemini cost is first covered by
    the weekly free share (monthly credit / 4, unused share does not roll over); Claude and the rest is the paid
    amount to charge back. A weekly cycle that crosses the month start shares one free share across both months."""
    ag = ctx.settings["governance"]["antigravity"]
    anchor = period_anchor(ctx.settings)
    tz = timeutil.get_zone(anchor["tz"])
    now = utcnow()
    start_local, end_local = _report_month(ctx, tz, now)
    month_start = start_local.astimezone(dt.timezone.utc)
    segs, (_ms, month_end), _ = credit_ledger.segments(credit_cycle(ctx.settings), now, month_start=month_start)
    segs = [seg for seg in segs if seg.start < now]
    tasks = {}
    _usage_tasks(ctx, tasks, True, segs, None, None, False, now)
    if (ag.get("pricing_source") or "auto") == "auto":
        tasks["prices"] = lambda: price_table(ctx, "USD")
    res = ctx.run(tasks)
    table = None
    if "prices" in res:
        ok, value = res["prices"]
        if ok:
            table = value
        else:
            ctx.warn_error("pricing:auto", value, "Automatic token prices")
    pricing = pricing_from(ag, table, currency="USD")
    every, _week, month_usage, parts = _segment_usages(res, segs)
    exceptions = active_exceptions(governance_state(ctx), now)
    people = sorted(e for e in month_usage.users if identity.is_person_key(e))
    terms = {}
    ledgers = (_credit_ledgers(ag, people, exceptions, segs, parts, pricing, tiers=license_credits(ctx), terms=terms)
               if every.ok else {})
    prefixes = credit_prefixes(ag)
    rows, models_used = [], set()
    keys = ("requests", "tokens", "promptTokens", "cachedTokens", "outputTokens")
    totals = dict({k: 0 for k in keys}, cost=0.0, gemini=0.0, other=0.0, credit=0.0, chargeable=0.0)
    known_all = True
    for email in people:
        rec = month_usage.users[email]
        led, budget, known, partial, _ = ledgers[email]
        by_model = _model_rows(rec, pricing, prefixes)
        models_used.update(m["model"] for m in by_model)
        money = (lambda v: round(v, 6) if known else None)
        term = terms.get(email) or {}
        row = dict(_token_columns(rec), uid=uid_for(email), email=email, cost=money(led.cost_month),
                   costPartial=partial, gemini=money(led.gemini_month), other=money(led.other_month),
                   credit=money(led.free_month), chargeable=money(led.paid_month), budget=budget,
                   creditWeek=round(led.credit_week, 6), byModel=by_model, last=iso(rec.get("last")),
                   weeks=[{"start": iso(w["start"]), "end": iso(w["end"]), "gemini": round(w["gemini"], 6),
                           "other": round(w["other"], 6), "free": round(w["free"], 6), "paid": round(w["paid"], 6)}
                          for w in led.weeks] if known else None,
                   creditMonth=term.get("creditMonth"), creditSource=term.get("creditSource"),
                   tier=term.get("tier"),
                   byProject=project_split(rec, pricing, prefixes, led.free_month, "gemini", known))
        rows.append(row)
        for k in keys:
            totals[k] += row[k]
        if not known:
            known_all = False
            continue
        totals["cost"] += led.cost_month
        totals["gemini"] += led.gemini_month
        totals["other"] += led.other_month
        totals["credit"] += led.free_month
        totals["chargeable"] += led.paid_month
    for k in ("cost", "gemini", "other", "credit", "chargeable"):
        totals[k] = round(totals[k], 6) if known_all else None
    if every.truncated:
        ctx.warn("logging:%s" % urllib.parse.unquote(ag["inference_log"]), "truncated",
                 "Inference log entries were truncated at max_log_entries; the month is incomplete.")
    credit_usd = float(ag.get("credit_per_user_month") or 0)
    return {"month": start_local.strftime("%Y-%m"), "start": iso(month_start),
            "end": iso(end_local.astimezone(dt.timezone.utc)), "complete": month_end <= now,
            "timezone": anchor["tz"], "currency": "USD", "pricing": pricing.public(models_used),
            "capModel": CREDIT_MODEL, "creditPerUserMonthUsd": credit_usd, "creditPerUser": credit_usd,
            "creditPerWeek": round(credit_usd / 4.0, 6), "paidBudgetMonth": usd_budget(ag.get("paid_budget_month")),
            "creditModels": list(prefixes), "usageSource": every.source if every.ok else "unavailable",
            "users": rows, "totals": totals}


def billing_report(ctx):
    """Per-person Antigravity usage and cost for one calendar month (month boundaries at local midnight in the reset
    time zone), for internal chargeback. Tokens are what the inference log recorded for each call; cost applies the
    unit price of each model (cached prompt tokens at the cached-token price, thinking tokens as output). The
    monthly Antigravity credit per licensed user can be deducted. Google bills per project, so this is the
    allocation of usage to people, not an invoice line."""
    ag = ctx.settings["governance"]["antigravity"]
    if credit_mode(ag):
        return _credit_billing_report(ctx)
    anchor = period_anchor(ctx.settings)
    tz = timeutil.get_zone(anchor["tz"])
    now = utcnow()
    start_local, end_local = _report_month(ctx, tz, now)
    start = start_local.astimezone(dt.timezone.utc)
    end = min(end_local.astimezone(dt.timezone.utc), now)
    tasks = {"usage": lambda: antigravity_usage(ctx, start, end)}
    if (ag.get("pricing_source") or "auto") == "auto":
        tasks["prices"] = lambda: price_table(ctx, ag.get("currency") or "")
    res = ctx.run(tasks)
    table = None
    if "prices" in res:
        ok, value = res["prices"]
        if ok:
            table = value
        else:
            ctx.warn_error("pricing:auto", value, "Automatic token prices")
    pricing = pricing_from(ag, table)
    ok, usage = res["usage"]
    if not ok:
        usage = Usage("unavailable")
    credit_usd = float(ag.get("credit_per_user_month") or 0)
    credit = _to_currency_from_usd(credit_usd, pricing.currency, table) if credit_usd > 0 else 0.0
    tiers = license_credits(ctx) if usage.ok else {}
    prefixes = credit_prefixes(ag)
    rows, models_used = [], set()
    totals = {"requests": 0, "tokens": 0, "promptTokens": 0, "cachedTokens": 0, "outputTokens": 0,
              "cost": 0.0, "chargeable": 0.0}
    cost_known = charge_known = True
    for email in sorted(usage.users):
        rec = usage.users[email]
        cost, partial = estimate_cost(rec, pricing)
        by_model = []
        for model, agg in sorted((rec.get("byModel") or {}).items()):
            models_used.add(model)
            mcost, mpartial = estimate_cost({"tokens": agg.get("tokens"), "byModel": {model: agg}}, pricing)
            prompt = to_int(agg.get("promptTokens"))
            by_model.append({"model": model, "requests": to_int(agg.get("requests")), "tokens": to_int(agg.get("tokens")),
                             "promptTokens": prompt, "cachedTokens": to_int(agg.get("cachedTokens")),
                             "outputTokens": max(to_int(agg.get("tokens")) - prompt, to_int(agg.get("candidateTokens")), 0),
                             "cost": mcost, "costPartial": mpartial})
        prompt = to_int(rec.get("promptTokens"))
        output = max(to_int(rec.get("tokens")) - prompt, to_int(rec.get("candidateTokens")), 0)
        tier, tier_usd = tiers.get(email) or (None, None)
        user_credit = credit
        if tier_usd is not None:
            user_credit = _to_currency_from_usd(tier_usd, pricing.currency, table) if tier_usd > 0 else 0.0
        chargeable = None
        if cost is not None and user_credit is not None:
            chargeable = round(max(0.0, cost - user_credit), 6)
        known = chargeable is not None
        row = {"uid": uid_for(email), "email": email, "requests": to_int(rec.get("requests")),
               "tokens": to_int(rec.get("tokens")), "promptTokens": prompt,
               "cachedTokens": to_int(rec.get("cachedTokens")), "outputTokens": output,
               "cost": cost, "costPartial": partial, "credit": user_credit, "chargeable": chargeable,
               "byModel": by_model, "last": iso(rec.get("last")),
               "creditSource": "license" if tier else "fixed", "tier": tier,
               "byProject": project_split(rec, pricing, prefixes, (cost - chargeable) if known else 0.0, "cost",
                                          known)}
        rows.append(row)
        for key in ("requests", "tokens", "promptTokens", "cachedTokens", "outputTokens"):
            totals[key] += row[key]
        if cost is None:
            cost_known = False
        else:
            totals["cost"] += cost
        if chargeable is None:
            charge_known = False
        else:
            totals["chargeable"] += chargeable
    totals["cost"] = round(totals["cost"], 6) if cost_known else None
    totals["chargeable"] = round(totals["chargeable"], 6) if charge_known else None
    if usage.truncated:
        ctx.warn("logging:%s" % urllib.parse.unquote(ag["inference_log"]), "truncated",
                 "Inference log entries were truncated at max_log_entries; the month is incomplete.")
    return {"month": start_local.strftime("%Y-%m"), "start": iso(start), "end": iso(end_local.astimezone(dt.timezone.utc)),
            "complete": end_local.astimezone(dt.timezone.utc) <= now, "timezone": anchor["tz"],
            "currency": pricing.currency, "pricing": pricing.public(models_used),
            "creditPerUserMonthUsd": credit_usd, "creditPerUser": credit, "usageSource": usage.source,
            "users": rows, "totals": totals}


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


def webhook_text(text):
    """The text with < and > replaced by the look-alikes ‹ and ›. Slack and Google Chat read <...> as mentions and
    links (<!channel>, <users/all>, <https://...|text>), and the texts carry names from the HR CSV (unit paths): those
    must stay plain text. Entities (&lt;) would be shown as written by Google Chat, so & and the rest stay as they are."""
    return str(text).replace("<", "\u2039").replace(">", "\u203a")


def send_webhook(ctx, text):
    url = ctx.settings["governance"]["antigravity"].get("webhook_url") or ""
    if not url or not config.valid_webhook(url):
        return False
    try:
        import requests
        resp = requests.post(url, json={"text": webhook_text(text)}, timeout=5, allow_redirects=False)
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
    if plan["aborted"] or ((plan.get("deferredBlocks") or plan.get("capacityHeld"))
                           and not any(c["status"] == "applied" for c in changes)):
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
ENFORCEMENT_STATUS_PATH = "/api/v1/governance/antigravity/enforcement"


def _refresh_status_cache(ctx):
    """Drops only the cached governance status (it carries the last-run summary) and the cached enforcement status
    (latest job execution and deny policy counts)."""
    cache = getattr(ctx.app, "cache", None)
    if cache is not None and hasattr(cache, "clear_matching"):
        cache.clear_matching(lambda k: isinstance(k, tuple) and len(k) > 1 and k[0] == "route"
                             and k[1] in (STATUS_PATH, ENFORCEMENT_STATUS_PATH))


def _suspension_record(ctx, user, until, trigger, now):
    # resetAt is when this person's caps lift (credit caps: the weekly reset or the month start, whichever restores).
    until = parse_rfc3339(user.get("resetAt")) or until
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


def _save_cycle(ctx, payload, internal, changes, result, eval_mode, trigger, suspension_trigger, deny_record=None):
    """Persists suspension records, prunes stale records and expired exception rules, and records the
    run. Returns the users that entered the review queue in this period and were not announced yet. deny_record
    (iam_deny) is where the managed deny policy is attached now ({"attachments", "policyId", "at"}), kept so that a
    later change of the attachment points removes the policy from the old ones."""
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
            # Records of users who are no longer suspended (restored outside this cycle) are dropped. An approved
            # suspension that is not applied yet (deferred, held or failed) stays until its period ends.
            state["suspensions"] = [s for s in state["suspensions"] if s["email"] in blocked_now
                                    or (by_email.get(s["email"]) or {}).get("state") == CAPPED
                                    or (s.get("approved") and not suspension_ended(s, now))]
        # A person still over the cap stays suspended until the current period ends. A record made under an earlier,
        # shorter period (for example weekly before the switch to monthly caps) is extended to that end, so that the
        # record, the restore time shown and a restore without telemetry all follow the current policy.
        for record in state["suspensions"]:
            user = by_email.get(record["email"]) or {}
            if user.get("state") != CAPPED or user.get("overCap") is not True:
                continue
            lift, until = parse_rfc3339(user.get("resetAt")), parse_rfc3339(record.get("until"))
            if lift is not None and until is not None and lift > until:
                record["until"] = iso(lift)
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
        if deny_record is not None:
            state["deny"] = deny_record
    try:
        mutate_state(ctx, apply)
    except ApiError as err:
        LOG.warning("governance state was not saved: %s", err.code)
    return list(fresh)


def _run(ctx, mode, period, window, token_cap, request_cap, eval_mode, trigger, suspension_trigger, kind):
    """One enforcement cycle (mode dry_run | apply). eval_mode "auto" blocks over-cap users;
    "manual" leaves them in the review queue while restores and exception rules still apply. The enforcement backend
    decides how the plan is applied: one atomic update of the primary project's allow policy (iam_allow_swap), the
    managed deny policy at every attachment point (iam_deny), or not at all (notify_only)."""
    previous = (governance_state(ctx).get("lastRun") or {}).get("result")
    deny_record = None
    backend = enforcement_backend(ctx.settings)
    with _WRITE_LOCK:
        for attempt in (1, 2):
            payload, internal = compute_status(ctx, period, window, token_cap, request_cap, mode=eval_mode,
                                               fresh=True)
            plan, gates = payload["plan"], payload["gates"]
            users = {u["uid"]: u for u in payload["users"] or []}
            cfg = internal["cfg"]
            backend = internal.get("backend") or backend
            changes = [{"uid": uid, "email": users[uid]["email"],
                        "action": "disallow" if (users[uid].get("exception") or {}).get("type") == "block" else "block",
                        "status": "skipped",
                        "message": "Deferred to the next run: more suspensions than the change budget allows in one "
                                   "run."}
                       for uid in plan.get("deferredBlocks") or []]
            changes += [{"uid": uid, "email": users[uid]["email"],
                         "action": "disallow" if (users[uid].get("exception") or {}).get("type") == "block" else "block",
                         "status": "skipped",
                         "message": "Deferred to the next run: the deny policy would go over the limit of %d people."
                                    % enforcers.DENY_PRINCIPAL_LIMIT}
                        for uid in plan.get("capacityHeld") or []]
            executed = False
            if mode == "dry_run" or plan["aborted"] or backend == "notify_only":
                if mode == "dry_run":
                    message = "Dry run: no IAM change was made."
                elif plan["aborted"]:
                    message = "Aborted by safety gates: no IAM change was made."
                else:
                    message = NOTIFY_ONLY_MESSAGE
                for action in CYCLE_ACTIONS:
                    for uid in plan[action]:
                        changes.append({"uid": uid, "email": users[uid]["email"], "action": action,
                                        "status": "skipped", "message": message})
                if mode == "apply" and backend != "iam_deny":
                    deny_record = _cleanup_leftover_deny(ctx, internal, trigger)
                break
            if backend == "iam_deny":
                executed, applied, deny_record = _apply_deny(ctx, internal, users, plan)
                changes.extend(applied)
                break
            deny_record = _cleanup_leftover_deny(ctx, internal, trigger) if mode == "apply" else None
            markers = {m["role"]: m["exists"] for m in payload["prerequisites"]["markerRoles"]}
            needed = {_marker_for(action, cfg) for action in CYCLE_ACTIONS if plan[action]} - {None}
            if trigger == "admin":
                unavailable = prepare_markers(ctx, cfg, needed, markers)
            else:
                # A scheduled run never creates roles: nobody is there to see it. The next administrator's action does.
                unavailable = {m: "Marker role %s does not exist. Run setup first." % m
                               for m in needed if markers.get(m) is not True}
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
                    if marker in unavailable:
                        changes.append({"uid": uid, "email": user["email"], "action": action, "status": "failed",
                                        "message": unavailable[marker]})
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
            new_review = _save_cycle(ctx, payload, internal, changes, result, eval_mode, trigger, suspension_trigger,
                                     deny_record=deny_record)
        audit_action = ("cap_dry_run" if mode == "dry_run" else "cap_apply") if kind == "cap" else \
            ("enforce_dry_run" if mode == "dry_run" else "enforce")
        audit_logged = write_audit(ctx, audit_action, {
            "mode": mode, "result": result, "target": "%d users" % len(changes), "gates": gates,
            "changes": [{"email": c["email"], "action": c["action"], "status": c["status"]} for c in changes],
            "period": period, "window": window, "tokenCap": token_cap, "requestCap": request_cap,
            "costCap": payload["policy"]["costCap"], "enforcementMode": eval_mode, "trigger": trigger,
            "review": len(plan["review"]), "backend": backend})
        webhook = False
        counts = (_applied(changes, "block"), _applied(changes, "restore"), _applied(changes, "disallow"))
        if mode == "apply" and kind == "cap" and (executed or plan["aborted"] or plan["manual"]):
            webhook = send_webhook(ctx, "AI Control Plane cap %s by %s: %d blocked, %d restored, %d manual action "
                                        "item(s)%s." % (result, ctx.identity.email, counts[0], counts[1],
                                                        len(plan["manual"]),
                                                        "; aborted by safety gates" if plan["aborted"] else ""))
        elif mode == "apply" and kind == "enforce" and (executed or new_review or
                                                        ((plan["aborted"] or plan.get("deferredBlocks")
                                                          or plan.get("capacityHeld"))
                                                         and previous != "aborted")):
            parts = ["AI Control Plane %s enforcement (%s): %d suspended until the period reset, %d restored, "
                     "%d blocked by exception rules" % (eval_mode, result, counts[0], counts[1], counts[2])]
            if plan.get("deferredBlocks"):
                parts.append("%d suspension(s) deferred to the next run by the change budget (%d per run)"
                             % (len(plan["deferredBlocks"]), plan.get("changeBudget") or 0))
            if plan.get("capacityHeld"):
                parts.append("%d suspension(s) held because the deny policy would go over the limit of %d people"
                             % (len(plan["capacityHeld"]), enforcers.DENY_PRINCIPAL_LIMIT))
            if new_review:
                parts.append("%d user(s) over the cap await review: %s" % (len(new_review), ", ".join(new_review[:10])))
            if plan["aborted"]:
                parts.append("aborted by safety gates")
            if backend == "notify_only":
                parts.append("notify only, so no access was changed")
            webhook = send_webhook(ctx, "; ".join(parts) + ".")
        alerts = _budget_alerts(ctx, internal) if mode == "apply" else 0
    if executed:
        ctx.app.invalidate_governance()
    elif mode == "apply":
        _refresh_status_cache(ctx)
    out = {"mode": mode, "executed": executed, "plan": plan, "gates": gates, "changes": changes,
           "auditLogged": audit_logged, "webhookSent": webhook, "backend": backend, "budgetAlerts": alerts}
    if kind == "enforce":
        out.update({"enforcementMode": eval_mode, "trigger": trigger, "newReview": new_review})
    return out


def _deny_principal_for(person, listed, logged):
    """The deny principal of `person`: for a workforce identity the principal as logged (usage.principals), because
    the subject is case-sensitive and the person key is lower-cased; else the entry the policy lists now, else the one
    built from the key (Google accounts: e-mail addresses are case-insensitive)."""
    raw = (logged or {}).get(person)
    found = identity.deny_principal(raw) if raw else None
    return found or listed.get(person) or identity.deny_principal(person)


def _apply_deny(ctx, internal, users, plan):
    """Applies the plan to the managed deny policy (iam_deny): everyone listed now stays listed unless restored, and
    suspensions and block rules add people. enforcers.apply checks the invariants first and writes nothing when one
    fails. Attachment points that are no longer configured (the scope or deny_attachment changed) get the policy
    removed. Returns (executed, changes, deny record for the governance state)."""
    dstate = internal["deny"]
    listed = dict(internal.get("denyPeople") or {})
    logged = getattr(internal.get("usage"), "principals", None) or {}
    now = internal["now"]
    changes, wanted, adds, removes = [], [], {}, set()
    for action in CYCLE_ACTIONS:
        for uid in plan[action]:
            user = users[uid]
            email = user["email"]
            try:
                check_transition(user["state"], action)
            except ValueError:
                changes.append({"uid": uid, "email": email, "action": action, "status": "skipped",
                                "message": "The user state changed; no IAM change was made."})
                continue
            # A restore removes the entry as listed; a suspension writes the principal as logged.
            principal = (listed.get(email) or identity.deny_principal(email)) if action == "restore" else \
                _deny_principal_for(email, listed, logged)
            if not principal:
                changes.append({"uid": uid, "email": email, "action": action, "status": "failed",
                                "message": "This identity cannot be written to a deny policy."})
                continue
            if action == "restore":
                removes.add(email)
            else:
                adds[email] = principal
            wanted.append((uid, email, action, principal))
    # People who stay listed keep their entry, rewritten in the logged case when it was written lower-cased before.
    desired = {_deny_principal_for(key, listed, logged) for key in listed if key not in removes} | set(adds.values())
    points = [st.attachment for st in dstate.attachments]
    record = {"attachments": list(points), "policyId": enforcers.policy_id(ctx.settings), "at": iso(now)}
    result = enforcers.apply(ctx, dstate, desired)
    executed = bool(result["applied"])
    if result["violations"]:
        message = "The deny policy change was refused: %s" % result["violations"][0]
        for uid, email, action, _principal in wanted:
            changes.append({"uid": uid, "email": email, "action": action, "status": "failed", "message": message})
    else:
        failed = [item for item in result["items"] if item.get("status") == "failed"]
        pending = any(item.get("status") == "pending" for item in result["items"])
        missing = set(result.get("missing") or ())
        for uid, email, action, principal in wanted:
            side = "remove" if action == "restore" else "add"
            bad = [item for item in failed if principal in (item.get(side) or ())]
            if principal in missing and side == "add":
                changes.append({"uid": uid, "email": email, "action": action, "status": "failed",
                                "message": MISSING_ACCOUNT_MESSAGE})
            elif bad:
                where = ", ".join(_attachment_label(item["attachment"]) for item in bad[:3])
                changes.append({"uid": uid, "email": email, "action": action, "status": "failed",
                                "message": "The deny policy update failed at %s (%s)." % (where, bad[0].get("error")
                                                                                         or "error")})
            else:
                changes.append({"uid": uid, "email": email, "action": action, "status": "applied",
                                "message": "Deny policy updated; it takes effect within a few minutes." if pending
                                else "Deny policy updated."})
        if missing:
            ctx.warn(enforcers.SOURCE, "invalid", "%d account(s) could not be listed in the deny policy because IAM "
                                                  "does not find them (deleted, or not a Google account)."
                     % len(missing))
    stale = [ap for ap in ((internal.get("state") or {}).get("deny") or {}).get("attachments") or []
             if isinstance(ap, str) and ap not in points]
    if stale:
        cleanup = enforcers.apply(ctx, enforcers.read_state(ctx, attachments=stale), frozenset(), attachments=stale)
        executed = executed or bool(cleanup["applied"])
        kept = [item["attachment"] for item in cleanup["items"] if item.get("status") == "failed"]
        record["attachments"] += kept
        LOG.info("deny policy removed from %d attachment point(s) that are no longer configured; %d kept",
                 len(stale) - len(kept), len(kept))
    return executed, changes, record


LEFTOVER_RETRY = dt.timedelta(hours=1)


def _cleanup_leftover_deny(ctx, internal, trigger):
    """With another backend than iam_deny: removes the managed deny policy left at the attachment points recorded while
    iam_deny was in use, so nobody stays suspended by a method that is no longer used. Needs Deny Admin; tried by
    administrator runs and at most hourly by scheduled runs. Returns the deny record to store, or None to keep it."""
    record = (internal.get("state") or {}).get("deny") or {}
    points = [ap for ap in record.get("attachments") or [] if isinstance(ap, str)]
    if not points:
        return None
    now = internal["now"]
    last = parse_rfc3339(record.get("cleanupAt"))
    if trigger != "admin" and last is not None and now - last < LEFTOVER_RETRY:
        return None
    if record.get("policyId") and record.get("policyId") != enforcers.policy_id(ctx.settings):
        return None   # written under another policy id: reported by the enforcement status, removed by hand
    cleanup = enforcers.apply(ctx, enforcers.read_state(ctx, attachments=points), frozenset(), attachments=points)
    kept = [item["attachment"] for item in cleanup["items"] if item.get("status") == "failed"]
    LOG.info("leftover deny policies: %d removed, %d kept", len(points) - len(kept), len(kept))
    return dict(record, attachments=kept, cleanupAt=iso(now))


def _budget_alerts(ctx, internal):
    """Announces the budget thresholds (organization units and projects) crossed for the first time this month to the
    webhook. Returns the number of thresholds announced."""
    status = internal.get("budgetStatus")
    if status is None:
        return 0
    from . import budgets, chargeback, org_model   # budgets imports chargeback, which imports this module
    month = chargeback.current_month(ctx)
    try:
        fresh = budgets.record_alerts(ctx, month, budgets.new_alerts(status, month, budgets.load_alerts(ctx)))
        if not fresh:
            return 0
        org = org_model.load(ctx)
    except (ApiError, ValueError) as err:
        LOG.warning("budget alerts not sent: %s", getattr(err, "code", type(err).__name__))
        return 0
    lines = [budgets.alert_text(node, threshold, status, org) for node, threshold in fresh[:20]]
    if len(fresh) > 20:
        lines.append("%d more budget alert(s)." % (len(fresh) - 20))
    send_webhook(ctx, "AI Control Plane budgets: " + " ".join(lines))
    write_audit(ctx, "budget_alert", {"result": "success", "target": "%d thresholds" % len(fresh), "month": month,
                                      "alerts": [{"budget": node["id"], "threshold": threshold}
                                                 for node, threshold in fresh[:50]]})
    return len(fresh)


def cap(ctx):
    """Administrator run with ad hoc caps: every over-cap user is suspended (bulk approval), even in
    manual enforcement mode."""
    mode = ctx.body.get("mode")
    if mode not in ("dry_run", "apply"):
        raise ApiError("bad_request", "Field 'mode' must be 'dry_run' or 'apply'.")
    period, window, token_cap, request_cap = policy_params(ctx, ctx.body)
    if mode == "apply" and enforcement_backend(ctx.settings) == "iam_deny" and jobs.job_name(ctx.settings):
        raise ApiError("bad_request", "The enforcement job applies the saved policy only. Save these caps in the "
                                      "policy and select Run enforcement now, or run a dry run here.")
    return _run(ctx, mode, period, window, token_cap, request_cap, eval_mode="auto", trigger="admin",
                suspension_trigger="bulk", kind="cap")


def enforce(ctx):
    """Saved policy in the configured enforcement mode. Called by Cloud Scheduler (OIDC) or by an
    administrator ("Run now"). With the iam_deny backend and an enforcement job, a run that applies starts the job
    (its service account alone holds Deny Admin) and returns {"triggered": true, "job", "execution", "operation"}."""
    dry = ctx.body.get("dryRun", False)
    if not isinstance(dry, bool):
        raise ApiError("bad_request", "Field 'dryRun' must be true or false.")
    trigger = "scheduler" if ctx.identity.auth_mode == "scheduler" else "admin"
    if not dry and enforcement_backend(ctx.settings) == "iam_deny" and jobs.job_name(ctx.settings):
        return _start_job(ctx, trigger)
    return enforce_cycle(ctx, trigger=trigger, dry_run=dry)


def enforce_cycle(ctx, *, trigger="scheduler", dry_run=False):
    """One enforcement cycle of the saved policy in the configured mode: the run behind the enforce endpoint, also
    called by the enforcement job (controlplane/enforce_job.py). Returns the enforce endpoint's result."""
    ag = ctx.settings["governance"]["antigravity"]
    return _run(ctx, "dry_run" if dry_run else "apply", ag["period"], ag["window"], ag["token_cap"],
                ag["request_cap"], eval_mode=ag.get("enforcement_mode") or "manual", trigger=trigger,
                suspension_trigger="auto", kind="enforce")


def _start_job(ctx, trigger):
    """Starts one execution of the enforcement job (services/jobs.py) and records it in the audit log. An
    administrator's run is recorded as such (note_run_request), so it never counts as a scheduled run."""
    if trigger == "admin":
        note_run_request(ctx, trigger)
    try:
        started = jobs.trigger(ctx)
    except GcpError as err:
        LOG.warning("enforcement job run refused kind=%s", err.kind)
        write_audit(ctx, "enforce_job_run", {"result": "failed", "target": jobs.job_name(ctx.settings),
                                             "trigger": trigger, "error": err.kind})
        raise ApiError("upstream_error", "The enforcement job could not be started (%s). The dashboard service "
                                         "account needs roles/run.jobsExecutor on the job." % err.kind)
    audit_logged = write_audit(ctx, "enforce_job_run", {"result": "success", "target": started["job"],
                                                        "trigger": trigger, "execution": started["execution"]})
    _refresh_status_cache(ctx)
    return {"triggered": True, "mode": "apply", "executed": False, "job": started["job"],
            "execution": started["execution"], "operation": started["operation"], "trigger": trigger,
            "backend": "iam_deny", "auditLogged": audit_logged}


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
            "paidBudget": caps.get("paidBudget"), "creditMonth": caps.get("creditMonth"),
            "note": note, "createdBy": ctx.identity.email, "createdAt": iso(utcnow())}


def _put_exception(state, item):
    state["exceptions"] = [e for e in state.get("exceptions") or [] if e["email"] != item["email"]] + [item]


def _reconcile(ctx):
    """Applies recorded administrator decisions with the iam_deny backend: the enforcement job runs them when one is
    configured (its service account alone writes deny policies), else an enforcement run here. Returns {"via": "job"
    or "run", "started": bool, "error": code or None, "execution", "changes": {person: change of the run}}."""
    if jobs.job_name(ctx.settings):
        note_run_request(ctx, "admin")
        try:
            started = jobs.trigger(ctx)
        except GcpError as err:
            LOG.warning("enforcement job run refused kind=%s", err.kind)
            return {"via": "job", "started": False, "error": err.kind, "execution": None, "changes": {}}
        return {"via": "job", "started": True, "error": None, "execution": started.get("execution"), "changes": {}}
    out = enforce_cycle(ctx, trigger="admin")
    return {"via": "run", "started": True, "error": None, "execution": None,
            "changes": {c["email"]: c for c in out.get("changes") or [] if c.get("email")}}


def _decision_result(user, action, rec, done):
    """The result of one recorded decision after _reconcile: applied, queued (the job applies it, or the run deferred
    it), skipped or failed."""
    base = {"uid": user["uid"], "email": user["email"]}
    if rec["via"] == "job":
        if rec["started"]:
            return dict(base, status="queued", message="Recorded. The enforcement job is applying it; IAM can take a few "
                                                       "minutes to put it into effect.")
        return dict(base, status="queued", message="Recorded. The enforcement job could not be started (%s); the next "
                                                   "scheduled run applies it." % rec["error"])
    change = rec["changes"].get(user["email"])
    if change is None:
        if action == "block":
            return dict(base, status="skipped", message="The user is no longer over the cap, so nobody was suspended.")
        return dict(base, status="applied", message=done)
    if change["status"] == "applied":
        return dict(base, status="applied", message=done)
    return dict(base, status="queued" if change["status"] == "skipped" else "failed", message=change["message"])


def _review_rules(ctx, backend, decision, uids, note):
    """review() with the iam_deny and notify_only backends. A suspension is recorded as approved and an allow as a
    rule for the rest of the period; with iam_deny an enforcement run (or the job) applies them. Notify only refuses
    suspensions."""
    ag = ctx.settings["governance"]["antigravity"]
    payload, internal = compute_status(ctx, ag["period"], ag["window"], ag["token_cap"], ag["request_cap"],
                                       mode="manual")
    users = {u["uid"]: u for u in payload["users"] or []}
    results, approve, allow = [], [], []
    for uid in uids:
        user = users.get(uid)
        if user is None:
            results.append({"uid": uid, "email": None, "status": "skipped", "message": "User not found."})
            continue
        skip = None
        if decision == "suspend":
            if backend == "notify_only":
                skip = NOTIFY_ONLY_REFUSED
            elif user["state"] != EA:
                skip = "The user has no access that can be suspended."
            elif user["overCap"] is not True:
                skip = "The user is not over the cap."
            elif (user["exception"] or {}).get("type") == "allow":
                skip = "An allow exception applies to this user."
            else:
                approve.append(user)
        elif user["state"] in (EA, CAPPED):
            allow.append(user)
        else:
            skip = "The user cannot be allowed in its current state."
        if skip:
            results.append({"uid": uid, "email": user["email"], "status": "skipped", "message": skip})
    if approve or allow:
        def apply(state):
            susp = {s["email"]: s for s in state.get("suspensions") or []}
            for user in approve:
                record = _suspension_record(ctx, user, internal["nextReset"], "review", internal["now"])
                record["approved"] = True
                susp[user["email"]] = record
            for user in allow:
                if (susp.get(user["email"]) or {}).get("approved"):
                    susp.pop(user["email"], None)   # an allow cancels an approved suspension not applied yet
                _put_exception(state, _exception_item(
                    ctx, user["email"], "allow", internal["periodEnd"],
                    note or "Allowed for the rest of the period by an administrator."))
            state["suspensions"] = list(susp.values())
        mutate_state(ctx, apply, required=True)
    restores = [user for user in allow if user["state"] == CAPPED]
    rec = _reconcile(ctx) if backend == "iam_deny" and (approve or restores) else None
    for user in approve:
        results.append(_decision_result(user, "block", rec, "Suspended until the period reset."))
    for user in allow:
        if user["state"] == CAPPED and rec is not None:
            results.append(_decision_result(user, "restore", rec, "Restored for the rest of the period."))
        else:
            results.append({"uid": user["uid"], "email": user["email"], "status": "applied",
                            "message": "Allowed for the rest of the period."})
    done = [r for r in results if r["status"] in ("applied", "queued")]
    audit_logged = write_audit(ctx, "review_%s" % decision, {
        "result": "success" if len(done) == len(results) else ("partial" if done else "failed"),
        "target": "%d users" % len(results), "backend": backend, "via": rec["via"] if rec else None,
        "changes": [{"email": r["email"], "status": r["status"]} for r in results]})
    if approve or allow:
        ctx.app.invalidate_governance()
    return {"decision": decision, "results": results, "auditLogged": audit_logged, "via": rec["via"] if rec else None}


def review(ctx):
    """suspend: over-cap users with access are suspended until the period reset.
    allow: users are allowed for the rest of the period (suspended users are restored now)."""
    decision = ctx.body.get("decision")
    if decision not in ("suspend", "allow"):
        raise ApiError("bad_request", "Field 'decision' must be 'suspend' or 'allow'.")
    uids = _uids(ctx.body.get("uids"))
    note = _note(ctx.body.get("note"))
    backend = enforcement_backend(ctx.settings)
    if backend != "iam_allow_swap":
        return _review_rules(ctx, backend, decision, uids, note)
    ag = ctx.settings["governance"]["antigravity"]
    executed = False
    with _WRITE_LOCK:
        for attempt in (1, 2):
            payload, internal = compute_status(ctx, ag["period"], ag["window"], ag["token_cap"], ag["request_cap"],
                                               mode="manual")
            users = {u["uid"]: u for u in payload["users"] or []}
            cfg = internal["cfg"]
            markers = {m["role"]: m["exists"] for m in payload["prerequisites"]["markerRoles"]}
            cap_problem = False  # False: not checked yet. None: the cap marker can be assigned. Text: why it cannot.
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
                    else:
                        if cap_problem is False:
                            cap_problem = prepare_markers(ctx, cfg, {cfg.cap_marker_id}, markers).get(cfg.cap_marker_id)
                        if cap_problem:
                            results.append({"uid": uid, "email": user["email"], "status": "failed",
                                            "message": cap_problem})
                            continue
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
                        note or "Allowed for the rest of the period by an administrator."))
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
                "costCap": _optional_cap(body, "costCap", False, 1e9),
                # Credit caps (USD): an individual monthly paid budget (0 = credits only) and monthly credit.
                "paidBudget": _optional_cap(body, "paidBudget", False, 1e9),
                "creditMonth": _optional_cap(body, "creditMonth", False, 1e6)}
        if all(v is None for v in caps.values()):
            raise ApiError("bad_request", "Set at least one individual cap.")
    email, uid = _target_email(ctx)
    decide = {"block": _decide_block, "allow": _decide_allow}.get(kind, lambda state: None)
    email, previous, new_state, action = _change_user(ctx, decide, uid=uid, email=email)
    ag = ctx.settings["governance"]["antigravity"]
    until = None
    if scope == "period":
        until = current_period_end(ctx)
    item = _exception_item(ctx, email, kind, until, note, caps)

    def apply(state):
        _put_exception(state, item)
        if action is not None:
            state["suspensions"] = [s for s in state.get("suspensions") or [] if s["email"] != email]
    mutate_state(ctx, apply, required=True)
    enforcement = None
    if action is not None and enforcement_backend(ctx.settings) == "iam_deny":
        # The rule is the decision; an enforcement run (or the job) writes the deny policy.
        enforcement = _decision_result({"uid": uid_for(email), "email": email}, action, _reconcile(ctx),
                                       "Deny policy updated.")
    run = _apply_now(ctx) if (action is None and kind == "limit") else None
    audit_logged = write_audit(ctx, "exception_save", {
        "result": "success", "target": email, "type": kind, "scope": scope, "until": item["until"],
        "caps": caps or None, "iamAction": action, "previousState": previous, "newState": new_state,
        "enforcement": (enforcement or {}).get("status"), "run": (run or {}).get("via")})
    ctx.app.invalidate_governance()
    return {"exception": dict(public_exception(item), email=email, uid=uid_for(email), state=new_state),
            "change": {"action": action, "previousState": previous, "newState": new_state} if action else None,
            "enforcement": enforcement, "run": run, "auditLogged": audit_logged}


def _apply_now(ctx):
    """A rule change that can suspend or restore someone (an individual cap saved, an allow rule or an individual cap
    removed): in automatic mode an enforcement run applies it at once; manual mode leaves it to the review queue and
    the next scheduled run. Returns request_enforcement's result, or None when nothing ran."""
    ag = ctx.settings["governance"]["antigravity"]
    if (ag.get("enforcement_mode") or "manual") != "auto":
        return None
    return request_enforcement(ctx, trigger="admin")


def exception_delete(ctx):
    """Removes one user's exception rule. Removing a block rule restores access (unless
    restoreAccess is false); other rules return the user to the default policy. With the iam_deny backend a removed
    block rule without restoreAccess keeps the person suspended until the current period ends."""
    email, _ = _target_email(ctx)
    if not email:
        raise ApiError("bad_request", "Field 'email' is required.")
    restore_access = ctx.body.get("restoreAccess", True)
    if not isinstance(restore_access, bool):
        raise ApiError("bad_request", "Field 'restoreAccess' must be true or false.")
    current = next((e for e in governance_state(ctx).get("exceptions") or [] if e["email"] == email), None)
    if current is None:
        raise ApiError("not_found", "No exception rule exists for this user.")
    deny = enforcement_backend(ctx.settings) == "iam_deny"
    change, record = None, None
    now = utcnow()
    if current["type"] == "block" and restore_access:
        _, previous, new_state, action = _change_user(ctx, _decide_unblock, email=email)
        if action:
            change = {"action": action, "previousState": previous, "newState": new_state}
            if deny:
                # A suspension that ended now: the next run lifts the deny entry unless a cap applies again.
                record = {"email": email, "since": iso(now), "until": iso(now), "trigger": "unblock",
                          "by": ctx.identity.email, "reasons": []}
    elif current["type"] == "block" and deny:
        _, previous, _, _ = _change_user(ctx, lambda state: None, email=email)
        if previous == BLOCKED:
            record = {"email": email, "since": iso(now), "until": iso(current_period_end(ctx, now)), "trigger": "admin",
                      "by": ctx.identity.email, "reasons": [], "hold": True}

    def apply(state):
        state["exceptions"] = [e for e in state.get("exceptions") or [] if e["email"] != email]
        if record is not None:
            state["suspensions"] = [s for s in state.get("suspensions") or [] if s["email"] != email] + [record]
    mutate_state(ctx, apply, required=True)
    enforcement = None
    if change is not None and deny:
        enforcement = _decision_result({"uid": uid_for(email), "email": email}, "restore", _reconcile(ctx),
                                       "Deny policy updated.")
    run = _apply_now(ctx) if current["type"] in ("allow", "limit") else None
    audit_logged = write_audit(ctx, "exception_delete", {"result": "success", "target": email,
                                                         "type": current["type"],
                                                         "iamAction": change["action"] if change else None,
                                                         "enforcement": (enforcement or {}).get("status"),
                                                         "run": (run or {}).get("via")})
    ctx.app.invalidate_governance()
    return {"email": email, "removed": True, "change": change, "enforcement": enforcement, "run": run,
            "auditLogged": audit_logged}


# ---------------------------------------------------------------------------
# POST /api/v1/governance/antigravity/user-action
# ---------------------------------------------------------------------------

def _resolve_uid(ctx, uid, policy_idx, cfg, usage):
    for email in usage.users:
        if uid_for(email) == uid:
            return email
    direct_scope = {*cfg.replaceable_roles, *cfg.all_extra_access_roles, *cfg.markers, *cfg.privileged_roles}
    for email in policy_idx.direct_users(direct_scope):
        if uid_for(email) == uid:
            return email
    if getattr(ctx.app, "settings", None) is not None:
        for email in ctx.app.settings.blocked_accounts():
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


def _unconditional_marker_conflict(idx, email, cfg):
    """True when the user is in CONFLICTING_BINDINGS solely because unconditional replaceable access roles and marker
    roles coexist (for example roles/discoveryengine.user alongside CustomAntigravityAdminBlocked). An explicit
    administrator allow or disallow resolves this cleanly; automated runs never touch it."""
    direct = idx.user_bindings(email)
    roles = {role for role, _ in direct}
    if roles & (set(cfg.privileged_roles) | set(cfg.all_extra_access_roles)):
        return False
    managed = set(cfg.replaceable_roles) | cfg.markers
    if any(cond for role, cond in direct if role in managed):
        return False
    return bool(roles & set(cfg.replaceable_roles)) and bool(roles & cfg.markers)


def _change_user_rules(ctx, backend, decide, uid=None, email=None):
    """_change_user with the iam_deny and notify_only backends: the person's state comes from the deny policy (notify
    only: enforceable) and nothing is written here; the caller records the rule and requests an enforcement run.
    Returns (email, previous_state, new_state, action) like _change_user; with notify only the action is None."""
    cfg = gov_config(ctx.settings)
    listed = {}
    if backend == "iam_deny":
        dstate = enforcers.read_state(ctx)
        if not dstate.readable:
            raise ApiError("upstream_error", REMEDIATION["DENY_POLICY_UNREADABLE"])
        listed = _deny_people(dstate)
    gstate = governance_state(ctx)
    target = email
    if not target:
        known = list(listed) + [e["email"] for e in gstate.get("exceptions") or []] + \
            [s["email"] for s in gstate.get("suspensions") or []]
        target = next((key for key in known if uid_for(key) == uid), None)
    if not target:
        ag = ctx.settings["governance"]["antigravity"]
        start, end = timeutil.governance_window(ag["period"], ag["window"], **period_anchor(ctx.settings))
        target = _resolve_uid(ctx, uid, PolicyIndex(None, readable=False), cfg, antigravity_usage(ctx, start, end))
    if not target:
        raise ApiError("not_found", "User not found.")
    if backend == "notify_only":
        return target, EA, EA, None
    state, _ = _deny_state(target, listed, active_exceptions(gstate, utcnow()))
    action = decide(state) if callable(decide) else decide
    if action is None:
        return target, state, state, None
    try:
        check_transition(state, action)
    except ValueError:
        if action == "allow":
            raise ApiError("conflict", "The user already has access.")
        raise ApiError("conflict", "The user does not currently have access that can be disallowed.")
    return target, state, TARGET_STATE[action], action


def _change_user(ctx, decide, uid=None, email=None):
    """One user's IAM transition as an etag-guarded read-modify-write (one retry on conflict).
    decide is an action or callable(state) -> action | None. Returns (email, previous_state,
    new_state, action); action None means that no change was needed."""
    backend = enforcement_backend(ctx.settings)
    if backend != "iam_allow_swap":
        return _change_user_rules(ctx, backend, decide, uid=uid, email=email)
    cfg = gov_config(ctx.settings)
    ag = ctx.settings["governance"]["antigravity"]
    start, end = timeutil.governance_window(ag["period"], ag["window"], **period_anchor(ctx.settings))
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
            repairable = (state == NE and reason == "CONFLICTING_BINDINGS" and
                          _unconditional_marker_conflict(idx, target, cfg))
            if callable(decide):
                action = decide(EA if repairable else state)
            else:
                action = decide
            if action is None:
                return target, state, state, None
            if repairable and action in ("allow", "disallow"):
                pass
            elif state in (NE, UNKNOWN):
                raise ApiError("conflict", REMEDIATION.get(reason) or "This user cannot be managed per user.")
            else:
                try:
                    check_transition(state, action)
                except ValueError:
                    if action == "allow":
                        raise ApiError("conflict", "The user already has access.")
                    raise ApiError("conflict", "The user does not currently have access that can be disallowed.")
            marker = _marker_for(action, cfg)
            if marker:
                markers = {m["role"]: m["exists"] for m in marker_status(ctx, cfg)}
                unavailable = prepare_markers(ctx, cfg, {marker}, markers)
                if marker in unavailable:
                    raise ApiError("conflict", unavailable[marker])
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
    would suspend them again); allowing a blocked user removes the block rule. With the iam_deny backend, lifting a
    block also records a suspension that ended now, so the next run removes the deny entry unless a cap applies."""
    ag = ctx.settings["governance"]["antigravity"]
    item = None
    if action == "disallow":
        item = _exception_item(ctx, email, "block", None, "Disallowed by an administrator.")
    elif action == "allow" and previous == CAPPED:
        until = timeutil.governance_period_end(ag["period"], ag["window"], **period_anchor(ctx.settings))
        item = _exception_item(ctx, email, "allow", until, "Restored early by an administrator.")
    ended = None
    if action == "allow" and previous == BLOCKED and enforcement_backend(ctx.settings) == "iam_deny":
        now = iso(utcnow())
        ended = {"email": email, "since": now, "until": now, "trigger": "unblock", "by": ctx.identity.email,
                 "reasons": []}

    def apply(state):
        state["suspensions"] = [s for s in state.get("suspensions") or [] if s["email"] != email]
        if ended is not None:
            state["suspensions"].append(ended)
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
    if action == "allow" and getattr(ctx.app, "settings", None) is not None and ctx.app.settings.is_blocked(email):
        try:
            ctx.app.settings.mutate_auth(lambda auth: (auth.get("blocked") or {}).pop(email, None), ctx.identity.email)
        except ApiError:
            pass
    return public_exception(item)


def user_action(ctx):
    uid = ctx.body.get("uid")
    action = ctx.body.get("action")
    if action not in ("allow", "disallow"):
        raise ApiError("bad_request", "Field 'action' must be 'allow' or 'disallow'.")
    if not isinstance(uid, str) or len(uid) != 16 or any(c not in "0123456789abcdef" for c in uid):
        raise ApiError("bad_request", "Field 'uid' is invalid.")
    backend = enforcement_backend(ctx.settings)
    if backend == "notify_only":
        raise ApiError("conflict", NOTIFY_ONLY_REFUSED)
    email, state, new_state, _ = _change_user(ctx, action, uid=uid)
    exception = _sync_after_action(ctx, email, state, action)
    enforcement = None
    if backend == "iam_deny":
        enforcement = _decision_result({"uid": uid, "email": email}, "disallow" if action == "disallow" else "restore",
                                       _reconcile(ctx), "Deny policy updated.")
    audit_logged = write_audit(ctx, action, {"result": "success", "target": email, "previousState": state,
                                             "newState": new_state, "uid": uid,
                                             "enforcement": (enforcement or {}).get("status")})
    ctx.app.invalidate_governance()
    return {"uid": uid, "email": email, "action": action, "previousState": state, "newState": new_state,
            "auditLogged": audit_logged, "exception": exception, "enforcement": enforcement}


# ---------------------------------------------------------------------------
# GET /api/v1/governance/antigravity/enforcement
# ---------------------------------------------------------------------------

def enforcement_status(ctx):
    """How suspensions are applied: {"backend", "attachment", "policyId", "permissions", "geApp", "agentPlatform",
    "surgeGuard", "job", "realtime", "deny", "leftover", "spendCaps"}. deny (iam_deny): {"readable", "people",
    "partial", "attachments": [{attachment, exists, count, limit, ratio, error, foreign}]}; leftover: deny policies
    recorded while iam_deny was in use and not removed yet, with another backend. Reads only; never raises for an
    unavailable part."""
    settings = ctx.settings
    ag = settings["governance"]["antigravity"]
    backend = enforcement_backend(settings)
    gstate = governance_state(ctx)
    out = {"backend": backend, "attachment": ag.get("deny_attachment") or "projects",
           "policyId": enforcers.policy_id(settings), "permissions": enforcers.denied_permissions(settings),
           "geApp": ag.get("deny_ge_app") is True, "agentPlatform": ag.get("deny_agent_platform") is True,
           "surgeGuard": {"min": to_int(ag.get("surge_guard_min")) or 10,
                          "ratio": float(ag.get("surge_guard_ratio") or 0.5)},
           "job": jobs.status(ctx), "realtime": realtime_status(ctx, gstate),
           "deny": None, "leftover": None, "spendCaps": None}
    record = gstate.get("deny") or {}
    recorded = [ap for ap in record.get("attachments") or [] if isinstance(ap, str)]
    if backend == "iam_deny":
        dstate = enforcers.read_state(ctx)
        _deny_warnings(ctx, dstate)
        out["deny"] = {"readable": dstate.readable, "people": len(_deny_people(dstate)),
                       "partial": len(dstate.partial),
                       "attachments": [{"attachment": st.attachment, "exists": st.exists, "count": len(st.principals),
                                        "limit": enforcers.DENY_PRINCIPAL_LIMIT,
                                        "ratio": round(len(st.principals) / float(enforcers.DENY_PRINCIPAL_LIMIT), 4),
                                        "error": st.error, "foreign": st.foreign} for st in dstate.attachments]}
        stale = [ap for ap in recorded if ap not in {st.attachment for st in dstate.attachments}]
        if stale:
            out["leftover"] = {"attachments": stale, "policyId": record.get("policyId") or out["policyId"]}
    elif recorded:
        out["leftover"] = {"attachments": recorded, "policyId": record.get("policyId") or out["policyId"]}
        ctx.warn(enforcers.SOURCE, "partial_data",
                 "Deny policies written while the enforcement method was the IAM deny policy remain at %d attachment "
                 "point(s); the people listed there stay suspended until they are removed." % len(recorded))
    if record.get("policyId") and record.get("policyId") != out["policyId"]:
        ctx.warn(enforcers.SOURCE, "partial_data",
                 "The deny policy id changed from %s to %s; remove the old policy with gcloud iam policies delete."
                 % (record["policyId"], out["policyId"]))
    try:
        from . import spend_caps
        out["spendCaps"] = spend_caps.get_spend_caps(ctx)
    except (ApiError, GcpError) as err:
        LOG.warning("spend caps status unavailable: %s", getattr(err, "code", None) or getattr(err, "kind", ""))
    except Exception:  # pylint: disable=broad-except
        LOG.exception("spend caps status failed")
    markers = gstate.get("markers")
    if backend != "iam_allow_swap" and markers:
        out["markers"] = {key: markers.get(key) for key in ("at", "holders", "released", "waiting", "manual", "error")}
        if markers.get("waiting"):
            ctx.warn(MARKER_SOURCE, "partial_data",
                     "%d people still hold a marker role of the allow-policy method; they get their access role back "
                     "once the current enforcement method no longer needs the marker." % markers["waiting"])
        if markers.get("error"):
            ctx.warn(MARKER_SOURCE, "api_error",
                     "Marker roles left by the allow-policy method could not be checked or released (%s). The dashboard "
                     "service account needs the project IAM roles of the allow-policy method until they are released."
                     % markers["error"])
    return out


SPEND_CAP_VIEWER_KEYS = ("enabled", "accountConfigured", "error")


def enforcement_viewer_view(data):
    """GET .../enforcement for viewers and guests (the route's viewer_view, applied after the cache, which does not
    know the caller): spend caps keep only SPEND_CAP_VIEWER_KEYS. Their budget names carry the billing account id
    (administrators only, like /api/v1/budgets/spend-caps), and caps and desired amounts are keyed by project id, which
    guest redaction cannot reach because dict keys are never scrubbed."""
    caps = data.get("spendCaps") if isinstance(data, dict) else None
    if not isinstance(caps, dict):
        return data
    return dict(data, spendCaps=dict({key: caps.get(key) for key in SPEND_CAP_VIEWER_KEYS}, adminOnly=True))


# ---------------------------------------------------------------------------
# Marker roles left by iam_allow_swap after a switch to iam_deny or notify_only
# ---------------------------------------------------------------------------

MARKER_SOURCE = "api:cloudresourcemanager.projects.iamPolicy"


def _marker_candidates(ctx, backend, idx, cfg, holders, now):
    """(release, waiting, manual) of the marker holders. A person is released once the current backend no longer
    needs the marker; see release_markers."""
    gstate = governance_state(ctx)
    exceptions = active_exceptions(gstate, now)
    records = {s["email"]: s for s in gstate.get("suspensions") or []}
    listed, rows, usage_ok = set(), {}, False
    if backend == "iam_deny":
        ag = ctx.settings["governance"]["antigravity"]
        payload, internal = compute_status(ctx, ag["period"], ag["window"], ag["token_cap"], ag["request_cap"],
                                           mode="manual")
        dstate = internal.get("deny")
        if dstate is not None and dstate.readable:
            listed = {identity.person_key(p) for p in dstate.principals}   # listed at every attachment point
        rows = {u["email"]: u for u in payload["users"] or []}
        usage = internal.get("usage")
        usage_ok = bool(usage is not None and usage.ok)
    release, waiting, manual = [], [], []
    for email in holders:
        state, reason = classify(email, False, idx, cfg)
        if state == NE and reason == "CONFLICTING_BINDINGS" and _unconditional_marker_conflict(idx, email, cfg):
            release.append(email)   # also holds the access role: removing the marker changes no access
            continue
        if state not in (CAPPED, BLOCKED):
            manual.append(email)
            continue
        kind = (exceptions.get(email) or {}).get("type")
        record = records.get(email)
        running = record is not None and not suspension_ended(record, now)
        if kind == "allow":
            free = True
        elif kind == "block" or running:
            free = False
        elif backend == "iam_deny":
            row = rows.get(email)
            free = usage_ok and (row is None or row.get("overCap") is False)
        else:
            free = True
        (release if free or email in listed else waiting).append(email)
    return release, waiting, manual


def _save_markers(ctx, summary):
    try:
        mutate_state(ctx, lambda state: state.__setitem__("markers", summary))
    except ApiError as err:
        LOG.warning("marker release summary was not saved: %s", err.code)
    return summary


def release_markers(ctx, trigger="scheduled"):
    """With the iam_deny or notify_only backend: gives the access role back to the people who still hold a marker role
    of iam_allow_swap in the primary project's allow policy (suspended or blocked before the backend was switched), so
    that a switch never leaves anyone suspended for good. A person is released once the current backend no longer
    needs the marker, the same way iam_allow_swap would have restored them:
      iam_deny     the deny policy lists them at every attachment point, or nothing stops them now (an allow rule; or
                   no block rule, no running suspension record and usage read without being over a cap);
      notify_only  an allow rule, or no block rule and no running suspension record.
    People who also hold the access role only lose the marker. One etag-guarded setIamPolicy for everyone released
    (re-read and retried once on a conflict). Needs the project IAM write role of iam_allow_swap; without it nothing
    changes. Never raises for an upstream failure. Run by the maintenance tick (services/maintenance.py); the summary
    (counts only) is kept in the governance state for the enforcement status. Returns {"at", "holders", "released",
    "waiting", "manual", "error"}, or None with iam_allow_swap."""
    backend = enforcement_backend(ctx.settings)
    if backend == "iam_allow_swap":
        return None
    cfg = gov_config(ctx.settings)
    now = utcnow()
    out = {"at": iso(now), "holders": 0, "released": 0, "waiting": 0, "manual": 0, "error": None}
    released = []
    with _WRITE_LOCK:
        try:
            policy = get_policy(ctx)
        except GcpError as err:
            LOG.warning("marker roles: getIamPolicy failed kind=%s", err.kind)
            out["error"] = err.kind
            return _save_markers(ctx, out)
        idx = PolicyIndex(policy)
        holders = sorted(idx.direct_users(cfg.markers))
        out["holders"] = len(holders)
        if not holders:
            return _save_markers(ctx, out)
        release, waiting, manual = _marker_candidates(ctx, backend, idx, cfg, holders, now)
        out["manual"] = len(manual)
        for attempt in (1, 2):
            if not release:
                break
            try:
                set_policy(ctx, apply_changes(policy, [(email, "allow") for email in release], cfg))
                released = list(release)
                break
            except GcpError as err:
                if err.kind == "conflict" and attempt == 1:
                    try:
                        policy = get_policy(ctx)
                    except GcpError as again:
                        out["error"] = again.kind
                        break
                    idx = PolicyIndex(policy)
                    still = idx.direct_users(cfg.markers)
                    release = [email for email in release if email in still]
                    continue
                LOG.warning("marker roles: setIamPolicy failed kind=%s", err.kind)
                out["error"] = err.kind
                break
        out["released"] = len(released)
        out["waiting"] = out["holders"] - out["released"] - out["manual"]
    if released:
        write_audit(ctx, "marker_release", {"result": "success", "target": "%d users" % len(released),
                                            "backend": backend, "trigger": trigger, "users": released[:50]})
        ctx.app.invalidate_governance()
    LOG.info("marker roles: %d holders, %d released, %d waiting, %d manual", out["holders"], out["released"],
             out["waiting"], out["manual"])
    return _save_markers(ctx, out)


# ---------------------------------------------------------------------------
# Full AI service & dashboard access block / restore (Users & Access drawer)
# ---------------------------------------------------------------------------

AI_ROLE_PREFIXES = (
    "roles/discoveryengine.",
    "roles/businessaicode.",
    "roles/cloudaicompanion.",
    "roles/aiplatform.",
    "roles/notebooklm.",
    "roles/geminidataanalytics.",
    "roles/modelarmor.",
    "roles/mcp.",
)
AI_PERMISSION_PREFIXES = (
    "discoveryengine.",
    "businessaicode.",
    "cloudaicompanion.",
    "aiplatform.",
    "notebooklm.",
    "geminidataanalytics.",
    "modelarmor.",
)
PROJECT_WIDE_PRIVILEGED_ROLES = ("roles/owner", "roles/editor", "roles/resourcemanager.projectIamAdmin")


def _custom_role_is_ai(ctx, role_name, cache):
    if role_name in cache:
        return cache[role_name]
    if not str(role_name or "").startswith("projects/"):
        cache[role_name] = False
        return False
    try:
        data = ctx.rest("GET", "%s/%s" % (IAM_V1, role_name))
    except GcpError:
        cache[role_name] = False
        return False
    perms = [str(p) for p in data.get("includedPermissions") or []]
    if "resourcemanager.projects.setIamPolicy" in perms:
        cache[role_name] = False
        return False
    cache[role_name] = any(p.startswith(AI_PERMISSION_PREFIXES) for p in perms)
    return cache[role_name]


def is_ai_service_role(ctx, role_name, cfg, custom_cache):
    role = str(role_name or "")
    if role in cfg.markers or role in cfg.replaceable_roles or role in cfg.all_extra_access_roles:
        return True
    if role.startswith(AI_ROLE_PREFIXES):
        return True
    return _custom_role_is_ai(ctx, role, custom_cache)


def _normalize_restore_roles(removed_roles, cfg, fallback=None):
    out = []
    for role in removed_roles:
        mapped = cfg.access_role if (role in cfg.markers or role in cfg.replaceable_roles) else role
        if mapped not in out:
            out.append(mapped)
    if not out and fallback:
        for role in fallback:
            if role not in cfg.markers and role not in out:
                out.append(role)
    return out


def set_user_ai_access(ctx, action, uid=None, email=None, note=""):
    """Blocks or restores a user's entire AI access on this project (Gemini Enterprise, Gemini Notebook, Antigravity,
    Gemini Platform IAM roles) and revokes or restores their AI Control Plane dashboard sign-in."""
    if action not in ("block_all", "unblock_all"):
        raise ApiError("bad_request", "Field 'action' must be 'block_all' or 'unblock_all'.")
    note = _note(note)
    cfg = gov_config(ctx.settings)
    ag = ctx.settings["governance"]["antigravity"]
    start, end = timeutil.governance_window(ag["period"], ag["window"], **period_anchor(ctx.settings))
    mgr = getattr(ctx.app, "settings", None)
    existing_blocks = mgr.blocked_accounts() if mgr is not None else {}
    custom_cache = {}
    removed_roles = []
    restored_roles = []
    residual_warnings = []

    with _WRITE_LOCK:
        usage = antigravity_usage(ctx, start, end)
        for attempt in (1, 2):
            try:
                policy = get_policy(ctx)
            except GcpError as err:
                LOG.warning("getIamPolicy failed kind=%s", err.kind)
                raise ApiError("upstream_error", "The project IAM policy could not be read.")
            idx = PolicyIndex(policy)
            target = (email or "").strip().lower() or _resolve_uid(ctx, uid, idx, cfg, usage)
            if not target or not config.EMAIL_RE.match(target) or not is_human(target):
                raise ApiError("not_found", "User not found.")
            if target == ctx.identity.email:
                raise ApiError("conflict", "You cannot block your own account.")
            if action == "block_all" and is_admin_email(target, ctx.settings["security"].get("admins")):
                raise ApiError("conflict", "This user is a dashboard administrator. Remove administrator privileges "
                                           "in Settings > Access control first.")
            direct = idx.user_bindings(target)
            direct_roles = {role for role, _ in direct}
            if action == "block_all":
                project_priv = sorted(direct_roles & set(PROJECT_WIDE_PRIVILEGED_ROLES))
                if project_priv:
                    raise ApiError("conflict",
                                   "This user holds %s on the project, which grants full project control beyond AI "
                                   "services. Remove that role in Google Cloud IAM first." % project_priv[0])
                unconditional_ai = [role for role, cond in direct
                                    if not cond and is_ai_service_role(ctx, role, cfg, custom_cache)]
                prior = (existing_blocks.get(target) or {}).get("restoreRoles") or []
                restore_roles = _normalize_restore_roles(unconditional_ai, cfg, fallback=prior)
                removed_roles = list(unconditional_ai)
                if unconditional_ai:
                    new_policy = copy.deepcopy(policy)
                    new_policy["bindings"] = [dict(b) for b in new_policy.get("bindings") or []]
                    member = "user:%s" % target
                    for role in unconditional_ai:
                        _remove_member(new_policy, role, member)
                    new_policy["version"] = 3
                    try:
                        set_policy(ctx, new_policy)
                    except GcpError as err:
                        if err.kind == "conflict" and attempt == 1:
                            continue
                        LOG.warning("setIamPolicy failed kind=%s", err.kind)
                        raise ApiError("upstream_error", "The IAM policy update failed.")
                cond_ai = [role for role, cond in direct if cond and is_ai_service_role(ctx, role, cfg, custom_cache)]
                if cond_ai:
                    residual_warnings.append("Conditional project IAM bindings remain (%s) and must be removed in "
                                             "Google Cloud IAM." % ", ".join(sorted(set(cond_ai))))
                group_ai = any(grants_via_non_user(idx.non_user_members(b.get("role")), target)
                               for b in idx.bindings
                               if is_ai_service_role(ctx, b.get("role"), cfg, custom_cache))
                if group_ai:
                    residual_warnings.append("A Google Group or domain IAM binding on an AI role still grants access.")
                anc = make_ancestor_check(ctx, cfg, extra_prefixes=AI_ROLE_PREFIXES)(target)
                if anc is True:
                    residual_warnings.append("An inherited folder or organization IAM binding still grants access.")
                break
            else:
                prior = (existing_blocks.get(target) or {}).get("restoreRoles") or []
                restored_roles = [r for r in prior if r not in cfg.markers] or [cfg.access_role]
                new_policy = copy.deepcopy(policy)
                new_policy["bindings"] = [dict(b) for b in new_policy.get("bindings") or []]
                member = "user:%s" % target
                for role in (*cfg.markers,):
                    _remove_member(new_policy, role, member)
                for role in restored_roles:
                    _add_member(new_policy, role, member)
                new_policy["version"] = 3
                try:
                    set_policy(ctx, new_policy)
                except GcpError as err:
                    if err.kind == "conflict" and attempt == 1:
                        continue
                    LOG.warning("setIamPolicy failed kind=%s", err.kind)
                    raise ApiError("upstream_error", "The IAM policy update failed.")
                break

    if mgr is not None:
        if action == "block_all":
            from . import access as access_svc, iap_access
            admins_ui, allowed_ui = access_svc._ui_lists(mgr)
            if target in allowed_ui:
                access_svc._save_lists(ctx, admins_ui, [m for m in allowed_ui if m != target])
                try:
                    iap_access.revoke(ctx.app, ctx.quota_project, [target])
                except Exception:  # pylint: disable=broad-except
                    pass
            epoch = int(time.time() * 1000)
            block_doc = {"at": iso(utcnow()), "by": ctx.identity.email,
                         "note": note or "All AI access blocked by an administrator.",
                         "removedRoles": removed_roles, "restoreRoles": restore_roles}

            def apply_block(auth):
                auth["revoked"][target] = epoch
                auth.setdefault("blocked", {})[target] = block_doc
            mgr.mutate_auth(apply_block, ctx.identity.email)
        else:
            mgr.mutate_auth(lambda auth: (auth.get("blocked") or {}).pop(target, None), ctx.identity.email)

    def apply_gov(state):
        state["suspensions"] = [s for s in state.get("suspensions") or [] if s["email"] != target]
        if action == "block_all":
            _put_exception(state, _exception_item(ctx, target, "block", None,
                                                  note or "All AI access blocked by an administrator."))
        else:
            state["exceptions"] = [e for e in state.get("exceptions") or []
                                   if not (e["email"] == target and e["type"] == "block")]
    try:
        mutate_state(ctx, apply_gov)
    except ApiError as err:
        LOG.warning("governance state was not saved after %s: %s", action, err.code)

    audit_logged = write_audit(ctx, action, {
        "result": "success", "target": target, "uid": uid_for(target),
        "removedRoles": removed_roles if action == "block_all" else None,
        "restoredRoles": restored_roles if action == "unblock_all" else None,
        "dashboardBlocked": action == "block_all",
        "warnings": residual_warnings or None})
    ctx.app.invalidate_governance()
    return {"uid": uid_for(target), "email": target, "action": action,
            "aiBlocked": action == "block_all",
            "removedRoles": removed_roles, "restoredRoles": restored_roles,
            "warnings": residual_warnings, "auditLogged": audit_logged}


# ---------------------------------------------------------------------------
# POST /api/v1/governance/antigravity/setup
# ---------------------------------------------------------------------------

def setup(ctx):
    """Creates the marker roles that are missing and restores the ones deleted in the last 7 days."""
    cfg = gov_config(ctx.settings)
    with _WRITE_LOCK:
        results, audit_logged = _setup_markers(ctx, cfg, "admin")
    ctx.app.invalidate_governance()
    return {"results": results, "auditLogged": audit_logged}


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
