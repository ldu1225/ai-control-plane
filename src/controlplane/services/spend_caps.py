"""Google Cloud spend caps for project budgets marked as hard caps (opt-in: governance.antigravity.spend_caps).

For every project budget with action hard_cap (services/budgets.py) the control plane keeps one Cloud Billing spend
cap budget on the billing account of workspace.billing.account_id (Cloud Billing Budget API v1, "Using the Cloud
Billing Budget API for spend cap budgets"). When the project's gross estimated cost of the Agent Platform service
reaches the amount, Google pauses new Agent Platform usage in that project for the rest of the month. Google lifts the
cap at the start of the next month; an administrator can lift it earlier (lift).

What Google requires of a spend cap budget, and what the control plane sends (create_body):
- budgetFilter: exactly one project (projects/NUMBER, from Resource Manager) and exactly one eligible service (Agent
  Platform, found in the Cloud Billing Catalog), calendarPeriod MONTH, creditTypesTreatment EXCLUDE_ALL_CREDITS and
  no other filter (no resource ancestors, credit types, subaccounts or labels);
- amount.specifiedAmount >= 0 in the billing account currency;
- exactly three CURRENT_SPEND threshold rules at 0.5, 0.8 and 1.0;
- notificationsRule.enableProjectLevelRecipients true and nothing else (no Pub/Sub topic, no monitoring channels,
  default recipients kept);
- ownershipScope unset (all users) and spendCap.inputState CONFIGURED on creation;
- while spendCap.outputState is ENFORCED only spendCap.inputState may change. apply_caps then changes nothing; only an
  explicit lift patches spendCap.inputState to AWAITING_NEXT_PERIOD.
Services eligible for spend caps (Cloud Billing documentation, November 2026): Gemini API (AEFD-7695-64FA,
generativelanguage.googleapis.com), Gemini Enterprise Agent Platform (C7E2-9256-1C43, aiplatform.googleapis.com),
Cloud Run (152E-C115-5142) and Cloud Run functions (29E7-DA93-CA13). Spend caps need a first-party Cloud Billing
account (reseller accounts are out of scope) and billing.budgets.configureSpendCap.

Ownership: the control plane's budgets are those whose display name is DISPLAY_PREFIX + project id (Google allows 60
characters, so project ids of at most 38 characters). Other budgets of the account are read only to learn the account
currency and are never changed or deleted. A cap that is no longer wanted (its budget was removed or is no longer a
hard cap) is reported as an orphan and kept; deleting it is left to an administrator in the Cloud console. The name
alone proves nothing, so before an update or a lift the budget must also cover exactly that project (by number) and
Agent Platform and have a spend cap (_not_owned); otherwise it is left alone and reported as not_owned.

Scope: spend caps are created or changed only for workspace projects (ctx.all_projects, see
budgets.workspace_projects). The billing account can also pay for projects of other teams, which a dashboard
administrator must not be able to pause; a hard cap of another project is reported as outside_workspace.

Amount changes: an update patches amount.specifiedAmount. When the amount goes up it also sets spendCap.inputState
CONFIGURED, as the documented update does: Google configures a lifted cap again only after its amount is increased. A
lower amount leaves the spend cap state alone, so a cap an administrator lifted stays lifted until the next month.

Currency: dashboard budgets are in USD; a spend cap is in the billing account currency (the currency of the existing
budgets, else billingAccounts.get currencyCode). Caps are created or changed only on a USD billing account, where the
amount carries over unchanged. On any other currency the control plane refuses (status currency_mismatch) instead of
converting with an exchange rate, because a stale rate would move the point where Google stops usage. Lifting works in
any currency.

Measurement differs from the dashboard budget: Google measures the gross estimated list cost of all Agent Platform
usage in the project (every caller, credits excluded), while the dashboard budget counts the paid share of the usage
attributed to the project. For usage billed under Agent Platform the cap therefore stops usage no later than the
dashboard budget is used up. Usage billed under other services (for example the Gemini API) and subscription costs
are not covered.

Google is called only when spend caps are on and a billing account id is set; read_caps never raises GcpError.
"""

from __future__ import annotations

import decimal
import logging
import re
import urllib.parse

from .. import config
from ..gcp import GcpError
from ..httpkit import ApiError
from . import budgets, chargeback

LOG = logging.getLogger("controlplane.spend_caps")

BUDGETS_V1 = "https://billingbudgets.googleapis.com/v1"
BILLING_V1 = "https://cloudbilling.googleapis.com/v1"
CRM_V3 = "https://cloudresourcemanager.googleapis.com/v3"
DISPLAY_PREFIX = "AI Control Plane cap: "
MAX_DISPLAY_NAME = 60
# Cloud Billing Catalog display names of Agent Platform, used only to find the service: the billing product was
# renamed, so the new names and the old one are all matched (exact, case-insensitive).
SERVICE_NAMES = ("Agent Platform", "Gemini Enterprise Agent Platform", "Vertex AI")
SERVICE_ID = "C7E2-9256-1C43"   # Agent Platform in the documented list of services eligible for spend caps
THRESHOLDS = (0.5, 0.8, 1.0)
CONFIGURED = "CONFIGURED"
ENFORCED = "ENFORCED"
AWAITING_NEXT_PERIOD = "AWAITING_NEXT_PERIOD"
STATES = ("STATE_UNSPECIFIED", CONFIGURED, ENFORCED, AWAITING_NEXT_PERIOD)
OPS = ("create", "update", "none", "orphan")
BUDGET_PAGE_SIZE = 100          # the API maximum
MAX_BUDGET_PAGES = 100
SERVICE_PAGE_SIZE = 5000
MAX_SERVICE_PAGES = 5
LOOKUP_TTL = 24 * 3600          # service names, project numbers and account currencies do not change
# Used with fullmatch.
ACCOUNT_RE = re.compile(r"[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}")
SERVICE_RE = re.compile(r"services/[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}")
BUDGET_NAME_RE = re.compile(r"billingAccounts/([0-9A-Fa-f]{6}-[0-9A-Fa-f]{6}-[0-9A-Fa-f]{6})"
                            r"/budgets/[A-Za-z0-9_\-]{1,128}")
PROJECT_NAME_RE = re.compile(r"projects/([0-9]{1,30})")
CURRENCY_RE = re.compile(r"[A-Z]{3}")
REASON_RE = re.compile(r"[A-Z_]{1,40}")
# A 403 whose message says the API is disabled in the quota project, not that a permission is missing.
DISABLED_HINTS = ("has not been used", "SERVICE_DISABLED", "is disabled")
BLOCKED = {
    "enforced": "Google is enforcing this spend cap, so only a lift can change it. Lift it or wait for the next month.",
    "name_too_long": "The project id is too long for the 60-character budget name, so no spend cap is managed for it.",
    # The billing account can also pay for projects of other teams; a dashboard administrator must not pause those.
    "outside_workspace": "The project is not part of this workspace, so the control plane does not manage a spend cap "
                         "for it.",
}
# The display name alone proves nothing (anyone with budget rights on the account, or a second deployment, can use it):
# a budget is changed or lifted only when it covers exactly the project and Agent Platform and has a spend cap.
NOT_OWNED = ("The budget named for this project is not a spend cap of the control plane (it covers other projects or "
             "services, or has no spend cap), so it was left unchanged.")
AUDIT_CHANGES = 200             # per-project results kept in the spend_caps_apply audit entry
ORPHAN = ("The project is no longer a hard cap. Its spend cap was kept; delete it in the Cloud console if it is not "
          "needed any more.")


def _sentence(text):
    return text[:1].upper() + text[1:]


def _text(value, limit):
    return value[:limit] if isinstance(value, str) and value else None


def _strings(value, limit=10):
    """The strings of a JSON list (at most `limit`), else []."""
    return [item for item in value if isinstance(item, str)][:limit] if isinstance(value, list) else []


def _currency_message(currency):
    return ("The billing account is billed in %s. Budgets are in USD, so the control plane sets spend caps only on USD "
            "billing accounts; set this cap in the Cloud console." % (currency or "an unknown currency"))


def _message(err, what):
    """A curated message for a GcpError (upstream messages are never passed on); `what` starts in lower case."""
    kind = getattr(err, "kind", "")
    reason = getattr(err, "reason", "") or ""
    if kind == "denied" and any(hint in (getattr(err, "message", "") or "") for hint in DISABLED_HINTS):
        return ("The Cloud Billing Budget API (billingbudgets.googleapis.com) is not enabled in the quota project of "
                "the control plane. Enable it there and try again.")
    if kind == "denied":
        return ("Access to %s was denied. The service identity needs budget permissions on the billing account, "
                "including billing.budgets.configureSpendCap." % what)
    if kind == "not_found":
        return "%s was not found." % _sentence(what)
    if kind == "credentials":
        return "Google Cloud credentials are unavailable."
    if kind == "timeout":
        return "%s timed out." % _sentence(what)
    if kind in ("invalid", "precondition", "conflict"):
        code = reason if REASON_RE.fullmatch(reason) else str(getattr(err, "status", None) or kind)
        return "Google refused the request for %s (%s)." % (what, code)
    return "%s is temporarily unavailable." % _sentence(what)


# ---------------------------------------------------------------------------
# Settings and desired caps
# ---------------------------------------------------------------------------

def enabled(settings):
    """True when governance.antigravity.spend_caps is on."""
    try:
        return settings["governance"]["antigravity"].get("spend_caps") is True
    except (KeyError, TypeError, AttributeError):
        return False


def account_id(settings):
    """The billing account id of workspace.billing.account_id (upper case), or "" when it is not set or invalid."""
    try:
        raw = settings["workspace"]["billing"].get("account_id")
    except (KeyError, TypeError, AttributeError):
        return ""
    value = raw.strip().upper() if isinstance(raw, str) else ""
    return value if ACCOUNT_RE.fullmatch(value) else ""


def _gate(settings):
    """None when the control plane may call Google for spend caps, else the reason."""
    if not enabled(settings):
        return "Spend caps are off. Turn on governance.antigravity.spend_caps to manage them."
    if not account_id(settings):
        return "Spend caps need the billing account id in workspace.billing.account_id."
    return None


def display_name(project):
    """Display name of the control plane's spend cap budget of `project`."""
    return DISPLAY_PREFIX + project


def desired_caps(tree):
    """{project id: amount in USD} of the project budgets with action hard_cap of a budgets.BudgetTree."""
    out = {}
    for node in getattr(tree, "nodes", None) or ():
        if node.get("scope") == "project" and node.get("action") == "hard_cap":
            out[node["project"]] = float(node["monthlyPaidUsd"])
    return out


def _valid_amount(value):
    """A number from 0 to budgets.MAX_AMOUNT (NaN, infinities and huge integers fail the comparison)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= budgets.MAX_AMOUNT


def _money(amount, currency):
    """Money of a non-negative amount, rounded half up to cents."""
    cents = chargeback.to_cents(amount) or 0
    return {"currencyCode": currency, "units": str(cents // 100), "nanos": (cents % 100) * 10000000}


def _amount_of(budget):
    """(amount, currency) of a budget's amount.specifiedAmount; amount None without a readable amount."""
    amount = budget.get("amount")
    spec = amount.get("specifiedAmount") if isinstance(amount, dict) else None
    if not isinstance(spec, dict):
        return None, None
    code = spec.get("currencyCode")
    code = code if isinstance(code, str) and CURRENCY_RE.fullmatch(code) else None
    try:
        value = decimal.Decimal(int(spec.get("units") or 0)) + decimal.Decimal(int(spec.get("nanos") or 0)) / 10 ** 9
    except (TypeError, ValueError, decimal.InvalidOperation):
        return None, code
    return float(value), code


# ---------------------------------------------------------------------------
# Google calls
# ---------------------------------------------------------------------------

def _list(ctx, url, key, page_size, max_pages):
    """(items, truncated) of a paginated GET list."""
    items, token = [], None
    for _ in range(max_pages):
        params = {"pageSize": page_size}
        if token:
            params["pageToken"] = token
        data = ctx.rest("GET", url, params=params) or {}
        items.extend(item for item in data.get(key) or () if isinstance(item, dict))
        token = data.get("nextPageToken")
        if not token:
            return items, False
    return items, True


def _cached(ctx, key, compute):
    """compute() kept in the application cache for LOOKUP_TTL seconds (None is not kept)."""
    cache = getattr(getattr(ctx, "app", None), "cache", None)
    if cache is not None:
        hit, value = cache.get(key)
        if hit:
            return value
    value = compute()
    if cache is not None and value is not None:
        cache.set(key, value, LOOKUP_TTL)
    return value


def _find_service(ctx):
    services, _ = _list(ctx, "%s/services" % BILLING_V1, "services", SERVICE_PAGE_SIZE, MAX_SERVICE_PAGES)
    wanted = [name.casefold() for name in SERVICE_NAMES]
    best = None
    for svc in services:
        name = svc.get("name")
        if not isinstance(name, str) or not SERVICE_RE.fullmatch(name):
            continue
        display = svc.get("displayName")
        label = " ".join(display.split()).casefold() if isinstance(display, str) else ""
        if name == "services/" + SERVICE_ID:
            rank = -1
        elif label in wanted:
            rank = wanted.index(label)
        else:
            continue
        if best is None or rank < best[0]:
            best = (rank, name)
    return best[1] if best else None


def service_name(ctx):
    """"services/XXXX-XXXX-XXXX" of Agent Platform in the Cloud Billing Catalog, or None when the catalog lists no such
    service. A service matches by display name (SERVICE_NAMES: the new and the old billing name) or by the documented
    service id, which is preferred when several match. Cached for a day; raises GcpError."""
    return _cached(ctx, ("spend_caps", "service"), lambda: _find_service(ctx))


def project_number(ctx, project):
    """Project number of `project` from Resource Manager (cached for a day), or None. Raises GcpError."""
    def compute():
        data = ctx.rest("GET", "%s/projects/%s" % (CRM_V3, urllib.parse.quote(project, safe=""))) or {}
        found = PROJECT_NAME_RE.fullmatch(str(data.get("name") or ""))
        return found.group(1) if found else None
    return _cached(ctx, ("spend_caps", "project_number", project), compute)


def account_currency(ctx, account):
    """ISO currency code of the billing account (billingAccounts.get currencyCode, cached for a day), or None. Raises
    GcpError."""
    def compute():
        data = ctx.rest("GET", "%s/billingAccounts/%s" % (BILLING_V1, account)) or {}
        code = data.get("currencyCode")
        return code if isinstance(code, str) and CURRENCY_RE.fullmatch(code) else None
    return _cached(ctx, ("spend_caps", "currency", account), compute)


def _own_name(name, account):
    found = BUDGET_NAME_RE.fullmatch(name) if isinstance(name, str) else None
    return bool(found and found.group(1).upper() == account)


def _own_project(budget):
    """The project id of a budget whose display name marks it as the control plane's spend cap, else None."""
    name = budget.get("displayName")
    if not isinstance(name, str) or not name.startswith(DISPLAY_PREFIX):
        return None
    project = name[len(DISPLAY_PREFIX):]
    return project if config.valid_project(project) else None


def _state(value):
    return value if value in STATES else None


def read_caps(ctx):
    """The control plane's spend cap budgets on the billing account:
    {"ok", "error", "items": {project: {"budget", "amount", "currency", "inputState", "outputState", "etag",
    "reconciling", "filterProjects", "filterServices", "hasSpendCap"}}, "currency"}. currency is the billing account
    currency seen on any budget of the account (None without budgets). Google returns inputState only when it was set;
    outputState is the state Google is in (CONFIGURED, ENFORCED or AWAITING_NEXT_PERIOD). ok is False, with the reason
    in error, when spend caps are off, no billing account is set (no call is made then) or the budgets cannot be read
    completely. Never raises GcpError."""
    out = {"ok": False, "error": None, "items": {}, "currency": None}
    reason = _gate(ctx.settings)
    if reason:
        out["error"] = reason
        return out
    account = account_id(ctx.settings)
    try:
        found, truncated = _list(ctx, "%s/billingAccounts/%s/budgets" % (BUDGETS_V1, account), "budgets",
                                 BUDGET_PAGE_SIZE, MAX_BUDGET_PAGES)
    except GcpError as err:
        LOG.warning("spend caps could not be read kind=%s", err.kind)
        out["error"] = _message(err, "the budgets of the billing account")
        return out
    if truncated:
        out["error"] = ("The billing account has more than %d budgets, so the control plane does not manage spend caps "
                        "on it." % (BUDGET_PAGE_SIZE * MAX_BUDGET_PAGES))
        return out
    items, currency = {}, None
    for budget in found:
        amount, code = _amount_of(budget)
        currency = currency or code
        project = _own_project(budget)
        if project is None or not _own_name(budget.get("name"), account):
            continue
        if project in items:
            LOG.warning("a second spend cap budget of one project was ignored")
            continue
        cap = budget.get("spendCap") if isinstance(budget.get("spendCap"), dict) else {}
        flt = budget.get("budgetFilter") if isinstance(budget.get("budgetFilter"), dict) else {}
        items[project] = {"budget": budget["name"], "amount": amount, "currency": code,
                          "inputState": _state(cap.get("inputState")), "outputState": _state(cap.get("outputState")),
                          "etag": _text(budget.get("etag"), 200), "reconciling": cap.get("reconciling") is True,
                          # What the budget covers: checked before it is changed or lifted (_not_owned).
                          "filterProjects": _strings(flt.get("projects")),
                          "filterServices": _strings(flt.get("services")),
                          "hasSpendCap": isinstance(budget.get("spendCap"), dict)}
    out.update(ok=True, items=items, currency=currency)
    return out


def _not_owned(ctx, project, cap):
    """NOT_OWNED when the budget `cap` (a read_caps item) is not provably the control plane's spend cap of `project`,
    else None: it must cover exactly that project (by number) and Agent Platform, and have a spend cap. Raises
    GcpError (project number and service lookups, both cached for a day)."""
    if not isinstance(cap, dict) or cap.get("hasSpendCap") is not True:
        return NOT_OWNED
    number, service = project_number(ctx, project), service_name(ctx)
    if not number or not service or cap.get("filterProjects") != ["projects/%s" % number] or \
            cap.get("filterServices") != [service]:
        return NOT_OWNED
    return None


# ---------------------------------------------------------------------------
# Plan and apply
# ---------------------------------------------------------------------------

def _plan_item(project, amount, item, account_currency=None):
    item = item if isinstance(item, dict) else None
    currency = (item.get("currency") if item else None) or account_currency
    usd = currency in (None, "USD")   # None: no budget told the currency yet; creates check the account first
    entry = {"project": project, "op": None, "amount": amount, "current": item.get("amount") if item else None,
             "currency": currency, "budget": item.get("budget") if item else None,
             "state": item.get("outputState") if item else None, "blocked": None}
    if amount is None:
        entry["op"] = "orphan"
    elif item is None:
        entry["op"] = "create"
        if len(display_name(project)) > MAX_DISPLAY_NAME:
            entry["blocked"] = "name_too_long"
        elif not usd:
            entry["blocked"] = "currency_mismatch"
    elif usd and chargeback.to_cents(item.get("amount")) == chargeback.to_cents(amount):
        entry["op"] = "none"
    else:
        entry["op"] = "update"
        if item.get("outputState") == ENFORCED:
            entry["blocked"] = "enforced"
        elif not usd:
            entry["blocked"] = "currency_mismatch"
    return entry


def plan_caps(current, desired, workspace=None):
    """What apply_caps would do, sorted by project: [{"project", "op", "amount", "current", "currency", "budget",
    "state", "blocked"}] with op
    create  a hard cap without a spend cap budget;
    update  a spend cap budget with another amount;
    none    a spend cap budget with the wanted amount;
    orphan  a spend cap budget of the control plane whose project is no longer a hard cap (reported, never deleted).
    amount is the wanted amount in USD (None for orphans), current the amount of the existing budget and state its
    outputState. blocked tells why apply_caps will refuse: "outside_workspace" (the project is not in `workspace`, a
    set of project ids; None skips this check), "enforced" (Google enforces the cap; only a lift may change it),
    "currency_mismatch" (the billing account is not in USD) or "name_too_long" (the project id does not fit the
    60-character display name); None otherwise. `current` is a read_caps result (nothing is planned when its read
    failed) or its items; `desired` is desired_caps(tree)."""
    if isinstance(current, dict) and "ok" in current and isinstance(current.get("items"), dict):
        if not current.get("ok"):
            return []
        items, currency = current["items"], current.get("currency")
    else:
        items, currency = (current if isinstance(current, dict) else {}), None
    wanted = {project: float(amount) for project, amount in (desired or {}).items()
              if isinstance(project, str) and config.valid_project(project) and _valid_amount(amount)}
    plan = [_plan_item(project, wanted.get(project), items.get(project), currency)
            for project in sorted(set(wanted) | set(items))]
    if workspace is not None:
        for entry in plan:
            if entry["op"] in ("create", "update") and entry["project"] not in workspace:
                entry["blocked"] = "outside_workspace"
    return plan


def _plan_ok(item):
    if not isinstance(item, dict) or item.get("op") not in OPS:
        return False
    if not isinstance(item.get("project"), str) or not config.valid_project(item["project"]):
        return False
    return item["op"] not in ("create", "update") or _valid_amount(item.get("amount"))


def _result(item, op, status, message=None, budget=None):
    return {"project": item["project"], "op": op, "amount": item.get("amount"), "status": status, "message": message,
            "budget": budget if budget is not None else item.get("budget")}


def _blocked(item):
    if item["blocked"] == "currency_mismatch":
        return _result(item, item["op"], "currency_mismatch", _currency_message(item.get("currency")))
    return _result(item, item["op"], item["blocked"], BLOCKED.get(item["blocked"]))


def _passive(item):
    """Result of an item that never changes anything: none, orphan or refused."""
    if item["op"] == "none":
        return _result(item, "none", "unchanged", "The spend cap already has this amount.")
    if item["op"] == "orphan":
        return _result(item, "orphan", "orphan", ORPHAN)
    return _blocked(item) if item.get("blocked") else None


def create_body(project, number, service, amount, currency="USD"):
    """Budget of a new spend cap: one project (by number) and one service, monthly, credits excluded, the three
    CURRENT_SPEND thresholds 0.5 / 0.8 / 1.0, project-level recipients and nothing else, inputState CONFIGURED."""
    return {
        "displayName": display_name(project),
        "budgetFilter": {"projects": ["projects/%s" % number], "services": [service],
                         "creditTypesTreatment": "EXCLUDE_ALL_CREDITS", "calendarPeriod": "MONTH"},
        "amount": {"specifiedAmount": _money(amount, currency)},
        "thresholdRules": [{"thresholdPercent": t, "spendBasis": "CURRENT_SPEND"} for t in THRESHOLDS],
        "notificationsRule": {"enableProjectLevelRecipients": True},
        "spendCap": {"inputState": CONFIGURED},
    }


def _create(ctx, item, account, known_currency):
    project = item["project"]
    currency = known_currency or account_currency(ctx, account)
    if currency != "USD":
        return _result(item, "create", "currency_mismatch", _currency_message(currency))
    service = service_name(ctx)
    if not service:
        return _result(item, "create", "failed", "Agent Platform was not found in the Cloud Billing Catalog, so no "
                                                 "spend cap was created.")
    number = project_number(ctx, project)
    if not number:
        return _result(item, "create", "failed", "The project number of %s could not be read." % project)
    created = ctx.rest("POST", "%s/billingAccounts/%s/budgets" % (BUDGETS_V1, account),
                       json_body=create_body(project, number, service, item["amount"], currency)) or {}
    name = created.get("name") if _own_name(created.get("name"), account) else None
    LOG.info("spend cap created project=%s", project)
    return _result(item, "create", "created", "A spend cap of USD %.2f was created." % item["amount"], budget=name)


def _update(ctx, item, account):
    if not _own_name(item.get("budget"), account):
        return _result(item, "update", "failed", "The spend cap budget name is not valid.")
    mask = ["amount.specifiedAmount"]
    body = {"amount": {"specifiedAmount": _money(item["amount"], item.get("currency") or "USD")}}
    current = chargeback.to_cents(item.get("current"))
    if current is not None and chargeback.to_cents(item["amount"]) > current:
        # Google configures a lifted cap again only after its amount is increased (documented update).
        mask.append("spendCap.inputState")
        body["spendCap"] = {"inputState": CONFIGURED}
    ctx.rest("PATCH", "%s/%s" % (BUDGETS_V1, item["budget"]), params={"updateMask": ",".join(mask)}, json_body=body)
    LOG.info("spend cap updated project=%s rearmed=%s", item["project"], len(mask) > 1)
    return _result(item, "update", "updated", "The spend cap is now USD %.2f." % item["amount"])


def _execute(ctx, item, fresh):
    passive = _passive(item)
    if passive is not None:
        return passive
    account = account_id(ctx.settings)
    try:
        if item["op"] == "create":
            return _create(ctx, item, account, fresh.get("currency"))
        problem = _not_owned(ctx, item["project"], (fresh.get("items") or {}).get(item["project"]))
        if problem:
            LOG.warning("spend cap update refused project=%s: the budget is not the control plane's", item["project"])
            return _result(item, "update", "not_owned", problem)
        return _update(ctx, item, account)
    except GcpError as err:
        LOG.warning("spend cap %s failed project=%s kind=%s", item["op"], item["project"], err.kind)
        return _result(item, item["op"], "failed", _message(err, "the spend cap of project %s" % item["project"]))


def _outside(ctx, items):
    """Projects of the create and update items that are not workspace projects (budgets.workspace_projects)."""
    wanted = {item["project"] for item in items if item["op"] in ("create", "update")}
    return wanted - budgets.workspace_projects(ctx) if wanted else set()


def _outside_result(item):
    return _result(item, item["op"], "outside_workspace", BLOCKED["outside_workspace"])


def apply_caps(ctx, plan, dry_run=False, current=None):
    """Carries out plan_caps items. Returns [{"project", "op", "amount", "status", "message", "budget"}], status:
    created, updated, unchanged (op none), orphan (kept, never deleted), dry_run (would create or update), enforced,
    currency_mismatch, name_too_long or outside_workspace (refused, see plan_caps), not_owned (the budget named for the
    project is not provably the control plane's, see _not_owned), skipped (spend caps off or no billing account: no
    call is made) or failed (Google refused or could not be reached; the message says why).

    A dry run makes no Budget API call. Otherwise every create and update is planned again against the current spend
    caps: a successful read_caps result the caller has just made (`current`), else a new read. So a stale plan never
    touches a cap that is enforced by then and never creates a second cap; a cap that Google enforces between that
    read and the patch is refused by Google (FAILED_PRECONDITION) and reported as failed. Workspace membership is
    checked here again, whatever the plan says."""
    items = [item for item in plan or () if _plan_ok(item)]
    if dry_run:
        outside = _outside(ctx, items)
        return [_outside_result(item) if item["project"] in outside else
                (_passive(item) or _result(item, item["op"], "dry_run")) for item in items]
    reason = _gate(ctx.settings)
    if reason:
        return [_result(item, item["op"], "skipped", reason) for item in items]
    outside = _outside(ctx, items)
    fresh = None
    if any(item["op"] in ("create", "update") and item["project"] not in outside for item in items):
        fresh = current if isinstance(current, dict) and current.get("ok") and \
            isinstance(current.get("items"), dict) else read_caps(ctx)
    results = []
    for item in items:
        if item["op"] in ("none", "orphan"):
            results.append(_passive(item))
        elif item["project"] in outside:
            results.append(_outside_result(item))
        elif not fresh["ok"]:
            results.append(_result(item, item["op"], "failed", fresh["error"]))
        else:
            step = _plan_item(item["project"], float(item["amount"]), fresh["items"].get(item["project"]),
                              fresh.get("currency"))
            results.append(_execute(ctx, step, fresh))
    return results


def lift(ctx, project):
    """Lifts the enforced spend cap of `project`: patches only spendCap.inputState to AWAITING_NEXT_PERIOD. An explicit
    administrator action (planning and apply_caps never lift). Google restores the service within about an hour and
    the cap stays lifted for the rest of the month (Google configures it again at the start of the next month or when
    its amount is increased). Returns {"project", "op": "lift", "status", "message", "budget"} with status lifted,
    not_enforced, not_found, not_owned (the budget is not provably the control plane's, see _not_owned), skipped (spend
    caps off or no billing account: no call is made) or failed. Raises ApiError(bad_request) for an invalid project
    id."""
    project = project.strip().lower() if isinstance(project, str) else ""
    if not config.valid_project(project):
        raise ApiError("bad_request", "Field 'project' must be a Google Cloud project id.")
    result = {"project": project, "op": "lift", "status": None, "message": None, "budget": None}
    reason = _gate(ctx.settings)
    if reason:
        return dict(result, status="skipped", message=reason)
    current = read_caps(ctx)
    if not current["ok"]:
        return dict(result, status="failed", message=current["error"])
    item = current["items"].get(project)
    if item is None:
        return dict(result, status="not_found", message="Project %s has no spend cap of the control plane." % project)
    result["budget"] = item["budget"]
    if item["outputState"] != ENFORCED:
        return dict(result, status="not_enforced", message="The spend cap of project %s is not enforced (%s), so "
                    "there is nothing to lift." % (project, item["outputState"] or "state unknown"))
    try:
        problem = _not_owned(ctx, project, item)
        if problem:
            LOG.warning("spend cap lift refused project=%s: the budget is not the control plane's", project)
            return dict(result, status="not_owned", message=problem)
        ctx.rest("PATCH", "%s/%s" % (BUDGETS_V1, item["budget"]), params={"updateMask": "spendCap.inputState"},
                 json_body={"spendCap": {"inputState": AWAITING_NEXT_PERIOD}})
    except GcpError as err:
        LOG.warning("spend cap lift failed project=%s kind=%s", project, err.kind)
        return dict(result, status="failed", message=_message(err, "the spend cap of project %s" % project))
    LOG.info("spend cap lifted project=%s", project)
    return dict(result, status="lifted", message="The spend cap of project %s is lifted until the start of the next "
                "month. Usage can take up to an hour to resume." % project)


# ---------------------------------------------------------------------------
# Optional handlers: GET status, POST apply, POST lift
# ---------------------------------------------------------------------------

def _require_admin(ctx):
    """Defense in depth: the routes that change spend caps are registered for administrators only."""
    if getattr(getattr(ctx, "identity", None), "role", None) != "admin":
        raise ApiError("forbidden", "Administrator privileges are required.")


def _audit(ctx, action, payload):
    from . import governance  # imported here to avoid an import cycle
    try:
        return bool(governance.write_audit(ctx, action, payload))
    except Exception:  # pylint: disable=broad-except
        # The change is made already; the failed entry is logged with its traceback and reported as not logged.
        LOG.exception("spend caps audit entry failed")
        return False


def _invalidate(ctx):
    invalidate = getattr(getattr(ctx, "app", None), "invalidate_governance", None)
    if callable(invalidate):
        invalidate()


def get_spend_caps(ctx):
    """Status of the spend caps: {"enabled", "accountConfigured", "desired", "caps", "currency", "plan", "error"}.
    Google is read only when spend caps are on and a billing account is set. Hard caps of projects outside the workspace
    are planned as blocked "outside_workspace"."""
    desired = desired_caps(budgets.load(ctx))
    out = {"enabled": enabled(ctx.settings), "accountConfigured": bool(account_id(ctx.settings)), "desired": desired,
           "caps": {}, "currency": None, "plan": [], "error": _gate(ctx.settings)}
    if out["error"]:
        return out
    current = read_caps(ctx)
    out.update(caps=current["items"], currency=current["currency"], error=current["error"],
               plan=plan_caps(current, desired, budgets.workspace_projects(ctx)))
    return out


def _audit_changes(results):
    """Per-project outcomes for the audit entry: everything that changed, failed or was refused (no e-mail addresses
    are involved); unchanged and orphan caps are only counted. At most AUDIT_CHANGES entries."""
    out = [{"project": item["project"], "op": item["op"], "status": item["status"], "amount": item.get("amount")}
           for item in results if item["status"] not in ("unchanged", "orphan")]
    return out[:AUDIT_CHANGES], max(0, len(out) - AUDIT_CHANGES)


def apply_spend_caps(ctx):
    """{"dryRun": true|false (default true)} (administrators): plans the hard caps of the stored budgets against the
    spend caps on the billing account and applies the plan (a dry run changes nothing). Only workspace projects get a
    spend cap. An applied run writes a governance audit entry with the counts of each status and the outcome per
    project. Returns {"dryRun", "plan", "results", "auditLogged"}."""
    _require_admin(ctx)
    body = ctx.body if isinstance(getattr(ctx, "body", None), dict) else {}
    dry_run = body.get("dryRun")
    if dry_run is None:
        dry_run = True
    if not isinstance(dry_run, bool):
        raise ApiError("bad_request", "Field 'dryRun' must be true or false.")
    reason = _gate(ctx.settings)
    if reason:
        raise ApiError("not_configured", reason)
    desired = desired_caps(budgets.load(ctx))
    current = read_caps(ctx)
    if not current["ok"]:
        raise ApiError("upstream_error", current["error"])
    plan = plan_caps(current, desired, budgets.workspace_projects(ctx))
    results = apply_caps(ctx, plan, dry_run=dry_run, current=current)
    audit_logged = None
    if not dry_run:
        counts = {}
        for item in results:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        changes, omitted = _audit_changes(results)
        entry = {"results": counts, "planned": len(plan), "changes": changes}
        if omitted:
            entry["changesOmitted"] = omitted
        audit_logged = _audit(ctx, "spend_caps_apply", entry)
        _invalidate(ctx)
    return {"dryRun": dry_run, "plan": plan, "results": results, "auditLogged": audit_logged}


def lift_spend_cap(ctx):
    """{"project"} (administrators): lifts the project's enforced spend cap (lift) and writes a governance audit entry
    with the project id and the outcome."""
    _require_admin(ctx)
    body = ctx.body if isinstance(getattr(ctx, "body", None), dict) else {}
    reason = _gate(ctx.settings)
    if reason:
        raise ApiError("not_configured", reason)
    result = lift(ctx, body.get("project"))
    result["auditLogged"] = _audit(ctx, "spend_cap_lift", {"project": result["project"], "status": result["status"]})
    if result["status"] == "lifted":
        _invalidate(ctx)
    return result
