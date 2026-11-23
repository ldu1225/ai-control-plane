"""Enforcement job: one Antigravity cap enforcement cycle in a Cloud Run job.

    python -m controlplane.enforce_job [--dry-run] [--trigger scheduler|admin]

With ENFORCEMENT_BACKEND=iam_deny, scripts/deploy.sh (or terraform/) deploys this module as the Cloud Run job
<service>-enforcer from the dashboard image. The job runs as its own service account, the only identity that holds
Deny Admin (roles/iam.denyAdmin, organization level). Cloud Scheduler runs it on ENFORCEMENT_SCHEDULE, and "Run now" in
the dashboard, an administrator's policy or rule change in automatic mode and a usage event that reaches a cap
(services/usage_events.py) ask Cloud Run to run it (services/jobs.py). Every run builds the application from the
deployment configuration and the settings bucket without starting an HTTP server, recomputes the plan from the source
data and applies it through governance.enforce_cycle, the cycle behind the enforce endpoint (safety gates and invariants
included). --trigger defaults to scheduler: a run nobody watches never creates roles. An execution the dashboard started
records the dashboard's reason instead (governance.claim_run_request: usage or admin).

stdout gets exactly one JSON line with counts only (never e-mail addresses); Cloud Logging stores it as a structured
entry:
    {"severity", "message", "trigger", "mode", "backend", "enforcementMode", "executed", "result", "blocked",
     "restored", "disallowed", "failed", "deferred", "aborted", "review", "planned", "exitCode", ...}

Exit codes:
    0  the run succeeded: changes applied, nothing to do, a dry run, or the safety gates aborted it (nothing changed)
    1  the run failed: a change failed, the settings bucket could not be read (nothing is applied from defaults) or an
       unexpected error occurred. Cloud Run marks the execution failed; the next scheduled run tries again.
    2  configuration error that a retry does not fix (result "configuration_error", "reason" says which): invalid
       arguments or configuration, the workspace is not configured, the antigravity module is off, the backend is
       iam_allow_swap (allow-policy swaps belong to the web service, whose service account holds the project IAM
       roles; the job's service account has none), the settings bucket is missing in a Cloud Run job, or the image
       has no enforcement cycle.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time

from . import __version__, config
from .app import App, _configure_logging
from .gcp import GcpError
from .httpkit import ApiError
from .security import Identity
from .services import base as svc_base
from .services import governance

LOG = logging.getLogger("controlplane.enforce_job")

JOB_EMAIL = "enforcement-job"
JOB_AUTH_MODE = "job"
TRIGGERS = ("scheduler", "admin")
# Backends the job applies. iam_allow_swap needs project IAM admin roles, which only the web service account holds.
JOB_BACKENDS = ("iam_deny", "notify_only")
ACTIONS = ("block", "restore", "disallow")
# ApiError codes from the cycle that describe the configuration rather than a failed run.
CONFIG_API_ERRORS = ("not_configured", "module_disabled", "bad_request", "forbidden")
EXIT_OK, EXIT_FAILED, EXIT_CONFIG = 0, 1, 2
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


class JobStop(Exception):
    """Ends the run before the enforcement cycle changes anything: a short reason code and the exit code."""

    def __init__(self, reason, message, code):
        super().__init__(message)
        self.reason = reason
        self.code = code


def job_environ(environ):
    """The environment App is built with. The job serves no HTTP and signs nobody in, so sign-in settings it may share
    with the web service (AUTH_MODE google with an OAuth client, the image's 0.0.0.0 bind address, a sign-in bridge)
    must not stop it: App gets auth mode iap, which needs no session key or OAuth secret and is valid anywhere, and a
    loopback address that nothing binds. Workspace, storage and governance settings are unchanged."""
    env = dict(environ)
    env["AUTH_MODE"] = "iap"
    env["HOST"] = "127.0.0.1"
    env.pop("BRIDGE_RETURN_URL", None)
    return env


def job_identity():
    """The principal recorded as actor of the job's changes (audit log, suspension records)."""
    return Identity(JOB_EMAIL, "admin", JOB_AUTH_MODE, "job:" + JOB_EMAIL, name="Enforcement job")


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="python -m controlplane.enforce_job",
                                     description="Run one Antigravity cap enforcement cycle.")
    parser.add_argument("--dry-run", action="store_true", help="compute and report the plan; change nothing")
    parser.add_argument("--trigger", choices=TRIGGERS, default="scheduler",
                        help="who started the run (default scheduler; admin may create missing marker roles)")
    return parser.parse_args(argv)


def check_ready(app, settings, environ=None):
    """Refuses to run on settings that are not the administrators' or that the job cannot apply (raises JobStop)."""
    environ = os.environ if environ is None else environ
    if getattr(app.settings, "load_error", None):
        raise JobStop("settings_unavailable", "The settings bucket could not be read; nothing is applied from the "
                      "defaults.", EXIT_FAILED)
    if environ.get("CLOUD_RUN_JOB") and app.settings.persistence().get("backend") != "gcs":
        raise JobStop("settings_store_not_shared", "The job has no settings bucket (SETTINGS_BUCKET), so it cannot "
                      "read the dashboard's settings and governance state.", EXIT_CONFIG)
    if not config.workspace_configured(settings):
        raise JobStop("workspace_not_configured", "The workspace is not configured (workspace.project_id).",
                      EXIT_CONFIG)
    if not (settings.get("modules") or {}).get("antigravity", False):
        raise JobStop("module_disabled", "The antigravity module is disabled.", EXIT_CONFIG)
    backend = _backend(settings)
    if backend not in JOB_BACKENDS:
        raise JobStop("backend_not_supported", "The enforcement backend is %s. The job applies iam_deny and "
                      "notify_only; iam_allow_swap is applied by the web service (redeploy with "
                      "ENFORCEMENT_BACKEND=iam_allow_swap to move enforcement back to it)." % backend, EXIT_CONFIG)


def run_cycle(args, environ=None):
    """Builds the application and runs one cycle. Returns (cycle result, settings)."""
    environ = os.environ if environ is None else environ
    cycle = getattr(governance, "enforce_cycle", None)
    if cycle is None:
        raise JobStop("cycle_unavailable", "This image has no governance.enforce_cycle.", EXIT_CONFIG)
    try:
        app = App(environ=job_environ(environ))
    except ValueError as err:
        raise JobStop("configuration_invalid", "The configuration could not be loaded: %s" % err, EXIT_CONFIG)
    settings, version = app.settings.snapshot()
    check_ready(app, settings, environ)
    ctx = svc_base.Ctx(app, settings, version, job_identity(), params={}, body={})
    # Nobody waits on a job run: deny policy reads may wait out the per-minute read quota (enforcers.QUOTA_WAIT_SECONDS)
    # instead of stopping at gate G1 when the dashboard read in the same minute.
    ctx.deny_quota_patience = True
    trigger = args.trigger
    claim = getattr(governance, "claim_run_request", None)
    if trigger == "scheduler" and claim is not None:
        # The dashboard records why it starts an execution (a usage event over a cap, or an administrator) just
        # before it does; Cloud Scheduler records nothing, so its executions stay "scheduler".
        trigger = claim(ctx, default="scheduler")
    out = cycle(ctx, trigger=trigger, dry_run=args.dry_run)
    if not isinstance(out, dict):
        raise TypeError("the enforcement cycle returned %s" % type(out).__name__)
    out.setdefault("trigger", trigger)
    return out, settings


def summarize(out):
    """Counts of one cycle result (the dict of the enforce endpoint); no identities."""
    plan = out.get("plan") if isinstance(out.get("plan"), dict) else {}
    changes = [c for c in out.get("changes") or [] if isinstance(c, dict)]

    def applied(action):
        return sum(1 for c in changes if c.get("action") == action and c.get("status") == "applied")

    return {"mode": out.get("mode"), "enforcementMode": out.get("enforcementMode"),
            "executed": bool(out.get("executed")), "result": result_of(out, plan, changes),
            "blocked": applied("block"), "restored": applied("restore"), "disallowed": applied("disallow"),
            "failed": sum(1 for c in changes if c.get("status") == "failed"),
            "deferred": len(plan.get("deferredBlocks") or []) + len(plan.get("capacityHeld") or []),
            "aborted": bool(plan.get("aborted")),
            "review": len(plan.get("review") or []),
            "planned": {action: len(plan.get(action) or []) for action in ACTIONS}}


def result_of(out, plan, changes):
    """The run result: the cycle's own "result" when it reports one, else the rules of the enforce endpoint's audit
    entry (dry_run, aborted, success, partial, failed)."""
    if isinstance(out.get("result"), str) and out["result"]:
        return out["result"]
    if out.get("mode") == "dry_run":
        return "dry_run"
    statuses = [c.get("status") for c in changes]
    if plan.get("aborted") or (plan.get("deferredBlocks") and "applied" not in statuses):
        return "aborted"
    if statuses and all(s == "applied" for s in statuses):
        return "success"
    if "applied" in statuses:
        return "partial"
    if "failed" in statuses:
        return "failed"
    return "success"


def exit_code_for(summary):
    """1 when a change failed; a partial result whose other changes were only deferred or skipped is a success."""
    return EXIT_FAILED if summary["failed"] or summary["result"] == "failed" else EXIT_OK


def scrub(text, limit=300):
    """Error text for the summary line: e-mail addresses removed, length bounded."""
    return _EMAIL_RE.sub("<email>", str(text or ""))[:limit]


def emit(summary):
    """Prints the one JSON summary line (Cloud Logging reads severity and message from it)."""
    code = summary["exitCode"]
    if code:
        severity = "ERROR"
    elif summary["result"] == "aborted" or summary["deferred"]:
        severity = "WARNING"
    else:
        severity = "INFO"
    if summary["result"] in ("configuration_error", "error"):
        message = "Antigravity enforcement job: %s (%s)" % (summary["result"].replace("_", " "), summary["reason"])
    else:
        message = ("Antigravity enforcement job: %s (%d suspended, %d restored, %d blocked by rules, %d failed, "
                   "%d deferred)" % (summary["result"], summary["blocked"], summary["restored"],
                                     summary["disallowed"], summary["failed"], summary["deferred"]))
    line = dict(severity=severity, message=message, **summary)
    sys.stdout.write(json.dumps(line, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv=None):
    """Runs one cycle and returns the exit code (see the module docstring)."""
    started = time.monotonic()
    _configure_logging()
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        if not exc.code:  # --help
            return EXIT_OK
        args = argparse.Namespace(dry_run=False, trigger="unknown")
        stop = JobStop("invalid_arguments", "Invalid arguments (see the usage above).", EXIT_CONFIG)
        return _finish(_base(args), started, stop=stop)
    LOG.info("enforcement job starting trigger=%s dry_run=%s execution=%s version=%s", args.trigger, args.dry_run,
             os.environ.get("CLOUD_RUN_EXECUTION") or "-", __version__)
    summary = _base(args)
    try:
        out, settings = run_cycle(args)
        summary["backend"] = _backend(settings)
        if isinstance(out.get("trigger"), str):
            summary["trigger"] = out["trigger"]
        summary.update({k: v for k, v in summarize(out).items() if v is not None})
    except JobStop as stop:
        return _finish(summary, started, stop=stop)
    except ApiError as err:
        code = EXIT_CONFIG if err.code in CONFIG_API_ERRORS else EXIT_FAILED
        LOG.warning("enforcement cycle refused code=%s", err.code)
        return _finish(summary, started, stop=JobStop(err.code, err.message, code))
    except GcpError as err:
        LOG.error("enforcement cycle failed: upstream error kind=%s status=%s", err.kind, err.status)
        return _finish(summary, started, stop=JobStop("upstream_" + err.kind, "A Google Cloud API call failed (%s)."
                                                      % err.kind, EXIT_FAILED))
    except Exception as err:  # pylint: disable=broad-except
        LOG.exception("enforcement job failed")
        return _finish(summary, started, stop=JobStop("internal", type(err).__name__, EXIT_FAILED))
    summary["exitCode"] = exit_code_for(summary)
    return _finish(summary, started)


def _backend(settings):
    ag = ((settings or {}).get("governance") or {}).get("antigravity") or {}
    return ag.get("enforcement_backend") or "iam_allow_swap"


def _base(args):
    return {"trigger": args.trigger, "mode": "dry_run" if args.dry_run else "apply", "backend": None,
            "enforcementMode": None, "executed": False, "result": None, "blocked": 0, "restored": 0,
            "disallowed": 0, "failed": 0, "deferred": 0, "aborted": False, "review": 0,
            "planned": {action: 0 for action in ACTIONS}, "version": __version__,
            "execution": os.environ.get("CLOUD_RUN_EXECUTION") or None}


def _finish(summary, started, stop=None):
    if stop is not None:
        summary.update(result="configuration_error" if stop.code == EXIT_CONFIG else "error", reason=stop.reason,
                       error=scrub(stop), exitCode=stop.code)
    summary["durationMs"] = int((time.monotonic() - started) * 1000)
    emit(summary)
    return summary["exitCode"]


if __name__ == "__main__":
    sys.exit(main())
