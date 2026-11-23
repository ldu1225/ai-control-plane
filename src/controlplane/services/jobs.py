"""Enforcement job of the Antigravity caps: its name, the "Run now" request and its latest execution.

With the iam_deny backend, scripts/deploy.sh deploys a Cloud Run job (python -m controlplane.enforce_job) that applies
suspensions with its own service account and records it as governance.antigravity.enforcement_job
(projects/PROJECT/locations/REGION/jobs/JOB, env ENFORCEMENT_JOB). The web service only asks Cloud Run to run it and
shows how the latest execution went; it never applies deny policies itself while a job is configured.

Cloud Run Admin API v2: jobs.run returns a long-running operation whose metadata is the new Execution; Job.
latestCreatedExecution (an ExecutionReference: short name, times, completionStatus) names the most recent execution, and
executions.get returns its task counts. The web service account needs roles/run.jobsExecutor (run.jobs.run) and
roles/run.viewer (run.jobs.get, run.executions.get) on the job only.
"""

from __future__ import annotations

import logging

from .. import config
from ..gcp import GcpError
from ..httpkit import ApiError

LOG = logging.getLogger("controlplane.jobs")

RUN_V2 = "https://run.googleapis.com/v2"
COUNT_FIELDS = ("succeededCount", "failedCount", "runningCount", "cancelledCount")
TIME_FIELDS = ("createTime", "startTime", "completionTime")
# ExecutionReference.completionStatus of the job resource -> state shown for the latest execution.
COMPLETION_STATES = {"EXECUTION_SUCCEEDED": "SUCCEEDED", "EXECUTION_FAILED": "FAILED",
                     "EXECUTION_RUNNING": "RUNNING", "EXECUTION_PENDING": "PENDING",
                     "EXECUTION_CANCELLED": "CANCELLED"}
EXECUTION_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


def job_name(settings):
    """The configured enforcement job, projects/P/locations/R/jobs/J; "" when unset or not a valid job name (the
    value is used in API URLs, so anything that does not match config.ENFORCEMENT_JOB_RE is ignored)."""
    ag = ((settings or {}).get("governance") or {}).get("antigravity") or {}
    name = str(ag.get("enforcement_job") or "").strip()
    return name if name and config.ENFORCEMENT_JOB_RE.match(name) else ""


def trigger(ctx):
    """Asks Cloud Run to start one execution of the enforcement job ("Run now").

    Returns {"job": name, "operation": operation name, "execution": execution name or None}. The execution runs
    asynchronously; status() reports how it went. Raises ApiError(bad_request) when no job is configured and GcpError
    when Cloud Run refuses (for example denied without roles/run.jobsExecutor on the job)."""
    name = job_name(ctx.settings)
    if not name:
        raise ApiError("bad_request", "No enforcement job is configured for this deployment.")
    op = ctx.rest("POST", "%s/%s:run" % (RUN_V2, name), json_body={})
    op = op if isinstance(op, dict) else {}
    meta = op.get("metadata") if isinstance(op.get("metadata"), dict) else {}
    execution = _text(meta.get("name")) or None
    LOG.info("enforcement job run requested job=%s execution=%s", name, execution or "-")
    return {"job": name, "operation": _text(op.get("name")) or None, "execution": execution}


def status(ctx):
    """Whether an enforcement job is configured and how its latest execution went. Never raises.

    Returns {"configured", "job", "latest", "error"}: latest is None before the first execution, otherwise
    {"name", "createTime", "startTime", "completionTime", "succeededCount", "failedCount", "runningCount",
    "cancelledCount", "state"} with state SUCCEEDED, FAILED, CANCELLED, RUNNING, PENDING or UNKNOWN. error is None or a
    short code: the GcpError kind of the failed read (denied, not_found, unavailable, credentials, ...),
    "invalid_response" or "internal". When only the execution cannot be read, latest still carries what the job
    resource reports."""
    name = job_name(ctx.settings)
    out = {"configured": bool(name), "job": name, "latest": None, "error": None}
    if not name:
        return out
    try:
        job = ctx.rest("GET", "%s/%s" % (RUN_V2, name))
        if not isinstance(job, dict):
            out["error"] = "invalid_response"
            return out
        ref = job.get("latestCreatedExecution")
        if not isinstance(ref, dict) or not _text(ref.get("name")):
            return out  # deployed, never run yet
        exec_name = _execution_name(name, _text(ref.get("name")))
        if not exec_name:
            out["error"] = "invalid_response"
            return out
        latest = _latest(exec_name, ref, None)
        try:
            execution = ctx.rest("GET", "%s/%s" % (RUN_V2, exec_name))
            if isinstance(execution, dict):
                latest = _latest(exec_name, ref, execution)
            else:
                out["error"] = "invalid_response"
        except GcpError as err:
            out["error"] = err.kind
        out["latest"] = latest
    except GcpError as err:
        out["error"] = err.kind
    except Exception:  # pylint: disable=broad-except
        LOG.exception("enforcement job status could not be read")
        out["error"] = "internal"
    return out


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _count(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _execution_name(job, ref_name):
    """Full execution name. ExecutionReference.name is the short execution id; a full name must belong to the job.
    "" for anything else (the name goes into an API URL)."""
    prefix = job + "/executions/"
    short = ref_name[len(prefix):] if ref_name.startswith(prefix) else ref_name
    if not short or not set(short) <= EXECUTION_ID_CHARS:
        return ""
    return prefix + short


def _latest(exec_name, ref, execution):
    """Summary of the latest execution from the job's ExecutionReference and, when read (execution is a dict), the
    Execution itself."""
    read = isinstance(execution, dict)
    source = execution if read else {}
    latest = {"name": exec_name}
    for key in TIME_FIELDS:
        latest[key] = _text(source.get(key)) or _text(ref.get(key)) or None
    for key in COUNT_FIELDS:
        latest[key] = _count(source.get(key))
    latest["state"] = _state(source, latest) if read else \
        COMPLETION_STATES.get(_text(ref.get("completionStatus")), "UNKNOWN")
    return latest


def _completed_condition(execution):
    for cond in execution.get("conditions") or []:
        if isinstance(cond, dict) and cond.get("type") == "Completed":
            return _text(cond.get("state"))
    return ""


def _state(execution, latest):
    completed = _completed_condition(execution)
    if latest["completionTime"] or completed in ("CONDITION_SUCCEEDED", "CONDITION_FAILED"):
        if latest["cancelledCount"]:
            return "CANCELLED"
        if latest["failedCount"] or completed == "CONDITION_FAILED":
            return "FAILED"
        return "SUCCEEDED"
    if latest["runningCount"] or latest["startTime"]:
        return "RUNNING"
    return "PENDING"
