"""Organization and project budgets: monthly paid budgets for organization units and projects, measured with the
chargeback facts (services/chargeback.py), with threshold alerts.

The budgets are the aux document "budgets" (settings_store.AuxDocStore). A save replaces the whole list:

    {"version": 4, "updatedAt": "2026-11-19T03:00:00Z", "updatedBy": "admin@example.com",
     "nodes": [{"id": "b_1a2b3c4d5e", "scope": "unit", "unitId": "u_5e4d3c2b1a", "monthlyPaidUsd": 5000.0,
                "action": "suspend", "thresholds": [50, 80, 100]},
               {"id": "b_0f9e8d7c6b", "scope": "project", "project": "sales-analytics", "monthlyPaidUsd": 800.0,
                "action": "hard_cap", "thresholds": [50, 80, 100]}]}

- A node id is "b_" + the first 10 hex digits of SHA-256("unit:" + unit id) or SHA-256("project:" + project id), so
  a budget keeps its id across saves. There is at most one budget per unit and per project, and at most MAX_NODES.
- monthlyPaidUsd: USD per calendar month of the chargeback report, 0 to 1e9. 0 allows no paid usage at all.
- Unit budgets cover the unit and every unit below it. Actions:
  suspend  when the budget is used up, the people whose usage is attributed to the unit are stopped while nothing is
           free for them, as with the personal paid budget (the enforcement cycle in services/governance.py decides);
  review   when the budget is used up, those people go to the review queue;
  notify   threshold alerts only.
  Project budgets: notify, or hard_cap (also a Google Cloud spend cap on the project, services/spend_caps.py, when
  governance.antigravity.spend_caps is on; only for workspace projects, see workspace_projects).
- thresholds: sorted unique whole percentages 1..100; each one is announced once per month (default 50, 80, 100).

The organization-wide and personal budgets stay in the settings (governance.antigravity.org_paid_budget_month,
paid_budget_month and the limit rules); they are not repeated here.

Spend is the chargeable amount (what is paid beyond the included credit) of the month's chargeback facts, in integer
cents: a unit's spend covers the facts attributed to the unit or to a unit below it, a project's spend the facts of
the project (its cost is their cost before credit). A budget is used up when its spend reaches the budget. Budgets are
in USD, so they are only measured against a chargeback report in USD.

Alerts: the aux document "budget_alerts" {"month": "YYYY-MM", "notified": {node id: [thresholds]}} records what was
announced; it starts over when the month changes. Alert texts never contain e-mail addresses.

GET  /api/v1/budgets  the budgets and their status in the running month
POST /api/v1/budgets  {"nodes": [...]} replaces the budgets (administrators)
"""

from __future__ import annotations

import hashlib
import logging
import math
import re

from .. import config
from ..gcp import GcpError
from ..httpkit import ApiError, now_iso
from ..settings_store import StoreConflict, StoreError, aux_docs
from . import chargeback, org_model

LOG = logging.getLogger("controlplane.budgets")

DOC = "budgets"
ALERTS_DOC = "budget_alerts"
SOURCE = "budgets:status"
SCOPES = ("unit", "project")
UNIT_ACTIONS = ("suspend", "review", "notify")
PROJECT_ACTIONS = ("notify", "hard_cap")
DEFAULT_THRESHOLDS = (50, 80, 100)
MAX_NODES = 5000
MAX_AMOUNT = 1e9
MAX_THRESHOLDS = 100
FACTS_TTL = 300
# Used with fullmatch.
MONTH_RE = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")
NODE_ID_RE = re.compile(r"b_[0-9a-f]{10}")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
ACTION_LABELS = {"suspend": "suspend", "review": "review", "notify": "notify", "hard_cap": "hard cap"}


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _text(value, limit):
    return value[:limit] if isinstance(value, str) and value else None


def _choices(values):
    return values[0] if len(values) == 1 else "%s or %s" % (", ".join(values[:-1]), values[-1])


def _clip(text, limit=80):
    return text if len(text) <= limit else text[:limit - 3] + "..."


def node_id(scope, target):
    """Stable id of the budget of a unit (scope "unit", target a unit id) or of a project (scope "project")."""
    return "b_" + hashlib.sha256(("%s:%s" % (scope, target)).encode("utf-8")).hexdigest()[:10]


def _target(node):
    return node["unitId"] if node["scope"] == "unit" else node["project"]


def _copy(node):
    return dict(node, thresholds=list(node["thresholds"]))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _valid_unit_id(value):
    """A stored unit id or the virtual Unassigned unit (usage that no rule attributes)."""
    return isinstance(value, str) and (value == org_model.UNASSIGNED or bool(org_model.UNIT_ID_RE.fullmatch(value)))


def _known_unit(org, unit_id):
    return unit_id == org_model.UNASSIGNED or unit_id in org.units


def _unit_label(org, unit_id):
    if org is not None and _known_unit(org, unit_id):
        return _clip(org.unit(unit_id)["path"])
    return unit_id


def workspace_projects(ctx):
    """The workspace projects (ctx.all_projects) as a set: the only projects a hard cap, and so a Google spend cap, may
    act on. Empty when the project list cannot be built (fail closed)."""
    try:
        return {p for p in (getattr(ctx, "all_projects", None) or ()) if isinstance(p, str)}
    except (GcpError, ApiError) as err:
        LOG.warning("workspace projects unavailable: %s", getattr(err, "code", None) or getattr(err, "kind", "error"))
        return set()


def _amount(value):
    """A budget amount in USD rounded half up to cents, or None unless it is a number from 0 to MAX_AMOUNT."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:  # an integer too large for a float
        return None
    if not math.isfinite(number) or not 0 <= number <= MAX_AMOUNT:
        return None
    return chargeback.to_cents(number) / 100.0


def _thresholds(value):
    """Sorted unique whole percentages 1..100 (DEFAULT_THRESHOLDS when missing), or None when invalid."""
    if value is None:
        return list(DEFAULT_THRESHOLDS)
    if not isinstance(value, list) or len(value) > MAX_THRESHOLDS:
        return None
    out = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        if isinstance(item, float) and not (math.isfinite(item) and item.is_integer()):
            return None
        if not 1 <= item <= 100:
            return None
        out.add(int(item))
    return sorted(out)


def _clean_node(entry, index, org=None, workspace=None):
    """The clean form of budget number `index` (1-based) of a request; ApiError(bad_request) names the entry."""
    where = "Budget %d" % index
    if not isinstance(entry, dict):
        raise ApiError("bad_request", "%s must be an object." % where)
    scope = entry.get("scope")
    if scope not in SCOPES:
        raise ApiError("bad_request", "%s: scope must be unit or project." % where)
    if scope == "unit":
        target = entry.get("unitId")
        target = target.strip() if isinstance(target, str) else target
        if not _valid_unit_id(target):
            raise ApiError("bad_request", "%s: unitId must be the id of a unit in the organization tree "
                                          "(u_ and 10 hexadecimal digits)." % where)
        if org is not None and not _known_unit(org, target):
            raise ApiError("bad_request", "%s: unit %s is not in the organization model. Reload the organization "
                                          "tree and choose a unit from it." % (where, target))
        actions = UNIT_ACTIONS
        label = "%s (unit %s)" % (where, _unit_label(org, target))
    else:
        target = entry.get("project")
        target = target.strip().lower() if isinstance(target, str) else ""
        if not config.valid_project(target):
            raise ApiError("bad_request", "%s: project must be a Google Cloud project id." % where)
        actions = PROJECT_ACTIONS
        label = "%s (project %s)" % (where, target)
    amount = _amount(entry.get("monthlyPaidUsd"))
    if amount is None:
        raise ApiError("bad_request", "%s: monthlyPaidUsd must be a number from 0 to 1000000000 (USD per month)."
                       % label)
    action = entry.get("action")
    if action not in actions:
        raise ApiError("bad_request", "%s: action must be %s." % (label, _choices(actions)))
    if action == "hard_cap" and workspace is not None and target not in workspace:
        # A hard cap becomes a Google spend cap that pauses Agent Platform in the project: only for this workspace.
        raise ApiError("bad_request", "%s: a hard cap is allowed only for projects of this workspace. Choose notify "
                                      "or a workspace project." % label)
    thresholds = _thresholds(entry.get("thresholds"))
    if thresholds is None:
        raise ApiError("bad_request", "%s: thresholds must be a list of whole percentages from 1 to 100." % label)
    node = {"id": node_id(scope, target), "scope": scope}
    node["unitId" if scope == "unit" else "project"] = target
    node.update(monthlyPaidUsd=amount, action=action, thresholds=thresholds)
    return node


def validate_nodes(raw, org=None, workspace=None):
    """Clean budget nodes of a request body's "nodes" list, in request order. Ids are always recomputed (an id sent by
    the client is ignored), thresholds default to DEFAULT_THRESHOLDS and project ids are lower-cased. With an
    organization model (org_model.OrgModel) every unitId must be one of its units or the Unassigned unit; with
    `workspace` (a set of project ids, see workspace_projects) a hard cap must name one of them. Raises
    ApiError(bad_request) with a message that names the first bad entry (1-based, as "Budget 3")."""
    if not isinstance(raw, list):
        raise ApiError("bad_request", "Field 'nodes' must be a list of budgets.")
    if len(raw) > MAX_NODES:
        raise ApiError("bad_request", "At most %d budgets are allowed; the request has %d." % (MAX_NODES, len(raw)))
    out, targets, ids = [], {}, {}
    for index, entry in enumerate(raw, 1):
        node = _clean_node(entry, index, org, workspace)
        key = (node["scope"], _target(node))
        if key in targets:
            what = ("unit %s" % _unit_label(org, key[1])) if node["scope"] == "unit" else ("project %s" % key[1])
            raise ApiError("bad_request", "Budget %d: the %s already has a budget (budget %d). Each unit and project "
                                          "can have one budget." % (index, what, targets[key]))
        if node["id"] in ids:  # two targets with the same 40-bit id: practically never
            raise ApiError("bad_request", "Budget %d gets the same id as budget %d. Change one of them."
                           % (index, ids[node["id"]]))
        targets[key] = ids[node["id"]] = index
        out.append(node)
    return out


# ---------------------------------------------------------------------------
# Stored budgets
# ---------------------------------------------------------------------------

def _read_nodes(raw):
    """Valid nodes of a stored document, without duplicates and at most MAX_NODES; a hand-edited or damaged document
    is read defensively (bad entries are left out, never raised)."""
    out, targets, ids = [], set(), set()
    skipped = 0
    for index, entry in enumerate(raw if isinstance(raw, list) else (), 1):
        if len(out) >= MAX_NODES:
            skipped += 1
            continue
        try:
            node = _clean_node(entry, index)
        except ApiError:
            skipped += 1
            continue
        key = (node["scope"], _target(node))
        if key in targets or node["id"] in ids:
            skipped += 1
            continue
        targets.add(key)
        ids.add(node["id"])
        out.append(node)
    if skipped:
        LOG.warning("budgets document: %d invalid or duplicate entries were ignored", skipped)
    return out


class BudgetTree:
    """The stored budgets. Use load(ctx).

    version, updated_at, updated_by  document metadata (0, None, None without a document)
    nodes     clean budget nodes in stored order
    units     {unit id: node} of the unit budgets
    projects  {project id: node} of the project budgets
    empty     True without budgets"""

    def __init__(self, doc=None):
        doc = doc if isinstance(doc, dict) else {}
        self.version = _count(doc.get("version"))
        self.updated_at = _text(doc.get("updatedAt"), 40)
        self.updated_by = _text(doc.get("updatedBy"), 254)
        self.nodes = _read_nodes(doc.get("nodes"))
        self.units = {node["unitId"]: node for node in self.nodes if node["scope"] == "unit"}
        self.projects = {node["project"]: node for node in self.nodes if node["scope"] == "project"}
        self.empty = not self.nodes


def load(ctx):
    """The stored budgets as a BudgetTree. Raises ApiError(upstream_error) when the document cannot be read."""
    store = aux_docs(getattr(ctx, "app", None))
    try:
        doc, _generation = store.load(DOC)
    except (StoreError, OSError) as err:
        LOG.warning("budgets could not be read: %s", type(err).__name__)
        raise ApiError("upstream_error", "The budgets could not be read. Try again.")
    return BudgetTree(doc)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _cents(value):
    """Integer cents of a fact amount; None (unknown) and anything that is not a number count 0."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(round(value))
    return 0


def _entry(node, spent):
    budget = chargeback.to_cents(node["monthlyPaidUsd"]) or 0
    if budget > 0:
        crossed = [t for t in node["thresholds"] if spent * 100 >= t * budget]
    else:
        crossed = list(node["thresholds"]) if spent > 0 else []  # any paid usage exceeds a budget of 0
    return {"node": node, "budgetCents": budget, "spentCents": spent,
            "ratio": round(spent / budget, 4) if budget > 0 else None, "usedUp": spent >= budget,
            "crossed": crossed}


class BudgetStatus:
    """Spend against every budget in one month (see evaluate).

    units     {unit id: {"node", "budgetCents", "spentCents", "ratio", "usedUp", "crossed"}}
    projects  {project id: {"node", "budgetCents", "spentCents", "costCents", "ratio", "usedUp", "crossed"}}
    ratio is spent / budget (None for a budget of 0), usedUp is spent >= budget (a budget of 0 allows no paid usage,
    so it is always used up) and crossed lists the thresholds reached (with a budget of 0: all of them once there is
    paid spend)."""

    def __init__(self, units, projects, org):
        self.units = units
        self.projects = projects
        self._org = org

    def _nodes_for(self, units, action):
        if isinstance(units, str):
            units = (units,)
        out, seen = [], set()
        for unit_id in units or ():
            for anc in self._org.ancestors(unit_id):
                item = self.units.get(anc)
                if item is None or not item["usedUp"] or item["node"]["action"] != action:
                    continue
                if item["node"]["id"] not in seen:
                    seen.add(item["node"]["id"])
                    out.append(item["node"])
        return out

    def suspend_nodes_for(self, units):
        """Used-up unit budgets with action suspend on the ancestor chain (the unit itself included) of any of the
        unit ids `units` (for example the units a person's facts are attributed to), nearest first, without
        duplicates. Unknown unit ids count as the Unassigned unit."""
        return self._nodes_for(units, "suspend")

    def review_nodes_for(self, units):
        """Like suspend_nodes_for, for used-up unit budgets with action review."""
        return self._nodes_for(units, "review")

    def public(self):
        """JSON-ready status: {"units": [...], "projects": [...]} with money in currency units (USD). Units come in
        the order of the organization tree, then the Unassigned unit and units no longer in the model (path None,
        known False); projects by id."""
        org = self._org
        order = [uid for uid in org.dfs_order() if uid in self.units]
        if org_model.UNASSIGNED in self.units:
            order.append(org_model.UNASSIGNED)
        order += sorted(uid for uid in self.units if uid not in org.units and uid != org_model.UNASSIGNED)
        units = []
        for unit_id in order:
            item = self.units[unit_id]
            node = item["node"]
            known = _known_unit(org, unit_id)
            unit = org.unit(unit_id) if known else {}
            units.append({"id": node["id"], "unitId": unit_id, "path": unit.get("path"), "depth": unit.get("depth", 0),
                          "known": known, "budget": chargeback.money(item["budgetCents"]),
                          "spent": chargeback.money(item["spentCents"]), "ratio": item["ratio"],
                          "usedUp": item["usedUp"], "action": node["action"], "thresholds": list(node["thresholds"]),
                          "crossed": list(item["crossed"])})
        projects = []
        for project in sorted(self.projects):
            item = self.projects[project]
            node = item["node"]
            projects.append({"id": node["id"], "project": project, "budget": chargeback.money(item["budgetCents"]),
                             "spent": chargeback.money(item["spentCents"]),
                             "cost": chargeback.money(item["costCents"]), "ratio": item["ratio"],
                             "usedUp": item["usedUp"], "action": node["action"],
                             "thresholds": list(node["thresholds"]), "crossed": list(item["crossed"])})
        return {"units": units, "projects": projects}


def evaluate(tree, org, facts):
    """Spend of every budget of `tree` (a BudgetTree) from chargeback facts (chargeback.build(...)["facts"]: money in
    integer USD cents, None when unknown) with the organization model `org`:
    - a unit budget spends the chargeable amount of every fact whose unit is the budget's unit or one below it (the
      fact's unit or any of its org.ancestors); facts of unknown units count for the Unassigned unit;
    - a project budget spends the chargeable amount of the project's facts; its cost is the sum of their cost.
    Unknown amounts (None) count 0. Returns a BudgetStatus. The facts must be in USD, like the budgets."""
    own, spent, cost = {}, {}, {}
    for fact in facts or ():
        if not isinstance(fact, dict):
            continue
        charge = _cents(fact.get("chargeable"))
        unit, project = fact.get("unit"), fact.get("project")
        own[unit] = own.get(unit, 0) + charge
        spent[project] = spent.get(project, 0) + charge
        cost[project] = cost.get(project, 0) + _cents(fact.get("cost"))
    rolled = {}
    for unit, cents in own.items():
        for anc in org.ancestors(unit):
            rolled[anc] = rolled.get(anc, 0) + cents
    units = {uid: _entry(node, rolled.get(uid, 0)) for uid, node in tree.units.items()}
    projects = {}
    for project, node in tree.projects.items():
        projects[project] = dict(_entry(node, spent.get(project, 0)), costCents=cost.get(project, 0))
    return BudgetStatus(units, projects, org)


# ---------------------------------------------------------------------------
# Threshold alerts
# ---------------------------------------------------------------------------

def _clean_notified(raw):
    """{node id: set of thresholds} of a stored "notified" mapping (invalid entries are left out)."""
    out = {}
    for key, values in (raw.items() if isinstance(raw, dict) else ()):
        if not isinstance(key, str) or not NODE_ID_RE.fullmatch(key) or not isinstance(values, list):
            continue
        out[key] = {v for v in values if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 100}
    return out


def _notified_for(month, notified):
    if not isinstance(notified, dict):
        return {}
    if isinstance(notified.get("notified"), dict) and "month" in notified:  # the alerts document (load_alerts)
        return _clean_notified(notified["notified"]) if notified.get("month") == month else {}
    return _clean_notified(notified.get(month))


def load_alerts(ctx):
    """The alerts document as {"month": "YYYY-MM" or None, "notified": {node id: [thresholds]}}. Raises
    ApiError(upstream_error) when it cannot be read."""
    try:
        doc, _generation = aux_docs(getattr(ctx, "app", None)).load(ALERTS_DOC)
    except (StoreError, OSError) as err:
        LOG.warning("budget alerts could not be read: %s", type(err).__name__)
        raise ApiError("upstream_error", "The budget alerts could not be read. Try again.")
    month = doc.get("month")
    return {"month": month if isinstance(month, str) and MONTH_RE.fullmatch(month) else None,
            "notified": {key: sorted(values) for key, values in _clean_notified(doc.get("notified")).items()}}


def new_alerts(status, month, notified):
    """[(node, threshold)] of the thresholds crossed in `month` that were not announced yet: unit budgets first, then
    project budgets, thresholds in ascending order. `notified` is {month: {node id: [thresholds]}} or the alerts
    document from load_alerts (which only counts for its own month)."""
    done = _notified_for(month, notified)
    out = []
    for item in list(status.units.values()) + list(status.projects.values()):
        node = item["node"]
        seen = done.get(node["id"]) or set()
        out.extend((node, threshold) for threshold in item["crossed"] if threshold not in seen)
    return out


def record_alerts(ctx, month, pairs):
    """Records (node, threshold) pairs as announced for `month` (YYYY-MM) in ALERTS_DOC, which starts over when the
    month changes, and returns the pairs that were not recorded before. Send exactly those: a concurrent run that
    recorded a pair first gets it back only once. Pairs of a month older than the stored one are ignored. Raises
    ApiError(conflict | upstream_error) when the document cannot be saved."""
    if not isinstance(month, str) or not MONTH_RE.fullmatch(month):
        raise ValueError("month must look like YYYY-MM")
    wanted = []
    for node, threshold in pairs or ():
        if isinstance(node, dict) and isinstance(node.get("id"), str) and NODE_ID_RE.fullmatch(node["id"]) and \
                isinstance(threshold, int) and not isinstance(threshold, bool) and 1 <= threshold <= 100:
            wanted.append((node, threshold))
    if not wanted:
        return []
    fresh = []

    def apply(doc):
        del fresh[:]
        stored = doc.get("month")
        if isinstance(stored, str) and MONTH_RE.fullmatch(stored) and stored > month:
            return []  # a late run for a month that is over: nothing is announced any more
        if stored != month or not isinstance(doc.get("notified"), dict):
            doc.clear()
            doc.update({"month": month, "notified": {}})
        notified = doc["notified"]
        for node, threshold in wanted:
            have = notified.get(node["id"])
            have = [v for v in have if isinstance(v, int) and not isinstance(v, bool)] if isinstance(have, list) \
                else []
            if threshold in have:
                continue
            notified[node["id"]] = sorted(set(have) | {threshold})
            fresh.append((node, threshold))
        return list(fresh)

    try:
        return aux_docs(getattr(ctx, "app", None)).mutate(ALERTS_DOC, apply)
    except StoreConflict:
        raise ApiError("conflict", "The budget alerts were changed at the same time. Try again.")
    except (StoreError, OSError) as err:
        LOG.error("budget alerts could not be saved: %s", type(err).__name__)
        raise ApiError("upstream_error", "The budget alerts could not be saved. Try again.")


def _usd(cents):
    return "{:,.2f}".format(cents / 100.0)


def alert_text(node, threshold, status, org):
    """One short English sentence for a crossed threshold, for example "Unit Sales/Korea reached 80% of its monthly
    paid budget: USD 4,000.00 of USD 5,000.00 spent this month (action: suspend)." E-mail addresses never appear
    (a unit name that looks like one is replaced)."""
    if node["scope"] == "unit":
        item = status.units.get(node["unitId"]) or {}
        target = "Unit %s" % _unit_label(org, node["unitId"])
    else:
        item = status.projects.get(node["project"]) or {}
        target = "Project %s" % node["project"]
    budget = item.get("budgetCents")
    if budget is None:
        budget = chargeback.to_cents(node["monthlyPaidUsd"]) or 0
    spent = item.get("spentCents") or 0
    action = ACTION_LABELS.get(node["action"], node["action"])
    if budget > 0:
        text = "%s reached %d%% of its monthly paid budget: USD %s of USD %s spent this month (action: %s)." % (
            target, threshold, _usd(spent), _usd(budget), action)
    else:
        text = "%s has paid usage of USD %s this month, but its monthly paid budget is USD 0 (action: %s)." % (
            target, _usd(spent), action)
    return _EMAIL_RE.sub("<email>", text)


# ---------------------------------------------------------------------------
# GET /api/v1/budgets, POST /api/v1/budgets
# ---------------------------------------------------------------------------

def _require_admin(ctx):
    """Defense in depth: the route is registered for administrators only."""
    if getattr(getattr(ctx, "identity", None), "role", None) != "admin":
        raise ApiError("forbidden", "Administrator privileges are required.")


def _facts(ctx, month):
    """(facts, currency) of `month` from chargeback.build, kept with the request memo for FACTS_TTL seconds."""
    def compute(child):
        model = chargeback.build(child, month)
        return {"facts": model.get("facts") or [], "currency": (model.get("summary") or {}).get("currency") or "USD"}

    memo = getattr(ctx, "memo", None)
    value = compute(ctx) if memo is None else memo("budget_facts", month, compute, ttl=FACTS_TTL)
    return value["facts"], value["currency"]


def _status(ctx, tree, month):
    """BudgetStatus.public() of `month`, or None with a warning. The status is optional: the budgets stay readable and
    editable when it fails, and unexpected errors are logged with their traceback."""
    if tree.empty:
        return {"units": [], "projects": []}
    try:
        org = org_model.load(ctx)
        facts, currency = _facts(ctx, month)
        if currency != "USD":
            ctx.warn(SOURCE, "currency_mismatch",
                     "Budgets are in USD, so they are not measured against a chargeback report in %s." % currency)
            return None
        return evaluate(tree, org, facts).public()
    except GcpError as err:
        ctx.warn_error(SOURCE, err, "The chargeback figures of the budget status")
    except ApiError as err:
        LOG.warning("budget status unavailable: %s", err.code)
        ctx.warn(SOURCE, "status_unavailable", "The budget status is unavailable: %s" % err.message)
    except Exception:  # pylint: disable=broad-except
        LOG.exception("budget status failed")
        ctx.warn(SOURCE, "internal_error", "The budget status could not be computed.")
    return None


def _spend_caps_on(ctx):
    settings = getattr(ctx, "settings", None)
    try:
        return settings["governance"]["antigravity"].get("spend_caps") is True
    except (KeyError, TypeError, AttributeError):
        return False


def get_budgets(ctx):
    """GET /api/v1/budgets: {"version", "updatedAt", "updatedBy", "nodes", "month", "currency": "USD", "status",
    "actions", "defaultThresholds", "maxNodes", "spendCaps"}. status is BudgetStatus.public() of the running chargeback
    month (chargeback.current_month), or None with a warning when the chargeback figures or the organization model
    cannot be read or the report is not in USD. spendCaps tells whether hard caps also get a Google spend cap."""
    tree = load(ctx)
    month = chargeback.current_month(ctx)
    return {"version": tree.version, "updatedAt": tree.updated_at, "updatedBy": tree.updated_by,
            "nodes": [_copy(node) for node in tree.nodes], "month": month, "currency": "USD",
            "status": _status(ctx, tree, month),
            "actions": {"unit": list(UNIT_ACTIONS), "project": list(PROJECT_ACTIONS)},
            "defaultThresholds": list(DEFAULT_THRESHOLDS), "maxNodes": MAX_NODES, "spendCaps": _spend_caps_on(ctx)}


def _counts(nodes):
    actions = [node["action"] for node in nodes]
    return {"nodes": len(nodes), "units": sum(1 for node in nodes if node["scope"] == "unit"),
            "projects": sum(1 for node in nodes if node["scope"] == "project"),
            "suspend": actions.count("suspend"), "review": actions.count("review"), "notify": actions.count("notify"),
            "hardCap": actions.count("hard_cap")}


def _audit(ctx, action, payload):
    from . import governance  # imported here to avoid an import cycle
    try:
        return bool(governance.write_audit(ctx, action, payload))
    except Exception:  # pylint: disable=broad-except
        # The budgets are saved already; the failed entry is logged with its traceback and reported as not logged.
        LOG.exception("budgets audit entry failed")
        return False


def save_budgets(ctx):
    """POST /api/v1/budgets {"nodes": [...]} (administrators). Every budget is validated against the organization
    model and, for hard caps, the workspace projects (validate_nodes); the stored list is replaced (version + 1), the
    governance audit log records the counts (never e-mail addresses) and cached responses are dropped. Returns the
    stored document with "auditLogged"."""
    _require_admin(ctx)
    body = ctx.body if isinstance(getattr(ctx, "body", None), dict) else {}
    if "nodes" not in body:
        raise ApiError("bad_request", "Field 'nodes' is required.")
    raw = body.get("nodes")
    hard = isinstance(raw, list) and any(isinstance(n, dict) and n.get("action") == "hard_cap" for n in raw)
    nodes = validate_nodes(raw, org_model.load(ctx), workspace=workspace_projects(ctx) if hard else None)
    now = now_iso()
    actor = str(getattr(ctx.identity, "email", "") or "")[:254]

    def apply(doc):
        version = _count(doc.get("version")) + 1
        doc.clear()
        doc.update({"version": version, "updatedAt": now, "updatedBy": actor, "nodes": [_copy(n) for n in nodes]})
        return version

    try:
        version = aux_docs(getattr(ctx, "app", None)).mutate(DOC, apply)
    except StoreConflict:
        raise ApiError("conflict", "The budgets were changed at the same time. Reload them and try again.")
    except (StoreError, OSError) as err:
        LOG.error("budgets could not be saved: %s", type(err).__name__)
        raise ApiError("upstream_error", "The budgets could not be saved. Try again.")
    counts = _counts(nodes)
    LOG.info("budgets saved version=%d units=%d projects=%d", version, counts["units"], counts["projects"])
    audit_logged = _audit(ctx, "budgets_save", dict(counts, version=version))
    invalidate = getattr(getattr(ctx, "app", None), "invalidate_governance", None)
    if callable(invalidate):
        invalidate()
    return {"version": version, "updatedAt": now, "updatedBy": actor, "nodes": [_copy(n) for n in nodes],
            "auditLogged": audit_logged}
