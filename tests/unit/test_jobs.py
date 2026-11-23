"""services/jobs.py: the enforcement Cloud Run job's name, "Run now" request and latest execution (fake ctx.rest)."""

import unittest

import support

from controlplane.gcp import GcpError
from controlplane.httpkit import ApiError
from controlplane.services import jobs

JOB = "projects/demo-project/locations/asia-northeast3/jobs/ai-control-plane-enforcer"
RUN = "https://run.googleapis.com/v2/"
EXECUTION = JOB + "/executions/ai-control-plane-enforcer-7xk2p"


class RestCtx(support.FakeCtx):
    """FakeCtx whose rest() answers routes[(method, url)] and records every call."""

    def __init__(self, job=JOB, routes=None):
        super().__init__(settings=support.make_settings(governance={"enforcement_job": job}))
        self.routes = routes or {}
        self.calls = []

    def rest(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        answer = self.routes.get((method, url))
        if answer is None:
            raise GcpError("not_found", status=404)
        if isinstance(answer, Exception):
            raise answer
        return answer


def job_resource(**ref):
    return {"name": JOB, "latestCreatedExecution": ref} if ref else {"name": JOB}


class JobNameTest(unittest.TestCase):
    def test_valid_names_only(self):
        self.assertEqual(jobs.job_name(support.make_settings(governance={"enforcement_job": JOB})), JOB)
        self.assertEqual(jobs.job_name(support.make_settings(governance={"enforcement_job": "  %s " % JOB})), JOB)
        for bad in ("", None, "ai-control-plane-enforcer", JOB + "/../../other", JOB.replace("demo", "Demo"),
                    "projects/demo-project/locations/asia-northeast3/services/ai-control-plane"):
            self.assertEqual(jobs.job_name(support.make_settings(governance={"enforcement_job": bad})), "", bad)
        self.assertEqual(jobs.job_name({}), "")
        self.assertEqual(jobs.job_name(None), "")


class TriggerTest(unittest.TestCase):
    def test_runs_the_job_and_reports_the_execution(self):
        op = {"name": "projects/demo-project/locations/asia-northeast3/operations/op-1",
              "metadata": {"@type": "type.googleapis.com/google.cloud.run.v2.Execution", "name": EXECUTION}}
        ctx = RestCtx(routes={("POST", RUN + JOB + ":run"): op})
        self.assertEqual(jobs.trigger(ctx), {"job": JOB, "operation": op["name"], "execution": EXECUTION})
        self.assertEqual(ctx.calls, [("POST", RUN + JOB + ":run", {"json_body": {}})])

    def test_operation_without_metadata(self):
        ctx = RestCtx(routes={("POST", RUN + JOB + ":run"): {"name": "operations/op-2"}})
        self.assertEqual(jobs.trigger(ctx), {"job": JOB, "operation": "operations/op-2", "execution": None})

    def test_not_configured_is_a_bad_request_without_any_call(self):
        ctx = RestCtx(job="")
        with self.assertRaises(ApiError) as caught:
            jobs.trigger(ctx)
        self.assertEqual(caught.exception.code, "bad_request")
        self.assertEqual(ctx.calls, [])

    def test_cloud_run_refusals_reach_the_caller(self):
        ctx = RestCtx(routes={("POST", RUN + JOB + ":run"): GcpError("denied", status=403)})
        with self.assertRaises(GcpError) as caught:
            jobs.trigger(ctx)
        self.assertEqual(caught.exception.kind, "denied")


class StatusTest(unittest.TestCase):
    def status(self, routes, job=JOB):
        ctx = RestCtx(job=job, routes=routes)
        return jobs.status(ctx), ctx.calls

    def test_not_configured(self):
        out, calls = self.status({}, job="")
        self.assertEqual(out, {"configured": False, "job": "", "latest": None, "error": None})
        self.assertEqual(calls, [])

    def test_never_run(self):
        out, calls = self.status({("GET", RUN + JOB): job_resource()})
        self.assertEqual(out, {"configured": True, "job": JOB, "latest": None, "error": None})
        self.assertEqual(len(calls), 1)

    def test_latest_execution_succeeded(self):
        ref = {"name": "ai-control-plane-enforcer-7xk2p", "createTime": "2026-11-19T04:00:00Z",
               "completionTime": "2026-11-19T04:01:10Z", "completionStatus": "EXECUTION_SUCCEEDED"}
        execution = {"name": EXECUTION, "createTime": "2026-11-19T04:00:00Z", "startTime": "2026-11-19T04:00:05Z",
                     "completionTime": "2026-11-19T04:01:10Z", "succeededCount": 1, "taskCount": 1,
                     "conditions": [{"type": "Completed", "state": "CONDITION_SUCCEEDED"}]}
        out, calls = self.status({("GET", RUN + JOB): job_resource(**ref), ("GET", RUN + EXECUTION): execution})
        self.assertIsNone(out["error"])
        self.assertEqual(out["latest"], {
            "name": EXECUTION, "createTime": "2026-11-19T04:00:00Z", "startTime": "2026-11-19T04:00:05Z",
            "completionTime": "2026-11-19T04:01:10Z", "succeededCount": 1, "failedCount": 0, "runningCount": 0,
            "cancelledCount": 0, "state": "SUCCEEDED"})
        self.assertEqual([c[1] for c in calls], [RUN + JOB, RUN + EXECUTION])

    def test_states_from_the_execution(self):
        ref = {"name": "ai-control-plane-enforcer-7xk2p"}
        cases = (
            ({"startTime": "t", "runningCount": 1}, "RUNNING"),
            ({}, "PENDING"),
            ({"completionTime": "t", "failedCount": 1,
              "conditions": [{"type": "Completed", "state": "CONDITION_FAILED"}]}, "FAILED"),
            ({"completionTime": "t", "cancelledCount": 1}, "CANCELLED"),
            ({"conditions": [{"type": "Completed", "state": "CONDITION_FAILED"}]}, "FAILED"),
        )
        for execution, state in cases:
            out, _ = self.status({("GET", RUN + JOB): job_resource(**ref), ("GET", RUN + EXECUTION): execution})
            self.assertEqual(out["latest"]["state"], state, execution)

    def test_full_reference_names_are_accepted(self):
        out, calls = self.status({("GET", RUN + JOB): job_resource(name=EXECUTION),
                                  ("GET", RUN + EXECUTION): {"succeededCount": 1, "completionTime": "t"}})
        self.assertEqual(out["latest"]["name"], EXECUTION)
        self.assertEqual(calls[-1][1], RUN + EXECUTION)

    def test_foreign_or_malformed_execution_names_are_never_requested(self):
        for name in ("projects/other/locations/x/jobs/y/executions/z", "../../../v1/projects", "A B"):
            out, calls = self.status({("GET", RUN + JOB): job_resource(name=name)})
            self.assertEqual(out["error"], "invalid_response", name)
            self.assertIsNone(out["latest"])
            self.assertEqual(len(calls), 1, name)

    def test_unreadable_execution_keeps_the_job_view(self):
        ref = {"name": "ai-control-plane-enforcer-7xk2p", "createTime": "c", "completionTime": "d",
               "completionStatus": "EXECUTION_FAILED"}
        out, _ = self.status({("GET", RUN + JOB): job_resource(**ref),
                              ("GET", RUN + EXECUTION): GcpError("denied", status=403)})
        self.assertEqual(out["error"], "denied")
        self.assertEqual(out["latest"]["state"], "FAILED")
        self.assertEqual(out["latest"]["createTime"], "c")
        self.assertEqual(out["latest"]["completionTime"], "d")
        self.assertIsNone(out["latest"]["startTime"])

    def test_unknown_completion_status(self):
        out, _ = self.status({("GET", RUN + JOB): job_resource(name="ai-control-plane-enforcer-1"),
                              ("GET", RUN + JOB + "/executions/ai-control-plane-enforcer-1"):
                                  GcpError("unavailable", status=503)})
        self.assertEqual(out["latest"]["state"], "UNKNOWN")
        self.assertEqual(out["error"], "unavailable")

    def test_never_raises(self):
        out, _ = self.status({("GET", RUN + JOB): GcpError("denied", status=403)})
        self.assertEqual(out, {"configured": True, "job": JOB, "latest": None, "error": "denied"})
        out, _ = self.status({("GET", RUN + JOB): ["not", "an", "object"]})
        self.assertEqual(out["error"], "invalid_response")
        with self.assertLogs("controlplane.jobs", "ERROR"):
            out, _ = self.status({("GET", RUN + JOB): RuntimeError("boom")})
        self.assertEqual(out["error"], "internal")
        self.assertIsNone(out["latest"])


if __name__ == "__main__":
    unittest.main()
