"""Maintenance tick: scheduled work, run from Cloud Scheduler (POST /api/v1/system/maintenance every few minutes,
MAINTENANCE_SCHEDULE in scripts/deploy.sh) and from an in-process monitor, always idempotently:

- the daily official price and exchange-rate check at pricing.sync_time in pricing.sync_timezone (default: the
  workspace timezone), so the schedule is changed in Settings without touching Cloud Scheduler;
- the telemetry route budget guard on every tick (pause routes over budget, resume them at the next local day);
- once a day, after the price check: retention re-application and group following for telemetry routes.

Each task is claimed in the maintenance document before it runs, so several instances and the in-process monitor
never run the same task at the same time; a failed run is retried on a later tick.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import threading

from ..httpkit import now_iso
from ..settings_store import aux_docs
from .base import parse_rfc3339, require_identity_admin

LOG = logging.getLogger("controlplane.maintenance")

DOC = "maintenance"
# A claimed task whose run did not finish is retried after this long (a crashed instance must not block it).
STALE_RUN_SECONDS = 900
# Ticks closer together than this skip the route budget guard (the scheduler and the in-process monitor overlap).
GUARD_MIN_SECONDS = 300
MONITOR_SECONDS = 300


def _now():
    return dt.datetime.now(dt.timezone.utc)


def cloud_run_scheduler_managed(app, environ=None):
    """True on Cloud Run when Cloud Scheduler drives maintenance (non-IAP mode).
    Cloud Run throttles CPU outside active HTTP requests, so daemon threads freeze mid-RPC and cause socket errors."""
    env = os.environ if environ is None else environ
    if not env.get("K_SERVICE"):
        return False
    configured = "auto"
    if app is not None and getattr(app, "settings", None) is not None:
        try:
            settings, _ = app.settings.snapshot()
            configured = ((settings.get("security") or {}).get("auth_mode") or "auto").strip().lower()
        except Exception:  # pylint: disable=broad-except
            configured = "auto"
    from ..security import resolve_auth_mode
    try:
        return resolve_auth_mode(configured, env) != "iap"
    except ValueError:
        return False


def _claim(app, task, due):
    """Marks `task` as running when due(record) says so and no other run is in progress. True when claimed."""
    def apply(doc):
        runs = doc.setdefault("runs", {})
        rec = dict(runs.get(task) or {})
        if not due(rec):
            return False
        started = parse_rfc3339(rec.get("startedAt"))
        if rec.get("status") == "running" and started and (_now() - started).total_seconds() < STALE_RUN_SECONDS:
            return False
        rec.update({"status": "running", "startedAt": now_iso()})
        runs[task] = rec
        return True
    try:
        return bool(aux_docs(app).mutate(DOC, apply))
    except Exception as err:  # pylint: disable=broad-except
        LOG.warning("maintenance claim failed task=%s: %s", task, type(err).__name__)
        return False


def _finish(app, task, ok, summary=None, error=None):
    def apply(doc):
        rec = dict((doc.setdefault("runs", {})).get(task) or {})
        rec.update({"status": "ok" if ok else "error", "finishedAt": now_iso(), "summary": summary,
                    "error": (str(error)[:200] if error else None)})
        if ok:
            rec["lastSuccess"] = rec["finishedAt"]
        doc["runs"][task] = rec
        return True
    try:
        aux_docs(app).mutate(DOC, apply)
    except Exception as err:  # pylint: disable=broad-except
        LOG.warning("maintenance record failed task=%s: %s", task, type(err).__name__)


def status(app):
    doc, _ = aux_docs(app).load(DOC)
    return doc.get("runs") or {}


def _system_ctx(app, actor):
    from .base import Ctx
    from ..security import Identity
    settings, version = app.settings.snapshot()
    return Ctx(app, settings, version, Identity(actor, "admin", "scheduler", "maintenance:%s" % actor))


def tick(app, actor="system:maintenance", trigger="scheduled"):
    """Runs every task that is due. Returns {task: summary or error}."""
    from . import pricing_sync, telemetry, telemetry_routing
    from .. import config
    settings, _ = app.settings.snapshot()
    out = {}
    if not config.workspace_configured(settings):
        return {"skipped": "workspace not configured"}

    # 1. Daily official price and exchange-rate check (a manual sync since the scheduled time also counts).
    synced_at = (app.settings.pricing_state() or {}).get("syncedAt")
    if _claim(app, "pricing_sync", lambda rec: pricing_sync.is_due(settings, synced_at)):
        try:
            res = pricing_sync.sync_pricing(app, actor=actor, trigger=trigger)
            out["pricing_sync"] = {"models": len(res.get("models") or []), "newAlerts": res.get("newAlertsCount")}
            _finish(app, "pricing_sync", True, out["pricing_sync"])
        except Exception as err:  # pylint: disable=broad-except
            LOG.warning("scheduled price sync failed: %s", type(err).__name__)
            out["pricing_sync"] = {"error": type(err).__name__}
            _finish(app, "pricing_sync", False, error=type(err).__name__)

    if not (settings.get("modules") or {}).get("telemetry", True):
        return out

    # 2. Periodic telemetry discovery snapshot (all projects + Gemini source profiling) so opening Telemetry Extensions
    #    is always instant even on cold Cloud Run instances.
    if getattr(app, "gcp", None) is not None:
        snap_doc, _ = aux_docs(app).load(telemetry.DISCOVERY_DOC)
        snap_project = snap_doc.get("project") if isinstance(snap_doc, dict) else None
        snap_updated = parse_rfc3339(snap_doc.get("updatedAt")) if isinstance(snap_doc, dict) else None
        snap_pending = bool((((snap_doc or {}).get("data") or {}).get("aiGrouping") or {}).get("pending")) \
            if isinstance(snap_doc, dict) else False
        snap_cfg_changed = bool(snap_doc.get("configFp")
                                and snap_doc.get("configFp") != telemetry._discovery_config_fp(settings)) \
            if isinstance(snap_doc, dict) else False

        def discovery_due(_rec):
            if snap_project != settings["workspace"]["project_id"] or snap_updated is None:
                return True
            if snap_pending or snap_cfg_changed:
                return True
            return (_now() - snap_updated).total_seconds() >= telemetry.DISCOVERY_REFRESH_SECONDS

        if _claim(app, "telemetry_discovery", discovery_due):
            try:
                snap = telemetry.refresh_discovery_snapshot(app, actor=actor, allow_uncached_ai=True)
                data = (snap or {}).get("data") or {}
                disc_summary = {
                    "projects": len((snap or {}).get("projects") or []),
                    "tables": len(data.get("tables") or []),
                    "logs": len(data.get("logs") or []),
                    "profiled": ((data.get("aiGrouping") or {}).get("profiled")) or 0,
                }
                out["telemetry_discovery"] = disc_summary
                _finish(app, "telemetry_discovery", True, disc_summary)
            except Exception as err:  # pylint: disable=broad-except
                LOG.warning("scheduled telemetry discovery failed: %s", type(err).__name__)
                out["telemetry_discovery"] = {"error": type(err).__name__}
                _finish(app, "telemetry_discovery", False, error=type(err).__name__)

    state, _ = aux_docs(app).load(telemetry_routing.STATE_DOC)
    if not state.get("routes"):
        return out

    # 3. Daily route upkeep after the price check: retention and group following.
    def daily_due(rec):
        return pricing_sync.is_due(settings, rec.get("lastSuccess"))

    daily = _claim(app, "telemetry_daily", daily_due)

    # 4. Route budget guard on every tick (at most every GUARD_MIN_SECONDS), always when the daily upkeep runs.
    def guard_due(rec):
        last = parse_rfc3339(rec.get("lastSuccess"))
        return last is None or (_now() - last).total_seconds() >= GUARD_MIN_SECONDS

    if daily or _claim(app, "telemetry_guard", guard_due):
        task = "telemetry_daily" if daily else "telemetry_guard"
        try:
            ctx = _system_ctx(app, actor)
            summary = telemetry_routing.run_enforcement(ctx, trigger="daily" if daily else trigger, follow=daily)
            out[task] = summary
            _finish(app, task, True, {k: summary.get(k) for k in ("paused", "resumed", "followed", "retention",
                                                                  "errors", "gbToday")})
            if daily:
                _finish(app, "telemetry_guard", True, {"via": "telemetry_daily"})
        except Exception as err:  # pylint: disable=broad-except
            LOG.warning("telemetry route enforcement failed: %s", type(err).__name__)
            out[task] = {"error": type(err).__name__}
            _finish(app, task, False, error=type(err).__name__)
    return out


def run(ctx):
    """POST /api/v1/system/maintenance: the scheduler tick (administrators may run it too)."""
    if getattr(ctx.identity, "auth_mode", "") != "scheduler":
        require_identity_admin(ctx)
    trigger = "scheduled" if ctx.identity.auth_mode == "scheduler" else "manual"
    results = tick(ctx.app, actor=ctx.identity.email or "admin", trigger=trigger)
    return {"results": results, "runs": status(ctx.app)}


class MaintenanceMonitor:
    """In-process fallback for the scheduler tick (local runs, or IAP instances where Cloud Scheduler cannot call in).
    On standard Cloud Run deployments, Cloud Scheduler invokes POST /api/v1/system/maintenance with dedicated CPU."""

    def __init__(self, app, interval=MONITOR_SECONDS):
        self.app = app
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if cloud_run_scheduler_managed(self.app):
            LOG.info("Cloud Run scheduler mode: skipping in-process maintenance thread (driven by Cloud Scheduler)")
            return False
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="maintenance-monitor", daemon=True)
            self._thread.start()
        return True

    def stop(self):
        self._stop.set()

    def _loop(self):
        if self._stop.wait(3.0):
            return
        while True:
            try:
                tick(self.app, actor="system:monitor", trigger="scheduled")
            except Exception as err:  # pylint: disable=broad-except
                LOG.warning("maintenance tick failed: %s", type(err).__name__)
            if self._stop.wait(self.interval):
                return
