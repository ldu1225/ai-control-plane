"""controlplane.enforce_job: the Cloud Run job that runs one Antigravity enforcement cycle.

The application is faked (no Google Cloud) and governance.enforce_cycle is mocked, so these tests pin the job's own
contract: what it refuses to run on, the context it builds, the one-line summary (counts only, no e-mail addresses)
and the exit codes.
"""

import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import support

from controlplane import enforce_job
from controlplane.app import App
from controlplane.gcp import GcpError
from controlplane.httpkit import ApiError
from controlplane.services import base as svc_base
from controlplane.services import governance

USERS = ("alice@example.com", "bob@example.com", "carol@example.com", "dave@example.com")


def deny_settings(**governance_values):
    values = {"enforcement_backend": "iam_deny", "enforcement_mode": "auto"}
    values.update(governance_values)
    return support.make_settings(governance=values)


def cycle_result(changes=(), mode="apply", **plan):
    full_plan = {"block": [], "restore": [], "disallow": [], "review": [], "manual": [], "aborted": False}
    full_plan.update(plan)
    return {"mode": mode, "executed": any(c["status"] == "applied" for c in changes), "plan": full_plan,
            "gates": [{"id": "G1_POLICY_READABLE", "status": "pass"}], "changes": list(changes),
            "auditLogged": True, "webhookSent": False, "enforcementMode": "auto", "trigger": "scheduler",
            "newReview": []}


def change(email, action, status):
    return {"uid": "0123456789abcdef", "email": email, "action": action, "status": status, "message": "m"}


class FakeSettingsManager:
    def __init__(self, settings, backend="gcs", load_error=None):
        self._settings = settings
        self._backend = backend
        self.load_error = load_error

    def snapshot(self):
        return copy.deepcopy(self._settings), 7

    def persistence(self):
        return {"backend": self._backend, "durable": self._backend != "memory"}


class JobRun:
    """Runs enforce_job.main with a fake App and a mocked enforce_cycle; keeps what happened."""

    def __init__(self, argv=(), settings=None, result=None, cycle_error=None, app_error=None, store="gcs",
                 load_error=None, env=None):
        self.apps = []
        self.ctx = None

        def make_app(environ=None):
            if app_error is not None:
                raise app_error
            app = mock.Mock(spec=["settings", "environ"])
            app.environ = environ
            app.settings = FakeSettingsManager(deny_settings() if settings is None else settings, store, load_error)
            self.apps.append(app)
            return app

        def cycle(ctx, trigger, dry_run):
            self.ctx = ctx
            if cycle_error is not None:
                raise cycle_error
            return cycle_result() if result is None else result

        out, err = io.StringIO(), io.StringIO()
        environ = {"PATH": os.environ.get("PATH", ""), "SETTINGS_BUCKET": "demo-bucket"}
        environ.update(env or {})
        with mock.patch.object(enforce_job, "App", side_effect=make_app), \
                mock.patch.object(governance, "enforce_cycle", side_effect=cycle, create=True) as patched, \
                mock.patch.object(enforce_job, "_configure_logging"), \
                mock.patch.dict(os.environ, environ, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.code = enforce_job.main(list(argv))
        self.cycle = patched
        self.stdout, self.stderr = out.getvalue(), err.getvalue()
        lines = [line for line in self.stdout.splitlines() if line.strip()]
        self.lines = lines
        self.summary = json.loads(lines[-1]) if lines else None


class SuccessfulRunsTest(unittest.TestCase):
    def test_apply_run_counts_changes_and_builds_a_job_context(self):
        changes = [change(USERS[0], "block", "applied"), change(USERS[1], "restore", "applied"),
                   change(USERS[2], "disallow", "applied"), change(USERS[3], "block", "skipped")]
        run = JobRun(result=cycle_result(changes, block=["a", "b"], restore=["c"], disallow=["d"],
                                         review=["e", "f"], deferredBlocks=["b"]))
        self.assertEqual(run.code, 0)
        self.assertEqual(len(run.lines), 1, run.stdout)  # exactly one line on stdout
        summary = run.summary
        for key, value in (("trigger", "scheduler"), ("mode", "apply"), ("backend", "iam_deny"),
                           ("enforcementMode", "auto"), ("executed", True), ("blocked", 1), ("restored", 1),
                           ("disallowed", 1), ("failed", 0), ("deferred", 1), ("aborted", False), ("review", 2),
                           ("exitCode", 0)):
            self.assertEqual(summary[key], value, key)
        self.assertEqual(summary["planned"], {"block": 2, "restore": 1, "disallow": 1})
        self.assertEqual(summary["result"], "partial")  # one change was skipped (deferred)
        self.assertEqual(summary["severity"], "WARNING")  # something was deferred to the next run
        self.assertNotIn("@", run.stdout)
        # The cycle got the trigger and a context with the job principal and empty request data.
        run.cycle.assert_called_once()
        self.assertEqual(run.cycle.call_args.kwargs, {"trigger": "scheduler", "dry_run": False})
        ctx = run.ctx
        self.assertIsInstance(ctx, svc_base.Ctx)
        self.assertIs(ctx.app, run.apps[0])
        self.assertEqual((ctx.identity.email, ctx.identity.role, ctx.identity.auth_mode),
                         ("enforcement-job", "admin", "job"))
        self.assertEqual((ctx.params, ctx.body, ctx.version), ({}, {}, 7))
        self.assertEqual(ctx.settings["governance"]["antigravity"]["enforcement_backend"], "iam_deny")
        # Nobody waits on a job run, so its deny policy reads may wait out the per-minute read quota.
        self.assertIs(ctx.deny_quota_patience, True)

    def test_all_applied_is_success_at_info(self):
        run = JobRun(result=cycle_result([change(USERS[0], "block", "applied")], block=["a"]))
        self.assertEqual((run.code, run.summary["result"], run.summary["severity"]), (0, "success", "INFO"))
        self.assertIn("1 suspended", run.summary["message"])

    def test_nothing_to_do(self):
        run = JobRun()
        self.assertEqual((run.code, run.summary["result"], run.summary["executed"]), (0, "success", False))

    def test_dry_run_and_admin_trigger(self):
        run = JobRun(argv=["--dry-run", "--trigger", "admin"],
                     result=cycle_result([change(USERS[0], "block", "skipped")], mode="dry_run", block=["a"]))
        self.assertEqual(run.code, 0)
        self.assertEqual(run.cycle.call_args.kwargs, {"trigger": "admin", "dry_run": True})
        self.assertEqual((run.summary["mode"], run.summary["result"], run.summary["blocked"]), ("dry_run", "dry_run", 0))
        self.assertEqual(run.summary["planned"]["block"], 1)

    def test_aborted_by_safety_gates_is_not_a_failure(self):
        run = JobRun(result=cycle_result([change(USERS[0], "block", "skipped")], block=["a"], aborted=True))
        self.assertEqual(run.code, 0)
        self.assertEqual((run.summary["result"], run.summary["aborted"], run.summary["severity"]),
                         ("aborted", True, "WARNING"))

    def test_the_cycle_result_wins_when_it_reports_one(self):
        result = cycle_result([change(USERS[0], "block", "applied")], block=["a"])
        result["result"] = "success"
        self.assertEqual(JobRun(result=result).summary["result"], "success")

    def test_notify_only_runs(self):
        run = JobRun(settings=deny_settings(enforcement_backend="notify_only"))
        self.assertEqual((run.code, run.summary["backend"]), (0, "notify_only"))
        run.cycle.assert_called_once()

    def test_app_is_built_from_the_job_environment(self):
        run = JobRun(env={"AUTH_MODE": "google", "HOST": "0.0.0.0", "BRIDGE_RETURN_URL": "https://x.example.com"})
        environ = run.apps[0].environ
        self.assertEqual((environ["AUTH_MODE"], environ["HOST"]), ("iap", "127.0.0.1"))
        self.assertNotIn("BRIDGE_RETURN_URL", environ)
        self.assertEqual(environ["SETTINGS_BUCKET"], "demo-bucket")


class FailedRunsTest(unittest.TestCase):
    def test_failed_changes_exit_1(self):
        run = JobRun(result=cycle_result([change(USERS[0], "block", "applied"), change(USERS[1], "block", "failed")],
                                         block=["a", "b"]))
        self.assertEqual(run.code, 1)
        self.assertEqual((run.summary["result"], run.summary["failed"], run.summary["severity"]),
                         ("partial", 1, "ERROR"))
        self.assertNotIn("@", run.stdout)
        run = JobRun(result=cycle_result([change(USERS[1], "restore", "failed")], restore=["b"]))
        self.assertEqual((run.code, run.summary["result"]), (1, "failed"))

    def test_unreadable_settings_bucket_changes_nothing(self):
        run = JobRun(load_error="settings object could not be read (denied)")
        self.assertEqual((run.code, run.summary["result"], run.summary["reason"]), (1, "error", "settings_unavailable"))
        run.cycle.assert_not_called()

    def test_upstream_and_unexpected_errors(self):
        run = JobRun(cycle_error=GcpError("denied", status=403, message="caller alice@example.com denied"))
        self.assertEqual((run.code, run.summary["reason"]), (1, "upstream_denied"))
        self.assertNotIn("@", run.stdout)
        with self.assertLogs("controlplane.enforce_job", "ERROR") as logs:
            run = JobRun(cycle_error=RuntimeError("bob@example.com broke it"))
        self.assertEqual((run.code, run.summary["result"], run.summary["reason"], run.summary["error"]),
                         (1, "error", "internal", "RuntimeError"))
        self.assertNotIn("@", run.stdout)
        self.assertIn("enforcement job failed", logs.output[0])

    def test_api_errors_from_the_cycle(self):
        run = JobRun(cycle_error=ApiError("upstream_error", "The governance state store is unavailable."))
        self.assertEqual((run.code, run.summary["result"], run.summary["reason"]), (1, "error", "upstream_error"))
        run = JobRun(cycle_error=ApiError("not_configured", "Scope for carol@example.com is missing."))
        self.assertEqual((run.code, run.summary["result"], run.summary["reason"]),
                         (2, "configuration_error", "not_configured"))
        self.assertNotIn("carol@example.com", run.stdout)
        self.assertIn("<email>", run.summary["error"])

    def test_non_dict_result(self):
        run = JobRun(result=["unexpected"])
        self.assertEqual((run.code, run.summary["reason"], run.summary["error"]), (1, "internal", "TypeError"))


class ConfigurationErrorsTest(unittest.TestCase):
    def assert_config_error(self, run, reason):
        self.assertEqual(run.code, 2, run.stdout)
        self.assertEqual((run.summary["result"], run.summary["reason"], run.summary["exitCode"], run.summary["severity"]),
                         ("configuration_error", reason, 2, "ERROR"))
        run.cycle.assert_not_called()

    def test_workspace_not_configured(self):
        settings = deny_settings()
        settings["workspace"]["project_id"] = "your-gcp-project-id"
        self.assert_config_error(JobRun(settings=settings), "workspace_not_configured")

    def test_module_disabled(self):
        settings = deny_settings()
        settings["modules"]["antigravity"] = False
        self.assert_config_error(JobRun(settings=settings), "module_disabled")

    def test_allow_policy_swaps_belong_to_the_web_service(self):
        run = JobRun(settings=deny_settings(enforcement_backend="iam_allow_swap"))
        self.assert_config_error(run, "backend_not_supported")
        self.assertIn("ENFORCEMENT_BACKEND=iam_allow_swap", run.summary["error"])

    def test_cloud_run_job_without_the_settings_bucket(self):
        self.assert_config_error(JobRun(store="file", env={"CLOUD_RUN_JOB": "ai-control-plane-enforcer"}),
                                 "settings_store_not_shared")
        # Outside a Cloud Run job (a local run) the file store is the administrator's own store.
        self.assertEqual(JobRun(store="file").code, 0)

    def test_invalid_configuration(self):
        self.assert_config_error(JobRun(app_error=ValueError("invalid security.auth_mode in config.yaml")),
                                 "configuration_invalid")

    def test_invalid_arguments_still_print_one_summary(self):
        run = JobRun(argv=["--trigger", "cron"])
        self.assert_config_error(run, "invalid_arguments")
        self.assertEqual(run.summary["trigger"], "unknown")
        self.assertIn("usage:", run.stderr)

    def test_missing_cycle(self):
        out = io.StringIO()
        with mock.patch.object(governance, "enforce_cycle", None, create=True), \
                mock.patch.object(enforce_job, "_configure_logging"), contextlib.redirect_stdout(out):
            code = enforce_job.main([])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())["reason"], "cycle_unavailable")


class JobEnvironmentTest(unittest.TestCase):
    """The job's App must build from the web service's environment even though nobody signs in to the job."""

    def environ(self, tmp, **extra):
        path = os.path.join(tmp, "config.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("storage:\n  backend: memory\n")
        env = {"CONFIG_PATH": path, "PROJECT_ID": "demo-project", "HOST": "0.0.0.0", "PORT": "8080"}
        env.update(extra)
        return env

    def test_sign_in_settings_of_the_service_do_not_stop_the_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            # AUTH_MODE unset in an image that binds 0.0.0.0: the web start-up guard refuses it ...
            with self.assertRaises(ValueError):
                App(environ=self.environ(tmp))
            # ... and so does an OAuth client id without its secret.
            google = self.environ(tmp, AUTH_MODE="google",
                                  GOOGLE_OAUTH_CLIENT_ID="123456789012-abcdefgh12345678.apps.googleusercontent.com")
            with self.assertRaises(ValueError):
                App(environ=google)
            for env in (self.environ(tmp), google):
                app = App(environ=enforce_job.job_environ(env))
                settings, _ = app.settings.snapshot()
                self.assertEqual(settings["workspace"]["project_id"], "demo-project")
                self.assertIsNone(app.auth)
                self.assertEqual(settings["server"]["host"], "127.0.0.1")

    def test_job_environ_does_not_change_the_input(self):
        env = {"AUTH_MODE": "google", "HOST": "0.0.0.0", "BRIDGE_RETURN_URL": "https://x", "SETTINGS_BUCKET": "b"}
        job = enforce_job.job_environ(env)
        self.assertEqual(env["AUTH_MODE"], "google")
        self.assertEqual(job, {"AUTH_MODE": "iap", "HOST": "127.0.0.1", "SETTINGS_BUCKET": "b"})


class ModuleEntryPointTest(unittest.TestCase):
    """python -m controlplane.enforce_job (the Cloud Run job's command) with PYTHONPATH=src, as in the image."""

    def run_module(self, *args):
        env = dict(os.environ, PYTHONPATH=support.SRC)
        return subprocess.run([sys.executable, "-m", "controlplane.enforce_job", *args], capture_output=True,
                              text=True, env=env, cwd=os.path.dirname(support.SRC), check=False, timeout=120)

    def test_help(self):
        res = self.run_module("--help")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("--dry-run", res.stdout)
        self.assertIn("--trigger", res.stdout)

    def test_bad_arguments_exit_2_with_one_json_line(self):
        res = self.run_module("--trigger", "nobody")
        self.assertEqual(res.returncode, 2, res.stderr)
        line = [ln for ln in res.stdout.splitlines() if ln.startswith("{")][-1]
        self.assertEqual(json.loads(line)["reason"], "invalid_arguments")


if __name__ == "__main__":
    unittest.main()
