"""Credit caps drill: a simulated month of scheduled enforcement against the real engine.

Nothing in the control plane is replaced. services/governance.py runs exactly as in production: enforcement every five
minutes the way Cloud Scheduler calls it, exception rules saved through their API, the monthly usage report and the
ledger in services/credit_ledger.py. Only Google Cloud and the clock are simulated:

- the project IAM policy (getIamPolicy / setIamPolicy with etags, the cap marker roles),
- the Antigravity inference log in a BigQuery sink table with the schema of a real one (typed payload column,
  lower-case field names), answered by an in-memory aggregate,
- time, five minutes per scheduler run.

Simulated people try to use Gemini and Claude on a working-hours schedule. A call succeeds, and is logged, only while
the person holds the Antigravity access role, as in the product.

After every enforcement run an independent reference ledger, written call by call from the rules, checks the result:

  R1  nobody is stopped who has paid budget left, or free credit left this week without paying again this week
  R2  everybody who is over (budget spent and nothing free right now) is stopped by that run; when more people are
      over than one run may stop (the change budget), that run stops as many as allowed and the rest follow in the
      next runs, within ceil(people / change budget) runs
  R3  every stop has a restore time no later than the end of the month, and everybody is restored by then
  R4  spending beyond the paid budget stays within one five-minute interval after each restore (plus one interval
      for each run a person waited for the change budget)
  R5  people with an allow rule are never stopped; people with access through a group are never touched
  R6  the monthly usage report equals the reference ledger for every person (to the cent)

Used by scripts/caps_drill.py (people run it) and tests/unit/test_caps_drill.py (every test run).
"""

from __future__ import annotations

import bisect
import copy
import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from unittest import mock

from . import config, timeutil
from .cache import TTLCache
from .gcp import GcpError
from .schema import Source, TableSchema
from .security import Identity
from .services import credit_ledger
from .services import governance as gov
from .services.base import AGY_SINK
from .settings_store import MemoryStore, SettingsManager

UTC = dt.timezone.utc
PROJECT = "cymbal-ge-drill"
ACCESS_ROLE = "roles/discoveryengine.agentspaceUser"
SCHEDULER = "ai-control-plane-scheduler@%s.iam.gserviceaccount.com" % PROJECT
ADMIN = "admin@cymbal.example.com"
GROUP = "group:contractors@cymbal.example.com"
TICK = dt.timedelta(minutes=5)
NEAR = 1e-4   # USD: reference and engine may round differently this close to a limit; such runs are not judged

GEMINI, CLAUDE = "gemini-3-pro", "claude-opus-5-5"
# USD per million tokens (input, output, prompt tokens served from the cache). The engine gets the same prices as
# entered prices, so both sides price every call identically.
PRICES = {GEMINI: {"input": 2.0, "output": 12.0, "cached": 0.2},
          CLAUDE: {"input": 5.0, "output": 25.0, "cached": 0.5}}
CALL = {"prompt": 40000, "cached": 30000, "output": 2000}   # one agent call; promptTokenCount includes cached tokens


def call_cost(model):
    p = PRICES[model]
    return ((CALL["prompt"] - CALL["cached"]) * p["input"] + CALL["cached"] * p["cached"]
            + CALL["output"] * p["output"]) / 1e6


def is_gemini(model):
    return model.startswith("gemini")


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------

@dataclass
class Person:
    email: str
    title: str
    schedule: object            # local datetime of a five-minute slot -> [(model, calls)]
    rule: dict = None           # exception rule, as POST /api/v1/governance/antigravity/exceptions takes it
    via_group: bool = False     # Antigravity access through a Google group: the control plane cannot stop them


def _workday(t, start=9, end=18):
    return t.weekday() < 5 and start <= t.hour < end


def default_people(month_start_local):
    """Eight people covering every rule. With Standard credit (USD 2.50 a week) and a USD 100 monthly paid budget:
    one stays inside the free credit, the others run out in different ways and at different speeds."""
    first_workdays = []
    day = month_start_local.date()
    while len(first_workdays) < 2:
        if day.weekday() < 5:
            first_workdays.append(day)
        day += dt.timedelta(days=1)

    def light(t):
        return [(GEMINI, 1)] if _workday(t, 10, 18) and t.minute == 0 else []

    def gemini_heavy(t):
        return [(GEMINI, 3)] if _workday(t) else []

    def claude_only(t):
        return [(CLAUDE, 2)] if _workday(t) else []

    def first_week_spender(t):
        if not _workday(t):
            return []
        if t.date() in first_workdays:
            return [(CLAUDE, 6), (GEMINI, 1)]
        return [(GEMINI, 1)] + ([(CLAUDE, 1)] if t.minute % 15 == 0 else [])

    def steady(t):
        if not _workday(t):
            return []
        out = [(CLAUDE, 1)] if t.minute % 10 == 0 else []
        if t.minute % 30 == 0:
            out.append((GEMINI, 1))
        return out

    def credits_only(t):
        if not _workday(t):
            return []
        out = [(GEMINI, 1)] if t.minute % 10 == 0 else []
        if t.weekday() == 3 and t.hour == 15 and t.minute == 0:
            out.append((CLAUDE, 1))
        return out

    def vip(t):
        return [(CLAUDE, 4)] if _workday(t) else []

    def contractor(t):
        return [(CLAUDE, 1)] if _workday(t) and t.minute % 30 == 0 else []

    return [
        Person("light@cymbal.example.com", "Gemini only, about USD 2 a week (inside the free credit)", light),
        Person("gemini-heavy@cymbal.example.com", "Gemini only, USD 1.80 an hour", gemini_heavy),
        Person("claude-only@cymbal.example.com", "Claude only, USD 2.76 an hour (Claude is always paid)", claude_only),
        Person("first-week@cymbal.example.com", "spends the credit and USD 100 in the first two working days",
               first_week_spender),
        Person("steady@cymbal.example.com", "Claude every 10 minutes, about USD 31 a week", steady),
        Person("credits-only@cymbal.example.com", "individual rule: credits only (paid budget USD 0)", credits_only,
               rule={"type": "limit", "paidBudget": 0, "note": "drill: credits only"}),
        Person("vip@cymbal.example.com", "allow rule (never stopped), Claude USD 5.52 an hour", vip,
               rule={"type": "allow", "note": "drill: always allow"}),
        Person("contractor@cymbal.example.com", "access through a Google group (cannot be stopped)", contractor,
               via_group=True),
    ]


def crowd_people(n):
    """n more people who use Claude every 10 minutes and Gemini once an hour on working days (about USD 0.74 an hour
    each, in two groups five minutes apart). With an organization paid budget they all become over at the same
    moment, more people than one enforcement run may stop (the change budget)."""
    def make(i):
        offset = (i * 5) % 10

        def schedule(t):
            if not _workday(t) or (t.minute % 10) != offset:
                return []
            return [(CLAUDE, 1)] + ([(GEMINI, 1)] if t.minute < 10 else [])
        return Person("staff-%02d@cymbal.example.com" % (i + 1), "Claude every 10 minutes, Gemini every hour", schedule)
    return [make(i) for i in range(n)]


# ---------------------------------------------------------------------------
# Simulated Google Cloud
# ---------------------------------------------------------------------------

def sink_schema():
    """Schema of a real Log Router table of businessaicode.googleapis.com/inference_response."""
    return TableSchema.from_dicts([
        {"name": "logName", "type": "STRING"},
        {"name": "jsonpayload_v1_inferenceresponselog", "type": "RECORD", "fields": [
            {"name": "_type", "type": "STRING"},
            {"name": "metadata", "type": "RECORD", "fields": [
                {"name": n, "type": "STRING"}
                for n in ("prompttokencount", "cachedcontenttokencount", "candidatestokencount", "totaltokencount")]}]},
        {"name": "timestamp", "type": "TIMESTAMP"},
        {"name": "receiveTimestamp", "type": "TIMESTAMP"},
        {"name": "labels", "type": "RECORD", "fields": [
            {"name": n, "type": "STRING"}
            for n in ("request_id", "trajectory_id", "model", "model_provider", "client_name", "user_id")]},
    ])


class World:
    """IAM policy and inference log of the simulated project."""

    def __init__(self, persons, sink_created):
        members = ["user:%s" % p.email for p in persons if not p.via_group]
        if any(p.via_group for p in persons):
            members.append(GROUP)
        self.policy = {"version": 3, "etag": "BwE0", "bindings": [
            {"role": ACCESS_ROLE, "members": members}, {"role": "roles/owner", "members": ["user:%s" % ADMIN]}]}
        self.via_group = {p.email for p in persons if p.via_group}
        self.series = {}    # (email, model) -> prefix sums over calls in time order
        self.calls = defaultdict(list)    # email -> [(time, model)]
        self.policy_writes = 0
        self.audit_entries = 0
        self.sink = Source(PROJECT, "ge_analytics", AGY_SINK, "table", AGY_SINK, sink_schema(), created=sink_created)

    # -- IAM -------------------------------------------------------------------------------------------------
    def has_access(self, email):
        if email in self.via_group:
            return True
        member = "user:%s" % email
        return any(b.get("role") == ACCESS_ROLE and not b.get("condition") and member in b.get("members", [])
                   for b in self.policy["bindings"])

    def is_capped(self, email):
        member = "user:%s" % email
        marker = gov.marker_name(PROJECT, "CustomAntigravityCapBlocked")
        return any(b.get("role") == marker and member in b.get("members", []) for b in self.policy["bindings"])

    # -- inference log ----------------------------------------------------------------------------------------
    def log_call(self, when, email, model):
        s = self.series.setdefault((email, model), {"t": [], "n": [0], "tok": [0], "p": [0], "c": [0], "ca": [0]})
        s["t"].append(when)
        s["n"].append(s["n"][-1] + 1)
        s["tok"].append(s["tok"][-1] + CALL["prompt"] + CALL["output"])
        s["p"].append(s["p"][-1] + CALL["prompt"])
        s["c"].append(s["c"][-1] + CALL["output"])
        s["ca"].append(s["ca"][-1] + CALL["cached"])
        self.calls[email].append((when, model))

    def _rows(self, start, end, email=None):
        rows = []
        for (mail, model), s in self.series.items():
            if email and mail != email:
                continue
            i, j = bisect.bisect_left(s["t"], start), bisect.bisect_left(s["t"], end)
            if j <= i:
                continue
            rows.append({"email": mail, "model": model, "requests": s["n"][j] - s["n"][i],
                         "tokens": s["tok"][j] - s["tok"][i], "prompt_tokens": s["p"][j] - s["p"][i],
                         "candidate_tokens": s["c"][j] - s["c"][i], "cached_tokens": s["ca"][j] - s["ca"][i],
                         "clients": ["antigravity"], "last_ts": s["t"][j - 1]})
        return rows

    # -- API surface the control plane calls ----------------------------------------------------------------
    def rest(self, method, url, body):
        if url.endswith(":getIamPolicy"):
            return copy.deepcopy(self.policy)
        if url.endswith(":setIamPolicy"):
            policy = copy.deepcopy(body["policy"])
            if policy.get("etag") != self.policy["etag"]:
                raise GcpError("conflict", status=409)
            self.policy_writes += 1
            policy["etag"] = "BwE%d" % self.policy_writes
            self.policy = policy
            return copy.deepcopy(policy)
        if url.endswith(":getAncestry"):
            return {"ancestor": [{"resourceId": {"type": "project", "id": PROJECT}}]}
        if url.endswith(":testIamPermissions"):
            return {"permissions": list((body or {}).get("permissions") or [])}
        if method == "GET" and "/roles/" in url:
            return {"name": url.rsplit("/", 1)[-1], "deleted": False}
        if url.endswith("/entries:write"):
            self.audit_entries += 1
            return {}
        if url.endswith("/entries:list"):
            return {"entries": []}
        raise GcpError("not_found", status=404)

    def query(self, sql, params):
        if "GROUP BY email, model" in sql:
            return self._rows(params["w_start"], params["w_end"], params.get("email"))
        if "AS first_ts" in sql:
            firsts = [s["t"][bisect.bisect_left(s["t"], params["w_start"])] for s in self.series.values()
                      if bisect.bisect_left(s["t"], params["w_start"]) < bisect.bisect_left(s["t"], params["w_end"])]
            return [{"project": PROJECT, "first_ts": min(firsts)}] if firsts else []
        if "SELECT 1 FROM s LIMIT 1" in sql:
            return [{"n": 1 if self._rows(params["w_start"], params["w_end"]) else 0}]
        return []


class DrillApp:
    def __init__(self, manager):
        self.cache = TTLCache(512)
        self.settings = manager
        self.invalidations = 0

    def invalidate_governance(self):
        self.invalidations += 1
        self.cache.clear()


class DrillCtx:
    """The parts of services.base.Ctx the governance service uses, backed by the simulated project."""

    def __init__(self, drill, identity, params=None, body=None):
        self._drill = drill
        self.app = drill.app
        self.identity = identity
        self.params = dict(params or {})
        self.body = dict(body or {})
        self.settings, self.version = drill.manager.snapshot()
        self.warnings = []
        self.sources = set()
        self.project = PROJECT
        self.projects = [PROJECT]
        self.quota_project = PROJECT

    @property
    def ws(self):
        return self.settings["workspace"]

    @property
    def tz_name(self):
        return self.ws.get("timezone") or "UTC"

    @property
    def audit_dataset(self):
        return self.ws.get("audit_dataset") or "ge_analytics"

    @property
    def excluded_patterns(self):
        return list(self.settings["model_catalog"]["excluded_sku_patterns"])

    def rest(self, method, url, **kwargs):
        return self._drill.world.rest(method, url, kwargs.get("json_body"))

    def query(self, sql, params, module, **kwargs):  # pylint: disable=unused-argument
        return self._drill.world.query(sql, params)

    def audit_source(self, logical, optional=False):  # pylint: disable=unused-argument
        return self._drill.world.sink if logical == AGY_SINK else None

    def billing_source(self, warn_unconfigured=False):  # pylint: disable=unused-argument
        return None

    def run(self, tasks):
        out = {}
        for key, fn in tasks.items():
            try:
                out[key] = (True, fn())
            except Exception as err:  # pylint: disable=broad-except
                out[key] = (False, err)
        return out

    run_composite = run

    def memo(self, name, key, fn, ttl=None):  # pylint: disable=unused-argument
        return fn(self)

    def warn(self, source, code, message):
        self.warnings.append({"source": source, "code": code, "message": message})

    def warn_error(self, source, err, what):
        self.warnings.append({"source": source, "code": getattr(err, "kind", "error"), "message": what})

    def add_source(self, label):
        self.sources.add(label)


# ---------------------------------------------------------------------------
# Reference ledger: the rules applied call by call, independently of services/credit_ledger.py
# ---------------------------------------------------------------------------

class Reference:
    """One person's spend, updated as each call is logged (in time order)."""

    def __init__(self, credit_week, budget):
        self.credit_week, self.budget = credit_week, budget
        self.free_left = {}                      # weekly cycle start -> free credit left
        self.paid = defaultdict(float)           # month start -> paid
        self.free = defaultdict(float)           # month start -> free credit used
        self.cost = defaultdict(float)           # month start -> cost
        self.paid_week = defaultdict(float)      # (cycle start, month start) -> paid
        self.weeks = defaultdict(lambda: {"gemini": 0.0, "other": 0.0, "free": 0.0, "paid": 0.0})

    def add(self, week_start, month_start, model):
        c = call_cost(model)
        left = self.free_left.get(week_start, self.credit_week)
        free = min(c, left) if is_gemini(model) else 0.0
        self.free_left[week_start] = left - free
        self.paid[month_start] += c - free
        self.free[month_start] += free
        self.cost[month_start] += c
        self.paid_week[(week_start, month_start)] += c - free
        w = self.weeks[week_start]
        w["gemini" if is_gemini(model) else "other"] += c
        w["free"] += free
        w["paid"] += c - free

    def left(self, week_start):
        return self.free_left.get(week_start, self.credit_week)


# ---------------------------------------------------------------------------
# The drill
# ---------------------------------------------------------------------------

@dataclass
class Event:
    at: dt.datetime
    email: str
    action: str                 # block | restore
    reasons: list = field(default_factory=list)
    until: dt.datetime = None


@dataclass
class Result:
    start: dt.datetime
    end: dt.datetime
    tz: str
    budget: object
    credit_month: float
    org_budget: object
    persons: list
    events: list
    violations: list
    runs: int
    policy_writes: int
    denied: dict
    weeks: dict                 # email -> [{start, gemini, other, free, paid, month_paid}]
    month_totals: dict          # (email, month start) -> {"paid", "free", "cost"} from the reference ledger
    reports: dict               # month "YYYY-MM" -> billing report payload
    overshoot: dict             # (email, month start) -> (overshoot USD, allowance USD)
    final_capped: list
    org_overshoot: dict = field(default_factory=dict)   # month start -> (overshoot, allowance, by unstoppable) USD
    deferred_max: int = 0       # most suspensions one run deferred to the next runs (change budget)
    deferred_runs: int = 0      # runs that deferred suspensions
    change_budget: int = 0

    @property
    def passed(self):
        return not self.violations


class Drill:
    def __init__(self, start, days, budget=100.0, credit_month=10.0, org_budget=None, tz="Asia/Seoul", weekday=0,
                 at="00:00", persons=None, skip_quiet=False, crowd=0):
        zone = timeutil.get_zone(tz)
        self.start = start.astimezone(UTC)
        self.end = self.start + dt.timedelta(days=days)
        self.tz, self.zone = tz, zone
        self.budget, self.credit_month, self.org_budget = budget, float(credit_month), org_budget
        self.cycle = credit_ledger.Cycle(tz=tz, weekday=weekday, at=at)
        month_start_local = self.cycle.month(self.start)[0].astimezone(zone)
        self.persons = list(persons if persons is not None else default_people(month_start_local))
        self.persons += crowd_people(crowd)
        self.by_email = {p.email: p for p in self.persons}
        self.skip_quiet = skip_quiet
        overrides = {
            "workspace": {"project_id": PROJECT, "timezone": tz, "audit_dataset": "ge_analytics"},
            "security": {"public_base_url": "https://control-plane.cymbal.example.com"},
            "governance": {"antigravity": {
                "cap_model": "credit_budget", "credit_per_user_month": self.credit_month,
                "paid_budget_month": -1.0 if budget is None else float(budget),
                "org_paid_budget_month": -1.0 if org_budget is None else float(org_budget),
                "enforcement_mode": "auto", "reset_timezone": tz, "reset_weekday": weekday, "reset_time": at,
                "pricing_source": "manual", "usage_source": "bigquery", "token_cap": 0, "request_cap": 0,
                "model_prices": [dict(model=m, **p) for m, p in PRICES.items()],
                "scheduler_invokers": [SCHEDULER]}},
        }
        base = config.deep_merge(copy.deepcopy(config.DEFAULTS), overrides)
        self.manager = SettingsManager(base, MemoryStore())
        self.app = DrillApp(self.manager)
        self.world = World(self.persons, sink_created=self.start - dt.timedelta(days=90))
        self.now = self.start
        self.events, self.violations = [], []
        self.denied = defaultdict(int)
        self.runs = 0
        self._restores = defaultdict(int)      # (email, month start) -> restores
        self._deferred = defaultdict(int)      # (email, month start) -> runs the person waited for the change budget
        self._waiting = defaultdict(int)       # email -> consecutive runs waited
        self._org_paid = defaultdict(float)    # month start -> paid by everyone (organization budget)
        self._paid_after_org = defaultdict(float)   # (email, month start) -> paid after the organization budget ran out
        self._slot_paid_max = defaultdict(float)
        self.deferred_max = self.deferred_runs = self.change_budget = 0
        self.refs = {p.email: Reference(*self.terms(p.email)) for p in self.persons}

    # -- contexts ---------------------------------------------------------------------------------------------
    def admin(self, body=None, params=None):
        return DrillCtx(self, Identity(ADMIN, "admin", "password", "drill:admin"), params=params, body=body)

    def scheduler(self):
        return DrillCtx(self, Identity(SCHEDULER, "admin", "scheduler", "drill:scheduler"))

    # -- reference ledger (call by call, from the rules) ------------------------------------------------------
    def terms(self, email):
        rule = self.by_email[email].rule or {}
        credit = self.credit_month
        budget = self.budget
        if rule.get("type") == "limit":
            if rule.get("creditMonth") is not None:
                credit = float(rule["creditMonth"])
            if rule.get("paidBudget") is not None:
                budget = float(rule["paidBudget"])
        return credit / 4.0, budget

    def reference_over(self, now):
        ws, ms = self.cycle.week(now)[0], self.cycle.month(now)[0]
        org_paid = sum(r.paid[ms] for r in self.refs.values())
        org_spent = self.org_budget is not None and org_paid >= self.org_budget - 1e-9
        verdict = {}
        for email, r in self.refs.items():
            budget, left, paid_week = r.budget, r.left(ws), r.paid_week[(ws, ms)]
            spent = org_spent or (budget is not None and r.paid[ms] >= budget - 1e-9)
            over = spent and (left <= 1e-9 or paid_week > 1e-9)
            near = ((budget is not None and abs(r.paid[ms] - budget) < NEAR) or abs(left) < NEAR
                    or (self.org_budget is not None and abs(org_paid - self.org_budget) < NEAR)
                    or 0 < paid_week < NEAR)
            verdict[email] = (over, near, r)
        return verdict

    # -- running ----------------------------------------------------------------------------------------------
    def _act(self, slot_start):
        local = slot_start.astimezone(self.zone)
        acted = False
        cycle_cache = {}
        for p in self.persons:
            slot_paid = 0.0
            for model, calls in p.schedule(local):
                for k in range(calls):
                    when = slot_start + dt.timedelta(seconds=10 + 7 * k)
                    if self.world.has_access(p.email):
                        key = when.replace(second=0)
                        if key not in cycle_cache:
                            cycle_cache[key] = (self.cycle.week(when)[0], self.cycle.month(when)[0])
                        self.world.log_call(when, p.email, model)
                        ref, ms = self.refs[p.email], cycle_cache[key][1]
                        before = ref.paid[ms]
                        ref.add(*cycle_cache[key], model)
                        paid = ref.paid[ms] - before
                        if self.org_budget is not None and paid > 0:
                            if self._org_paid[ms] >= self.org_budget - 1e-9:
                                self._paid_after_org[(p.email, ms)] += paid
                            self._org_paid[ms] += paid
                        acted = True
                    else:
                        self.denied[p.email] += 1
                    slot_paid += call_cost(model)
            self._slot_paid_max[p.email] = max(self._slot_paid_max[p.email], slot_paid)
        return acted

    def _boundary_between(self, a, b):
        return self.cycle.week(a)[0] != self.cycle.week(b)[0] or self.cycle.month(a)[0] != self.cycle.month(b)[0]

    def _check(self, now, verdict, out):
        state = self.manager.governance_state()
        suspended = {s["email"]: s for s in state.get("suspensions") or []}
        week_end, month_end = self.cycle.week(now)[1], self.cycle.month(now)[1]
        plan = out.get("plan") or {}
        deferred = set(plan.get("deferredBlocks") or [])
        budget = plan.get("changeBudget") or 0
        stopped = sum(1 for c in out.get("changes") or [] if c["status"] == "applied"
                      and c["action"] in ("block", "disallow"))
        max_wait = -(-len(self.persons) // budget) if budget else 0
        for p in self.persons:
            over, near, _ = verdict[p.email]
            capped = self.world.is_capped(p.email)
            rule = (p.rule or {}).get("type")
            if p.via_group:
                if capped or not self.world.has_access(p.email):
                    self.violations.append(("R5", now, p.email, "a group-granted person was changed"))
                continue
            if rule == "allow":
                if capped:
                    self.violations.append(("R5", now, p.email, "a person with an allow rule was stopped"))
                continue
            waiting = over and not capped and gov.uid_for(p.email) in deferred
            if waiting:
                # More people are over than one run may stop: this run must have used its whole change budget, and
                # nobody may wait longer than it takes to work through everybody.
                self._waiting[p.email] += 1
                self._deferred[(p.email, self.cycle.month(now)[0])] += 1
                if stopped < budget:
                    self.violations.append(("R2", now, p.email, "deferred while the run stopped only %d of %d"
                                            % (stopped, budget)))
                elif self._waiting[p.email] > max_wait:
                    self.violations.append(("R2", now, p.email, "waited %d runs for the change budget"
                                            % self._waiting[p.email]))
            else:
                self._waiting.pop(p.email, None)
            if near:
                continue
            if capped and not over:
                self.violations.append(("R1", now, p.email, "stopped while budget or free credit was left"))
            if over and not capped and not waiting:
                self.violations.append(("R2", now, p.email, "over but not stopped by the run"))
            if capped:
                until = gov.parse_rfc3339((suspended.get(p.email) or {}).get("until"))
                if until is None or until > max(week_end, month_end):
                    self.violations.append(("R3", now, p.email, "no restore time before the month ends: %s" % until))

    def _record(self, now, out):
        state = self.manager.governance_state()
        suspended = {s["email"]: s for s in state.get("suspensions") or []}
        plan = out.get("plan") or {}
        self.change_budget = plan.get("changeBudget") or self.change_budget
        if plan.get("deferredBlocks"):
            self.deferred_runs += 1
            self.deferred_max = max(self.deferred_max, len(plan["deferredBlocks"]))
        for change in out.get("changes") or []:
            if change["status"] != "applied":
                continue
            email, action = change["email"], change["action"]
            susp = suspended.get(email) or {}
            self.events.append(Event(now, email, action, list(susp.get("reasons") or []),
                                     gov.parse_rfc3339(susp.get("until")) if action == "block" else None))
            if action == "restore":
                ms = self.cycle.month(now)[0]
                self._restores[(email, ms)] += 1

    def run(self):
        last_run = self.start
        with mock.patch.object(gov, "utcnow", lambda: self.now):
            self.now = self.start
            for p in self.persons:
                if p.rule:
                    gov.exception_save(self.admin(body=dict(p.rule, email=p.email)))
            slot = self.start
            while slot < self.end:
                acted = self._act(slot)
                slot += TICK
                self.now = slot
                due = (not self.skip_quiet or acted or self._boundary_between(last_run, slot)
                       or slot - last_run >= dt.timedelta(hours=1) or self._expiring(last_run, slot)
                       or bool(self._waiting))
                if not due:
                    continue
                out = gov.enforce(self.scheduler())
                self.runs += 1
                last_run = slot
                self._record(slot, out)
                self._check(slot, self.reference_over(slot), out)
            final_capped = self._final_restore()
            reports = self._reports()
        return self._result(final_capped, reports)

    def _expiring(self, a, b):
        state = self.manager.governance_state()
        times = [gov.parse_rfc3339(x.get("until")) for x in (state.get("suspensions") or []) + (state.get("exceptions") or [])]
        return any(t is not None and a < t <= b for t in times)

    def _final_restore(self):
        """R3: without further usage, every stop ends by its restore time."""
        state = self.manager.governance_state()
        untils = [gov.parse_rfc3339(s.get("until")) for s in state.get("suspensions") or []]
        if not untils:
            return []
        if None in untils:
            self.violations.append(("R3", self.now, "-", "a suspension without a restore time"))
            return [s["email"] for s in state.get("suspensions") or []]
        self.now = max(untils)   # the scheduler run at the restore time
        out = gov.enforce(self.scheduler())
        self.runs += 1
        self._record(self.now, out)
        left = [p.email for p in self.persons if self.world.is_capped(p.email)]
        for email in left:
            self.violations.append(("R3", self.now, email, "still stopped after its restore time"))
        return left

    def _reports(self):
        reports = {}
        month = self.cycle.month(self.start)[0]
        while month < self.end:
            label = month.astimezone(self.zone).strftime("%Y-%m")
            self.now = max(self.now, self.end)
            rep = gov.billing_report(self.admin(params={"month": label}))
            reports[label] = rep
            by_email = {u["email"]: u for u in rep["users"]}
            for p in self.persons:
                ref = self.refs[p.email]
                paid, free = ref.paid[month], ref.free[month]
                row = by_email.get(p.email)
                if row is None:
                    if paid > 0.005 or free > 0.005:
                        self.violations.append(("R6", month, p.email, "missing from the report"))
                    continue
                if abs((row["chargeable"] or 0) - paid) > 0.005 or abs((row["credit"] or 0) - free) > 0.005:
                    self.violations.append(("R6", month, p.email, "report paid %.4f / free %.4f, reference %.4f / %.4f"
                                            % (row["chargeable"] or 0, row["credit"] or 0, paid, free)))
            month = self.cycle.month(month)[1]
        return reports

    def _result(self, final_capped, reports):
        weeks, totals, overshoot = {}, {}, {}
        for p in self.persons:
            ref = self.refs[p.email]
            rows = []
            ws = self.cycle.week(self.start)[0]
            while ws < self.end:
                we = self.cycle.week(ws)[1]
                w = ref.weeks.get(ws) or {"gemini": 0.0, "other": 0.0, "free": 0.0, "paid": 0.0}
                end_month = self.cycle.month(min(we, self.end) - dt.timedelta(seconds=1))[0]
                month_paid = sum(v for (wk, m), v in ref.paid_week.items() if wk <= ws and m == end_month)
                rows.append(dict(w, start=ws, month_paid=month_paid))
                ws = we
            weeks[p.email] = rows
            for ms in sorted(ref.cost):
                totals[(p.email, ms)] = {"paid": ref.paid[ms], "free": ref.free[ms], "cost": ref.cost[ms]}
                if ref.budget is None or p.via_group or (p.rule or {}).get("type") == "allow":
                    continue
                over_by = max(0.0, ref.paid[ms] - ref.budget)
                allowance = self._allowance(p.email, ms)
                overshoot[(p.email, ms)] = (over_by, allowance)
                if over_by > allowance:
                    self.violations.append(("R4", ms, p.email, "USD %.2f beyond the budget, allowed %.2f"
                                            % (over_by, allowance)))
        org_overshoot = {}
        if self.org_budget is not None:
            # After the organization budget runs out everyone is credits only: each person who can be stopped pays at
            # most one interval before the next run (plus one per restore and per run waited for the change budget);
            # people who cannot be stopped (allow rule, group access) keep paying by design.
            for ms, paid in sorted(self._org_paid.items()):
                over_by = max(0.0, paid - self.org_budget)
                allowance = max(call_cost(m) for m in PRICES)   # the call that crossed the budget
                unstoppable = 0.0
                for p in self.persons:
                    if p.via_group or (p.rule or {}).get("type") == "allow":
                        unstoppable += self._paid_after_org[(p.email, ms)]
                    else:
                        allowance += self._allowance(p.email, ms) - 0.01
                allowance += unstoppable + 0.01
                org_overshoot[ms] = (over_by, allowance, unstoppable)
                if over_by > allowance:
                    self.violations.append(("R4", ms, "-", "organization: USD %.2f beyond its budget, allowed %.2f"
                                            % (over_by, allowance)))
        return Result(self.start, self.end, self.tz, self.budget, self.credit_month, self.org_budget, self.persons,
                      self.events, self.violations, self.runs, self.world.policy_writes, dict(self.denied), weeks,
                      totals, reports, overshoot, final_capped, org_overshoot=org_overshoot,
                      deferred_max=self.deferred_max, deferred_runs=self.deferred_runs,
                      change_budget=self.change_budget)

    def _allowance(self, email, ms):
        """USD a person may pay beyond a spent budget in month ms: one scheduler interval of their heaviest use before
        the first stop, after each restore and for each run they waited for the change budget."""
        episodes = self._restores[(email, ms)] + 1 + self._deferred[(email, ms)]
        return episodes * self._slot_paid_max[email] + 0.01


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

RULES = {
    "R1": ("Nobody is stopped while budget or free credit is left", "한도나 무료 크레딧이 남은 사람은 정지되지 않는다"),
    "R2": ("Everybody who is over is stopped by the next run (a wave above the change budget within a few runs)",
           "한도를 넘은 사람은 다음 실행(5분 이내)에 정지된다 (변경 예산을 넘는 대량 정지는 몇 번의 실행에 나눠서)"),
    "R3": ("Every stop ends by the weekly reset or the month start", "모든 정지는 주간 초기화나 월 초에 풀린다"),
    "R4": ("Spending beyond a budget stays within one interval per restore (person and organization)",
           "한도 초과분은 복원 1회당 실행 간격(5분) 사용량 이내 (개인·조직 한도 모두)"),
    "R5": ("Allow rules and group-granted people are never stopped", "항상 허용·그룹 권한 사용자는 정지되지 않는다"),
    "R6": ("The monthly report equals the reference ledger", "월간 리포트 금액 = 기준 계산 (센트 단위)"),
}
REASONS = {"budget": ("paid budget spent", "월 추가 결제 한도 소진"),
           "credit": ("weekly free credit used up", "이번 주 무료 크레딧 소진"),
           "paid": ("paid usage again this week", "이번 주 추가 결제 발생"),
           "tokens": ("token cap", "토큰 한도"), "requests": ("request cap", "요청 한도")}
CROWD_PREFIX = "staff-"


def _usd(v):
    return "$%.2f" % v if v is not None else "-"


def format_report(res, lang="ko"):
    ko = lang == "ko"
    zone = timeutil.get_zone(res.tz)

    def local(t):
        return t.astimezone(zone).strftime("%m-%d %a %H:%M")

    lines = []
    budget = "없음" if (ko and res.budget is None) else ("none" if res.budget is None else _usd(res.budget))
    if ko:
        lines.append("Antigravity 크레딧 한도 드릴 (실제 집행 엔진, 5분마다 스케줄 실행)")
        lines.append("기간 %s ~ %s (%s) · 1인 월 크레딧 %s (주 %s, Gemini만) · 1인 월 추가 결제 한도 %s%s"
                     % (local(res.start), local(res.end), res.tz, _usd(res.credit_month), _usd(res.credit_month / 4),
                        budget, (" · 조직 월 추가 결제 한도 %s" % _usd(res.org_budget)) if res.org_budget else ""))
        lines.append("사용자 %d명 · 집행 실행 %d회 · IAM 변경 %d회 · 변경 예산 실행당 %d명"
                     % (len(res.persons), res.runs, res.policy_writes, res.change_budget))
        if res.deferred_runs:
            lines.append("한 번에 정지할 인원이 변경 예산을 넘은 실행 %d회 (최대 %d명을 다음 실행으로 넘김)"
                         % (res.deferred_runs, res.deferred_max))
    else:
        lines.append("Antigravity credit caps drill (real enforcement engine, scheduled every 5 minutes)")
        lines.append("%s to %s (%s) · credit %s per person and month (%s a week, Gemini only) · paid budget %s%s"
                     % (local(res.start), local(res.end), res.tz, _usd(res.credit_month), _usd(res.credit_month / 4),
                        budget, (" · organization paid budget %s" % _usd(res.org_budget)) if res.org_budget else ""))
        lines.append("%d people · %d enforcement runs · %d IAM policy writes · change budget %d per run"
                     % (len(res.persons), res.runs, res.policy_writes, res.change_budget))
        if res.deferred_runs:
            lines.append("%d runs had more people to stop than the change budget (at most %d waited for the next run)"
                         % (res.deferred_runs, res.deferred_max))
    for ms, (over_by, allowance, unstoppable) in sorted(res.org_overshoot.items()):
        lines.append(("%s 조직 한도 초과분 %s (그중 항상 허용·그룹 사용자 %s, 나머지는 정지 전 5분 사용분) · 허용 범위 %s"
                      if ko else "%s organization overshoot %s (%s by people who cannot be stopped, the rest in the "
                      "minutes before each stop) · allowed %s")
                     % (ms.astimezone(zone).strftime("%Y-%m"), _usd(over_by), _usd(unstoppable), _usd(allowance)))
    lines.append("")
    crowd = [p for p in res.persons if p.email.startswith(CROWD_PREFIX)]
    for p in res.persons:
        if p in crowd:
            continue
        lines.append("■ %s — %s" % (p.email, p.title))
        lines.append("   %s" % ("주       Gemini   Claude   무료 적용  추가 결제  월 누적 추가 결제" if ko else
                                "week     Gemini   Claude   free       paid       paid this month"))
        for w in res.weeks[p.email]:
            if w["gemini"] == 0 and w["other"] == 0:
                continue
            lines.append("   %-8s %-8s %-8s %-10s %-10s %s" % (w["start"].astimezone(zone).strftime("%m-%d"),
                                                            _usd(w["gemini"]), _usd(w["other"]), _usd(w["free"]),
                                                            _usd(w["paid"]), _usd(w["month_paid"])))
        evs = [e for e in res.events if e.email == p.email]
        if not evs:
            lines.append("   %s" % ("정지 없음" if ko else "never stopped"))
        for e in evs:
            if e.action == "block":
                why = ", ".join(REASONS.get(r, (r, r))[1 if ko else 0] for r in e.reasons)
                lines.append("   %s  %s (%s) → %s %s" % (local(e.at), "정지" if ko else "stopped", why,
                                                        "복원 예정" if ko else "restore at",
                                                        local(e.until) if e.until else "-"))
            else:
                lines.append("   %s  %s" % (local(e.at), "복원" if ko else "restored"))
        if res.denied.get(p.email):
            lines.append("   %s %d" % ("정지 중 거부된 호출" if ko else "calls refused while stopped", res.denied[p.email]))
        lines.append("")
    if crowd:
        emails = {p.email for p in crowd}
        evs = [e for e in res.events if e.email in emails]
        lines.append("■ %s01 ~ %s%02d (%d%s) — %s" % (CROWD_PREFIX, CROWD_PREFIX, len(crowd), len(crowd),
                                                      "명" if ko else " people", crowd[0].title))
        by_time = defaultdict(lambda: [0, 0])
        for e in evs:
            by_time[e.at][0 if e.action == "block" else 1] += 1
        for at in sorted(by_time):
            blocked, restored = by_time[at]
            parts = []
            if blocked:
                parts.append(("정지 %d명" if ko else "%d stopped") % blocked)
            if restored:
                parts.append(("복원 %d명" if ko else "%d restored") % restored)
            lines.append("   %s  %s" % (local(at), ", ".join(parts)))
        lines.append("")
    lines.append("검증 규칙" if ko else "Rules")
    for rid, (en, kr) in RULES.items():
        bad = [v for v in res.violations if v[0] == rid]
        lines.append("  %s %s  %s" % ("PASS" if not bad else "FAIL", rid, kr if ko else en))
        for v in bad[:5]:
            lines.append("       %s %s %s" % (v[1], v[2], v[3]))
    lines.append("")
    lines.append(("결과: PASS (위반 0건)" if ko else "Result: PASS (0 violations)") if res.passed else
                 ("결과: FAIL (위반 %d건)" % len(res.violations) if ko else "Result: FAIL (%d violations)"
                  % len(res.violations)))
    return "\n".join(lines)
