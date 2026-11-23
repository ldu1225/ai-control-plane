"""Real-time enforcement of the Antigravity caps in automatic mode: one usage event per inference log entry.

A Log Router sink in every scanned project routes the Antigravity inference log
(businessaicode.googleapis.com/inference_response) to a Pub/Sub topic, and a push subscription posts every entry to
POST /api/v1/governance/antigravity/usage-event with a Google OIDC token of the dashboard's own service account
(scripts/deploy.sh; governance.antigravity.usage_events_subscription names the subscription). For each entry the handler
adds the entry to the person's usage in the latest governance status (the same prices; only entries newer than the
person's newest counted one) and, when the person reaches a cap, starts an enforcement run at once: the enforcement job
(iam_deny) or a cycle in this process. Enforcement runs also read the newest entries from Cloud Logging
(governance.antigravity_usage with fresh=True), so the run sees the entry that reached the cap. The scheduled enforcement
run (ENFORCEMENT_SCHEDULE, every 5 minutes by default) stays the safety net: it restores people at the period reset and
catches anything an event missed.

Pub/Sub push semantics: a 2xx response acknowledges the message; any other status asks Pub/Sub to deliver it again later
(the subscription's retry policy, 10 to 60 seconds). A message is acknowledged when nothing has to happen, when an
enforcement run that reads the entry is running or finished, or once one was started. It is delivered again while a run
that read usage before the entry existed is still running, so that the person is evaluated again after it. Malformed
messages are acknowledged and logged without their content: delivering them again cannot fix them.

Limits: the request that reaches the cap has already been answered when its log entry is written, requests in flight
finish, and IAM applies a deny policy change within minutes (usually under 2, sometimes 7 or more).
"""

from __future__ import annotations

import base64
import binascii
import collections
import datetime as dt
import json
import logging
import threading
import urllib.parse

from ..gcp import GcpError
from ..httpkit import ApiError
from ..security import SCHEDULER_MODE
from . import credit_ledger, governance, jobs
from .base import iso, parse_rfc3339, to_int

LOG = logging.getLogger("controlplane.usage_events")

PATH = "/api/v1/governance/antigravity/usage-event"
MAX_DATA_CHARS = 60 * 1024                    # base64 of one log entry; the request body is limited to 64 KB
SNAPSHOT_TTL = dt.timedelta(seconds=60)       # a governance status is reused this long to project usage
JOB_STATUS_TTL = dt.timedelta(seconds=5)      # the latest job execution is reused this long
# An entry can be read from Cloud Logging this long after its timestamp; a run that reads usage later sees it.
READ_LAG = dt.timedelta(seconds=10)
# A job execution reads usage at least this long after it was created (the container starts first).
JOB_STARTUP = dt.timedelta(seconds=10)
# One instance starts at most one run per this interval; events in between are delivered again.
MIN_TRIGGER_INTERVAL = dt.timedelta(seconds=30)
HEARTBEAT_EVERY = dt.timedelta(minutes=5)     # how often the last event time is written to the governance state
SEEN_TTL = dt.timedelta(minutes=30)
SEEN_MAX = 20000
ACTIVE_STATES = ("RUNNING", "PENDING")


# ---------------------------------------------------------------------------
# Message and entry parsing
# ---------------------------------------------------------------------------

def decode_push(body):
    """The log entry (dict) of a Pub/Sub push body {"message": {"data": base64 JSON, ...}, "subscription"}, or None."""
    message = body.get("message") if isinstance(body, dict) else None
    if not isinstance(message, dict):
        return None
    data = message.get("data")
    if not isinstance(data, str) or not data or len(data) > MAX_DATA_CHARS:
        return None
    try:
        entry = json.loads(base64.b64decode(data, validate=True).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return entry if isinstance(entry, dict) else None


def _text(value, limit=200):
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else ""


def entry_usage(entry, log_id, projects):
    """The usage of one Antigravity inference log entry: {"person", "ts", "key", "model", "tokens", "prompt", "cand",
    "cached"}. None when the entry is not the configured inference log of a scanned project, has no timestamp or names
    no person (service accounts get no per-user caps)."""
    name = entry.get("logName")
    if not isinstance(name, str) or "/logs/" not in name:
        return None
    project = governance.entry_project(name)
    if not project or project not in projects:
        return None
    if urllib.parse.unquote(name.split("/logs/", 1)[1]) != urllib.parse.unquote(str(log_id or "")):
        return None
    labels = entry.get("labels") if isinstance(entry.get("labels"), dict) else {}
    principal = labels.get("user_id")
    person = governance.usage_user(principal) if isinstance(principal, str) else None
    if person is None:
        return None
    ts = parse_rfc3339(entry.get("timestamp")) or parse_rfc3339(entry.get("receiveTimestamp"))
    if ts is None:
        return None
    payload = entry.get("jsonPayload") if isinstance(entry.get("jsonPayload"), dict) else {}
    meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    total = max(0, to_int(meta.get("totalTokenCount")))
    model = _text(labels.get("model_id")) or _text(labels.get("model")) or None
    key = _text(entry.get("insertId")) or _text(labels.get("request_id")) or \
        "%s|%s|%s|%d" % (project, person, iso(ts), total)
    return {"person": person, "ts": ts, "key": key, "model": model, "tokens": total,
            "prompt": max(0, to_int(meta.get("promptTokenCount"))),
            "cand": max(0, to_int(meta.get("candidatesTokenCount"))),
            "cached": max(0, to_int(meta.get("cachedContentTokenCount")))}


def usage_record(items):
    """A usage record (governance.Usage.user shape, for estimate_cost and split_cost) of entry usages."""
    rec = {"requests": 0, "tokens": 0, "promptTokens": 0, "candidateTokens": 0, "cachedTokens": 0, "byModel": {}}
    for item in items:
        rec["requests"] += 1
        rec["tokens"] += item["tokens"]
        rec["promptTokens"] += item["prompt"]
        rec["candidateTokens"] += item["cand"]
        rec["cachedTokens"] += item["cached"]
        governance.Usage.add_model(rec, item["model"], 1, item["tokens"], item["prompt"], item["cand"], item["cached"])
    return rec


def _as_utc(value):
    if isinstance(value, dt.datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=dt.timezone.utc)
    return parse_rfc3339(value) if isinstance(value, str) else None


# ---------------------------------------------------------------------------
# Projection on the latest governance status
# ---------------------------------------------------------------------------

class Snapshot:
    """What the projection needs from one governance status (governance.default_status)."""

    def __init__(self, payload, internal, version, at, prefixes):
        policy = payload.get("policy") or {}
        self.version = version
        self.at = at
        self.policy = policy
        self.window_start = parse_rfc3339(policy.get("windowStart"))
        self.next_reset = parse_rfc3339(policy.get("nextReset"))
        self.users = {u["email"]: u for u in payload.get("users") or [] if isinstance(u, dict) and u.get("email")}
        self.rules = {e["email"]: e for e in payload.get("exceptions") or [] if isinstance(e, dict) and e.get("email")}
        usage = internal.get("usage")
        self.usage_ok = bool(getattr(usage, "ok", False))
        self.last = {email: _as_utc(rec.get("last")) for email, rec in (getattr(usage, "users", None) or {}).items()}
        self.pricing = internal.get("pricing")
        self.credit = bool(internal.get("credit"))
        self.org_used_up = bool(internal.get("orgUsedUp"))
        self.prefixes = prefixes

    def stale(self, version, now):
        return (version != self.version or now - self.at >= SNAPSHOT_TTL
                or (self.next_reset is not None and now >= self.next_reset))


def _period_over(snap, row, rule, rec):
    policy = snap.policy
    if row is not None:
        caps = row.get("caps") or {}
        tcap, rcap, ccap = to_int(caps.get("tokenCap")), to_int(caps.get("requestCap")), float(caps.get("costCap") or 0)
        tokens, requests, cost = to_int(row.get("tokens")), to_int(row.get("requests")), row.get("estimatedCost")
    else:
        tcap, rcap, ccap = governance.effective_caps(rule, to_int(policy.get("tokenCap")),
                                                     to_int(policy.get("requestCap")), float(policy.get("costCap") or 0))
        tokens = requests = 0
        cost = 0.0 if getattr(snap.pricing, "configured", False) else None
    added = None
    if snap.pricing is not None:
        added, _partial = governance.estimate_cost(rec, snap.pricing)
    # Unknown cost stays unknown (the cost cap does not apply); an unpriced model adds nothing, as in the status.
    total = None if cost is None else float(cost) + float(added or 0.0)
    over, _ratio, _reasons = governance.cap_check(tokens + rec["tokens"], requests + rec["requests"], tcap, rcap, total,
                                                  ccap)
    return over


def _credit_over(snap, row, rec):
    caps = (row or {}).get("caps") or {}
    tokens = to_int((row or {}).get("tokens")) + rec["tokens"]
    requests = to_int((row or {}).get("requests")) + rec["requests"]
    if governance.cap_check(tokens, requests, to_int(caps.get("tokenCap")), to_int(caps.get("requestCap")))[0]:
        return True
    credit = (row or {}).get("credit")
    if not credit or not credit.get("costKnown") or snap.pricing is None:
        return False
    gemini, other, _known, _partial = governance.split_cost(rec, snap.pricing, snap.prefixes)
    free = min(gemini, max(0.0, float(credit.get("freeLeftWeek") or 0)))
    paid = gemini - free + other
    led = credit_ledger.Ledger(credit_week=float(credit.get("creditWeek") or 0),
                               free_used_week=float(credit.get("freeUsedWeek") or 0) + free,
                               paid_week=float(credit.get("paidWeek") or 0) + paid,
                               paid_month=float(credit.get("paidMonth") or 0) + paid)
    org_or_unit = snap.org_used_up or bool((row or {}).get("unitBudget"))
    return credit_ledger.decide(led, credit.get("budget"), org_or_unit)[0]


def project(snap, person, unseen):
    """What happens to `person` with their usage in the status plus `unseen` entries (entry usages newer than the
    person's newest counted entry): "suspended", "blocked", "allowed", "not_enforceable" (no access the caps act on),
    "held" (over, but the plan defers or reviews the suspension), "unknown" (usage unavailable), "over" (an enforcement
    run suspends the person) or "under"."""
    row = snap.users.get(person)
    rule = (row or {}).get("exception") or snap.rules.get(person)
    kind = (rule or {}).get("type")
    state = (row or {}).get("state") or governance.EA
    if state == governance.CAPPED:
        return "suspended"
    if state == governance.BLOCKED or kind == "block":
        return "blocked"
    if kind == "allow":
        return "allowed"
    if state != governance.EA:
        return "not_enforceable"
    if not snap.usage_ok:
        return "unknown"
    if row is not None and row.get("overCap") is True:
        return "over" if row.get("plannedAction") == "block" else "held"
    rec = usage_record(unseen)
    over = _credit_over(snap, row, rec) if snap.credit else _period_over(snap, row, rule, rec)
    return "over" if over else "under"


# ---------------------------------------------------------------------------
# Per-instance state: entries seen, the status snapshot, runs started
# ---------------------------------------------------------------------------

class Realtime:
    def __init__(self):
        self.lock = threading.Lock()        # seen entries and heartbeat
        self.snap_lock = threading.Lock()   # one status computation at a time
        self.run_lock = threading.Lock()    # one decision to start a run (and one run in this process) at a time
        self.seen = collections.OrderedDict()
        self.snap = None
        self.job = None                     # (read at, latest execution or None)
        self.last_trigger = None
        self.local = None                   # in-process run: {"started", "finished", "ok"}
        self.last_beat = None

    # -- entries -------------------------------------------------------------------------------------------------
    def remember(self, item, now):
        with self.lock:
            if item["key"] not in self.seen:
                self.seen[item["key"]] = item
            floor = now - SEEN_TTL
            while self.seen and (len(self.seen) > SEEN_MAX or next(iter(self.seen.values()))["ts"] < floor):
                self.seen.popitem(last=False)

    def unseen(self, person, after, since, item):
        """The person's entries newer than `after` (their newest counted entry) and not before `since` (the period
        start), `item` included."""
        with self.lock:
            items = [it for it in self.seen.values() if it["person"] == person]
        if not any(it["key"] == item["key"] for it in items):
            items.append(item)
        return [it for it in items if (after is None or it["ts"] > after) and (since is None or it["ts"] >= since)]

    # -- status snapshot ---------------------------------------------------------------------------------------
    def snapshot(self, ctx, now, force=False):
        with self.snap_lock:
            snap = self.snap
            if force or snap is None or snap.stale(ctx.version, now):
                payload, internal = governance.default_status(ctx.child())
                prefixes = governance.credit_prefixes(ctx.settings["governance"]["antigravity"])
                snap = Snapshot(payload, internal, ctx.version, now, prefixes)
                self.snap = snap
            return snap

    def drop_snapshot(self):
        with self.snap_lock:
            self.snap = None

    # -- runs ------------------------------------------------------------------------------------------------------
    def latest_job(self, ctx, now):
        cached = self.job
        if cached is not None and now - cached[0] < JOB_STATUS_TTL:
            return cached[1]
        latest = jobs.status(ctx).get("latest")
        self.job = (now, latest if isinstance(latest, dict) else None)
        return self.job[1]

    def ensure_run(self, ctx, snap, entry_ts, now):
        """Makes sure an enforcement run reads this entry: "covered" (a run that reads it is running or finished),
        "refresh" (a run finished after the snapshot: evaluate again), "triggered", "wait" (deliver again later) or
        "failed" (the run could not be started; deliver again later)."""
        need = min(entry_ts, now) + READ_LAG
        with self.run_lock:
            if governance.enforcement_backend(ctx.settings) == "iam_deny" and jobs.job_name(ctx.settings):
                return self._ensure_job(ctx, snap, need, now)
            return self._ensure_local(ctx, snap, need, now)

    def _ensure_job(self, ctx, snap, need, now):
        latest = self.latest_job(ctx, now)
        if latest:
            created = parse_rfc3339(latest.get("createTime"))
            state = latest.get("state")
            if created is not None and created + JOB_STARTUP >= need and state in ACTIVE_STATES + ("SUCCEEDED",):
                finished = parse_rfc3339(latest.get("completionTime"))
                if state == "SUCCEEDED" and finished is not None and finished > snap.at:
                    return "refresh"
                return "covered"
            if state in ACTIVE_STATES:
                return "wait"
        if self.last_trigger is not None and now - self.last_trigger < MIN_TRIGGER_INTERVAL:
            return "wait"
        result = governance.request_enforcement(ctx, trigger="usage")
        if not result.get("started"):
            return "failed"
        self.last_trigger = now
        # Until the next read of the job, later events see this execution as created now.
        self.job = (now, {"createTime": now.isoformat(), "state": "PENDING", "completionTime": None})
        return "triggered"

    def _ensure_local(self, ctx, snap, need, now):
        local = self.local
        if local is not None and local["started"] >= need and local["ok"]:
            return "refresh" if local["finished"] > snap.at else "covered"
        if now < need:
            return "wait"   # Cloud Logging may not return the entry yet
        if self.last_trigger is not None and now - self.last_trigger < MIN_TRIGGER_INTERVAL:
            return "wait"
        self.last_trigger = now
        result = governance.request_enforcement(ctx, trigger="usage")
        self.local = {"started": now, "finished": governance.utcnow(), "ok": bool(result.get("started"))}
        self.drop_snapshot()
        return "triggered" if result.get("started") else "failed"

    # -- governance state heartbeat (times and counts only) ------------------------------------------------------
    def beat(self, ctx, now, triggered_via=None):
        with self.lock:
            due = self.last_beat is None or now - self.last_beat >= HEARTBEAT_EVERY
            if not (due or triggered_via):
                return
            self.last_beat = now

        def apply(state):
            rec = dict(state.get("usageEvents") or {})
            rec["lastEventAt"] = iso(now)
            if triggered_via:
                rec.update(lastTriggerAt=iso(now), lastTriggerVia=triggered_via, triggers=to_int(rec.get("triggers")) + 1)
            state["usageEvents"] = rec
        try:
            governance.mutate_state(ctx, apply)
        except ApiError as err:
            LOG.warning("usage event heartbeat not saved: %s", err.code)


STATE = Realtime()


def reset():
    """Forgets this instance's entries, snapshot and runs (tests and the caps drill)."""
    global STATE  # pylint: disable=global-statement
    STATE = Realtime()


# ---------------------------------------------------------------------------
# POST /api/v1/governance/antigravity/usage-event
# ---------------------------------------------------------------------------

def _ack(action, **extra):
    return dict({"acknowledged": True, "action": action}, **extra)


def _retry(reason):
    """Asks Pub/Sub to deliver the message again later (any status other than 2xx)."""
    return ApiError("unavailable", "Deliver again later: %s." % reason, status=503)


def handle(ctx):
    """One Pub/Sub push message (one inference log entry). See the module docstring for when it acknowledges."""
    if ctx.identity.auth_mode != SCHEDULER_MODE:
        raise ApiError("forbidden", "Only the Pub/Sub push subscription of this deployment posts usage events.")
    state = STATE
    now = governance.utcnow()
    entry = decode_push(ctx.body)
    if entry is None:
        LOG.warning("usage event ignored: not a Pub/Sub message with a JSON log entry")
        return _ack("ignored", reason="invalid")
    ag = ctx.settings["governance"]["antigravity"]
    projects = list(getattr(ctx, "projects", None) or [ctx.project])
    item = entry_usage(entry, ag.get("inference_log"), projects)
    if item is None:
        return _ack("ignored", reason="not_inference_entry")
    state.beat(ctx, now)
    if (ag.get("enforcement_mode") or "manual") != "auto":
        return _ack("ignored", reason="manual_mode")
    if governance.enforcement_backend(ctx.settings) == "notify_only":
        return _ack("ignored", reason="notify_only")
    state.remember(item, now)
    try:
        verdict, decision = _decide(ctx, state, item, now)
    except (GcpError, ApiError) as err:
        LOG.warning("usage event not evaluated: %s", getattr(err, "code", None) or getattr(err, "kind", "error"))
        return _ack("ignored", reason="status_unavailable")
    if decision is None:
        return _ack(verdict)
    if decision in ("wait", "failed"):
        raise _retry("an enforcement run must read this entry" if decision == "wait" else
                     "the enforcement run could not be started")
    if decision == "triggered":
        state.beat(ctx, now, triggered_via="job" if jobs.job_name(ctx.settings) and
                   governance.enforcement_backend(ctx.settings) == "iam_deny" else "run")
        return _ack("enforcement_started")
    return _ack("covered")


def _decide(ctx, state, item, now):
    """(verdict, decision): the projection for the entry's person and, when they are over a cap, what ensure_run did
    (None when nothing has to run). A run that finished after the snapshot leads to one evaluation with a new one."""
    snap = state.snapshot(ctx, now)
    for attempt in (1, 2):
        verdict = project(snap, item["person"], state.unseen(item["person"], snap.last.get(item["person"]),
                                                             snap.window_start, item))
        if verdict != "over":
            return verdict, None
        decision = state.ensure_run(ctx, snap, item["ts"], now)
        if decision != "refresh":
            return verdict, decision
        if attempt == 1:
            snap = state.snapshot(ctx, now, force=True)
    return verdict, "covered"
