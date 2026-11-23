"""Internal chargeback: Antigravity usage cost, and optionally license seat cost, allocated to people, projects and
organization units, with an immutable month close.

The amounts are computed, not invoice lines: tokens x unit price per model (governance.billing_report, the same
ledger as the caps), minus the included credit each person used when chargeback.allocation_mode is credit_share.
Google bills per project, so each person's month is split over the projects they used (governance.project_split)
and every (person, project) part is attributed to an organization unit (org_model.OrgModel.unit_for). All rollups
are sums of the same integer-cent facts, so people, units, projects and the total always agree to the cent.

A month close freezes the figures of a past month in an internal document (and, optionally, appends them to a
BigQuery table); a closed month is served from that snapshot. Closing again creates a new close; earlier closes are
kept.
"""

from __future__ import annotations

import decimal
import hashlib
import json
import logging
import math
import re

from .. import config, identity, timeutil
from ..gcp import GcpError
from ..httpkit import ApiError
from ..security import uid_for
from ..settings_store import aux_docs
from . import governance, org_model
from .base import iso

LOG = logging.getLogger("controlplane.chargeback")

CLOSES_DOC = "chargeback_closes"
MAX_CLOSE_ROWS = 100000
MONTHS_BACK = 12
MONTH_RE = re.compile(r"([0-9]{4})-([0-9]{2})")   # fullmatch; ASCII digits only (\d also matches other scripts)
LEVELS = ("user", "unit", "project")
SUMS = ("requests", "tokens", "cost", "credit", "chargeable", "seat", "total")
BQ_V2 = "https://bigquery.googleapis.com/bigquery/v2"
CLOSE_TABLE = "chargeback_close"
BQ_BATCH = 500


# ---------------------------------------------------------------------------
# Months and money
# ---------------------------------------------------------------------------

def _zone(ctx):
    return timeutil.get_zone(governance.period_anchor(ctx.settings)["tz"])


def current_month(ctx, now=None):
    """The running calendar month in the reset time zone (the month boundaries of the billing report)."""
    return (now or governance.utcnow()).astimezone(_zone(ctx)).strftime("%Y-%m")


def recent_months(ctx, now=None):
    """The running month and the MONTHS_BACK months before it, newest first."""
    cur = current_month(ctx, now)
    year, month = int(cur[:4]), int(cur[5:])
    out = []
    for _ in range(MONTHS_BACK + 1):
        out.append("%04d-%02d" % (year, month))
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return out


def parse_month(raw, ctx, now=None):
    """A validated YYYY-MM month that is not in the future; the running month when `raw` is empty."""
    cur = current_month(ctx, now)
    text = str(raw or "").strip()
    if not text:
        return cur
    m = MONTH_RE.fullmatch(text)
    if not m or not 1 <= int(m.group(2)) <= 12 or int(m.group(1)) < 2020:
        raise ApiError("bad_request", "The month must look like YYYY-MM.")
    month = "%04d-%02d" % (int(m.group(1)), int(m.group(2)))   # the month names documents and queries
    if month > cur:
        raise ApiError("bad_request", "Parameter 'month' must not be in the future.")
    return month


_CENT = decimal.Decimal("1")


def to_cents(value):
    """Integer cents of an amount (half up), or None for a missing or invalid amount."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = decimal.Decimal(repr(float(value))) * 100
    except (TypeError, ValueError, decimal.InvalidOperation):
        return None
    if not amount.is_finite():
        return None
    return int(amount.quantize(_CENT, rounding=decimal.ROUND_HALF_UP))


def money(cents):
    return None if cents is None else round(cents / 100.0, 2)


def _weights(values):
    out = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = 0.0
        out.append(number if number > 0 and math.isfinite(number) else 0.0)
    return out


def allocate(total, weights, fallback=None):
    """Splits integer `total` (cents, >= 0) over `weights` so the parts sum to `total` exactly (largest remainder,
    ties to the earlier part). Without a positive weight the `fallback` weights decide, else the first part."""
    count = len(weights)
    if count == 0:
        return []
    if not total:
        return [0] * count
    clean = _weights(weights)
    if sum(clean) <= 0 and fallback is not None:
        clean = _weights(fallback)
    whole = sum(clean)
    if whole <= 0:
        return [total] + [0] * (count - 1)
    raw = [total * w / whole for w in clean]
    parts = [int(math.floor(r)) for r in raw]
    order = sorted(range(count), key=lambda i: (-(raw[i] - parts[i]), i))
    for i in order[:total - sum(parts)]:
        parts[i] += 1
    return parts


# ---------------------------------------------------------------------------
# Facts: (person, project) parts in integer cents
# ---------------------------------------------------------------------------

def _int(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def usage_facts(row, mode):
    """(person, project) facts of one billing report row. credit_share: what the person pays beyond the credit they
    used; list: list cost without credit. Money is None when the person's cost is unknown (a model without price)."""
    person = row.get("email")
    cost = to_cents(row.get("cost"))
    charge = to_cents(row.get("chargeable"))
    known = cost is not None and charge is not None
    parts = [p for p in row.get("byProject") or [] if isinstance(p, dict)]
    if not parts:
        parts = [{"project": "unknown", "requests": row.get("requests"), "tokens": row.get("tokens"),
                  "cost": row.get("cost"), "chargeable": row.get("chargeable"),
                  "credit": None if not known else (row.get("cost") or 0) - (row.get("chargeable") or 0)}]
    credit_parts = charge_parts = None
    if known:
        if mode == "list":
            charge = cost
        charge = max(0, min(charge, cost))
        fallback = [p.get("cost") for p in parts]
        credit_parts = allocate(cost - charge, [0 if mode == "list" else p.get("credit") for p in parts], fallback)
        charge_parts = allocate(charge, [p.get("cost") if mode == "list" else p.get("chargeable") for p in parts],
                                fallback)
    facts = []
    for i, part in enumerate(parts):
        fact = {"person": person, "project": str(part.get("project") or "unknown"),
                "requests": _int(part.get("requests")), "tokens": _int(part.get("tokens")), "seat": 0,
                "cost": None, "credit": None, "chargeable": None}
        if known:
            fact.update(credit=credit_parts[i], chargeable=charge_parts[i], cost=credit_parts[i] + charge_parts[i])
        facts.append(fact)
    return facts


def seat_facts(index, prices):
    """One fact per assigned license whose subscription tier has a seat price (chargeback.seat_prices, USD)."""
    facts = []
    for person, items in sorted((index.people if index is not None else {}).items()):
        for item in items:
            cents = to_cents(prices.get(item.get("tier")))
            if not cents:
                continue
            facts.append({"person": person, "project": item.get("project") or "unknown", "requests": 0, "tokens": 0,
                          "cost": 0, "credit": 0, "chargeable": 0, "seat": cents})
    return facts


def _zero():
    return {"requests": 0, "tokens": 0, "cost": 0, "credit": 0, "chargeable": 0, "seat": 0, "total": 0}


def _add(acc, fact):
    acc["requests"] += fact["requests"]
    acc["tokens"] += fact["tokens"]
    for key in ("cost", "credit", "chargeable", "seat"):
        if fact.get(key) is not None:
            acc[key] += fact[key]
    acc["total"] += (fact.get("chargeable") or 0) + (fact.get("seat") or 0)


def _amounts(acc, unknown=False):
    """Public amounts of an accumulator: money in currency units; usage money None when unknown."""
    out = {"requests": acc["requests"], "tokens": acc["tokens"], "seat": money(acc["seat"])}
    for key in ("cost", "credit", "chargeable", "total"):
        out[key] = None if unknown else money(acc[key])
    return out


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

def _billing_report(ctx, month):
    """governance.billing_report of `month` over every workspace project."""
    child = ctx.child()
    child.params = dict(ctx.params or {}, month=month, project="all")
    report = governance.billing_report(child)
    ctx.absorb(child)
    return report


def _project_labels(ctx):
    try:
        from . import scope
        return scope.project_labels(ctx) or {}
    except Exception as err:  # pylint: disable=broad-except
        LOG.warning("project labels unavailable: %s", type(err).__name__)
        return {}


def _license_index(ctx):
    from . import gemini_enterprise
    try:
        index = gemini_enterprise.license_index(ctx)
    except Exception as err:  # pylint: disable=broad-except
        LOG.warning("license index unavailable: %s", type(err).__name__)
        index = None
    return index if (index is not None and index.ok) else None


def _unit_public(org, unit_id):
    unit = org.unit(unit_id) or {}
    return {"id": unit_id, "name": unit.get("name") or unit_id, "path": unit.get("path") or unit_id,
            "parent": unit.get("parent"), "depth": unit.get("depth") or 0, "costCenter": unit.get("costCenter") or ""}


def build(ctx, month):
    """The live chargeback model of one month: {"summary": ..., "users": [...], "facts": [...]}."""
    cb = ctx.settings.get("chargeback") or {}
    mode = cb.get("allocation_mode") or "credit_share"
    label_key = cb.get("project_unit_label") or ""
    report = _billing_report(ctx, month)
    currency = report.get("currency") or "USD"
    org = org_model.load(ctx)
    labels = _project_labels(ctx) if label_key else {}
    prices = config.tier_amounts(cb.get("seat_prices"))
    index = None
    if prices and currency != "USD":
        ctx.warn("chargeback:seats", "currency_mismatch",
                 "Seat prices are in USD, so seat costs are not added to a report in %s." % currency)
    elif prices:
        index = _license_index(ctx)
        if index is None:
            ctx.warn("chargeback:seats", "licenses_unavailable",
                     "License assignments could not be read, so seat costs are missing.")
    rows = {}
    facts = []
    for row in report.get("users") or []:
        person = row.get("email")
        if not identity.is_person_key(person):
            continue
        rows[person] = row
        facts.extend(usage_facts(row, mode))
    facts.extend(seat_facts(index, prices))
    for fact in facts:
        fact["unit"], fact["via"] = org.unit_for(fact["person"], fact["project"],
                                                 labels.get(fact["project"]) or {}, label_key)
    return _rollup(month, report, org, facts, rows, index, mode, currency, bool(prices and index is not None),
                   label_key, labels)


def _rollup(month, report, org, facts, rows, index, mode, currency, seat_pricing, label_key, labels):
    people, units, projects = {}, {}, {}
    total = _zero()
    unknown_people = set()
    for fact in facts:
        _add(total, fact)
        person = people.setdefault(fact["person"], {"acc": _zero(), "units": {}, "projects": set()})
        _add(person["acc"], fact)
        weight = (fact.get("chargeable") or 0) + fact["seat"]
        key = (fact["unit"], fact["via"])
        person["units"][key] = person["units"].get(key, (0, 0))
        person["units"][key] = (person["units"][key][0] + weight, person["units"][key][1] + fact["requests"])
        person["projects"].add(fact["project"])
        if fact.get("cost") is None:
            unknown_people.add(fact["person"])
        unit = units.setdefault(fact["unit"], {"acc": _zero(), "users": set()})
        _add(unit["acc"], fact)
        unit["users"].add(fact["person"])
        proj = projects.setdefault(fact["project"], {"acc": _zero(), "users": set()})
        _add(proj["acc"], fact)
        proj["users"].add(fact["person"])
    # Rollups over the unit tree.
    rolled = {}
    for unit_id, item in units.items():
        for anc in org.ancestors(unit_id):
            agg = rolled.setdefault(anc, {"acc": _zero(), "users": set()})
            for key in SUMS:
                agg["acc"][key] += item["acc"][key]
            agg["users"] |= item["users"]
    order = [u for u in org.dfs_order() if u in rolled]
    if org_model.UNASSIGNED in rolled:
        order.append(org_model.UNASSIGNED)
    unit_rows = []
    for unit_id in order:
        own = units.get(unit_id) or {"acc": _zero(), "users": set()}
        agg = rolled[unit_id]
        row = _unit_public(org, unit_id)
        row.update(users=len(agg["users"]), ownUsers=len(own["users"]),
                   own=_money_only(own["acc"]), rollup=_money_only(agg["acc"]),
                   requests=agg["acc"]["requests"], tokens=agg["acc"]["tokens"])
        unit_rows.append(row)
    project_rows = []
    for project, item in projects.items():
        unit_id, _via = org.unit_for("", project, labels.get(project) or {}, label_key)
        unit = _unit_public(org, unit_id)
        project_rows.append(dict(_amounts(item["acc"]), project=project, unitId=unit_id, unitPath=unit["path"],
                                 users=len(item["users"])))
    project_rows.sort(key=lambda r: (-(r["total"] or 0), r["project"]))
    user_rows = []
    for person, item in people.items():
        ranked = sorted(item["units"].items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0][0]))
        (unit_id, via), _ = ranked[0]
        if len({u for (u, _v) in item["units"]}) > 1:
            via = "mixed"
        unit = _unit_public(org, unit_id)
        source = rows.get(person) or {}
        tiers = index.tiers(person) if index is not None else []
        parsed = identity.parse(person)
        user_rows.append(dict(_amounts(item["acc"], unknown=person in unknown_people),
                              uid=source.get("uid") or uid_for(person), person=person,
                              kind=parsed.kind if parsed else identity.OTHER, unitId=unit_id, unitPath=unit["path"],
                              costCenter=unit["costCenter"], via=via, projects=sorted(item["projects"]),
                              byModel=list(source.get("byModel") or []),
                              tier=source.get("tier") or (tiers[0] if tiers else None),
                              creditSource=source.get("creditSource")))
    user_rows.sort(key=lambda r: (-(r["total"] or 0), r["person"]))
    unassigned = units.get(org_model.UNASSIGNED)
    totals = dict(_amounts(total), users=len(people), unknownUsers=len(unknown_people),
                  unassignedUsers=len(unassigned["users"]) if unassigned else 0,
                  unassignedTotal=money(unassigned["acc"]["total"]) if unassigned else 0.0)
    _check_invariants(total, people, units, projects)
    summary = {
        "month": month, "currency": currency, "allocationMode": mode, "basis": "computed",
        "complete": bool(report.get("complete")), "usageSource": report.get("usageSource") or "unavailable",
        "seatPricing": seat_pricing, "totals": totals, "units": unit_rows, "projects": project_rows,
        "orgModel": {"version": org.version, "updatedAt": org.updated_at, "empty": org.empty,
                     "units": len(org.units), "members": len(org.members), "projects": len(org.projects),
                     "labelKey": label_key},
    }
    return {"summary": summary, "users": user_rows, "facts": facts, "orgVersion": org.version}


def _money_only(acc):
    return {key: money(acc[key]) for key in ("cost", "credit", "chargeable", "seat", "total")}


def _check_invariants(total, people, units, projects):
    """Every rollup is a sum of the same facts; a mismatch would be a bug, never a rounding effect."""
    for name, groups in (("people", people), ("units", units), ("projects", projects)):
        for key in ("cost", "credit", "chargeable", "seat", "total"):
            if sum(g["acc"][key] for g in groups.values()) != total[key]:
                LOG.error("chargeback invariant failed: %s.%s", name, key)
                raise ApiError("internal", "The chargeback totals are inconsistent.")


def _model(ctx, month):
    """The live model of a month for display (cached for five minutes; without the facts, which only a close needs)."""
    def compute(c):
        model = build(c, month)
        return {"summary": model["summary"], "users": model["users"], "orgVersion": model["orgVersion"]}

    memo = getattr(ctx, "memo", None)
    if memo is None:
        return compute(ctx)
    return memo("chargeback", month, compute, ttl=300)


# ---------------------------------------------------------------------------
# Month close
# ---------------------------------------------------------------------------

def _closes(ctx):
    doc, _ = aux_docs(getattr(ctx, "app", None)).load(CLOSES_DOC)
    return [c for c in (doc.get("closes") or []) if isinstance(c, dict) and c.get("month") and c.get("doc")]


def _public_close(item):
    return {"month": item["month"], "closeId": item.get("closeId"), "closedAt": item.get("closedAt"),
            "closedBy": item.get("closedBy"), "orgVersion": item.get("orgVersion"),
            "inputsSha256": item.get("inputsSha256"), "rows": item.get("rows")}


def _latest_close(closes, month):
    found = [c for c in closes if c["month"] == month]
    return found[-1] if found else None


def _snapshot(ctx, item):
    doc, _ = aux_docs(getattr(ctx, "app", None)).load(item["doc"])
    return doc if doc.get("closeId") == item.get("closeId") else None


def _fact_rows(facts):
    return sorted([f["person"], f["project"], f["unit"], f["via"], f["requests"], f["tokens"], f.get("cost"),
                   f.get("credit"), f.get("chargeable"), f["seat"]] for f in facts)


def close(ctx):
    """POST /api/v1/chargeback/close {month, reclose}: freezes a past month."""
    body = ctx.body or {}
    month = parse_month(body.get("month"), ctx)
    if month >= current_month(ctx):
        raise ApiError("bad_request", "Only a past month can be closed.")
    reclose = body.get("reclose") is True
    store = aux_docs(getattr(ctx, "app", None))
    earlier = [c for c in _closes(ctx) if c["month"] == month]
    if earlier and not reclose:
        raise ApiError("conflict", "This month is already closed. Confirm a re-close to create a new close.")
    model = build(ctx, month)
    facts = model["facts"]
    if len(facts) + len(model["users"]) > MAX_CLOSE_ROWS:
        raise ApiError("bad_request", "The month close has too many rows (more than %d)." % MAX_CLOSE_ROWS)
    seq = len(earlier) + 1
    close_id = "c_%s_%d" % (month.replace("-", ""), seq)
    doc_name = "chargeback_close_%s_%d" % (month.replace("-", "_"), seq)
    fact_rows = _fact_rows(facts)
    digest = hashlib.sha256(json.dumps(fact_rows, separators=(",", ":"), ensure_ascii=False)
                            .encode("utf-8")).hexdigest()
    closed_at = iso(governance.utcnow())
    snapshot = {"closeId": close_id, "month": month, "closedAt": closed_at, "closedBy": ctx.identity.email,
                "orgVersion": model["orgVersion"], "inputsSha256": digest, "summary": model["summary"],
                "users": model["users"]}

    def save_snapshot(doc):
        if doc.get("closeId"):
            raise ApiError("conflict", "Another month close was saved at the same time. Reload and try again.")
        doc.update(snapshot)
        return True

    def append(doc):
        closes = [c for c in doc.get("closes") or [] if isinstance(c, dict)]
        if len([c for c in closes if c.get("month") == month]) != seq - 1:
            raise ApiError("conflict", "Another month close was saved at the same time. Reload and try again.")
        closes.append({"month": month, "closeId": close_id, "doc": doc_name, "closedAt": closed_at,
                       "closedBy": ctx.identity.email, "orgVersion": model["orgVersion"],
                       "inputsSha256": digest, "rows": len(facts)})
        doc["closes"] = closes
        return True

    store.mutate(doc_name, save_snapshot)
    store.mutate(CLOSES_DOC, append)
    governance.write_audit(ctx, "chargeback_close", {"month": month, "closeId": close_id, "rows": len(facts),
                                                     "inputsSha256": digest, "orgVersion": model["orgVersion"],
                                                     "reclose": bool(earlier)})
    invalidate = getattr(getattr(ctx, "app", None), "invalidate_governance", None)
    if callable(invalidate):
        invalidate()
    copy = None
    dataset = (ctx.settings.get("chargeback") or {}).get("close_dataset") or ""
    if dataset:
        copy = _copy_to_bigquery(ctx, dataset, snapshot, facts, model["summary"])
    return {"month": month, "closeId": close_id, "closedAt": closed_at, "rows": len(facts), "status": "closed",
            "inputsSha256": digest, "bigquery": copy}


CLOSE_SCHEMA = [
    {"name": "close_id", "type": "STRING", "mode": "REQUIRED"},
    {"name": "month", "type": "STRING", "mode": "REQUIRED"},
    {"name": "closed_at", "type": "TIMESTAMP", "mode": "REQUIRED"},
    {"name": "currency", "type": "STRING"},
    {"name": "allocation_mode", "type": "STRING"},
    {"name": "org_version", "type": "INT64"},
    {"name": "person", "type": "STRING"},
    {"name": "person_kind", "type": "STRING"},
    {"name": "project", "type": "STRING"},
    {"name": "unit_id", "type": "STRING"},
    {"name": "unit_path", "type": "STRING"},
    {"name": "cost_center", "type": "STRING"},
    {"name": "attributed_by", "type": "STRING"},
    {"name": "requests", "type": "INT64"},
    {"name": "tokens", "type": "INT64"},
    {"name": "cost", "type": "NUMERIC"},
    {"name": "credit", "type": "NUMERIC"},
    {"name": "chargeable", "type": "NUMERIC"},
    {"name": "seat", "type": "NUMERIC"},
    {"name": "total", "type": "NUMERIC"},
]


def _decimal_text(cents):
    return None if cents is None else "%s%d.%02d" % ("-" if cents < 0 else "", abs(cents) // 100, abs(cents) % 100)


def _copy_to_bigquery(ctx, dataset_ref, snapshot, facts, summary):
    """Appends the close to <dataset>.chargeback_close (created on first use). Never raises: the internal snapshot is
    the record, a failed copy is a warning."""
    project, _, dataset = dataset_ref.rpartition(".")
    project = project or ctx.project
    label = "bigquery:%s.%s" % (dataset, CLOSE_TABLE)
    if not (config.valid_project(project) and re.match(r"^[A-Za-z0-9_]{1,1024}$", dataset or "")):
        ctx.warn(label, "not_configured", "The chargeback close dataset is not a valid identifier.")
        return None
    base = "%s/projects/%s/datasets/%s/tables" % (BQ_V2, project, dataset)
    org = None
    try:
        try:
            ctx.rest("GET", "%s/%s" % (base, CLOSE_TABLE))
        except GcpError as err:
            if err.kind != "not_found":
                raise
            ctx.rest("POST", base, json_body={
                "tableReference": {"projectId": project, "datasetId": dataset, "tableId": CLOSE_TABLE},
                "description": "AI Control Plane chargeback month closes (append-only).",
                "schema": {"fields": CLOSE_SCHEMA}})
        org = org_model.load(ctx)
        rows = []
        for i, fact in enumerate(facts):
            unit = _unit_public(org, fact["unit"])
            parsed = identity.parse(fact["person"])
            total = None if fact.get("chargeable") is None else fact["chargeable"] + fact["seat"]
            rows.append({"insertId": "%s-%d" % (snapshot["closeId"], i), "json": {
                "close_id": snapshot["closeId"], "month": snapshot["month"], "closed_at": snapshot["closedAt"],
                "currency": summary.get("currency"), "allocation_mode": summary.get("allocationMode"),
                "org_version": snapshot.get("orgVersion") or 0, "person": fact["person"],
                "person_kind": parsed.kind if parsed else identity.OTHER, "project": fact["project"],
                "unit_id": fact["unit"], "unit_path": unit["path"], "cost_center": unit["costCenter"],
                "attributed_by": fact["via"], "requests": fact["requests"], "tokens": fact["tokens"],
                "cost": _decimal_text(fact.get("cost")), "credit": _decimal_text(fact.get("credit")),
                "chargeable": _decimal_text(fact.get("chargeable")), "seat": _decimal_text(fact["seat"]),
                "total": _decimal_text(total)}})
        failed = 0
        for start in range(0, len(rows), BQ_BATCH):
            out = ctx.rest("POST", "%s/%s/insertAll" % (base, CLOSE_TABLE),
                           json_body={"rows": rows[start:start + BQ_BATCH], "skipInvalidRows": False})
            failed += len((out or {}).get("insertErrors") or [])
        if failed:
            ctx.warn(label, "partial_copy", "%d rows of the month close could not be copied to BigQuery." % failed)
        return {"table": "%s.%s.%s" % (project, dataset, CLOSE_TABLE), "rows": len(rows) - failed}
    except GcpError as err:
        ctx.warn_error(label, err, "The BigQuery copy of the month close")
        return None


# ---------------------------------------------------------------------------
# GET /api/v1/chargeback/summary, GET /api/v1/chargeback/report
# ---------------------------------------------------------------------------

def _view(ctx, month):
    """(status, close, summary, users) of a month: the latest close snapshot when the month is closed."""
    closes = _closes(ctx)
    item = _latest_close(closes, month)
    if item is not None:
        snap = _snapshot(ctx, item)
        if snap is not None and isinstance(snap.get("summary"), dict):
            return "closed", _public_close(item), snap["summary"], list(snap.get("users") or []), closes
        ctx.warn("chargeback:close", "snapshot_missing",
                 "The month close snapshot could not be read; live figures are shown.")
    model = _model(ctx, month)
    return "open", None, model["summary"], model["users"], closes


def summary(ctx):
    month = parse_month((ctx.params or {}).get("month"), ctx)
    status, close_meta, data, _users, closes = _view(ctx, month)
    out = dict(data)
    out.update({"month": month, "months": recent_months(ctx), "current": month == current_month(ctx),
                "status": status, "close": close_meta,
                "closes": [_public_close(c) for c in reversed(closes)]})
    return out


def report(ctx):
    params = ctx.params or {}
    month = parse_month(params.get("month"), ctx)
    level = (params.get("level") or "user").strip().lower()
    if level not in LEVELS:
        raise ApiError("bad_request", "Parameter 'level' must be user, unit or project.")
    status, _close, data, users, _closes_list = _view(ctx, month)
    rows = {"user": users, "unit": data.get("units") or [], "project": data.get("projects") or []}[level]
    return {"month": month, "level": level, "status": status, "currency": data.get("currency"),
            "allocationMode": data.get("allocationMode"), "rows": rows, "totals": data.get("totals")}
