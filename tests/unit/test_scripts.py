"""Deployment scripts: the Log Router filter and the deploy-time module list.

Cloud Logging binds OR more tightly than AND, so `(A OR B) AND C OR D` means `(A OR B) AND (C OR D)`.
Written that way, the sink filter routed audit logs only and silently dropped the Gemini Enterprise
usage, Model Armor and Antigravity logs. These tests keep the shared filter a top-level OR of its
clauses and keep the copies shown in the READMEs identical to it.
"""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane import config

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
NON_AUDIT_LOGS = ("discoveryengine.googleapis.com/gemini_enterprise_user_activity",
                  "discoveryengine.googleapis.com/gen_ai_user_message",
                  "modelarmor.googleapis.com/sanitize_operations",
                  "businessaicode.googleapis.com/inference_response")


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
        return handle.read()


def library_filter():
    match = re.search(r"<<'FILTER'\n(.*?)\nFILTER\n", read("scripts/lib/common.sh"), re.S)
    return match.group(1)


def normalize(text):
    return " ".join(text.split())


def split_top_level(expr):
    """(operators, clauses, final depth) outside parentheses and string literals."""
    ops, clauses, depth, start, i = [], [], 0, 0, 0
    while i < len(expr):
        char = expr[i]
        if char == '"':
            i += 1
            while i < len(expr) and expr[i] != '"':
                i += 2 if expr[i] == "\\" else 1
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and (i == 0 or expr[i - 1].isspace()):
            match = re.match(r"(AND|OR)(?=\s|\()", expr[i:])
            if match:
                ops.append(match.group(1))
                clauses.append(expr[start:i].strip())
                i += len(match.group(1))
                start = i
                continue
        i += 1
    clauses.append(expr[start:].strip())
    return ops, clauses, depth


class SinkFilterTest(unittest.TestCase):
    def test_top_level_is_a_disjunction(self):
        ops, clauses, depth = split_top_level(library_filter())
        self.assertEqual(depth, 0)
        self.assertEqual(set(ops), {"OR"})
        self.assertEqual(len(clauses), 1 + len(NON_AUDIT_LOGS))

    def test_non_audit_logs_are_their_own_clauses(self):
        _, clauses, _ = split_top_level(library_filter())
        for log_id in NON_AUDIT_LOGS:
            self.assertIn('log_id("%s")' % log_id, clauses)
        audit = clauses[0]
        self.assertIn('log_id("cloudaudit.googleapis.com/activity")', audit)
        self.assertIn('log_id("cloudaudit.googleapis.com/data_access")', audit)
        for service in ("aiplatform", "discoveryengine", "modelarmor", "businessaicode", "notebooklm"):
            self.assertIn('"%s.googleapis.com"' % service, audit)

    def test_the_old_filter_shape_is_caught(self):
        old = ('(log_id("cloudaudit.googleapis.com/activity") OR log_id("cloudaudit.googleapis.com/data_access")) '
               'AND protoPayload.serviceName=("aiplatform.googleapis.com" OR "modelarmor.googleapis.com") '
               'OR log_id("modelarmor.googleapis.com/sanitize_operations")')
        ops, _, _ = split_top_level(old)
        self.assertIn("AND", ops)

    def test_readmes_show_the_same_filter(self):
        expected = normalize(library_filter())
        for readme in ("README.md", "README.en.md"):
            blocks = [normalize(b) for b in re.findall(r"```text\n(.*?)\n```", read(readme), re.S)]
            self.assertIn(expected, blocks, "%s must show the filter from scripts/lib/common.sh" % readme)

    def test_only_the_library_builds_a_sink(self):
        for script in ("scripts/deploy.sh", "scripts/prepare.sh"):
            text = read(script)
            self.assertNotIn("--log-filter", text, script)
            self.assertIn('source "$ROOT/scripts/lib/common.sh"', text, script)
            self.assertIn("ensure_ai_log_sink", text, script)


class DeployModulesTest(unittest.TestCase):
    def test_module_list_matches_config(self):
        match = re.search(r"\nMODULE_IDS=\(([^)]*)\)", read("scripts/deploy.sh"))
        self.assertIsNotNone(match)
        self.assertEqual(tuple(match.group(1).split()), config.MODULE_IDS)

    def test_deploy_env_example_documents_the_new_settings(self):
        example = read("deploy.env.example")
        for key in (
            "DISABLED_MODULES",
            "ENABLE_GE_OBSERVABILITY",
            "GE_PROMPT_LOGGING",
            "SCAN_PROJECTS",
            "SCAN_ALL_PROJECTS",
            "PREPARE_LINKED_PROJECTS",
            "MAINTENANCE_SCHEDULE",
            "PRICING_SYNC_TIME",
            "PRICING_CURRENCY",
        ):
            self.assertRegex(example, r"(?m)^%s=" % key)

    def test_no_fixed_price_check_time_or_timezone_in_deploy(self):
        deploy = read("scripts/deploy.sh")
        self.assertNotIn("0 4 * * *", deploy)
        self.assertNotIn('PRICING_TZ="Asia/Seoul"', deploy)
        self.assertNotIn("04:00 KST", deploy)
        self.assertIn('add_env RUNTIME_SERVICE_ACCOUNT "$SA_EMAIL"', deploy)
        self.assertIn('MAINT_URL="${BASE_URL}/api/v1/system/maintenance"', deploy)


def ge_script():
    """The Python program inside _ge_apps in scripts/lib/common.sh."""
    match = re.search(r"<<'PY' \|\| true\n(.*?)\nPY\n", read("scripts/lib/common.sh"), re.S)
    return match.group(1)


def http_error(url, code, status):
    body = json.dumps({"error": {"status": status, "message": status.lower()}}).encode()
    return urllib.error.HTTPError(url, code, status, {}, io.BytesIO(body))


def run_ge(mode, routes, prompts="false", dry="0", locations="global,us,eu"):
    """Runs the program with urlopen answered by routes[(method, url substring)]. Returns (stdout, stderr, calls)."""
    calls = []

    def fake_urlopen(req, timeout=None):  # pylint: disable=unused-argument
        body = json.loads(req.data) if req.data else None
        calls.append((req.get_method(), req.full_url, body))
        for (method, part), answer in routes.items():
            if method == req.get_method() and part in req.full_url:
                if isinstance(answer, Exception):
                    raise answer
                return io.BytesIO(json.dumps(answer).encode())
        raise http_error(req.full_url, 404, "NOT_FOUND")

    out, err = io.StringIO(), io.StringIO()
    argv = ["-", mode, "proj", locations, prompts, dry]
    with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, {"ACP_ACCESS_TOKEN": "t"}), \
            mock.patch("urllib.request.urlopen", fake_urlopen), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        exec(compile(ge_script(), "ge_apps", "exec"), {"__name__": "__main__"})  # pylint: disable=exec-used
    return out.getvalue(), err.getvalue(), calls


def engine(name, location, app_type="APP_TYPE_INTRANET", **config):
    return {"name": "projects/proj/locations/%s/collections/default_collection/engines/%s" % (location, name),
            "displayName": name, "appType": app_type, **({"observabilityConfig": config} if config else {})}


GLOBAL_ENGINES = ("GET", "/locations/global/collections/default_collection/engines")
US_ENGINES = ("GET", "/locations/us/collections/default_collection/engines")
EU_ENGINES = ("GET", "/locations/eu/collections/default_collection/engines")


class GeminiEnterpriseAppsTest(unittest.TestCase):
    """_ge_apps: which locations are detected and which apps get usage logs turned on."""

    def test_detect_finds_apps_and_license_only_locations(self):
        routes = {GLOBAL_ENGINES: {"engines": [engine("ge", "global")]},
                  US_ENGINES: {"engines": [engine("site-search", "us", app_type="APP_TYPE_UNSPECIFIED")]},
                  ("GET", "/locations/us/licenseConfigs"): {},
                  EU_ENGINES: {},
                  ("GET", "/locations/eu/licenseConfigs"): {"licenseConfigs": [{"name": "lc", "state": "ACTIVE"}]}}
        out, err, _ = run_ge("detect", routes)
        self.assertEqual(out.strip(), "global,eu")
        self.assertEqual(err, "")

    def test_detect_skips_unreadable_locations_quietly(self):
        routes = {GLOBAL_ENGINES: http_error("u", 403, "PERMISSION_DENIED"),
                  US_ENGINES: {"engines": [engine("ge", "us")]},
                  EU_ENGINES: urllib.error.URLError("offline")}
        out, err, _ = run_ge("detect", routes)
        self.assertEqual(out.strip(), "us")
        self.assertEqual(err, "")

    def test_detect_prints_nothing_without_gemini_enterprise(self):
        out, _, _ = run_ge("detect", {GLOBAL_ENGINES: {}, US_ENGINES: {}, EU_ENGINES: {}})
        self.assertEqual(out.strip(), "")

    def test_enable_patches_only_apps_that_need_it(self):
        routes = {GLOBAL_ENGINES: {"engines": [
                      engine("off", "global"),
                      engine("on", "global", observabilityEnabled=True, sensitiveLoggingEnabled=True),
                      engine("search", "global", app_type="APP_TYPE_UNSPECIFIED")]},
                  ("PATCH", "/engines/off?updateMask=observabilityConfig"): {}}
        out, err, calls = run_ge("enable", routes, locations="global")
        patches = [c for c in calls if c[0] == "PATCH"]
        self.assertEqual(len(patches), 1)
        self.assertIn("/engines/off?updateMask=observabilityConfig", patches[0][1])
        self.assertEqual(patches[0][2], {"observabilityConfig": {"observabilityEnabled": True,
                                                                 "sensitiveLoggingEnabled": False}})
        self.assertIn("off (global): observability turned on", out)
        self.assertIn("on (global): observability already on, prompts logged", out)
        self.assertNotIn("search", out)
        self.assertEqual(err, "")

    def test_enable_turns_on_prompt_logging_only_when_asked(self):
        routes = {GLOBAL_ENGINES: {"engines": [engine("plain", "global", observabilityEnabled=True)]},
                  ("PATCH", "/engines/plain?updateMask=observabilityConfig"): {}}
        _, _, calls = run_ge("enable", routes, locations="global")
        self.assertEqual([c for c in calls if c[0] == "PATCH"], [])
        _, _, calls = run_ge("enable", routes, prompts="true", locations="global")
        patches = [c for c in calls if c[0] == "PATCH"]
        self.assertEqual(patches[0][2]["observabilityConfig"]["sensitiveLoggingEnabled"], True)

    def test_enable_dry_run_changes_nothing(self):
        routes = {GLOBAL_ENGINES: {"engines": [engine("off", "global")]}}
        _, err, calls = run_ge("enable", routes, dry="1", locations="global")
        self.assertEqual([c for c in calls if c[0] == "PATCH"], [])
        self.assertIn("[dry-run] Gemini Enterprise app off (global): turn on observability", err)

    def test_enable_reports_refusals_with_the_console_path(self):
        routes = {GLOBAL_ENGINES: {"engines": [engine("off", "global")]},
                  ("PATCH", "/engines/off"): http_error("u", 403, "PERMISSION_DENIED")}
        _, err, _ = run_ge("enable", routes, locations="global")
        self.assertIn("could not turn on observability (PERMISSION_DENIED", err)
        self.assertIn("Configurations > Observability", err)


class GeminiEnterpriseLocationsWiringTest(unittest.TestCase):
    def test_deploy_detects_locations_when_unset(self):
        deploy = read("scripts/deploy.sh")
        self.assertIn('DISCOVERY_ENGINE_LOCATIONS="${DISCOVERY_ENGINE_LOCATIONS:-}"', deploy)
        self.assertIn('ge_locations "$PROJECT_ID"', deploy)
        self.assertLess(deploy.index('ge_locations "$PROJECT_ID"'), deploy.index("ensure_ge_observability"))
        self.assertRegex(read("deploy.env.example"), r'(?m)^DISCOVERY_ENGINE_LOCATIONS=""')

    def test_one_location_list_for_both_scripts(self):
        self.assertRegex(read("scripts/lib/common.sh"), r'(?m)^GE_LOCATIONS_ALL="global,us,eu"$')
        self.assertIn('GE_LOCATIONS="$GE_LOCATIONS_ALL"', read("scripts/prepare.sh"))

    def test_prompt_logging_on_by_default_and_guest_access_off_by_default(self):
        self.assertRegex(read("deploy.env.example"), r'(?m)^GE_PROMPT_LOGGING="true"')
        self.assertRegex(read("deploy.env.example"), r'(?m)^GUEST_ACCESS="false"')
        self.assertIn('GE_PROMPT_LOGGING="${GE_PROMPT_LOGGING:-true}"', read("scripts/deploy.sh"))
        self.assertIn('GUEST_ACCESS="${GUEST_ACCESS:-false}"', read("scripts/deploy.sh"))
        self.assertIn("LOG_PROMPTS=true", read("scripts/prepare.sh"))
        self.assertIn('ensure_ge_observability() { _ge_apps enable "$1" "$2" "${3:-true}"; }',
                      read("scripts/lib/common.sh"))


class InputValidationHelpersTest(unittest.TestCase):
    """Customer copy-paste defense in scripts/lib/common.sh and scripts/deploy.sh."""

    def _bash(self, body):
        import subprocess
        script = 'set -euo pipefail\nsource "%s"\n%s' % (os.path.join(ROOT, "scripts/lib/common.sh"), body)
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)

    def test_project_id_strips_common_copy_paste_prefixes(self):
        cases = (
            ("project my-demo-project-260520", "my-demo-project-260520"),
            ("gcloud config set project my-demo-project-260520", "my-demo-project-260520"),
            ("--project my-demo-project-260520", "my-demo-project-260520"),
            ("--project=my-demo-project-260520", "my-demo-project-260520"),
            ("projects/my-demo-project-260520", "my-demo-project-260520"),
            ("  my-demo-project-260520  ", "my-demo-project-260520"),
        )
        for raw, expected in cases:
            res = self._bash('p="$(normalize_project_id %r)"; validate_project_id "$p" test; printf "%%s" "$p"' % raw)
            self.assertEqual(res.returncode, 0, msg=(raw, res.stderr))
            self.assertEqual(res.stdout, expected)

    def test_uppercase_project_name_gives_clear_error(self):
        res = self._bash('validate_project_id "My-Upper-Project-Name" deploy.env')
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("contains uppercase letters (looks like a project Name)", res.stderr)

    def test_default_settings_bucket_never_exceeds_63_chars(self):
        pid30 = "a" + "b" * 28 + "9"
        res = self._bash('default_settings_bucket "%s" "ai-control-plane"' % pid30)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "%s-ai-control-plane-settings" % pid30)
        self.assertLessEqual(len(res.stdout), 63)

        long_svc = "a" + "c" * 38 + "z"
        res_long = self._bash('default_settings_bucket "%s" "%s"' % (pid30, long_svc))
        self.assertEqual(res_long.returncode, 0)
        self.assertLessEqual(len(res_long.stdout), 63)
        self.assertTrue(res_long.stdout.endswith("-settings"))

    def test_normalize_iam_member_handles_bare_emails_and_domains(self):
        cases = (
            ("alice@example.org", "user:alice@example.org"),
            ("*@example.org", "domain:example.org"),
            ("example.org", "domain:example.org"),
            ("user:alice@example.org", "user:alice@example.org"),
            ("domain:example.org", "domain:example.org"),
            ("allAuthenticatedUsers", "allAuthenticatedUsers"),
        )
        for raw, expected in cases:
            res = self._bash('normalize_iam_member %r' % raw)
            self.assertEqual(res.returncode, 0)
            self.assertEqual(res.stdout, expected)

    def test_caller_iam_member_formats_users_and_service_accounts(self):
        self.assertEqual(self._bash('caller_iam_member "alice@example.org"').stdout, "user:alice@example.org")
        self.assertEqual(
            self._bash('caller_iam_member "ci-bot@my-demo-project.iam.gserviceaccount.com"').stdout,
            "serviceAccount:ci-bot@my-demo-project.iam.gserviceaccount.com",
        )


def preflight_script():
    match = re.search(r"<<'PYPREFLIGHT'\n(.*?)\nPYPREFLIGHT\n", read("scripts/lib/common.sh"), re.S)
    return match.group(1)


class EnterpriseDeployHardeningTest(unittest.TestCase):
    """Caller IAM preflight, self-bootstrap, dedicated Cloud Build SA, and end-to-end deploy.sh dry-run."""

    def _run_preflight_py(self, handler, mode="deploy", need_build="1", need_sink="1",
                          need_iap="1", need_sched="0", need_audit="0"):
        out = io.StringIO()
        argv = ["-", mode, "demo-proj", need_build, need_sink, need_iap, need_sched, need_audit]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(os.environ, {"ACP_ACCESS_TOKEN": "tok"}), \
                mock.patch("urllib.request.urlopen", handler), \
                contextlib.redirect_stdout(out):
            try:
                exec(compile(preflight_script(), "preflight", "exec"), {"__name__": "__main__"})  # pylint: disable=exec-used
            except SystemExit:
                pass
        return json.loads(out.getvalue().strip())

    def test_preflight_ok_when_all_permissions_granted(self):
        def handler(req, timeout=None):  # pylint: disable=unused-argument
            body = json.loads(req.data)
            return io.BytesIO(json.dumps({"permissions": body["permissions"]}).encode())

        verdict = self._run_preflight_py(handler)
        self.assertEqual(verdict["status"], "ok")
        self.assertEqual(verdict["missing_perms"], [])

    def test_preflight_bootstraps_when_caller_has_iam_admin_without_cloudbuild(self):
        def handler(req, timeout=None):  # pylint: disable=unused-argument
            body = json.loads(req.data)
            granted = [p for p in body["permissions"] if p not in ("cloudbuild.builds.create", "iap.web.setIamPolicy")]
            return io.BytesIO(json.dumps({"permissions": granted}).encode())

        verdict = self._run_preflight_py(handler)
        self.assertEqual(verdict["status"], "bootstrap")
        self.assertIn("cloudbuild.builds.create", verdict["missing_perms"])
        self.assertIn("roles/cloudbuild.builds.editor", verdict["missing_roles"])
        self.assertIn("roles/iap.admin", verdict["missing_roles"])
        self.assertNotIn("roles/resourcemanager.projectIamAdmin", verdict["missing_roles"])

    def test_preflight_denies_editor_without_iam_admin(self):
        def handler(req, timeout=None):  # pylint: disable=unused-argument
            body = json.loads(req.data)
            blocked = {"resourcemanager.projects.setIamPolicy", "cloudbuild.builds.create",
                       "secretmanager.secrets.setIamPolicy", "run.services.setIamPolicy"}
            granted = [p for p in body["permissions"] if p not in blocked]
            return io.BytesIO(json.dumps({"permissions": granted}).encode())

        verdict = self._run_preflight_py(handler)
        self.assertEqual(verdict["status"], "denied")
        self.assertIn("resourcemanager.projects.setIamPolicy", verdict["missing_perms"])
        self.assertIn("cloudbuild.builds.create", verdict["missing_perms"])

    def test_preflight_falls_back_to_individual_probes_on_http_400(self):
        def handler(req, timeout=None):  # pylint: disable=unused-argument
            perms = json.loads(req.data)["permissions"]
            if len(perms) > 1 or perms[0] == "iap.web.setIamPolicy":
                raise http_error(req.full_url, 400, "INVALID_ARGUMENT")
            return io.BytesIO(json.dumps({"permissions": perms}).encode())

        verdict = self._run_preflight_py(handler)
        self.assertEqual(verdict["status"], "ok")

    def test_deploy_never_relies_on_default_compute_sa_or_us_staging(self):
        deploy = read("scripts/deploy.sh")
        common = read("scripts/lib/common.sh")
        teardown = read("scripts/teardown.sh")
        self.assertNotIn("-compute@developer.gserviceaccount.com", deploy)
        self.assertIn("CLOUD_LOGGING_ONLY", deploy)
        self.assertIn("--gcs-source-staging-dir", deploy)
        self.assertIn("docker build --platform linux/amd64", deploy)
        self.assertIn("ensure_service_identities", deploy)
        self.assertIn("--replication-policy=user-managed", common)
        self.assertIn("BUILD_SA=", teardown)

    def test_deploy_dry_run_executes_cleanly_end_to_end(self):
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = os.path.join(tmp, "bin")
            os.makedirs(bin_dir)
            gcloud_stub = os.path.join(bin_dir, "gcloud")
            with open(gcloud_stub, "w", encoding="utf-8") as fh:
                fh.write("""#!/usr/bin/env bash
case "$*" in
  *"config get-value account"*) echo "admin@example.org" ;;
  *"projects describe"*) echo "123456789012" ;;
  *"auth print-access-token"*) echo "" ;;
  *"projects get-iam-policy"*) echo '{"bindings":[]}' ;;
  *"run deploy --help"*) echo "--iap --no-iap --no-invoker-iam-check" ;;
  *"describe"*) exit 1 ;;
  *) exit 0 ;;
esac
""")
            os.chmod(gcloud_stub, 0o755)
            bq_stub = os.path.join(bin_dir, "bq")
            with open(bq_stub, "w", encoding="utf-8") as fh:
                fh.write("#!/usr/bin/env bash\nexit 1\n")
            os.chmod(bq_stub, 0o755)

            for access_mode in ("google", "password", "iap"):
                env_file = os.path.join(tmp, f"deploy-{access_mode}.env")
                with open(env_file, "w", encoding="utf-8") as fh:
                    fh.write(
                        'PROJECT_ID="project demo-project-260520"\n'
                        'REGION="asia-northeast3"\n'
                        'ADMIN_EMAILS="admin@example.org"\n'
                        'ALLOWED_USERS="viewer@example.org"\n'
                        'IAP_MEMBERS="example.org"\n'
                        f'ACCESS_MODE="{access_mode}"\n'
                    )
                env = os.environ.copy()
                env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
                res = subprocess.run(
                    ["bash", os.path.join(ROOT, "scripts/deploy.sh"), "--config", env_file, "--dry-run"],
                    capture_output=True,
                    text=True,
                    env=env,
                    check=False,
                )
                self.assertEqual(res.returncode, 0, msg=(access_mode, res.stdout, res.stderr))
                self.assertIn("Deployment complete:", res.stdout)
                if access_mode == "iap":
                    self.assertIn("maintenance schedule: IAP blocks Cloud Scheduler calls", res.stdout)
                else:
                    self.assertIn("[dry-run] scheduler job ai-control-plane-maintenance: */15 * * * * -> ",
                                  res.stdout)
                    self.assertIn("/api/v1/system/maintenance", res.stdout)
            # Invalid scheduling values stop the deployment before anything changes.
            for bad in ('PRICING_SYNC_TIME="25:00"', 'MAINTENANCE_SCHEDULE="every hour"', 'PRICING_CURRENCY="won!"'):
                env_file = os.path.join(tmp, "deploy-bad.env")
                with open(env_file, "w", encoding="utf-8") as fh:
                    fh.write('PROJECT_ID="demo-project-260520"\nREGION="asia-northeast3"\nADMIN_EMAILS="admin@example.org"\n'
                             'ACCESS_MODE="google"\n%s\n' % bad)
                res = subprocess.run(["bash", os.path.join(ROOT, "scripts/deploy.sh"), "--config", env_file, "--dry-run"],
                                     capture_output=True, text=True, env=env, check=False)
                self.assertNotEqual(res.returncode, 0, msg=(bad, res.stdout))

    def test_zero_config_cli_and_multi_project_install_dry_run(self):
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = os.path.join(tmp, "bin")
            os.makedirs(bin_dir)
            gcloud_stub = os.path.join(bin_dir, "gcloud")
            with open(gcloud_stub, "w", encoding="utf-8") as fh:
                fh.write("""#!/usr/bin/env bash
case "$*" in
  *"config get-value account"*) echo "owner@example.org" ;;
  *"config get-value project"*) echo "demo-active-proj" ;;
  *"projects describe"*) echo "123456789012" ;;
  *"auth print-access-token"*) echo "" ;;
  *"projects get-iam-policy"*) echo '{"bindings":[]}' ;;
  *"run deploy --help"*) echo "--iap --no-iap --no-invoker-iam-check" ;;
  *"describe"*) exit 1 ;;
  *) exit 0 ;;
esac
""")
            os.chmod(gcloud_stub, 0o755)
            bq_stub = os.path.join(bin_dir, "bq")
            with open(bq_stub, "w", encoding="utf-8") as fh:
                fh.write("#!/usr/bin/env bash\nexit 1\n")
            os.chmod(bq_stub, 0o755)

            env = os.environ.copy()
            env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")

            # 1) Zero-config deploy.sh using active gcloud project & account (no deploy.env, no flags except --dry-run)
            empty_cfg = os.path.join(tmp, "nonexistent.env")
            res_zero = subprocess.run(
                ["bash", os.path.join(ROOT, "scripts/deploy.sh"), "--project", "demo-zero-proj", "--dry-run"],
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(res_zero.returncode, 0, msg=(res_zero.stdout, res_zero.stderr, empty_cfg))
            self.assertIn("ADMIN_EMAILS defaulted to active gcloud account: owner@example.org", res_zero.stdout)
            self.assertIn("modelarmor.googleapis.com", res_zero.stderr)
            self.assertIn("dlp.googleapis.com", res_zero.stderr)
            self.assertIn("roles/logging.privateLogViewer", res_zero.stderr)
            self.assertIn("roles/logging.configWriter", res_zero.stderr)
            self.assertIn("roles/resourcemanager.projectIamAdmin", res_zero.stderr)
            self.assertIn("Data Access audit logs would be enabled across all AI services", res_zero.stdout)

            # 2) Alias compatibility (INITIAL_ADMINS / INITIAL_VIEWERS)
            alias_env_file = os.path.join(tmp, "alias.env")
            with open(alias_env_file, "w", encoding="utf-8") as fh:
                fh.write(
                    'PROJECT_ID="demo-alias-proj"\n'
                    'INITIAL_ADMINS="lead@example.org"\n'
                    'INITIAL_VIEWERS="member@example.org"\n'
                )
            res_alias = subprocess.run(
                ["bash", os.path.join(ROOT, "scripts/deploy.sh"), "--config", alias_env_file, "--dry-run"],
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(res_alias.returncode, 0, msg=(res_alias.stdout, res_alias.stderr))
            self.assertIn("lead@example.org", res_alias.stdout)
            self.assertIn("member@example.org", res_alias.stdout)

            # 3) Multi-project one-click install.sh (--projects proj-a,proj-b)
            res_multi = subprocess.run(
                [
                    "bash", os.path.join(ROOT, "scripts/install.sh"),
                    "--projects", "demo-proj-alpha,demo-proj-beta",
                    "--admins", "lead@example.org",
                    "--viewers", "member@example.org",
                    "--dry-run",
                ],
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(res_multi.returncode, 0, msg=(res_multi.stdout, res_multi.stderr))
            self.assertIn("Deployment complete: demo-proj-alpha", res_multi.stdout)
            self.assertIn("Deployment complete: demo-proj-beta", res_multi.stdout)
            self.assertIn("All 2 project deployment(s) completed", res_multi.stdout)

    def test_enable_data_access_logs_skips_unsupported_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = os.path.join(tmp, "policy.json")
            with open(policy_path, "w", encoding="utf-8") as fh:
                fh.write('{"bindings": [], "etag": "Bw=="}\n')

            fake_gcloud = os.path.join(tmp, "gcloud")
            with open(fake_gcloud, "w", encoding="utf-8") as fh:
                fh.write(
                    "#!/usr/bin/env python3\n"
                    "import json, shutil, sys\n"
                    "args = sys.argv[1:]\n"
                    "if args[:2] == ['projects', 'get-iam-policy']:\n"
                    f"    print(open({policy_path!r}).read())\n"
                    "    sys.exit(0)\n"
                    "if args[:2] == ['projects', 'set-iam-policy']:\n"
                    "    cand = json.load(open(args[3]))\n"
                    "    svcs = {c.get('service') for c in cand.get('auditConfigs', [])}\n"
                    "    for forbidden in ('notebooklm.googleapis.com', 'generativelanguage.googleapis.com', 'geminidataanalytics.googleapis.com'):\n"
                    "        if forbidden in svcs:\n"
                    "            sys.stderr.write(f'ERROR: INVALID_ARGUMENT: Service {forbidden} does not exist or does not support service level configuration\\n')\n"
                    "            sys.exit(1)\n"
                    f"    shutil.copyfile(args[3], {policy_path!r})\n"
                    "    sys.exit(0)\n"
                    "sys.exit(0)\n"
                )
            os.chmod(fake_gcloud, 0o755)
            env = dict(os.environ, PATH=f"{tmp}:{os.environ.get('PATH', '')}")
            res = subprocess.run(
                [
                    "bash", "-c",
                    f"source {os.path.join(ROOT, 'scripts/lib/common.sh')}; DRY_RUN=0; enable_data_access_logs demo-audit-proj",
                ],
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
            self.assertEqual(res.stderr, "")
            self.assertIn("Data Access audit logs enabled across all AI services", res.stdout)
            with open(policy_path, encoding="utf-8") as fh:
                saved = fh.read()
            self.assertIn("aiplatform.googleapis.com", saved)
            self.assertIn("discoveryengine.googleapis.com", saved)
            self.assertIn("modelarmor.googleapis.com", saved)
            self.assertIn("businessaicode.googleapis.com", saved)
            self.assertNotIn("notebooklm.googleapis.com", saved)
            self.assertNotIn("generativelanguage.googleapis.com", saved)

    def _make_fake_gcloud_env(self, tmp):
        bin_dir = os.path.join(tmp, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        gcloud_stub = os.path.join(bin_dir, "gcloud")
        with open(gcloud_stub, "w", encoding="utf-8") as fh:
            fh.write("""#!/usr/bin/env bash
case "$*" in
  *"config get-value account"*) echo "owner@example.org" ;;
  *"config get-value project"*) echo "demo-active-proj" ;;
  *"projects describe"*) echo "123456789012" ;;
  *"auth print-access-token"*) echo "" ;;
  *"projects get-iam-policy"*) echo '{"bindings":[]}' ;;
  *"run deploy --help"*) echo "--iap --no-iap --no-invoker-iam-check" ;;
  *"describe"*) exit 1 ;;
  *) exit 0 ;;
esac
""")
        os.chmod(gcloud_stub, 0o755)
        bq_stub = os.path.join(bin_dir, "bq")
        with open(bq_stub, "w", encoding="utf-8") as fh:
            fh.write("#!/usr/bin/env bash\nexit 1\n")
        os.chmod(bq_stub, 0o755)
        return bin_dir

    def test_unicode_cli_normalization_and_equals_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._make_fake_gcloud_env(tmp)
            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")
            res = subprocess.run(
                [
                    "bash", os.path.join(ROOT, "scripts/install.sh"),
                    "\u2014projects=\u201cdemo-proj-alpha,demo-proj-beta\u201d",
                    "\u2013admins", "admin@example.org",
                    "\u2212\u2212viewers=viewer@example.org",
                    "--dry-run",
                ],
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
            self.assertIn("Deployment complete: demo-proj-alpha", res.stdout)
            self.assertIn("Deployment complete: demo-proj-beta", res.stdout)

    def test_install_sh_heals_stale_dirty_clone_and_broken_deploy_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._make_fake_gcloud_env(tmp)
            # Create a local "upstream" git repo containing the current scripts so install.sh can clone/fetch offline.
            upstream = os.path.join(tmp, "upstream")
            os.makedirs(os.path.join(upstream, "scripts", "lib"), exist_ok=True)
            for rel in ("scripts/install.sh", "scripts/deploy.sh", "scripts/prepare.sh", "scripts/lib/common.sh"):
                src = os.path.join(ROOT, rel)
                dst = os.path.join(upstream, rel)
                with open(src, "rb") as sf, open(dst, "wb") as df:
                    df.write(sf.read())
                os.chmod(dst, 0o755)
            git_env = dict(
                os.environ,
                GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.org",
                GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.org",
            )
            subprocess.run(["git", "init", "-b", "main", upstream], check=True, capture_output=True, env=git_env)
            subprocess.run(["git", "-C", upstream, "add", "."], check=True, capture_output=True, env=git_env)
            subprocess.run(["git", "-C", upstream, "commit", "-m", "v2"], check=True, capture_output=True, env=git_env)

            # Simulate customer's existing stale & dirty ~/ai-control-plane with a syntax-broken deploy.env + index.lock
            install_dir = os.path.join(tmp, "home", "ai-control-plane")
            os.makedirs(os.path.dirname(install_dir), exist_ok=True)
            subprocess.run(["git", "clone", "--depth", "1", upstream, install_dir], check=True, capture_output=True, env=git_env)
            with open(os.path.join(install_dir, "scripts", "deploy.sh"), "w", encoding="utf-8") as fh:
                fh.write("#!/usr/bin/env bash\necho 'OLD BROKEN DEPLOY'; exit 99\n")
            with open(os.path.join(install_dir, "deploy.env"), "w", encoding="utf-8") as fh:
                fh.write("PROJECT_ID=((BROKEN SYNTAX\n")
            with open(os.path.join(install_dir, ".git", "index.lock"), "w", encoding="utf-8") as fh:
                fh.write("stale lock")

            # Also copy install.sh outside the repo to mimic `bash <(curl ...)` where LOCAL_ROOT is empty
            standalone_installer = os.path.join(tmp, "streamed_install.sh")
            with open(os.path.join(ROOT, "scripts/install.sh"), "rb") as sf, open(standalone_installer, "wb") as df:
                df.write(sf.read())
            os.chmod(standalone_installer, 0o755)

            env = dict(
                os.environ,
                HOME=os.path.join(tmp, "home"),
                ACP_REPO_URL=upstream,
                ACP_INSTALL_DIR=install_dir,
                PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
            )
            res = subprocess.run(
                ["bash", standalone_installer, "--project", "demo-healed-proj", "--admins", "admin@example.org", "--dry-run"],
                cwd=install_dir,
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
            self.assertIn("Updating AI Control Plane", res.stdout)
            self.assertIn("Deployment complete: demo-healed-proj", res.stdout)
            self.assertFalse(os.path.exists(os.path.join(install_dir, "deploy.env")))
            self.assertTrue(os.path.exists(os.path.join(install_dir, "deploy.env.bak")))

    def test_zero_billing_permissions_and_msp_masked_account_succeed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_gcloud = os.path.join(tmp, "gcloud")
            with open(fake_gcloud, "w", encoding="utf-8") as fh:
                fh.write(
                    "#!/usr/bin/env python3\n"
                    "import os, sys\n"
                    "args = sys.argv[1:]\n"
                    "mode = os.environ.get('FAKE_BILLING_MODE', 'denied')\n"
                    "if args[:3] == ['billing', 'projects', 'describe']:\n"
                    "    if mode == 'denied':\n"
                    "        sys.stderr.write('ERROR: PERMISSION_DENIED: Cloud Billing permission denied\\n')\n"
                    "        sys.exit(1)\n"
                    "    if mode == 'empty_json':\n"
                    "        print('{}')\n"
                    "        sys.exit(0)\n"
                    "    if mode == 'msp_masked':\n"
                    "        print('{\"billingAccountName\": \"billingAccounts/reseller-hidden\", \"billingEnabled\": true}')\n"
                    "        sys.exit(0)\n"
                    "    if mode == 'false_but_run_ok':\n"
                    "        print('{\"billingEnabled\": false}')\n"
                    "        sys.exit(0)\n"
                    "    if mode == 'truly_unbilled':\n"
                    "        print('{\"billingEnabled\": false}')\n"
                    "        sys.exit(0)\n"
                    "if args[:3] == ['billing', 'accounts', 'list']:\n"
                    "    sys.stderr.write('ERROR: PERMISSION_DENIED\\n')\n"
                    "    sys.exit(1)\n"
                    "if args[:2] == ['services', 'enable']:\n"
                    "    if 'cloudbilling.googleapis.com' in args:\n"
                    "        sys.stderr.write('ERROR: PERMISSION_DENIED: cloudbilling blocked\\n')\n"
                    "        sys.exit(1)\n"
                    "    if mode == 'truly_unbilled' and 'run.googleapis.com' in args:\n"
                    "        sys.stderr.write('ERROR: Billing must be enabled for activation of service run.googleapis.com\\n')\n"
                    "        sys.exit(1)\n"
                    "    sys.exit(0)\n"
                    "sys.exit(0)\n"
                )
            os.chmod(fake_gcloud, 0o755)
            common_sh = os.path.join(ROOT, "scripts/lib/common.sh")

            for mode in ("denied", "empty_json", "msp_masked", "false_but_run_ok"):
                env = dict(os.environ, PATH=f"{tmp}:{os.environ.get('PATH', '')}", FAKE_BILLING_MODE=mode)
                res = subprocess.run(
                    ["bash", "-c", f"source {common_sh}; DRY_RUN=0; ensure_project_billing demo-proj"],
                    capture_output=True, text=True, env=env, check=False,
                )
                self.assertEqual(res.returncode, 0, msg=(mode, res.stdout, res.stderr))
                # Reseller/masked billing account names must never leak invalid IDs to BILLING_ACCOUNT_ID.
                self.assertEqual(res.stdout.strip(), "", msg=(mode, res.stdout))

            # Restricted cloudbilling.googleapis.com must NOT fail enable_project_apis
            env_api = dict(os.environ, PATH=f"{tmp}:{os.environ.get('PATH', '')}", FAKE_BILLING_MODE="denied")
            res_api = subprocess.run(
                ["bash", "-c", f"source {common_sh}; DRY_RUN=0; enable_project_apis demo-proj 1 0 0"],
                capture_output=True, text=True, env=env_api, check=False,
            )
            self.assertEqual(res_api.returncode, 0, msg=(res_api.stdout, res_api.stderr))

            # Truly unbilled project with no billing linked at all gives clear Korean + English instructions
            env_unbilled = dict(os.environ, PATH=f"{tmp}:{os.environ.get('PATH', '')}", FAKE_BILLING_MODE="truly_unbilled")
            res_unbilled = subprocess.run(
                ["bash", "-c", f"source {common_sh}; DRY_RUN=0; ensure_project_billing demo-unbilled-proj"],
                capture_output=True, text=True, env=env_unbilled, check=False,
            )
            self.assertNotEqual(res_unbilled.returncode, 0)
            self.assertIn("Cloud Billing is not enabled on project demo-unbilled-proj", res_unbilled.stderr)
            self.assertIn("결제 계정(Billing Account)", res_unbilled.stderr)

    def test_preflight_denied_shows_clear_remediation_without_partial_resources(self):
        common_sh = os.path.join(ROOT, "scripts/lib/common.sh")
        cmd = (
            f"source {common_sh}; "
            "_check_project_permissions() { "
            'echo \'{"status":"denied","missing_perms":["resourcemanager.projects.setIamPolicy","run.services.create"],'
            '"missing_roles":["roles/resourcemanager.projectIamAdmin","roles/run.admin"]}\'; '
            "}; "
            "preflight_caller_permissions deploy demo-enterprise-proj viewer@example.org 1 1 1 0 1"
        )
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("roles/owner", res.stderr)
        self.assertIn("roles/resourcemanager.projectIamAdmin", res.stderr)
        self.assertIn("user:viewer@example.org", res.stderr)
        self.assertIn("[한국어 안내]", res.stderr)


class MultiProjectHubAndTeardownTest(unittest.TestCase):
    """End-to-end tests for --link-projects hub mode, interactive wizard, prepare-only, and teardown.sh."""

    def _make_stub_bin(self, tmp, log_file=None, projects_csv=None):
        bin_dir = os.path.join(tmp, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        gcloud_stub = os.path.join(bin_dir, "gcloud")
        projects_out = projects_csv or "alpha-hub\tAlpha Hub\nbeta-spoke\tBeta Spoke\ngamma-spoke\tGamma Spoke"
        log_stmt = f'echo "$*" >> "{log_file}"\n' if log_file else ""
        with open(gcloud_stub, "w", encoding="utf-8") as fh:
            fh.write(
                "#!/usr/bin/env bash\n"
                f"{log_stmt}"
                'case "$*" in\n'
                '  *"config get-value account"*) echo "owner@example.org" ;;\n'
                '  *"config get-value project"*) echo "alpha-hub" ;;\n'
                f'  *"projects list"*) printf "%b\\n" "{projects_out}" ;;\n'
                '  *"projects describe"*) echo "123456789012" ;;\n'
                '  *"auth print-access-token"*) echo "" ;;\n'
                '  *"projects get-iam-policy"*"value(bindings.role)"*) echo "roles/bigquery.jobUser" ;;\n'
                '  *"projects get-iam-policy"*) echo \'{"bindings":[]}\' ;;\n'
                '  *"logging sinks describe"*) exit 0 ;;\n'
                '  *"run deploy --help"*) echo "--iap --no-iap --no-invoker-iam-check" ;;\n'
                '  *"describe"*) exit 1 ;;\n'
                '  *) exit 0 ;;\n'
                'esac\n'
            )
        os.chmod(gcloud_stub, 0o755)
        bq_stub = os.path.join(bin_dir, "bq")
        with open(bq_stub, "w", encoding="utf-8") as fh:
            fh.write("#!/usr/bin/env bash\nexit 1\n")
        os.chmod(bq_stub, 0o755)
        return bin_dir

    def _copy_scripts_workspace(self, tmp):
        ws = os.path.join(tmp, "ws")
        os.makedirs(os.path.join(ws, "scripts", "lib"), exist_ok=True)
        for rel in (
            "scripts/install.sh",
            "scripts/deploy.sh",
            "scripts/prepare.sh",
            "scripts/teardown.sh",
            "scripts/lib/common.sh",
        ):
            src = os.path.join(ROOT, rel)
            dst = os.path.join(ws, rel)
            with open(src, "rb") as sf, open(dst, "wb") as df:
                df.write(sf.read())
            os.chmod(dst, 0o755)
        return ws

    def test_unified_hub_link_projects_persists_env_and_reloads_on_redeploy(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._make_stub_bin(tmp)
            ws = self._copy_scripts_workspace(tmp)
            env = dict(
                os.environ,
                PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
                ACP_SAVE_ENV_ON_DRY_RUN="1",
            )
            # Pass host project duplicated inside --link-projects + messy projects/ prefix to verify deduplication & normalization
            res = subprocess.run(
                [
                    "bash", os.path.join(ws, "scripts/install.sh"),
                    "--project", "demo-hub",
                    "--link-projects", "projects/demo-spoke-a, demo-spoke-b, demo-hub, demo-spoke-a",
                    "--admins", "admin@example.org",
                    "--dry-run",
                ],
                cwd=ws,
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
            self.assertIn("Linked scan   : demo-spoke-a,demo-spoke-b", res.stdout)
            self.assertIn("[dry-run] grant multi-project read roles on demo-spoke-a", res.stdout)
            self.assertIn("[dry-run] grant multi-project read roles on demo-spoke-b", res.stdout)
            self.assertIn("[dry-run] linked project demo-spoke-a: best-effort APIs, demo-spoke-a:ge_analytics", res.stdout)
            self.assertIn("[dry-run] linked project demo-spoke-b: best-effort APIs, demo-spoke-b:ge_analytics", res.stdout)
            # Gemini Enterprise seats of linked projects need the license reader custom role there too.
            for spoke in ("demo-spoke-a", "demo-spoke-b"):
                self.assertIn("[dry-run] custom role projects/%s/roles/CustomAiControlPlaneLicenseReader (" % spoke,
                              res.stdout)
            self.assertNotIn("No Gemini Enterprise app or seats found", res.stderr)
            self.assertIn("SCAN_PROJECTS=demo-spoke-a,demo-spoke-b", res.stderr)

            saved_env_path = os.path.join(ws, "deploy.demo-hub.env")
            self.assertTrue(os.path.exists(saved_env_path))
            with open(saved_env_path, encoding="utf-8") as fh:
                saved_env = fh.read()
            self.assertIn('SCAN_PROJECTS="demo-spoke-a,demo-spoke-b"', saved_env)

            # Re-running deploy.sh --project demo-hub WITHOUT --link-projects must preserve SCAN_PROJECTS from deploy.demo-hub.env!
            res_redeploy = subprocess.run(
                [
                    "bash", os.path.join(ws, "scripts/deploy.sh"),
                    "--project", "demo-hub",
                    "--no-auto-discover-projects",
                    "--dry-run",
                ],
                cwd=ws,
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(res_redeploy.returncode, 0, msg=(res_redeploy.stdout, res_redeploy.stderr))
            self.assertIn("Linked scan   : demo-spoke-a,demo-spoke-b", res_redeploy.stdout)
            self.assertIn("SCAN_ALL_PROJECTS=false", res_redeploy.stderr)

    def test_interactive_wizard_resolves_menu_numbers_for_host_and_linked_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._make_stub_bin(tmp)
            ws = self._copy_scripts_workspace(tmp)
            env = dict(
                os.environ,
                PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
                ACP_FORCE_WIZARD="1",
            )
            wizard_input = "1\n2,3\nasia-northeast3\nadmin@example.org\nviewer@example.org\n"
            res = subprocess.run(
                ["bash", os.path.join(ws, "scripts/install.sh"), "--dry-run"],
                cwd=ws,
                input=wizard_input,
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
            self.assertIn("Deployment complete: alpha-hub", res.stdout)
            self.assertIn("Linked scan   : beta-spoke,gamma-spoke", res.stdout)

    def test_prepare_only_with_link_projects_prepares_hub_and_spokes(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._make_stub_bin(tmp)
            ws = self._copy_scripts_workspace(tmp)
            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")
            res = subprocess.run(
                [
                    "bash", os.path.join(ws, "scripts/install.sh"),
                    "--prepare-only",
                    "--project", "demo-hub",
                    "--link-projects", "demo-spoke-a,demo-spoke-b",
                    "--dry-run",
                ],
                cwd=ws,
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
            self.assertIn("Preparing demo-hub for AI Control Plane", res.stdout)
            self.assertIn("Preparing demo-spoke-a for AI Control Plane", res.stdout)
            self.assertIn("Preparing demo-spoke-b for AI Control Plane", res.stdout)
            self.assertIn("grant the sink writer BigQuery Data Editor on demo-spoke-b:ge_analytics", res.stdout)

    def test_teardown_dry_run_and_live_stub_clean_pricing_scheduler_and_linked_iam(self):
        with tempfile.TemporaryDirectory() as tmp:
            gcloud_log = os.path.join(tmp, "gcloud_calls.log")
            bin_dir = self._make_stub_bin(tmp, log_file=gcloud_log)
            ws = self._copy_scripts_workspace(tmp)
            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")

            # 1) Dry-run with Unicode dashes/quotes, --link-projects, AND an unrelated deploy.env present in $ROOT:
            #    must NOT leak another project's SETTINGS_BUCKET or REGION, and must NOT mutate anything.
            with open(os.path.join(ws, "deploy.env"), "w", encoding="utf-8") as fh:
                fh.write(
                    'PROJECT_ID="unrelated-other-proj"\n'
                    'REGION="europe-west1"\n'
                    'SETTINGS_BUCKET="unrelated-other-proj-bucket"\n'
                )
            res_dry = subprocess.run(
                [
                    "bash", os.path.join(ws, "scripts/teardown.sh"),
                    "\u2014project=\u201cdemo-hub\u201d",
                    "\u2013link-projects=demo-spoke-a,demo-spoke-b",
                    "--delete-data",
                    "--dry-run",
                ],
                cwd=ws,
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(res_dry.returncode, 0, msg=(res_dry.stdout, res_dry.stderr))
            self.assertIn("project demo-hub (asia-northeast3)", res_dry.stdout)
            self.assertNotIn("unrelated-other-proj-bucket", res_dry.stdout)
            self.assertIn("gs://demo-hub-ai-control-plane-settings", res_dry.stdout)
            self.assertIn("[dry-run] scheduler jobs: ai-control-plane-enforce, ai-control-plane-maintenance, "
                          "ai-control-plane-pricing-sync", res_dry.stdout)
            self.assertIn("[dry-run] telemetry route sinks", res_dry.stdout)
            self.assertIn("[dry-run] IAM bindings on demo-hub and linked projects (demo-spoke-a,demo-spoke-b)", res_dry.stdout)
            if os.path.exists(gcloud_log):
                with open(gcloud_log, encoding="utf-8") as fh:
                    self.assertNotIn("delete", fh.read())

            # 2) Non-dry-run loading SCAN_PROJECTS automatically from deploy.demo-hub.env
            with open(os.path.join(ws, "deploy.demo-hub.env"), "w", encoding="utf-8") as fh:
                fh.write(
                    'PROJECT_ID="demo-hub"\n'
                    'REGION="asia-northeast3"\n'
                    'SCAN_PROJECTS="demo-spoke-a,demo-spoke-b"\n'
                )
            res_live = subprocess.run(
                [
                    "bash", os.path.join(ws, "scripts/teardown.sh"),
                    "--project", "demo-hub",
                    "--delete-data",
                    "--yes",
                ],
                cwd=ws,
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(res_live.returncode, 0, msg=(res_live.stdout, res_live.stderr))
            with open(gcloud_log, encoding="utf-8") as fh:
                calls = fh.read()
            self.assertIn("scheduler jobs delete ai-control-plane-pricing-sync", calls)
            self.assertIn("scheduler jobs delete ai-control-plane-maintenance", calls)
            # Managed telemetry route sinks are found by description on the hub and every linked project.
            self.assertIn('logging sinks list --project demo-hub --filter=description:"AI Control Plane telemetry route"',
                          calls)
            self.assertIn("logging sinks list --project demo-spoke-a", calls)
            self.assertIn("logging sinks list --project demo-spoke-b", calls)
            self.assertIn("scheduler jobs delete ai-control-plane-enforce", calls)
            self.assertIn("projects remove-iam-policy-binding demo-spoke-a", calls)
            self.assertIn("projects remove-iam-policy-binding demo-spoke-b", calls)
            self.assertIn("storage rm --recursive --all-versions gs://demo-hub-ai-control-plane-settings", calls)
            self.assertIn("logging sinks delete ai-control-plane-ai-logs", calls)

    def test_process_substitution_help_works_over_non_seekable_pipe(self):
        install_sh = os.path.join(ROOT, "scripts/install.sh")
        res = subprocess.run(
            ["bash", "-c", f'bash <(cat "{install_sh}") --help'],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        self.assertIn("Unified Multi-Project Hub", res.stdout)


LICENSE_ROLE_ID = "CustomAiControlPlaneLicenseReader"
HUB_SA = "ai-control-plane-sa@demo-hub-proj.iam.gserviceaccount.com"
# Answers the IAM calls of grant_license_reader; FAKE_ROLE picks the scenario and every call is logged.
FAKE_IAM_GCLOUD = """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GCLOUD_LOG"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(args) + "\\n")
mode = os.environ.get("FAKE_ROLE", "missing")
if args[:3] == ["iam", "roles", "undelete"]:
    sys.exit(1)
if args[:3] == ["iam", "roles", "describe"]:
    sys.exit(0 if mode in ("exists", "bind_denied") else 1)
if args[:3] == ["iam", "roles", "create"]:
    sys.exit(1 if mode == "create_denied" else 0)
if args[:2] == ["projects", "add-iam-policy-binding"]:
    sys.exit(1 if mode == "bind_denied" else 0)
sys.exit(0)
"""


def license_perms():
    return re.search(r'(?m)^LICENSE_READER_PERMS="([^"]+)"$', read("scripts/deploy.sh")).group(1)


def read_log(path):
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def write_stub_bin(tmp, log_file):
    """gcloud/bq stubs for deploy.sh and teardown.sh: no network, every gcloud call appended to log_file."""
    bin_dir = os.path.join(tmp, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    with open(os.path.join(bin_dir, "gcloud"), "w", encoding="utf-8") as fh:
        fh.write("#!/usr/bin/env bash\n"
                 'echo "$*" >> "%s"\n'
                 'case "$*" in\n'
                 '  *"config get-value account"*) echo "owner@example.org" ;;\n'
                 '  *"config get-value project"*) echo "demo-hub-proj" ;;\n'
                 '  *"projects describe"*) echo "123456789012" ;;\n'
                 '  *"auth print-access-token"*) echo "" ;;\n'
                 '  *"projects get-iam-policy demo-spoke-a"*"value(bindings.role)"*)\n'
                 '    printf "roles/bigquery.dataViewer\\nprojects/demo-spoke-a/roles/%s\\n" ;;\n'
                 '  *"projects get-iam-policy"*"value(bindings.role)"*) echo "roles/bigquery.jobUser" ;;\n'
                 "  *\"projects get-iam-policy\"*) echo '{\"bindings\":[]}' ;;\n"
                 '  *"run deploy --help"*) echo "--iap --no-iap --no-invoker-iam-check" ;;\n'
                 '  *"describe"*) exit 1 ;;\n'
                 '  *) exit 0 ;;\n'
                 'esac\n' % (log_file, LICENSE_ROLE_ID))
    with open(os.path.join(bin_dir, "bq"), "w", encoding="utf-8") as fh:
        fh.write("#!/usr/bin/env bash\nexit 1\n")
    for name in ("gcloud", "bq"):
        os.chmod(os.path.join(bin_dir, name), 0o755)
    return bin_dir


class LinkedProjectLicenseReaderTest(unittest.TestCase):
    """roles/discoveryengine.viewer cannot list license configs or user licenses, so every linked project needs the
    read-only license reader custom role, or its Gemini Enterprise seats are silently hidden."""

    def run_grant(self, mode, dry="0"):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "calls.log")
            stub = os.path.join(tmp, "gcloud")
            with open(stub, "w", encoding="utf-8") as fh:
                fh.write(FAKE_IAM_GCLOUD)
            os.chmod(stub, 0o755)
            env = dict(os.environ, PATH=f"{tmp}:{os.environ.get('PATH', '')}", FAKE_ROLE=mode, FAKE_GCLOUD_LOG=log)
            cmd = 'source "%s"; DRY_RUN=%s; grant_license_reader demo-ge-proj %s %s %s' % (
                os.path.join(ROOT, "scripts/lib/common.sh"), dry, HUB_SA, LICENSE_ROLE_ID, license_perms())
            res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env, check=False)
            calls = read_log(log).splitlines()
        return res, calls

    def bind_call(self):
        return ("projects add-iam-policy-binding demo-ge-proj --member serviceAccount:%s --role "
                "projects/demo-ge-proj/roles/%s --condition=None --quiet" % (HUB_SA, LICENSE_ROLE_ID))

    def test_permissions_cover_seats_and_license_configs(self):
        perms = license_perms().split(",")
        for perm in ("discoveryengine.licenseConfigs.list", "discoveryengine.userStores.listUserLicenses"):
            self.assertIn(perm, perms)

    def test_missing_role_is_created_ga_and_bound(self):
        res, calls = self.run_grant("missing")
        self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        self.assertEqual(res.stderr, "")
        self.assertEqual(calls[0], "iam roles undelete %s --project demo-ge-proj --quiet" % LICENSE_ROLE_ID)
        create = [c for c in calls if c.startswith("iam roles create")]
        self.assertEqual(len(create), 1)
        self.assertIn("--project demo-ge-proj", create[0])
        self.assertIn("--permissions %s --stage GA" % license_perms(), create[0])
        self.assertEqual(calls[-1], self.bind_call())
        self.assertIn("granted projects/demo-ge-proj/roles/%s" % LICENSE_ROLE_ID, res.stdout)

    def test_existing_role_is_updated_then_bound(self):
        res, calls = self.run_grant("exists")
        self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        self.assertIn("iam roles update %s --project demo-ge-proj --permissions %s --quiet"
                      % (LICENSE_ROLE_ID, license_perms()), calls)
        self.assertFalse([c for c in calls if c.startswith("iam roles create")])
        self.assertEqual(calls[-1], self.bind_call())

    def test_refusals_warn_with_the_exact_manual_commands(self):
        res, calls = self.run_grant("create_denied")
        self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        self.assertFalse([c for c in calls if c.startswith("projects add-iam-policy-binding")])
        self.assertIn('gcloud iam roles create %s --project demo-ge-proj --title "AI Control Plane License Reader" '
                      '--permissions %s --stage GA' % (LICENSE_ROLE_ID, license_perms()), res.stderr)
        bind_cmd = ("gcloud projects add-iam-policy-binding demo-ge-proj --member serviceAccount:%s "
                    "--role projects/demo-ge-proj/roles/%s --condition=None" % (HUB_SA, LICENSE_ROLE_ID))
        self.assertIn(bind_cmd, res.stderr)
        res, _ = self.run_grant("bind_denied")
        self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        self.assertIn(bind_cmd, res.stderr)
        self.assertNotIn("gcloud iam roles create", res.stderr)

    def test_dry_run_changes_nothing(self):
        res, calls = self.run_grant("missing", dry="1")
        self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        self.assertEqual(calls, [])
        self.assertIn("[dry-run] custom role projects/demo-ge-proj/roles/%s (%s) -> %s"
                      % (LICENSE_ROLE_ID, license_perms(), HUB_SA), res.stdout)

    def test_deploy_grants_it_on_every_linked_project(self):
        deploy = read("scripts/deploy.sh")
        call = 'grant_license_reader "$lp" "$SA_EMAIL" "$LICENSE_READER_ROLE_ID" "$LICENSE_READER_PERMS"'
        self.assertIn(call, deploy)
        block = deploy[deploy.index("# Linked secondary projects"):deploy.index('NORMALIZED_SCAN_PROJECTS="$(IFS=')]
        self.assertIn(call, block)
        self.assertLess(deploy.index("LICENSE_READER_PERMS="), deploy.index(call))

    def test_teardown_removes_the_binding_and_lists_the_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "calls.log")
            env = dict(os.environ, PATH=f"{write_stub_bin(tmp, log)}:{os.environ.get('PATH', '')}")
            res = subprocess.run(["bash", os.path.join(ROOT, "scripts/teardown.sh"), "--project", "demo-hub-proj",
                                  "--link-projects", "demo-spoke-a,demo-spoke-b,demo-spoke-a", "--yes"],
                                 capture_output=True, text=True, env=env, check=False)
            self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
            calls = read_log(log)
        self.assertIn("projects remove-iam-policy-binding demo-spoke-a --member serviceAccount:%s --role "
                      "projects/demo-spoke-a/roles/%s --condition=None" % (HUB_SA, LICENSE_ROLE_ID), calls)
        self.assertEqual(calls.count("projects get-iam-policy demo-spoke-a"), 1)  # duplicates are skipped
        for project in ("demo-hub-proj", "demo-spoke-a", "demo-spoke-b"):
            self.assertIn("gcloud iam roles delete %s --project %s" % (LICENSE_ROLE_ID, project), res.stdout)


class DeploySummaryAndConsentTest(unittest.TestCase):
    """The final summary warns when the hub has no Gemini Enterprise and nothing is linked (the customer case: the
    dashboard in a new project, Gemini Enterprise in an existing one), and the prompt logging opt-out works."""

    def deploy(self, *args, config=None):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "calls.log")
            env = dict(os.environ, PATH=f"{write_stub_bin(tmp, log)}:{os.environ.get('PATH', '')}")
            cmd = ["bash", os.path.join(ROOT, "scripts/deploy.sh")]
            if config:
                path = os.path.join(tmp, "deploy.env")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write('PROJECT_ID="demo-hub-proj"\nADMIN_EMAILS="admin@example.org"\n' + config)
                cmd += ["--config", path]
            res = subprocess.run(cmd + list(args) + ["--dry-run"], capture_output=True, text=True, env=env,
                                 check=False)
        self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        self.assertIn("Deployment complete: demo-hub-proj", res.stdout)
        return res

    def test_hub_without_gemini_enterprise_warns_in_both_languages(self):
        res = self.deploy("--project", "demo-hub-proj")
        self.assertIn("No Gemini Enterprise app or seats found in demo-hub-proj. If Gemini Enterprise runs in another "
                      "project, install into that project (recommended: governance and marker roles act on the "
                      "primary project's IAM) or re-run with --link-projects <GE project>.", res.stderr)
        self.assertIn("[한국어 안내] demo-hub-proj에서 Gemini Enterprise 앱이나 라이선스 좌석을 찾지 못했습니다", res.stderr)

    def test_no_warning_with_linked_projects_configured_locations_or_module_off(self):
        self.assertNotIn("No Gemini Enterprise app", self.deploy("--project", "demo-hub-proj",
                                                                 "--link-projects", "demo-ge-proj").stderr)
        # Configured locations are not looked up again in a dry run (no extra network call), so no warning.
        self.assertNotIn("No Gemini Enterprise app", self.deploy(config='DISCOVERY_ENGINE_LOCATIONS="global"\n').stderr)
        self.assertNotIn("No Gemini Enterprise app", self.deploy(config='DISABLED_MODULES="gemini_enterprise"\n').stderr)

    def test_summary_lookup_is_skipped_in_dry_runs(self):
        deploy = read("scripts/deploy.sh")
        tail = deploy[deploy.index('log "Deployment complete: ${PROJECT_ID}"'):]
        self.assertIn('if [[ -z "$HUB_GE_FOUND" && "$DRY_RUN" != "1" ]]; then', tail)
        self.assertEqual(tail.count("ge_locations"), 1)

    def test_no_log_prompts_turns_prompt_logging_off(self):
        self.assertIn("GE Prompt Logging=true", self.deploy("--project", "demo-hub-proj").stdout)
        res = self.deploy("--project", "demo-hub-proj", "--no-log-prompts")
        self.assertIn("GE Prompt Logging=false", res.stdout)
        self.assertIn("--no-log-prompts", subprocess.run(["bash", os.path.join(ROOT, "scripts/deploy.sh"), "--help"],
                                                         capture_output=True, text=True, check=False).stdout)

    def test_install_saves_the_opt_outs_for_later_deploys(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "calls.log")
            ws = os.path.join(tmp, "ws")  # install.sh writes deploy.<project>.env next to the scripts
            for rel in ("scripts/install.sh", "scripts/deploy.sh", "scripts/lib/common.sh"):
                os.makedirs(os.path.dirname(os.path.join(ws, rel)), exist_ok=True)
                with open(os.path.join(ROOT, rel), "rb") as src, open(os.path.join(ws, rel), "wb") as dst:
                    dst.write(src.read())
                os.chmod(os.path.join(ws, rel), 0o755)
            env = dict(os.environ, PATH=f"{write_stub_bin(tmp, log)}:{os.environ.get('PATH', '')}",
                       ACP_SAVE_ENV_ON_DRY_RUN="1")
            res = subprocess.run(["bash", os.path.join(ws, "scripts/install.sh"), "--project", "demo-hub-proj",
                                  "--admins", "admin@example.org", "--no-log-prompts", "--no-data-access-logs",
                                  "--read-only", "--dry-run"],
                                 cwd=ws, capture_output=True, text=True, env=env, check=False)
            self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
            self.assertIn("GE Prompt Logging=false", res.stdout)
            with open(os.path.join(ws, "deploy.demo-hub-proj.env"), encoding="utf-8") as fh:
                saved = fh.read()
        for line in ('GE_PROMPT_LOGGING="false"', 'ENABLE_DATA_ACCESS_LOGS="false"', 'ENABLE_GOVERNANCE_ACTIONS="false"',
                     'ENABLE_GE_OBSERVABILITY="true"'):
            self.assertIn(line, saved)


ENFORCER_SA = "ai-control-plane-enforcer@demo-hub-proj.iam.gserviceaccount.com"
SCHEDULER_SA = "ai-control-plane-scheduler@demo-hub-proj.iam.gserviceaccount.com"
ENFORCER_JOB_NAME = "projects/demo-hub-proj/locations/asia-northeast3/jobs/ai-control-plane-enforcer"
# Answers the calls of grant_org_deny_roles; FAKE_ORG picks the scenario and every call is logged.
FAKE_ORG_GCLOUD = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GCLOUD_LOG"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(args) + "\\n")
mode = os.environ.get("FAKE_ORG", "org")
if args[:2] == ["projects", "get-ancestors"]:
    if mode == "unreadable":
        sys.exit(1)
    chain = [{"id": "demo-hub-proj", "type": "project"}]
    if mode != "none":
        chain += [{"id": "4455", "type": "folder"}, {"id": "998877665544", "type": "organization"}]
    print(json.dumps(chain))
    sys.exit(0)
if args[:2] == ["organizations", "add-iam-policy-binding"]:
    sys.exit(1 if mode == "refused" else 0)
sys.exit(0)
"""


def lines_with(text, needle):
    return [line for line in text.splitlines() if needle in line]


def copy_scripts(ws, *rels):
    for rel in rels:
        os.makedirs(os.path.dirname(os.path.join(ws, rel)), exist_ok=True)
        with open(os.path.join(ROOT, rel), "rb") as src, open(os.path.join(ws, rel), "wb") as dst:
            dst.write(src.read())
        os.chmod(os.path.join(ws, rel), 0o755)


class DenyPolicyEnforcementDeployTest(unittest.TestCase):
    """ENFORCEMENT_BACKEND=iam_deny (README 8.9): a Cloud Run job applies suspensions with an IAM deny policy on the
    enforcement schedule as its own service account, the web endpoint's scheduler is not created (no double
    enforcement), and the organization roles are granted only with GRANT_ORG_ROLES=true. The default backend deploys
    exactly what it did before."""

    def deploy(self, *args, config="", expect_ok=True):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "calls.log")
            env = dict(os.environ, PATH=f"{write_stub_bin(tmp, log)}:{os.environ.get('PATH', '')}")
            path = os.path.join(tmp, "deploy.env")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('PROJECT_ID="demo-hub-proj"\nADMIN_EMAILS="admin@example.org"\n' + config)
            res = subprocess.run(["bash", os.path.join(ROOT, "scripts/deploy.sh"), "--config", path, *args, "--dry-run"],
                                 capture_output=True, text=True, env=env, check=False)
        if expect_ok:
            self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
            self.assertIn("Deployment complete: demo-hub-proj", res.stdout)
        return res

    def teardown(self, config, *args):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "calls.log")
            env = dict(os.environ, PATH=f"{write_stub_bin(tmp, log)}:{os.environ.get('PATH', '')}")
            path = os.path.join(tmp, "deploy.env")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('PROJECT_ID="demo-hub-proj"\nSCAN_PROJECTS="demo-spoke-a"\n' + config)
            res = subprocess.run(["bash", os.path.join(ROOT, "scripts/teardown.sh"), "--config", path, *args],
                                 capture_output=True, text=True, env=env, check=False)
            calls = read_log(log).splitlines()
        self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        return res, calls

    def run_org_roles(self, mode, grant, dry="0"):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "calls.log")
            stub = os.path.join(tmp, "gcloud")
            with open(stub, "w", encoding="utf-8") as fh:
                fh.write(FAKE_ORG_GCLOUD)
            os.chmod(stub, 0o755)
            env = dict(os.environ, PATH=f"{tmp}:{os.environ.get('PATH', '')}", FAKE_ORG=mode, FAKE_GCLOUD_LOG=log)
            cmd = ('source "%s"; DRY_RUN=%s; grant_org_deny_roles demo-hub-proj %s %s %s; '
                   'echo "commands=${#ORG_DENY_ROLE_COMMANDS[@]}"') % (
                os.path.join(ROOT, "scripts/lib/common.sh"), dry, ENFORCER_SA, HUB_SA, grant)
            res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env, check=False)
            calls = read_log(log).splitlines()
        self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        return res, calls

    @staticmethod
    def org_commands(org):
        return ["gcloud organizations add-iam-policy-binding %s --member=serviceAccount:%s --role=roles/iam.denyAdmin "
                "--condition=None" % (org, ENFORCER_SA),
                "gcloud organizations add-iam-policy-binding %s --member=serviceAccount:%s --role=roles/iam.denyReviewer "
                "--condition=None" % (org, HUB_SA)]

    def test_default_backend_deploys_no_job_and_keeps_the_web_scheduler(self):
        res = self.deploy()
        for text in (res.stdout, res.stderr):
            self.assertNotIn("ai-control-plane-enforcer", text)
            self.assertNotIn("run jobs ", text)
            self.assertNotIn("organization roles", text)
        self.assertIn("scheduler jobs create http ai-control-plane-enforce --project demo-hub-proj", res.stderr)
        self.assertIn("##SCHEDULER_INVOKERS=%s" % SCHEDULER_SA, res.stderr)
        self.assertNotIn("ENFORCEMENT_JOB=", res.stderr)

    def test_iam_deny_runs_the_job_on_the_schedule_instead_of_the_web_endpoint(self):
        res = self.deploy("--link-projects", "demo-spoke-a", config='ENFORCEMENT_BACKEND="iam_deny"\n')
        err = res.stderr
        self.assertIn("gcloud iam service-accounts create ai-control-plane-enforcer --project demo-hub-proj", err)
        job = lines_with(err, "run jobs deploy ai-control-plane-enforcer")
        self.assertEqual(len(job), 1, err)
        for part in ("--region asia-northeast3", "--service-account %s" % ENFORCER_SA,
                     "--command python --args=-m,controlplane.enforce_job", "--tasks 1 --max-retries 0 --task-timeout 600s",
                     "##SETTINGS_BUCKET=demo-hub-proj-ai-control-plane-settings", "##ENFORCEMENT_BACKEND=iam_deny",
                     "##ENFORCEMENT_JOB=" + ENFORCER_JOB_NAME, "##PYTHONPATH=/app/src"):
            self.assertIn(part, job[0])
        self.assertNotIn("--set-secrets", job[0])  # it serves no requests and signs nobody in
        sched = lines_with(err, "scheduler jobs create http ai-control-plane-enforcer ")
        self.assertEqual(len(sched), 1, err)
        for part in ("--location asia-northeast3", "--schedule */5 * * * *",
                     "--uri https://run.googleapis.com/v2/%s:run --http-method POST" % ENFORCER_JOB_NAME,
                     "--oauth-service-account-email %s" % SCHEDULER_SA):
            self.assertIn(part, sched[0])
        # No double enforcement: the web endpoint's scheduler and its invoker allow-list are not set up.
        self.assertNotIn("scheduler jobs create http ai-control-plane-enforce ", err)
        self.assertNotIn("--oidc-service-account-email", err)
        self.assertNotIn("SCHEDULER_INVOKERS", err)
        # Only the dashboard ("Run now", status card) and the scheduler may run the job, on this job only.
        for member, role in ((HUB_SA, "roles/run.jobsExecutor"), (HUB_SA, "roles/run.viewer"),
                             (SCHEDULER_SA, "roles/run.jobsExecutor")):
            self.assertIn("run jobs add-iam-policy-binding ai-control-plane-enforcer --project demo-hub-proj --region "
                          "asia-northeast3 --member serviceAccount:%s --role %s" % (member, role), err)
        # `gcloud run jobs add-iam-policy-binding` has no --condition flag; passing one stops the deployment (seen live).
        job_bindings = lines_with(err, "run jobs add-iam-policy-binding")
        self.assertEqual(len(job_bindings), 3, err)
        for line in job_bindings:
            self.assertNotIn("--condition", line)
        service = lines_with(err, "gcloud run deploy ai-control-plane ")
        self.assertTrue(service)
        for line in service:
            self.assertIn("##ENFORCEMENT_BACKEND=iam_deny", line)
            self.assertIn("##ENFORCEMENT_JOB=" + ENFORCER_JOB_NAME, line)
        # The job's account gets read roles and audit writes only; Deny Admin comes from the organization.
        roles = set(re.findall(r"projects add-iam-policy-binding demo-hub-proj --member serviceAccount:%s --role (\S+)"
                               % re.escape(ENFORCER_SA), err))
        self.assertEqual(roles, {"roles/bigquery.dataViewer", "roles/bigquery.jobUser", "roles/discoveryengine.viewer",
                                 "roles/logging.viewer", "roles/logging.privateLogViewer", "roles/logging.logWriter",
                                 "roles/serviceusage.serviceUsageConsumer"})
        self.assertIn("--member serviceAccount:%s --role roles/storage.objectAdmin" % ENFORCER_SA, err)
        self.assertIn("[dry-run] grant enforcement job read roles on demo-spoke-a -> %s" % ENFORCER_SA, res.stdout)
        self.assertIn("[dry-run] organization roles not granted (GRANT_ORG_ROLES=false)", res.stdout)
        self.assertIn("Antigravity caps: deny-policy enforcement by the Cloud Run job ai-control-plane-enforcer "
                      "(asia-northeast3) */5 * * * *", res.stdout)
        self.assertTrue(config.ENFORCEMENT_JOB_RE.match(ENFORCER_JOB_NAME))

    def test_job_region_attachment_and_org_grants_from_the_command_line(self):
        res = self.deploy("--enforcement-backend", "iam_deny", "--enforcement-job-region", "us-central1",
                          "--deny-attachment", "scope", "--grant-org-roles")
        job_name = "projects/demo-hub-proj/locations/us-central1/jobs/ai-control-plane-enforcer"
        job = lines_with(res.stderr, "run jobs deploy ai-control-plane-enforcer")[0]
        self.assertIn("--region us-central1", job)
        self.assertIn("##DENY_ATTACHMENT=scope", job)
        sched = lines_with(res.stderr, "scheduler jobs create http ai-control-plane-enforcer ")[0]
        self.assertIn("--location us-central1", sched)
        self.assertIn("--uri https://run.googleapis.com/v2/%s:run" % job_name, sched)
        service = lines_with(res.stderr, "gcloud run deploy ai-control-plane ")[0]
        self.assertIn("--region asia-northeast3", service)  # the dashboard stays in REGION
        self.assertIn("##ENFORCEMENT_JOB=" + job_name, service)
        self.assertIn("[dry-run] organization roles on the organization of demo-hub-proj: roles/iam.denyAdmin -> %s, "
                      "roles/iam.denyReviewer -> %s" % (ENFORCER_SA, HUB_SA), res.stdout)

    def test_iam_deny_keeps_its_schedule_in_iap_and_read_only_installs(self):
        # The scheduler calls the Cloud Run Admin API, not the dashboard: IAP never applies and the dashboard's account
        # needs no write role.
        res = self.deploy("--read-only", config='ACCESS_MODE="iap"\nIAP_MEMBERS="example.org"\n'
                                                'ENFORCEMENT_BACKEND="iam_deny"\n')
        self.assertTrue(lines_with(res.stderr, "scheduler jobs create http ai-control-plane-enforcer "))
        self.assertNotIn("no Antigravity enforcement schedule", res.stderr)
        res = self.deploy("--no-enforcement-schedule", config='ENFORCEMENT_BACKEND="iam_deny"\n')
        self.assertTrue(lines_with(res.stderr, "run jobs deploy ai-control-plane-enforcer"))
        self.assertFalse(lines_with(res.stderr, "scheduler jobs create http ai-control-plane-enforce"))
        self.assertNotIn("--member serviceAccount:%s --role roles/run.jobsExecutor" % SCHEDULER_SA, res.stderr)
        self.assertIn("Run enforcement now runs ai-control-plane-enforcer", res.stdout)

    def test_notify_only_needs_no_job_no_org_role_and_no_write_role(self):
        res = self.deploy("--read-only", config='ENFORCEMENT_BACKEND="notify_only"\n')
        for text in (res.stdout, res.stderr):
            self.assertNotIn("ai-control-plane-enforcer", text)
            self.assertNotIn("roles/resourcemanager.projectIamAdmin", text)
            self.assertNotIn("organization roles", text)
        self.assertIn("scheduler jobs create http ai-control-plane-enforce --project demo-hub-proj", res.stderr)
        self.assertIn("##ENFORCEMENT_BACKEND=notify_only", res.stderr)
        self.assertIn("Antigravity caps: notify only (plans and notifications; access never changes)", res.stdout)

    def test_invalid_enforcement_settings_stop_the_deployment(self):
        for bad, message in (('ENFORCEMENT_BACKEND="deny"', "ENFORCEMENT_BACKEND must be iam_allow_swap, iam_deny or "
                                                            "notify_only"),
                             ('DENY_ATTACHMENT="folder"', "DENY_ATTACHMENT must be projects or scope"),
                             ('GRANT_ORG_ROLES="yes"', "GRANT_ORG_ROLES must be true or false"),
                             ('ENFORCEMENT_JOB_REGION="Seoul"', "invalid ENFORCEMENT_JOB_REGION")):
            res = self.deploy(config=bad + "\n", expect_ok=False)
            self.assertNotEqual(res.returncode, 0, msg=bad)
            self.assertIn(message, res.stderr)

    def test_org_roles_are_granted_only_with_grant_org_roles(self):
        res, calls = self.run_org_roles("org", "true")
        self.assertEqual([c for c in calls if c.startswith("organizations ")], [
            "organizations add-iam-policy-binding 998877665544 --member=serviceAccount:%s --role=roles/iam.denyAdmin "
            "--condition=None --quiet" % ENFORCER_SA,
            "organizations add-iam-policy-binding 998877665544 --member=serviceAccount:%s --role=roles/iam.denyReviewer "
            "--condition=None --quiet" % HUB_SA])
        self.assertIn("granted roles/iam.denyAdmin to serviceAccount:%s on organization 998877665544" % ENFORCER_SA,
                      res.stdout)
        self.assertIn("commands=0", res.stdout)
        self.assertEqual(res.stderr, "")
        res, calls = self.run_org_roles("org", "false")
        self.assertFalse([c for c in calls if c.startswith("organizations ")])
        self.assertIn("(GRANT_ORG_ROLES=false)", res.stderr)
        for cmd in self.org_commands("998877665544"):
            self.assertIn(cmd, res.stderr)
        self.assertIn("commands=2", res.stdout)

    def test_refused_or_unreadable_organization_prints_the_commands(self):
        res, calls = self.run_org_roles("refused", "true")
        self.assertEqual(len([c for c in calls if c.startswith("organizations add-iam-policy-binding")]), 2)
        self.assertIn("could not grant the organization roles", res.stderr)
        for cmd in self.org_commands("998877665544"):
            self.assertIn(cmd, res.stderr)
        res, calls = self.run_org_roles("unreadable", "true")
        self.assertFalse([c for c in calls if c.startswith("organizations ")])
        for cmd in self.org_commands("ORGANIZATION_ID"):
            self.assertIn(cmd, res.stderr)
        self.assertIn("gcloud projects get-ancestors demo-hub-proj", res.stderr)

    def test_project_without_organization_cannot_use_deny_policies(self):
        res, calls = self.run_org_roles("none", "true")
        self.assertFalse([c for c in calls if c.startswith("organizations ")])
        self.assertIn("demo-hub-proj has no organization", res.stderr)
        self.assertIn("(ENFORCEMENT_BACKEND=iam_deny) is unavailable here", res.stderr)
        self.assertIn("[한국어 안내] demo-hub-proj 프로젝트에는 조직이 없습니다", res.stderr)
        self.assertIn("commands=0", res.stdout)

    def test_org_roles_dry_run_changes_nothing(self):
        res, calls = self.run_org_roles("org", "true", dry="1")
        self.assertEqual(calls, [])
        self.assertIn("[dry-run] organization roles on the organization of demo-hub-proj: roles/iam.denyAdmin -> %s"
                      % ENFORCER_SA, res.stdout)

    def test_install_forwards_and_saves_the_enforcement_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "calls.log")
            ws = os.path.join(tmp, "ws")  # install.sh writes deploy.<project>.env next to the scripts
            copy_scripts(ws, "scripts/install.sh", "scripts/deploy.sh", "scripts/lib/common.sh")
            env = dict(os.environ, PATH=f"{write_stub_bin(tmp, log)}:{os.environ.get('PATH', '')}",
                       ACP_SAVE_ENV_ON_DRY_RUN="1")
            base = ["bash", os.path.join(ws, "scripts/install.sh"), "--project", "demo-hub-proj",
                    "--admins", "admin@example.org"]
            res = subprocess.run(base + ["--enforcement-backend", "iam_deny", "--deny-attachment", "scope",
                                         "--enforcement-job-region", "us-central1", "--grant-org-roles", "--dry-run"],
                                 cwd=ws, capture_output=True, text=True, env=env, check=False)
            self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
            self.assertTrue(lines_with(res.stderr, "run jobs deploy ai-control-plane-enforcer"))
            with open(os.path.join(ws, "deploy.demo-hub-proj.env"), encoding="utf-8") as fh:
                saved = fh.read()
            # Values outside the allowed choices stop the installer before anything is saved or deployed.
            bad = subprocess.run(base + ["--enforcement-backend", "deny", "--dry-run"],
                                 cwd=ws, capture_output=True, text=True, env=env, check=False)
        for line in ('ENFORCEMENT_BACKEND="iam_deny"', 'DENY_ATTACHMENT="scope"', 'ENFORCEMENT_JOB_REGION="us-central1"',
                     'GRANT_ORG_ROLES="true"'):
            self.assertIn(line, saved)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("--enforcement-backend must be one of: iam_allow_swap iam_deny notify_only", bad.stderr)

    def test_teardown_stops_the_job_before_deleting_the_deny_policy(self):
        res, calls = self.teardown('ENFORCEMENT_BACKEND="iam_deny"\nENFORCEMENT_JOB_REGION="us-central1"\n', "--yes")

        def first(prefix):
            return next(i for i, call in enumerate(calls) if call.startswith(prefix))

        stop_schedule = first("scheduler jobs delete ai-control-plane-enforcer --project demo-hub-proj --location "
                              "us-central1")
        stop_job = first("run jobs delete ai-control-plane-enforcer --project demo-hub-proj --region us-central1")
        for project in ("demo-hub-proj", "demo-spoke-a"):
            delete = first("iam policies delete ai-control-plane-caps --attachment-point=cloudresourcemanager."
                           "googleapis.com/projects/%s --kind=denypolicies" % project)
            self.assertLess(stop_schedule, delete)
            self.assertLess(stop_job, delete)
        self.assertIn("iam service-accounts delete %s --project demo-hub-proj --quiet" % ENFORCER_SA, calls)
        self.assertTrue([c for c in calls if c.startswith("projects get-iam-policy demo-spoke-a") and ENFORCER_SA in c])
        # The organization cannot be read here, so the commands for an organization administrator are printed.
        self.assertIn("gcloud organizations remove-iam-policy-binding ORGANIZATION_ID --member=serviceAccount:%s "
                      "--role=roles/iam.denyAdmin --condition=None" % ENFORCER_SA, res.stdout)

    def test_teardown_leaves_deny_policies_alone_without_the_job(self):
        res, calls = self.teardown("", "--yes")
        self.assertFalse([c for c in calls if c.startswith("iam policies delete")])
        self.assertFalse([c for c in calls if c.startswith("organizations ")])
        self.assertIn("Cloud Run job ai-control-plane-enforcer", res.stdout)
        res, calls = self.teardown('ENFORCEMENT_BACKEND="iam_deny"\n', "--dry-run")
        self.assertFalse([c for c in calls if "delete" in c])
        self.assertIn("[dry-run] managed deny policy ai-control-plane-caps on demo-hub-proj and linked projects",
                      res.stdout)
        self.assertIn("ai-control-plane-enforcer (asia-northeast3)", res.stdout)

    def test_settings_are_documented_and_match_the_application(self):
        example = read("deploy.env.example")
        for line in ('ENFORCEMENT_BACKEND="iam_allow_swap"', 'DENY_ATTACHMENT="projects"', 'ENFORCEMENT_JOB_REGION=""',
                     'GRANT_ORG_ROLES="false"'):
            self.assertRegex(example, r"(?m)^%s" % re.escape(line))
        usage = subprocess.run(["bash", os.path.join(ROOT, "scripts/deploy.sh"), "--help"],
                               capture_output=True, text=True, check=False).stdout
        for flag in ("--enforcement-backend", "--deny-attachment", "--enforcement-job-region", "--grant-org-roles"):
            self.assertIn(flag, usage)
        self.assertIn("--enforcement-backend iam_deny", subprocess.run(
            ["bash", os.path.join(ROOT, "scripts/install.sh"), "--help"], capture_output=True, text=True,
            check=False).stdout)
        # The variables the scripts and Terraform set are the ones the application reads, with the same choices.
        terraform = read("terraform/main.tf")
        for env_name in ("ENFORCEMENT_BACKEND", "DENY_ATTACHMENT", "ENFORCEMENT_JOB"):
            self.assertIn(env_name, config.ENV_MAP)
            self.assertIn("%s " % env_name, terraform)
        deploy = read("scripts/deploy.sh")
        self.assertIn("iam_allow_swap|iam_deny|notify_only) ;;", deploy)
        self.assertIn("projects|scope) ;;", deploy)
        backend = config.VALIDATORS["governance.antigravity.enforcement_backend"]
        attachment = config.VALIDATORS["governance.antigravity.deny_attachment"]
        for value in ("iam_allow_swap", "iam_deny", "notify_only"):
            self.assertEqual(backend(value), value)
        for value in ("projects", "scope"):
            self.assertEqual(attachment(value), value)
        with self.assertRaises(config.Invalid):
            backend("deny")

    def test_readmes_document_the_settings_and_the_rollback_command(self):
        self.assertIn('gcloud iam policies delete "$DENY_POLICY_ID" --attachment-point="$point" --kind=denypolicies',
                      read("scripts/teardown.sh"))
        for readme, heading in (("README.md", "### 8.9 거부 정책 집행과 집행 작업"),
                                ("README.en.md", "### 8.9 Deny-policy enforcement and the enforcement job")):
            text = read(readme)
            self.assertIn(heading, text, readme)
            start = text.index(heading)
            section = text[start:text.index("\n## 9. ", start)]
            self.assertIn("gcloud iam policies delete ai-control-plane-caps \\\n  --attachment-point=cloudresourcemanager."
                          "googleapis.com/projects/PROJECT_ID --kind=denypolicies", section, readme)
            for key in ("ENFORCEMENT_BACKEND", "DENY_ATTACHMENT", "ENFORCEMENT_JOB_REGION", "GRANT_ORG_ROLES",
                        "roles/iam.denyAdmin", "roles/iam.denyReviewer", "roles/run.jobsExecutor", "2,500"):
                self.assertIn(key, section, (readme, key))


class RealtimeEnforcementScriptTest(unittest.TestCase):
    def _deploy_dry_run(self, extra_args=(), env_lines=""):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "calls.log")
            cfg = os.path.join(tmp, "deploy.env")
            with open(cfg, "w", encoding="utf-8") as fh:
                fh.write('PROJECT_ID="demo-hub-proj"\nREGION="asia-northeast3"\nADMIN_EMAILS="admin@example.org"\n'
                         'ACCESS_MODE="google"\nCREATE_LOG_SINK="false"\n' + env_lines)
            env = dict(os.environ, PATH=f"{write_stub_bin(tmp, log)}:{os.environ.get('PATH', '')}")
            res = subprocess.run(
                ["bash", os.path.join(ROOT, "scripts/deploy.sh"), "--config", cfg, "--dry-run", *extra_args],
                cwd=ROOT, capture_output=True, text=True, env=env, check=False,
            )
            return res

    def test_deploy_enables_realtime_by_default_even_without_bq_sink(self):
        res = self._deploy_dry_run()
        self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        self.assertIn("USAGE_EVENTS_SUBSCRIPTION=projects/demo-hub-proj/subscriptions/ai-control-plane-agy-usage-push",
                      res.stderr)
        self.assertIn("Pub/Sub topic ai-control-plane-agy-usage + log sink ai-control-plane-agy-usage", res.stdout)
        self.assertIn("Real-time caps:", res.stdout)

    def test_deploy_no_realtime_flag_and_iap_disable_push(self):
        res_off = self._deploy_dry_run(extra_args=("--no-realtime-enforcement",))
        self.assertEqual(res_off.returncode, 0, msg=(res_off.stdout, res_off.stderr))
        self.assertNotIn("USAGE_EVENTS_SUBSCRIPTION=", res_off.stderr)
        self.assertNotIn("Pub/Sub topic ai-control-plane-agy-usage", res_off.stdout)

        res_iap = self._deploy_dry_run(env_lines='ACCESS_MODE="iap"\nIAP_MEMBERS="user:admin@example.org"\n')
        self.assertEqual(res_iap.returncode, 0, msg=(res_iap.stdout, res_iap.stderr))
        self.assertNotIn("USAGE_EVENTS_SUBSCRIPTION=", res_iap.stderr)

    def test_teardown_dry_run_includes_realtime_pubsub_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "calls.log")
            cfg = os.path.join(tmp, "deploy.env")
            with open(cfg, "w", encoding="utf-8") as fh:
                fh.write('PROJECT_ID="demo-hub-proj"\nREGION="asia-northeast3"\n')
            env = dict(os.environ, PATH=f"{write_stub_bin(tmp, log)}:{os.environ.get('PATH', '')}")
            res = subprocess.run(
                ["bash", os.path.join(ROOT, "scripts/teardown.sh"), "--config", cfg, "--dry-run"],
                cwd=ROOT, capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
            self.assertIn("ai-control-plane-agy-usage-push", res.stdout)
            self.assertIn("ai-control-plane-agy-usage", res.stdout)

    def test_realtime_setting_documented_and_wired_in_terraform(self):
        self.assertRegex(read("deploy.env.example"), r'(?m)^REALTIME_ENFORCEMENT="true"')
        tf = read("terraform/main.tf")
        self.assertIn("USAGE_EVENTS_SUBSCRIPTION", tf)
        self.assertIn('resource "google_pubsub_subscription" "usage_events_push"', tf)
        self.assertIn('resource "google_logging_project_sink" "usage_events"', tf)
        common = read("scripts/lib/common.sh")
        self.assertNotRegex(common, r"gcloud pubsub topics add-iam-policy-binding[^\n]*\\\n[^\n]*--condition")


# Fake gcloud for the deploy, install and teardown runs below: every call is logged; FAKE_EXISTING lists resource names
# whose `describe` succeeds; FAKE_POLICY / FAKE_ROLES answer get-iam-policy (JSON / value(bindings.role));
# FAKE_SETTINGS is the dashboard's settings object (gcloud storage cat); FAKE_FAIL lists call prefixes that fail and
# FAKE_FAIL_RE is a regular expression for calls that fail. FAKE_SAY maps regular expressions to the output of matching
# calls (printed before a FAKE_FAIL exit, so a failing call can explain itself). The Cloud Build config of every
# `builds submit` is logged on a CONFIG line (newlines as " | ").
FAKE_GCLOUD = """#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
line = " ".join(args)
with open(os.environ["FAKE_GCLOUD_LOG"], "a", encoding="utf-8") as fh:
    fh.write(line + "\\n")
    if args[:2] == ["builds", "submit"] and "--config" in args:
        with open(args[args.index("--config") + 1], encoding="utf-8") as cfg:
            fh.write("CONFIG " + cfg.read().replace("\\n", " | ") + "\\n")
if "--data-file=-" in args:
    sys.stdin.read()  # as gcloud does: the writer of the pipe must never see a closed pipe
existing = [s for s in os.environ.get("FAKE_EXISTING", "").split(",") if s]
said = False
for pattern, text in json.loads(os.environ.get("FAKE_SAY", "{}")).items():
    if re.search(pattern, line):
        print(text)
        said = True
        break
if any(line.startswith(p) for p in os.environ.get("FAKE_FAIL", "").split("|") if p):
    sys.exit(1)
if os.environ.get("FAKE_FAIL_RE") and re.search(os.environ["FAKE_FAIL_RE"], line):
    sys.exit(1)
if said:
    sys.exit(0)
if "config get-value account" in line:
    print("owner@example.org")
elif "config get-value project" in line:
    print("demo-hub-proj")
elif "projects describe" in line:
    print("123456789012")
elif "auth print-access-token" in line:
    print("")
elif "projects get-iam-policy" in line and "value(bindings.role)" in line:
    print(os.environ.get("FAKE_ROLES", "roles/bigquery.jobUser"))
elif "projects get-iam-policy" in line:
    print(os.environ.get("FAKE_POLICY", '{"bindings": []}'))
elif "run deploy --help" in line:
    print("--iap --no-iap --no-invoker-iam-check")
elif "storage cat" in line:
    print(os.environ.get("FAKE_SETTINGS", ""))
elif "describe" in args:
    sys.exit(0 if any(name in args for name in existing) else 1)
sys.exit(0)
"""
# Fake bq: logs every call and shows FAKE_DATASET for `bq show`; anything else succeeds (or fails without a dataset).
FAKE_BQ = """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GCLOUD_LOG"], "a", encoding="utf-8") as fh:
    fh.write("bq " + " ".join(args) + "\\n")
dataset = os.environ.get("FAKE_DATASET", "")
if "show" in args:
    if not dataset:
        sys.exit(1)
    print(dataset)
sys.exit(0)
"""
SCRIPT_FILES = ("scripts/install.sh", "scripts/deploy.sh", "scripts/prepare.sh", "scripts/teardown.sh",
                "scripts/lib/common.sh")


def fake_env(tmp, **extra):
    """PATH with the fake gcloud and bq, and a docker that is not running (no local build fallback); returns (env,
    log path)."""
    bin_dir = os.path.join(tmp, "fakebin")
    os.makedirs(bin_dir, exist_ok=True)
    for name, body in (("gcloud", FAKE_GCLOUD), ("bq", FAKE_BQ), ("docker", "#!/bin/sh\nexit 1\n")):
        with open(os.path.join(bin_dir, name), "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(os.path.join(bin_dir, name), 0o755)
    log = os.path.join(tmp, "calls.log")
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}", FAKE_GCLOUD_LOG=log)
    env.update(extra)
    return env, log


def run_script(script, args, env, cwd=None, stdin=None):
    return subprocess.run(["bash", script, *args], cwd=cwd, input=stdin, capture_output=True, text=True, env=env,
                          check=False)


def parser_flags(rel):
    """Every option of the script's `while [[ $# -gt 0 ]]` argument parser (case labels such as --a|--b)."""
    text = read(rel)
    start = text.index("while [[ $# -gt 0 ]]; do")
    block = text[start:text.index("\ndone\n", start)]
    flags = set()
    for label in re.findall(r"(?m)^\s+((?:-{1,2}[a-z][a-z0-9-]*\|?)+)\)", block):
        flags.update(f for f in label.split("|") if f)
    return flags


def documented(flag, text):
    """True when the help text names the flag, also in the --[no-]flag shorthand."""
    if flag in text:
        return True
    base = flag[len("--no-"):] if flag.startswith("--no-") else flag[2:]
    return "--[no-]" + base in text


class DeploySettingsParityTest(unittest.TestCase):
    """Every flag deploy.sh accepts is in its help and accepted (and forwarded) by install.sh, every variable it reads
    is in deploy.env.example, and flags win over deploy.env and the installer's per-project snapshot."""

    def deploy(self, *args, config=None, expect_ok=True, **fake):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = fake_env(tmp, **fake)
            cmd = []
            if config is not None:
                path = os.path.join(tmp, "deploy.env")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(config)
                cmd += ["--config", path]
            res = run_script(os.path.join(ROOT, "scripts/deploy.sh"), cmd + list(args) + ["--dry-run"], env)
            calls = read_log(log)
        if expect_ok:
            self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        return res, calls

    def test_help_documents_every_flag_the_parser_accepts(self):
        usage = run_script(os.path.join(ROOT, "scripts/deploy.sh"), ["--help"], dict(os.environ)).stdout
        missing = sorted(f for f in parser_flags("scripts/deploy.sh") if not documented(f, usage))
        self.assertEqual(missing, [])
        # The whole header is printed (the old fixed line range cut new lines off).
        self.assertIn("-h, --help", usage)
        self.assertIn("deploy.env.example", usage)

    def test_installer_accepts_and_documents_every_deploy_flag(self):
        deploy_flags, install_flags = parser_flags("scripts/deploy.sh"), parser_flags("scripts/install.sh")
        self.assertEqual(sorted(deploy_flags - install_flags), [])
        # Everything the installer takes (except its own --prepare-only) can be passed on to deploy.sh.
        self.assertEqual(sorted(install_flags - deploy_flags - {"--prepare-only"}), [])
        usage = run_script(os.path.join(ROOT, "scripts/install.sh"), ["--help"], dict(os.environ)).stdout
        self.assertEqual(sorted(f for f in install_flags if not documented(f, usage)), [])

    def test_deploy_env_example_documents_every_variable_deploy_reads(self):
        reads = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*):-", read("scripts/deploy.sh")))
        aliases_and_internal = {"INITIAL_ADMINS", "ADMINS", "ADMIN_EMAIL", "INITIAL_VIEWERS", "VIEWERS", "VIEWER_EMAILS",
                                "LINKED_PROJECTS", "WORKSPACE_PROJECTS", "AUTO_DISCOVER_PROJECTS",
                                "PRICING_SYNC_SCHEDULE", "IAP_OAUTH_CLIENT"}
        example = read("deploy.env.example")
        missing = sorted(v for v in reads - aliases_and_internal if not re.search(r"(?m)^%s=" % v, example))
        self.assertEqual(missing, [])
        modules = example[example.index("DISABLED_MODULES="):example.index("# ---", example.index("DISABLED_MODULES="))]
        for module in config.MODULE_IDS:
            self.assertIn(module, modules)

    def test_example_file_deploys_as_is(self):
        example = read("deploy.env.example").replace('PROJECT_ID="your-project-id"', 'PROJECT_ID="demo-hub-proj"')
        res, _ = self.deploy(config=example)
        self.assertIn("Deployment complete: demo-hub-proj", res.stdout)

    def test_flags_win_over_the_config_file(self):
        cfg = ('PROJECT_ID="demo-hub-proj"\nADMIN_EMAILS="admin@example.org"\nVPC_NETWORK="cfg-net"\n'
               'VPC_SUBNET="cfg-subnet"\nMAINTENANCE_SCHEDULE="*/30 * * * *"\nENABLE_GOVERNANCE_ACTIONS="false"\n')
        res, _ = self.deploy("--vpc-network", "cli-net", "--vpc-subnet", "cli-subnet", "--maintenance-schedule",
                             "0 * * * *", "--no-read-only", config=cfg)
        self.assertIn("--network cli-net --subnet cli-subnet", res.stderr)
        self.assertNotIn("cfg-net", res.stderr + res.stdout)
        self.assertIn("[dry-run] scheduler job ai-control-plane-maintenance: 0 * * * *", res.stdout)
        self.assertIn("roles/resourcemanager.projectIamAdmin", res.stderr)  # --no-read-only beats the file
        res, _ = self.deploy(config=cfg)
        self.assertNotIn("roles/resourcemanager.projectIamAdmin", res.stderr)
        res, _ = self.deploy(config=cfg + 'PREFLIGHT_STRICT="maybe"\n', expect_ok=False)
        self.assertIn("PREFLIGHT_STRICT must be true or false", res.stderr)

    def test_maintenance_schedule_can_be_turned_off_and_old_jobs_go(self):
        base = 'PROJECT_ID="demo-hub-proj"\nADMIN_EMAILS="admin@example.org"\n'
        res, _ = self.deploy("--no-maintenance-schedule", config=base)
        self.assertIn("maintenance schedule: off (MAINTENANCE_SCHEDULE=off)", res.stdout)
        res, _ = self.deploy(config=base + 'MAINTENANCE_SCHEDULE="none"\n',
                             FAKE_EXISTING="ai-control-plane-maintenance")
        self.assertIn("[dry-run] remove scheduler job ai-control-plane-maintenance (MAINTENANCE_SCHEDULE=off)",
                      res.stdout)
        res, _ = self.deploy(config=base + 'ACCESS_MODE="iap"\nIAP_MEMBERS="example.org"\n',
                             FAKE_EXISTING="ai-control-plane-maintenance")
        self.assertIn("[dry-run] remove scheduler job ai-control-plane-maintenance (IAP blocks Cloud Scheduler calls)",
                      res.stdout)
        # A legacy PRICING_SYNC_SCHEDULE="off" no longer overrides an explicit flag.
        res, _ = self.deploy("--maintenance-schedule", "*/20 * * * *", config=base + 'PRICING_SYNC_SCHEDULE="off"\n')
        self.assertIn("[dry-run] scheduler job ai-control-plane-maintenance: */20 * * * *", res.stdout)

    def test_stale_web_enforcement_job_is_removed_when_no_schedule_applies(self):
        base = 'PROJECT_ID="demo-hub-proj"\nADMIN_EMAILS="admin@example.org"\n'
        for extra, args, why in (("", ("--no-enforcement-schedule",), "ENFORCEMENT_SCHEDULE off"),
                                 ("", ("--read-only",), "read-only install"),
                                 ('ACCESS_MODE="iap"\nIAP_MEMBERS="example.org"\n', (), "IAP access mode")):
            res, _ = self.deploy(*args, config=base + extra, FAKE_EXISTING="ai-control-plane-enforce")
            self.assertIn("[dry-run] remove scheduler job ai-control-plane-enforce (%s)" % why, res.stdout, args)
        res, _ = self.deploy(config=base, FAKE_EXISTING="ai-control-plane-enforce")
        self.assertNotIn("remove scheduler job ai-control-plane-enforce", res.stdout)
        self.assertIn("scheduler jobs update http ai-control-plane-enforce", res.stderr)

    def test_fresh_project_needs_scheduler_for_the_maintenance_tick_and_linked_traces(self):
        deploy = read("scripts/deploy.sh")
        self.assertRegex(deploy, r'\[\[ "\$MAINTENANCE_SCHEDULE" != "off" && "\$ACCESS_MODE" != "iap" \]\] && NEED_SCHED=1')
        linked = deploy[deploy.index("LINKED_READ_ROLES=("):deploy.index(")", deploy.index("LINKED_READ_ROLES=("))]
        self.assertIn("roles/cloudtrace.user", linked)
        # Turning real-time enforcement off removes the sinks of the linked projects and the topic as well.
        off = deploy[deploy.index('elif [[ "$DRY_RUN" != "1" ]]; then\n  # Real-time enforcement off'):]
        off = off[:off.index("\nfi\n")]
        self.assertIn('for sink_proj in "$PROJECT_ID" ${CLEAN_LINKED[@]+"${CLEAN_LINKED[@]}"}; do', off)
        self.assertIn('gcloud pubsub topics delete "$USAGE_EVENTS_TOPIC"', off)
        self.assertLess(off.index("subscriptions delete"), off.index("topics delete"))

    def test_none_clears_linked_projects_and_viewers_of_the_config(self):
        cfg = ('PROJECT_ID="demo-hub-proj"\nADMIN_EMAILS="admin@example.org"\nSCAN_PROJECTS="demo-spoke-a"\n'
               'ALLOWED_USERS="viewer@example.org"\n')
        res, _ = self.deploy(config=cfg)
        self.assertIn("##ALLOWED_USERS=viewer@example.org", res.stderr)
        self.assertIn("Linked scan   : demo-spoke-a", res.stdout)
        res, _ = self.deploy("--link-projects", "none", "--viewers", "None", config=cfg)
        self.assertNotIn("ALLOWED_USERS=", res.stderr)
        self.assertNotIn("demo-spoke-a", res.stdout + res.stderr)
        # "none" written in the file means the same; an empty flag value is still refused.
        res, _ = self.deploy(config=cfg.replace('"demo-spoke-a"', '"none"'))
        self.assertNotIn("Linked scan", res.stdout)
        res, _ = self.deploy("--viewers", "", config=cfg, expect_ok=False)
        self.assertIn("--viewers needs a value", res.stderr)

    def test_enforcement_schedule_off_is_case_insensitive(self):
        base = 'PROJECT_ID="demo-hub-proj"\nADMIN_EMAILS="admin@example.org"\n'
        for value in ("OFF", "None"):
            res, _ = self.deploy(config=base + 'ENFORCEMENT_SCHEDULE="%s"\n' % value)
            self.assertNotIn("scheduler jobs create http ai-control-plane-enforce ", res.stderr, value)
            self.assertNotIn("SCHEDULER_INVOKERS", res.stderr, value)
            self.assertIn("enforcement schedule: on-demand from the dashboard (ENFORCEMENT_SCHEDULE off)", res.stdout)


class ServiceEnvMatchesSetupTest(unittest.TestCase):
    """The dashboard shows the enforcement schedule and the real-time push as configured from SCHEDULER_INVOKERS and
    USAGE_EVENTS_SUBSCRIPTION, so the service keeps them only when the -enforce job and the push subscription exist. A
    real run (no --dry-run) against the fake gcloud: the IAP fallback never sets them, and a failed Pub/Sub setup
    takes the subscription out again with one extra revision."""

    CONFIG = ('PROJECT_ID="demo-hub-proj"\nADMIN_EMAILS="admin@example.org"\nCREATE_LOG_SINK="false"\n'
              'ENABLE_DATA_ACCESS_LOGS="false"\nENABLE_GE_OBSERVABILITY="false"\n'
              'IMAGE="asia-northeast3-docker.pkg.dev/demo-hub-proj/acp/acp:test"\n'
              # .invalid names never resolve, so the readiness probe ends at once without reaching any server.
              'PUBLIC_BASE_URL="https://dashboard.invalid"\n')
    SUBSCRIPTION = "USAGE_EVENTS_SUBSCRIPTION=projects/demo-hub-proj/subscriptions/ai-control-plane-agy-usage-push"

    def deploy(self, **fake):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = fake_env(tmp, **fake)
            path = os.path.join(tmp, "deploy.env")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.CONFIG)
            res = run_script(os.path.join(ROOT, "scripts/deploy.sh"), ["--config", path], env)
            calls = read_log(log).splitlines()
        self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        self.assertIn("Deployment complete: demo-hub-proj", res.stdout)
        return res, calls

    @staticmethod
    def service_deploys(calls):
        return [c for c in calls if c.startswith("run deploy ai-control-plane --")]

    @staticmethod
    def updates(calls):
        return [c for c in calls if c.startswith("run services update")]

    def test_schedule_and_push_set_up_keep_both_without_another_revision(self):
        res, calls = self.deploy()
        deploys = self.service_deploys(calls)
        self.assertEqual(len(deploys), 1, deploys)
        self.assertIn("##SCHEDULER_INVOKERS=%s" % SCHEDULER_SA, deploys[0])
        self.assertIn("##" + self.SUBSCRIPTION, deploys[0])
        self.assertTrue([c for c in calls if c.startswith("scheduler jobs create http ai-control-plane-enforce ")])
        self.assertEqual(self.updates(calls), [])
        self.assertIn("Real-time caps: active", res.stdout)

    def test_failed_pubsub_setup_takes_the_subscription_out_again(self):
        res, calls = self.deploy(FAKE_FAIL="logging sinks create ai-control-plane-agy-usage ")
        self.assertEqual(self.updates(calls), [
            "run services update ai-control-plane --project demo-hub-proj --region asia-northeast3 "
            "--remove-env-vars USAGE_EVENTS_SUBSCRIPTION --quiet"])
        self.assertIn("removed USAGE_EVENTS_SUBSCRIPTION from ai-control-plane", res.stdout)
        self.assertNotIn("Real-time caps: active", res.stdout)
        # The -enforce job exists, so its variable stays.
        self.assertIn("##SCHEDULER_INVOKERS=%s" % SCHEDULER_SA, self.service_deploys(calls)[0])
        res, calls = self.deploy(FAKE_FAIL="logging sinks create ai-control-plane-agy-usage |run services update")
        self.assertIn("could not remove USAGE_EVENTS_SUBSCRIPTION from ai-control-plane", res.stderr)
        self.assertIn("--remove-env-vars USAGE_EVENTS_SUBSCRIPTION", res.stderr)

    def test_iap_fallback_revision_never_announces_schedule_or_push(self):
        res, calls = self.deploy(
            FAKE_FAIL_RE=r"^run deploy ai-control-plane --.*( --no-invoker-iam-check | --allow-unauthenticated )")
        deploys = self.service_deploys(calls)
        self.assertEqual(len(deploys), 3, deploys)  # without invoker check, with allUsers, then behind IAP
        self.assertIn("--iap --invoker-iam-check", deploys[-1])
        for key in ("SCHEDULER_INVOKERS", "USAGE_EVENTS_SUBSCRIPTION"):
            self.assertNotIn(key, deploys[-1])
        self.assertEqual(self.updates(calls), [])
        self.assertFalse([c for c in calls if c.startswith("scheduler jobs create http ai-control-plane-enforce ")])
        self.assertFalse([c for c in calls if c.startswith("pubsub subscriptions create")])
        self.assertIn("Organization policies block a public entry", res.stderr)


class InstallerRerunTest(unittest.TestCase):
    """install.sh forwards every setting, saves it in deploy.PROJECT_ID.env and keeps what earlier runs saved."""

    def workspace(self, tmp):
        ws = os.path.join(tmp, "ws")
        copy_scripts(ws, *SCRIPT_FILES)
        return ws

    def install(self, ws, env, *args):
        res = run_script(os.path.join(ws, "scripts/install.sh"), list(args) + ["--dry-run"], env, cwd=ws)
        self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        return res

    def test_new_flags_are_forwarded_and_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self.workspace(tmp)
            env, _ = fake_env(tmp, ACP_SAVE_ENV_ON_DRY_RUN="1")
            res = self.install(ws, env, "--project", "demo-hub-proj", "--admins", "admin@example.org",
                               "--no-realtime-enforcement", "--no-maintenance-schedule", "--access-mode", "password",
                               "--service-name", "acp-two", "--dataset-location", "US", "--org-name", "Example Org",
                               "--enforcement-schedule", "*/10 * * * *")
            with open(os.path.join(ws, "deploy.demo-hub-proj.env"), encoding="utf-8") as fh:
                saved = fh.read()
        self.assertIn("maintenance schedule: off", res.stdout)
        self.assertNotIn("USAGE_EVENTS_SUBSCRIPTION=", res.stderr)
        self.assertIn("Deploying acp-two to demo-hub-proj (asia-northeast3)", res.stdout)
        for line in ('REALTIME_ENFORCEMENT="false"', 'MAINTENANCE_SCHEDULE="off"', 'ACCESS_MODE="password"',
                     'SERVICE_NAME="acp-two"', 'DATASET_LOCATION="US"', 'ORGANIZATION_NAME="Example Org"',
                     'ENFORCEMENT_SCHEDULE="*/10 * * * *"', 'ADMIN_EMAILS="admin@example.org"'):
            self.assertIn(line, saved)
        self.assertEqual(saved.count("DATASET_LOCATION="), 1)

    def test_rerun_without_flags_keeps_saved_settings_and_hand_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self.workspace(tmp)
            env, _ = fake_env(tmp, ACP_SAVE_ENV_ON_DRY_RUN="1")
            self.install(ws, env, "--project", "demo-hub-proj", "--region", "us-central1", "--admins",
                         "lead@example.org", "--service-name", "acp-two", "--dataset-location", "US")
            snapshot = os.path.join(ws, "deploy.demo-hub-proj.env")
            with open(snapshot, "a", encoding="utf-8") as fh:
                fh.write('BILLING_TABLE="gcp_billing_export_resource_v1_X"\n')
            res = self.install(ws, env, "--project", "demo-hub-proj")
            self.assertIn("Deploying acp-two to demo-hub-proj (us-central1)", res.stdout)
            self.assertNotIn("--region asia-northeast3", res.stderr)
            with open(snapshot, encoding="utf-8") as fh:
                saved = fh.read()
            for line in ('REGION="us-central1"', 'ADMIN_EMAILS="lead@example.org"', 'SERVICE_NAME="acp-two"',
                         'BILLING_TABLE="gcp_billing_export_resource_v1_X"'):
                self.assertIn(line, saved)
            self.install(ws, env, "--project", "demo-hub-proj", "--region", "europe-west1",
                         "--org-name", "It's $HOME")
            with open(snapshot, encoding="utf-8") as fh:
                saved = fh.read()
            self.assertIn('REGION="europe-west1"', saved)
            self.assertIn('DATASET_LOCATION="US"', saved)  # the dataset cannot move with the region
            # Values bash would expand are quoted so that sourcing the file gives them back unchanged.
            out = subprocess.run(["bash", "-c", 'source "$1"; printf "%s" "$ORGANIZATION_NAME"', "_", snapshot],
                                 capture_output=True, text=True, check=False).stdout
            self.assertEqual(out, "It's $HOME")

    def test_prepare_only_passes_only_the_flags_prepare_takes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self.workspace(tmp)
            env, _ = fake_env(tmp)
            res = self.install(ws, env, "--prepare-only", "--project", "demo-hub", "--read-only", "--access-mode",
                               "iap", "--enforcement-backend", "iam_deny", "--no-realtime-enforcement",
                               "--dataset", "ai_logs", "--no-log-prompts")
        self.assertIn("Preparing demo-hub for AI Control Plane", res.stdout)
        self.assertIn("demo-hub:ai_logs", res.stdout)
        self.assertNotIn("unknown argument", res.stderr)

    def test_clean_reclone_keeps_saved_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            upstream = os.path.join(tmp, "upstream")
            copy_scripts(upstream, *SCRIPT_FILES)
            git_env = dict(os.environ, GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.org",
                           GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.org")
            for cmd in (["git", "init", "-b", "main", upstream], ["git", "-C", upstream, "add", "."],
                        ["git", "-C", upstream, "commit", "-m", "v1"]):
                subprocess.run(cmd, check=True, capture_output=True, env=git_env)
            install_dir = os.path.join(tmp, "home", "ai-control-plane")
            os.makedirs(install_dir)  # no .git: the installer takes the clean re-clone path
            with open(os.path.join(install_dir, "deploy.demo-kept-proj.env"), "w", encoding="utf-8") as fh:
                fh.write('PROJECT_ID="demo-kept-proj"\nREGION="europe-west1"\nADMIN_EMAILS="admin@example.org"\n'
                         '# kept by hand\n')
            streamed = os.path.join(tmp, "streamed_install.sh")
            copy = read("scripts/install.sh")
            with open(streamed, "w", encoding="utf-8") as fh:
                fh.write(copy)
            env, _ = fake_env(tmp, HOME=os.path.join(tmp, "home"), ACP_REPO_URL=upstream, ACP_INSTALL_DIR=install_dir)
            res = run_script(streamed, ["--project", "demo-kept-proj", "--dry-run"], env, cwd=tmp)
            self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
            self.assertIn("Downloading AI Control Plane", res.stdout)
            self.assertIn("Deploying ai-control-plane to demo-kept-proj (europe-west1)", res.stdout)
            with open(os.path.join(install_dir, "deploy.demo-kept-proj.env"), encoding="utf-8") as fh:
                self.assertIn("# kept by hand", fh.read())
            self.assertTrue(os.path.isdir(os.path.join(install_dir, ".git")))

    def test_dataset_location_follows_the_region_unless_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self.workspace(tmp)
            env, _ = fake_env(tmp, ACP_SAVE_ENV_ON_DRY_RUN="1")
            snapshot = os.path.join(ws, "deploy.demo-hub-proj.env")
            self.install(ws, env, "--project", "demo-hub-proj")
            with open(snapshot, encoding="utf-8") as fh:
                self.assertNotIn("DATASET_LOCATION=", fh.read())
            # The organization refused the first region: re-running with --region moves the dataset location too.
            res = self.install(ws, env, "--project", "demo-hub-proj", "--region", "europe-west1")
            self.assertIn("Audit dataset : demo-hub-proj:ge_analytics (europe-west1,", res.stdout)
            # Earlier installers pinned DATASET_LOCATION to the region of their run: such a value follows a new region.
            with open(snapshot, "a", encoding="utf-8") as fh:
                fh.write('DATASET_LOCATION="europe-west1"\n')
            res = self.install(ws, env, "--project", "demo-hub-proj", "--region", "us-central1")
            self.assertIn("Audit dataset : demo-hub-proj:ge_analytics (us-central1,", res.stdout)
            with open(snapshot, encoding="utf-8") as fh:
                saved = fh.read()
            self.assertIn('DATASET_LOCATION=""', saved)
            self.assertIn('REGION="us-central1"', saved)
            # An explicit location is saved and kept when the region changes.
            self.install(ws, env, "--project", "demo-hub-proj", "--dataset-location", "US")
            res = self.install(ws, env, "--project", "demo-hub-proj", "--region", "asia-northeast3")
            self.assertIn("Audit dataset : demo-hub-proj:ge_analytics (US,", res.stdout)

    def test_none_clears_saved_linked_projects_and_viewers(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self.workspace(tmp)
            env, _ = fake_env(tmp, ACP_SAVE_ENV_ON_DRY_RUN="1")
            snapshot = os.path.join(ws, "deploy.demo-hub-proj.env")
            res = self.install(ws, env, "--project", "demo-hub-proj", "--link-projects", "demo-spoke-a",
                               "--viewers", "viewer@example.org")
            self.assertIn("Linked scan   : demo-spoke-a", res.stdout)
            self.assertIn("##ALLOWED_USERS=viewer@example.org", res.stderr)
            res = self.install(ws, env, "--project", "demo-hub-proj", "--link-projects", "NONE", "--viewers", "none")
            with open(snapshot, encoding="utf-8") as fh:
                saved = fh.read()
            self.assertIn('SCAN_PROJECTS=""', saved)
            self.assertIn('ALLOWED_USERS=""', saved)
            self.assertNotIn("demo-spoke-a", saved)
            self.assertNotIn("viewer@example.org", saved)
            self.assertNotIn("Linked scan", res.stdout)
            self.assertNotIn("ALLOWED_USERS=", res.stderr)
            # Later runs keep them cleared; an empty value is still refused.
            res = self.install(ws, env, "--project", "demo-hub-proj")
            self.assertNotIn("demo-spoke-a", res.stdout + res.stderr)
            self.assertNotIn("viewer@example.org", res.stdout + res.stderr)
            bad = run_script(os.path.join(ws, "scripts/install.sh"), ["--project", "demo-hub-proj", "--viewers", "",
                                                                       "--dry-run"], env, cwd=ws)
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn("--viewers needs a value", bad.stderr)


TEARDOWN_POLICY = json.dumps({"bindings": [
    {"role": "projects/demo-hub-proj/roles/CustomAntigravityCapBlocked", "members": ["user:capped@example.com"]},
    {"role": "projects/demo-hub-proj/roles/CustomAntigravityAdminBlocked", "members": []},
    {"role": "roles/discoveryengine.agentspaceUser", "members": ["user:active@example.com"]}]})


class TeardownCompletenessTest(unittest.TestCase):
    """teardown.sh stops every enforcer before restoring people, follows the settings saved in the dashboard, removes
    the dashboard's dataset access and deletes custom roles only when asked and only when nothing holds them."""

    def teardown(self, *args, config='PROJECT_ID="demo-hub-proj"\n', **fake):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = fake_env(tmp, **fake)
            path = os.path.join(tmp, "deploy.env")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(config)
            res = run_script(os.path.join(ROOT, "scripts/teardown.sh"), ["--config", path, *args], env)
            calls = read_log(log).splitlines()
        self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        return res, calls

    @staticmethod
    def first(calls, prefix):
        return next(i for i, call in enumerate(calls) if call.startswith(prefix))

    def test_enforcement_stops_before_anybody_is_restored(self):
        res, calls = self.teardown("--yes", config='PROJECT_ID="demo-hub-proj"\nENFORCEMENT_BACKEND="iam_deny"\n',
                                   FAKE_POLICY=TEARDOWN_POLICY)
        restore = self.first(calls, "projects add-iam-policy-binding demo-hub-proj --member user:capped@example.com")
        for prefix in ("scheduler jobs delete ai-control-plane-enforcer", "scheduler jobs delete ai-control-plane-enforce ",
                       "scheduler jobs delete ai-control-plane-maintenance", "pubsub subscriptions delete",
                       "run jobs delete ai-control-plane-enforcer", "run services delete ai-control-plane "):
            self.assertLess(self.first(calls, prefix), restore, prefix)
        self.assertLess(restore, self.first(calls, "iam policies delete ai-control-plane-caps"))
        self.assertIn("restored user:capped@example.com", res.stdout)

    def test_settings_saved_in_the_dashboard_decide_what_is_undone(self):
        settings = json.dumps({"version": 1, "overrides": {
            "workspace": {"projects": ["ui-added-proj", "Bad Project!"], "scope_type": "folder", "scope_ids": ["4455"]},
            "governance": {"antigravity": {"cap_marker_role": "TeamCapRole", "access_role": "roles/discoveryengine.user",
                                           "deny_policy_id": "ai-control-plane-team",
                                           "enforcement_backend": "iam_deny"}}}})
        policy = json.dumps({"bindings": [
            {"role": "projects/demo-hub-proj/roles/TeamCapRole", "members": ["user:capped@example.com"]}]})
        res, calls = self.teardown("--yes", FAKE_SETTINGS=settings, FAKE_POLICY=policy)
        self.assertIn("projects add-iam-policy-binding demo-hub-proj --member user:capped@example.com --role "
                      "roles/discoveryengine.user --condition=None --quiet", calls)
        self.assertIn("projects remove-iam-policy-binding demo-hub-proj --member user:capped@example.com --role "
                      "projects/demo-hub-proj/roles/TeamCapRole --condition=None --quiet", calls)
        deletes = [c for c in calls if c.startswith("iam policies delete")]
        for point in ("projects/demo-hub-proj", "projects/ui-added-proj", "folders/4455"):
            self.assertIn("iam policies delete ai-control-plane-team --attachment-point=cloudresourcemanager."
                          "googleapis.com/%s --kind=denypolicies --quiet" % point, deletes)
        self.assertEqual(len(deletes), 3)  # the invalid project id is never used
        res, calls = self.teardown("--dry-run", FAKE_SETTINGS=settings, FAKE_POLICY=policy)
        self.assertIn("projects/ui-added-proj folders/4455", res.stdout)
        self.assertFalse([c for c in calls if "delete" in c or "add-iam-policy-binding" in c])

    def test_dashboard_access_to_the_kept_audit_dataset_is_removed(self):
        dataset = json.dumps({"access": [
            {"role": "WRITER", "userByEmail": "ai-control-plane-sa@demo-hub-proj.iam.gserviceaccount.com"},
            {"role": "WRITER", "userByEmail": "service-123456789012@gcp-sa-logging.iam.gserviceaccount.com"}]})
        res, calls = self.teardown("--yes", FAKE_DATASET=dataset)
        self.assertTrue([c for c in calls if c.startswith("bq --project_id demo-hub-proj update --source")])
        self.assertIn("removed ai-control-plane-sa@demo-hub-proj.iam.gserviceaccount.com from demo-hub-proj:ge_analytics",
                      res.stdout)
        self.assertFalse([c for c in calls if c.startswith("bq") and " rm " in c])  # the dataset itself stays

    def test_custom_roles_are_deleted_only_when_asked_and_unused(self):
        res, calls = self.teardown("--yes", FAKE_POLICY=TEARDOWN_POLICY)
        self.assertFalse([c for c in calls if c.startswith("iam roles delete")])
        self.assertIn("--delete-roles", res.stdout)
        self.assertIn("gcloud iam roles delete CustomAntigravityCapBlocked --project demo-hub-proj", res.stdout)
        res, calls = self.teardown("--yes", "--delete-roles", "--link-projects", "demo-spoke-a",
                                   FAKE_POLICY=TEARDOWN_POLICY)
        deleted = {c.split()[3] + "@" + c.split()[5] for c in calls if c.startswith("iam roles delete")}
        self.assertEqual(deleted, {"CustomAntigravityRestrictedUser@demo-hub-proj",
                                   "CustomAntigravityAdminBlocked@demo-hub-proj",
                                   "CustomAiControlPlaneGatewayManager@demo-hub-proj",
                                   "CustomAiControlPlaneGeFeatureManager@demo-hub-proj",
                                   "CustomAiControlPlaneGeFeatureManager@demo-spoke-a",
                                   "CustomAiControlPlaneLicenseReader@demo-hub-proj",
                                   "CustomAiControlPlaneLicenseReader@demo-spoke-a"})
        self.assertIn("kept projects/demo-hub-proj/roles/CustomAntigravityCapBlocked (1 member(s) still hold it)",
                      res.stdout)
        # Another deployment in the project still holds the license reader: its marker roles stay.
        shared = json.loads(TEARDOWN_POLICY)
        shared["bindings"].append({"role": "projects/demo-hub-proj/roles/CustomAiControlPlaneLicenseReader",
                                   "members": ["serviceAccount:other-sa@demo-hub-proj.iam.gserviceaccount.com"]})
        res, calls = self.teardown("--yes", "--delete-roles", FAKE_POLICY=json.dumps(shared))
        self.assertFalse([c for c in calls if c.startswith("iam roles delete CustomAntigravity")])
        self.assertIn("another AI Control Plane deployment in demo-hub-proj still uses the marker roles", res.stdout)

    def test_enforcement_job_region_and_help(self):
        _, calls = self.teardown("--yes", "--enforcement-job-region", "us-east1")
        self.assertIn("scheduler jobs delete ai-control-plane-enforcer --project demo-hub-proj --location us-east1 "
                      "--quiet", calls)
        self.assertIn("run jobs delete ai-control-plane-enforcer --project demo-hub-proj --region us-east1 --quiet",
                      calls)
        usage = run_script(os.path.join(ROOT, "scripts/teardown.sh"), ["--help"], dict(os.environ)).stdout
        missing = sorted(f for f in parser_flags("scripts/teardown.sh") if not documented(f, usage))
        self.assertEqual(missing, [])

    def test_link_projects_none_cleans_no_linked_project(self):
        config = 'PROJECT_ID="demo-hub-proj"\nSCAN_PROJECTS="demo-spoke-a"\n'
        _, calls = self.teardown("--yes", config=config)
        self.assertTrue([c for c in calls if "demo-spoke-a" in c])
        _, calls = self.teardown("--yes", "--link-projects", "none", config=config)
        self.assertFalse([c for c in calls if "demo-spoke-a" in c])
        self.assertFalse([c for c in calls if "--project none" in c or "projects/none" in c])


RESTORE_GCLOUD = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GCLOUD_LOG"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(args) + "\\n")
mode = os.environ.get("FAKE_MODE", "ok")
if args[:2] == ["projects", "get-iam-policy"]:
    if mode == "garbage":
        print("Updated property [core/project].")
        sys.exit(0)
    print(json.dumps({"bindings": [
        {"role": "projects/demo/roles/TeamCap", "members": ["user:capped@example.com"]},
        {"role": "projects/demo/roles/OldCap", "members": ["user:old@example.com"]},
        {"role": "projects/demo/roles/TeamAdmin", "members": ["user:blocked@example.com"]}]}))
    sys.exit(0)
if args[:2] == ["projects", "add-iam-policy-binding"] and mode == "refuse":
    sys.exit(1)
sys.exit(0)
"""


class RestoreHelperTest(unittest.TestCase):
    def restore(self, mode, call):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "calls.log")
            with open(os.path.join(tmp, "gcloud"), "w", encoding="utf-8") as fh:
                fh.write(RESTORE_GCLOUD)
            os.chmod(os.path.join(tmp, "gcloud"), 0o755)
            env = dict(os.environ, PATH=f"{tmp}:{os.environ.get('PATH', '')}", FAKE_GCLOUD_LOG=log, FAKE_MODE=mode)
            res = subprocess.run(["bash", "-c", 'set -euo pipefail; source "%s"; %s; echo after' % (
                os.path.join(ROOT, "scripts/lib/common.sh"), call)], capture_output=True, text=True, env=env, check=False)
            calls = read_log(log)
        self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        self.assertIn("after", res.stdout)  # never stops the caller (teardown runs under set -e)
        return res, calls

    def test_configured_marker_roles_are_restored(self):
        res, calls = self.restore("ok", "restore_capped_users demo roles/discoveryengine.user false TeamCap,OldCap "
                                        "TeamAdmin")
        for member, role in (("capped", "TeamCap"), ("old", "OldCap")):
            self.assertIn("projects add-iam-policy-binding demo --member user:%s@example.com --role "
                          "roles/discoveryengine.user --condition=None" % member, calls)
            self.assertIn("projects remove-iam-policy-binding demo --member user:%s@example.com --role "
                          "projects/demo/roles/%s --condition=None" % (member, role), calls)
        self.assertIn("kept administrator block: user:blocked@example.com", res.stdout)

    def test_unreadable_policy_and_refusals_print_the_manual_commands(self):
        res, _ = self.restore("garbage", "restore_capped_users demo")
        self.assertIn("were not restored", res.stderr)
        self.assertIn("gcloud projects add-iam-policy-binding demo --member=user:EMAIL", res.stderr)
        res, calls = self.restore("refuse", "restore_capped_users demo roles/discoveryengine.user false TeamCap")
        self.assertIn("marker role kept. Run: gcloud projects add-iam-policy-binding demo "
                      "--member=user:capped@example.com --role=roles/discoveryengine.user --condition=None", res.stderr)
        self.assertNotIn("remove-iam-policy-binding", calls)


class TerraformAndTutorialTest(unittest.TestCase):
    def test_terraform_realtime_push_names_the_public_url(self):
        tf, variables = read("terraform/main.tf"), read("terraform/variables.tf")
        self.assertIn('PUBLIC_BASE_URL = var.public_base_url != "" || local.realtime_enabled ? local.public_base_url : ""',
                      tf)
        push = tf[tf.index('resource "google_pubsub_subscription" "usage_events_push"'):]
        self.assertIn('push_endpoint = "${local.run_url}${local.usage_event_path}"', push)
        self.assertIn('audience              = "${local.public_base_url}${local.usage_event_path}"', push)
        self.assertIn("(local.deny_enabled || var.enable_governance_actions)", tf)
        for role in ("roles/logging.privateLogViewer", "roles/cloudtrace.user", "roles/modelarmor.viewer",
                     "roles/networkservices.viewer", "roles/networksecurity.viewer"):
            self.assertIn('"%s"' % role, tf[tf.index("read_roles = ["):tf.index("]", tf.index("read_roles = ["))])
        for api in ("cloudtrace", "modelarmor", "networkservices", "networksecurity", "billingbudgets"):
            self.assertIn('"%s.googleapis.com"' % api, tf)
        for name in ("public_base_url", "enable_optional_apis"):
            self.assertIn('variable "%s"' % name, variables)
            self.assertRegex(read("terraform/terraform.tfvars.example"), r"(?m)^%s\s+=" % name)
        self.assertIn("ai-control-plane:2.2.0", variables)
        self.assertIn("ai-control-plane:2.2.0", read("terraform/terraform.tfvars.example"))
        self.assertIn("Not created by this module", tf)

    def test_tutorial_uses_flags_the_scripts_accept(self):
        text = read("TUTORIAL.md")
        self.assertIn("<walkthrough-project-setup", text)
        self.assertIn("./scripts/install.sh --project {{project-id}}", text)
        self.assertIn("./scripts/teardown.sh --project {{project-id}}", text)
        known = parser_flags("scripts/install.sh") | parser_flags("scripts/teardown.sh")
        used = set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]+", text))
        self.assertEqual(sorted(used - known), [])

    def test_owned_files_use_the_product_name_agent_platform(self):
        for rel in ("scripts/install.sh", "scripts/deploy.sh", "scripts/prepare.sh", "scripts/teardown.sh",
                    "scripts/test.sh", "scripts/lib/common.sh", "deploy.env.example", "terraform/main.tf",
                    "terraform/variables.tf", "terraform/terraform.tfvars.example", "TUTORIAL.md"):
            self.assertNotIn("Vertex" + " AI", read(rel), rel)

    def test_comments_match_what_the_scripts_and_the_module_do(self):
        # enable_data_access_logs configures 5 services; the comments used to promise 7.
        for rel in ("scripts/prepare.sh", "deploy.env.example"):
            text = read(rel)
            self.assertNotIn("7 AI services", text, rel)
            self.assertIn("5 AI services", text, rel)
            self.assertIn("Gemini Data Analytics", text, rel)
        # The example no longer pins the dataset location (empty = REGION, the same default as before).
        self.assertRegex(read("deploy.env.example"), r'(?m)^DATASET_LOCATION=""')
        # The module allows auth_mode iap/auto only, so real-time enforcement needs an external load balancer with IAP.
        variables = read("terraform/variables.tf")
        realtime = variables[variables.index('variable "realtime_enforcement"'):]
        realtime = realtime[:realtime.index("\n}\n")]
        self.assertIn("external HTTPS load balancer with IAP", realtime)
        self.assertIn("enable_iap = false", realtime)


# Answers `org-policies describe ... --effective` with FAKE_POLICIES (constraint name -> effective policy).
PREFLIGHT_GCLOUD = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if "describe" in args and "--effective" in args:
    name = next(a for a in args if a.startswith("constraints/"))[len("constraints/"):]
    policies = json.loads(os.environ.get("FAKE_POLICIES", "{}"))
    print(json.dumps(policies.get(name, {"constraint": "constraints/" + name})))
sys.exit(0)
"""


class LocationBlockerTest(unittest.TestCase):
    """gcp.resourceLocations findings name the flag that fixes them: --region, or --dataset-location for a BigQuery
    location set apart from the region."""

    def preflight(self, region, dsloc, allowed):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "gcloud"), "w", encoding="utf-8") as fh:
                fh.write(PREFLIGHT_GCLOUD)
            os.chmod(os.path.join(tmp, "gcloud"), 0o755)
            policies = {"gcp.resourceLocations": {"listPolicy": {"allowedValues": allowed}}}
            # Only the fake gcloud and the system tools (no local docker), as in test_preflight.py.
            env = dict(os.environ, PATH=tmp + os.pathsep + "/usr/bin:/bin", FAKE_POLICIES=json.dumps(policies))
            script = 'set -euo pipefail; source "%s"; preflight_org_policies demo-hub-proj "%s" "%s"' % (
                os.path.join(ROOT, "scripts/lib/common.sh"), region, dsloc)
            res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, check=False)
        return res, [l for l in res.stderr.splitlines() if "[중단] gcp.resourceLocations" in l]

    def test_separate_bigquery_location_points_to_dataset_location(self):
        res, blocks = self.preflight("asia-northeast3", "US", ["in:asia-northeast3-locations"])
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(len(blocks), 1, res.stderr)
        self.assertIn("BigQuery 위치 'us'", blocks[0])
        self.assertIn("--dataset-location <허용 위치>", blocks[0])
        self.assertNotIn("--region", blocks[0])

    def test_same_location_is_one_finding_naming_both_flags(self):
        res, blocks = self.preflight("asia-northeast3", "asia-northeast3", ["in:us-locations"])
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(len(blocks), 1, res.stderr)
        self.assertIn("리전·BigQuery 위치 'asia-northeast3'", blocks[0])
        self.assertIn("--region <허용 리전>", blocks[0])
        self.assertIn("--dataset-location <허용 위치>", blocks[0])

    def test_allowed_separate_locations_pass(self):
        res, blocks = self.preflight("asia-northeast3", "US", ["in:asia-northeast3-locations", "in:us-locations"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(blocks, [])
        self.assertIn("asia-northeast3, us 허용", res.stdout + res.stderr)


# get-iam-policy prints the policy file; set-iam-policy refuses FAKE_REFUSE (as projects without that service do) or
# stores the new policy.
AUDIT_GCLOUD = """#!/usr/bin/env python3
import json, os, shutil, sys
args = sys.argv[1:]
path = os.environ["FAKE_POLICY_FILE"]
if args[:2] == ["projects", "get-iam-policy"]:
    print(open(path, encoding="utf-8").read())
    sys.exit(0)
if args[:2] == ["projects", "set-iam-policy"]:
    services = {c.get("service") for c in json.load(open(args[3], encoding="utf-8")).get("auditConfigs", [])}
    refused = os.environ.get("FAKE_REFUSE", "")
    if refused and refused in services:
        sys.stderr.write("ERROR: INVALID_ARGUMENT: Service %s does not exist or does not support service level "
                         "configuration\\n" % refused)
        sys.exit(1)
    shutil.copyfile(args[3], path)
sys.exit(0)
"""


class DataAccessLogsMessageTest(unittest.TestCase):
    def enable(self, refuse=""):
        with tempfile.TemporaryDirectory() as tmp:
            policy = os.path.join(tmp, "policy.json")
            with open(policy, "w", encoding="utf-8") as fh:
                fh.write('{"bindings": [], "etag": "Bw=="}\n')
            with open(os.path.join(tmp, "gcloud"), "w", encoding="utf-8") as fh:
                fh.write(AUDIT_GCLOUD)
            os.chmod(os.path.join(tmp, "gcloud"), 0o755)
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ.get("PATH", ""), FAKE_POLICY_FILE=policy,
                       FAKE_REFUSE=refuse)
            res = subprocess.run(["bash", "-c", 'source "%s"; DRY_RUN=0; enable_data_access_logs demo-audit-proj' % (
                os.path.join(ROOT, "scripts/lib/common.sh"))], capture_output=True, text=True, env=env, check=False)
            with open(policy, encoding="utf-8") as fh:
                saved = json.load(fh)
        self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        return res, {c["service"] for c in saved.get("auditConfigs", [])}

    def test_message_names_the_five_services_it_configures(self):
        res, services = self.enable()
        self.assertEqual(services, {"aiplatform.googleapis.com", "discoveryengine.googleapis.com",
                                    "modelarmor.googleapis.com", "businessaicode.googleapis.com",
                                    "geminidataanalytics.googleapis.com"})
        self.assertIn("(Agent Platform, Gemini Enterprise, NotebookLM, Model Armor, Antigravity, "
                      "Gemini Data Analytics)", res.stdout)
        res, services = self.enable(refuse="geminidataanalytics.googleapis.com")
        self.assertNotIn("geminidataanalytics.googleapis.com", services)
        self.assertIn("skipped: geminidataanalytics.googleapis.com", res.stdout)


class DeploymentEdgeCasesHardeningTest(unittest.TestCase):
    """Regression tests for brand-new and existing GCP project edge cases found during E2E audit."""

    def _run_preflight_py(self, handler, mode="deploy", need_build="1", need_sink="1",
                          need_iap="1", need_sched="1", need_audit="1", need_rt="1"):
        out = io.StringIO()
        argv = ["-", mode, "demo-proj", need_build, need_sink, need_iap, need_sched, need_audit, need_rt]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(os.environ, {"ACP_ACCESS_TOKEN": "tok"}), \
                mock.patch("urllib.request.urlopen", handler), \
                contextlib.redirect_stdout(out):
            try:
                exec(compile(preflight_script(), "preflight", "exec"), {"__name__": "__main__"})  # pylint: disable=exec-used
            except SystemExit:
                pass
        return json.loads(out.getvalue().strip())

    def test_preflight_editor_plus_iam_admin_bootstraps_all_missing_roles(self):
        # Exact permission gaps of roles/editor + roles/resourcemanager.projectIamAdmin verified live against GCP.
        missing_for_editor = {
            "bigquery.datasets.update",
            "iam.roles.create",
            "artifactregistry.repositories.setIamPolicy",
            "pubsub.topics.setIamPolicy",
            "storage.buckets.setIamPolicy",
            "secretmanager.secrets.setIamPolicy",
            "run.services.setIamPolicy",
            "iam.serviceAccounts.setIamPolicy",
            "logging.sinks.create",
            "iap.web.setIamPolicy",
        }

        def handler(req, timeout=None):  # pylint: disable=unused-argument
            perms = json.loads(req.data)["permissions"]
            granted = [p for p in perms if p not in missing_for_editor]
            return io.BytesIO(json.dumps({"permissions": granted}).encode())

        verdict = self._run_preflight_py(handler)
        self.assertEqual(verdict["status"], "bootstrap")
        for role in ("roles/bigquery.admin", "roles/iam.roleAdmin", "roles/artifactregistry.admin",
                     "roles/pubsub.admin", "roles/storage.admin", "roles/secretmanager.admin",
                     "roles/run.admin", "roles/iam.serviceAccountAdmin", "roles/logging.configWriter",
                     "roles/iap.admin"):
            self.assertIn(role, verdict["missing_roles"])
        # Editor already has discoveryengine.engines.update; do not grant roles/discoveryengine.admin unnecessarily.
        self.assertNotIn("roles/discoveryengine.admin", verdict["missing_roles"])

    def test_preflight_owner_with_discoveryengine_does_not_grant_extra_discovery_admin(self):
        def handler(req, timeout=None):  # pylint: disable=unused-argument
            perms = json.loads(req.data)["permissions"]
            # Owner has every permission except project-level storage.buckets.setIamPolicy.
            granted = [p for p in perms if p != "storage.buckets.setIamPolicy"]
            return io.BytesIO(json.dumps({"permissions": granted}).encode())

        verdict = self._run_preflight_py(handler)
        self.assertEqual(verdict["status"], "bootstrap")
        self.assertEqual(verdict["missing_roles"], ["roles/storage.admin"])

    def test_preflight_mtls_401_falls_back_to_gcloud_iam_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            gcloud = os.path.join(tmp, "gcloud")
            with open(gcloud, "w", encoding="utf-8") as fh:
                fh.write("#!/usr/bin/env python3\n"
                         "import sys\n"
                         "args = sys.argv[1:]\n"
                         "if args[:2] == ['auth', 'print-access-token']:\n"
                         "    print('tok')\n"
                         "    sys.exit(0)\n"
                         "if args[:2] == ['projects', 'get-iam-policy']:\n"
                         "    sys.stderr.write('ERROR: PERMISSION_DENIED: The caller does not have permission\\n')\n"
                         "    sys.exit(1)\n"
                         "sys.exit(0)\n")
            os.chmod(gcloud, 0o755)
            # Intercept _check_project_permissions to emit http_401 directly without hitting the network.
            cmd = ('set -euo pipefail; source "%s"; '
                   '_check_project_permissions() { printf \'{"status":"skip","reason":"http_401"}\\n\'; }; '
                   'preflight_caller_permissions deploy demo-proj user@example.org 1 1 1 1 1 1') % (
                os.path.join(ROOT, "scripts/lib/common.sh"))
            env = dict(os.environ, PATH=f"{tmp}:{os.environ.get('PATH', '')}")
            res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env, check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("lacks required IAM permissions on project demo-proj", res.stderr)

    def test_short_service_name_rejected_and_prefixed_billing_account_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, _ = fake_env(tmp)
            for short_name in ("a", "ai"):
                res = run_script(os.path.join(ROOT, "scripts/deploy.sh"),
                                 ["--project", "demo-hub-proj", "--service-name", short_name, "--dry-run"], env)
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("3-42", res.stderr)
                res_p = run_script(os.path.join(ROOT, "scripts/prepare.sh"),
                                   ["--project", "demo-hub-proj", "--service-name", short_name, "--dry-run"], env)
                self.assertNotEqual(res_p.returncode, 0)
                self.assertIn("3-42", res_p.stderr)

            bad_ba = run_script(os.path.join(ROOT, "scripts/deploy.sh"),
                                ["--project", "demo-hub-proj", "--billing-account", "not-a-billing-id", "--dry-run"], env)
            self.assertNotEqual(bad_ba.returncode, 0)
            self.assertIn("invalid BILLING_ACCOUNT_ID", bad_ba.stderr)

            ok_ba = run_script(
                os.path.join(ROOT, "scripts/deploy.sh"),
                ["--project", "demo-hub-proj", "--billing-account", "billingAccounts/012345-6789ab-cdef01", "--dry-run"],
                env,
            )
            self.assertEqual(ok_ba.returncode, 0, msg=(ok_ba.stdout, ok_ba.stderr))
            self.assertIn("BILLING_ACCOUNT_ID=012345-6789AB-CDEF01", ok_ba.stderr)
            self.assertNotIn("BILLING_ACCOUNT_ID=billingAccounts/", ok_ba.stderr)

    def test_grant_dataset_role_handles_legacy_group_writer_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds_path = os.path.join(tmp, "dataset.json")
            with open(ds_path, "w", encoding="utf-8") as fh:
                json.dump({"location": "US", "access": [{"role": "OWNER", "specialGroup": "projectOwners"}]}, fh)
            bq = os.path.join(tmp, "bq")
            with open(bq, "w", encoding="utf-8") as fh:
                fh.write("#!/usr/bin/env python3\n"
                         "import os, shutil, sys\n"
                         "args = sys.argv[1:]\n"
                         "ds = os.environ['FAKE_DS_FILE']\n"
                         "if 'show' in args:\n"
                         "    print(open(ds, encoding='utf-8').read())\n"
                         "    sys.exit(0)\n"
                         "if 'update' in args:\n"
                         "    src = args[args.index('--source') + 1]\n"
                         "    shutil.copyfile(src, ds)\n"
                         "    sys.exit(0)\n"
                         "sys.exit(0)\n")
            os.chmod(bq, 0o755)
            env = dict(os.environ, PATH=f"{tmp}:{os.environ.get('PATH', '')}", FAKE_DS_FILE=ds_path)
            cmd = ('set -euo pipefail; source "%s"; '
                   'grant_dataset_role demo-proj ge_analytics WRITER group:cloud-logs@example.org; '
                   'grant_dataset_role demo-proj ge_analytics WRITER group:cloud-logs@example.org') % (
                os.path.join(ROOT, "scripts/lib/common.sh"))
            res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env, check=False)
            self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
            with open(ds_path, encoding="utf-8") as fh:
                saved = json.load(fh)
        group_entries = [a for a in saved["access"] if a.get("groupByEmail") == "cloud-logs@example.org"]
        self.assertEqual(len(group_entries), 1)
        self.assertEqual(group_entries[0]["role"], "WRITER")
        self.assertFalse(any("group:" in str(a.get("userByEmail", "")) for a in saved["access"]))

    def test_cloud_scheduler_blocked_falls_back_cleanly_and_removes_stale_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = fake_env(tmp, FAKE_FAIL="scheduler jobs create ")
            # Fast no-op sleep so retry exhaustion finishes immediately in unit tests.
            sleep_stub = os.path.join(tmp, "fakebin", "sleep")
            with open(sleep_stub, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\nexit 0\n")
            os.chmod(sleep_stub, 0o755)
            cfg = os.path.join(tmp, "deploy.env")
            with open(cfg, "w", encoding="utf-8") as fh:
                fh.write(ServiceEnvMatchesSetupTest.CONFIG)
            res = run_script(os.path.join(ROOT, "scripts/deploy.sh"), ["--config", cfg], env)
            calls = read_log(log).splitlines()
        self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        self.assertIn("could not create Cloud Scheduler job ai-control-plane-enforce", res.stderr)
        self.assertIn("removed SCHEDULER_INVOKERS from ai-control-plane", res.stdout)
        self.assertIn("no enforcement schedule", res.stdout)
        self.assertIn("run services update ai-control-plane --project demo-hub-proj --region asia-northeast3 "
                      "--remove-env-vars SCHEDULER_INVOKERS --quiet", calls)

    def test_existing_hub_dataset_location_aligns_linked_projects_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = fake_env(tmp, FAKE_EXISTING="ai-control-plane-ai-logs")
            # Ensure logging sinks describe --format=value(writerIdentity) prints a writer identity.
            gc_path = os.path.join(tmp, "fakebin", "gcloud")
            with open(gc_path, encoding="utf-8") as fh:
                gc_body = fh.read()
            gc_body = gc_body.replace(
                'elif "describe" in args:',
                'elif "writerIdentity" in line:\n    print("serviceAccount:sink-123@gcp-sa-logging.iam.gserviceaccount.com")\nelif "describe" in args:',
            )
            with open(gc_path, "w", encoding="utf-8") as fh:
                fh.write(gc_body)
            # Hub dataset already exists in US; spoke dataset does not exist yet.
            bq_path = os.path.join(tmp, "fakebin", "bq")
            with open(bq_path, "w", encoding="utf-8") as fh:
                fh.write("#!/usr/bin/env python3\n"
                         "import json, os, sys\n"
                         "args = sys.argv[1:]\n"
                         "with open(os.environ['FAKE_GCLOUD_LOG'], 'a', encoding='utf-8') as f:\n"
                         "    f.write('bq ' + ' '.join(args) + '\\n')\n"
                         "if 'show' in args:\n"
                         "    if any('demo-hub-proj:ge_analytics' in a for a in args):\n"
                         "        print(json.dumps({'location': 'US', 'access': []}))\n"
                         "        sys.exit(0)\n"
                         "    sys.exit(1)\n"
                         "sys.exit(0)\n")
            os.chmod(bq_path, 0o755)
            cfg = os.path.join(tmp, "deploy.env")
            with open(cfg, "w", encoding="utf-8") as fh:
                fh.write('PROJECT_ID="demo-hub-proj"\nADMIN_EMAILS="admin@example.org"\n'
                         'SCAN_PROJECTS="demo-spoke-a"\nENABLE_DATA_ACCESS_LOGS="false"\n'
                         'ENABLE_GE_OBSERVABILITY="false"\nREALTIME_ENFORCEMENT="false"\n'
                         'MAINTENANCE_SCHEDULE="off"\nENFORCEMENT_SCHEDULE="off"\n'
                         'IMAGE="asia-northeast3-docker.pkg.dev/demo-hub-proj/acp/acp:test"\n'
                         'PUBLIC_BASE_URL="https://dashboard.invalid"\n')
            res = run_script(os.path.join(ROOT, "scripts/deploy.sh"), ["--config", cfg], env)
            calls = read_log(log).splitlines()
        self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        self.assertIn("existing hub audit dataset demo-hub-proj:ge_analytics is in US", res.stdout)
        self.assertIn("Audit dataset : demo-hub-proj:ge_analytics (US,", res.stdout)
        self.assertTrue(any("bq --project_id demo-spoke-a --location US mk --dataset" in c for c in calls), calls)

    def test_custom_role_binding_retries_on_propagation_and_detects_30_day_deleted_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "attempts.txt")
            gcloud = os.path.join(tmp, "gcloud")
            with open(gcloud, "w", encoding="utf-8") as fh:
                fh.write("#!/usr/bin/env python3\n"
                         "import os, sys\n"
                         "args = sys.argv[1:]\n"
                         "mode = os.environ['TEST_MODE']\n"
                         "if args[:3] == ['iam', 'roles', 'undelete']:\n"
                         "    sys.exit(1)\n"
                         "if args[:3] == ['iam', 'roles', 'describe']:\n"
                         "    if mode == 'deleted':\n"
                         "        print('True')\n"
                         "        sys.exit(0)\n"
                         "    sys.exit(1)\n"
                         "if args[:3] == ['iam', 'roles', 'create']:\n"
                         "    sys.exit(0)\n"
                         "if args[:2] == ['projects', 'add-iam-policy-binding']:\n"
                         "    p = os.environ['ATTEMPT_FILE']\n"
                         "    n = int(open(p).read()) if os.path.exists(p) else 0\n"
                         "    open(p, 'w').write(str(n + 1))\n"
                         "    if n == 0:\n"
                         "        sys.stderr.write('ERROR: INVALID_ARGUMENT: Role projects/demo/roles/R does not exist\\n')\n"
                         "        sys.exit(1)\n"
                         "    sys.exit(0)\n"
                         "sys.exit(0)\n")
            os.chmod(gcloud, 0o755)
            sleep_stub = os.path.join(tmp, "sleep")
            with open(sleep_stub, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\nexit 0\n")
            os.chmod(sleep_stub, 0o755)

            env = dict(os.environ, PATH=f"{tmp}:{os.environ.get('PATH', '')}",
                       ATTEMPT_FILE=state, TEST_MODE="propagate")
            cmd = 'set -euo pipefail; source "%s"; grant_license_reader demo-proj %s %s %s' % (
                os.path.join(ROOT, "scripts/lib/common.sh"), HUB_SA, LICENSE_ROLE_ID, license_perms())
            res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env, check=False)
            self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
            self.assertIn("granted projects/demo-proj/roles/%s" % LICENSE_ROLE_ID, res.stdout)
            with open(state, encoding="utf-8") as fh:
                self.assertEqual(fh.read().strip(), "2")

            env_del = dict(env, TEST_MODE="deleted")
            res_del = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env_del, check=False)
            self.assertEqual(res_del.returncode, 0)
            self.assertIn("30-day deletion window", res_del.stderr)

    def test_install_sh_atomic_reclone_preserves_env_on_clone_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = os.path.join(tmp, "home", "ai-control-plane")
            os.makedirs(install_dir, exist_ok=True)
            snap = os.path.join(install_dir, "deploy.demo-proj.env")
            with open(snap, "w", encoding="utf-8") as fh:
                fh.write('PROJECT_ID="demo-proj"\nREGION="us-central1"\n')
            standalone = os.path.join(tmp, "streamed_install.sh")
            with open(os.path.join(ROOT, "scripts/install.sh"), "rb") as sf, open(standalone, "wb") as df:
                df.write(sf.read())
            os.chmod(standalone, 0o755)
            env = dict(os.environ, HOME=os.path.join(tmp, "home"),
                       ACP_REPO_URL=os.path.join(tmp, "nonexistent-repo.git"),
                       ACP_INSTALL_DIR=install_dir)
            res = subprocess.run(["bash", standalone, "--project", "demo-proj", "--dry-run"],
                                 capture_output=True, text=True, env=env, check=False)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("kept untouched", res.stderr)
            self.assertTrue(os.path.exists(snap))
            with open(snap, encoding="utf-8") as fh:
                self.assertIn('REGION="us-central1"', fh.read())

    def test_teardown_dry_run_delete_roles_includes_ge_feature_manager_and_handles_bq_preamble(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = fake_env(
                tmp,
                FAKE_DATASET='Welcome to BigQuery!\n{"labels": {"managed-by": "ai-control-plane"}}',
            )
            dry = run_script(os.path.join(ROOT, "scripts/teardown.sh"),
                             ["--project", "demo-hub-proj", "--dry-run", "--delete-roles"], env)
            self.assertEqual(dry.returncode, 0, msg=(dry.stdout, dry.stderr))
            self.assertIn("CustomAiControlPlaneGeFeatureManager", dry.stdout)

            live = run_script(os.path.join(ROOT, "scripts/teardown.sh"),
                              ["--project", "demo-hub-proj", "--yes", "--delete-data"], env)
            calls = read_log(log).splitlines()
        self.assertEqual(live.returncode, 0, msg=(live.stdout, live.stderr))
        self.assertIn("deleted dataset demo-hub-proj:telemetry_routed", live.stdout)
        self.assertTrue(any("bq --project_id demo-hub-proj rm -r -f -d demo-hub-proj:telemetry_routed" in c for c in calls))

    def test_readmes_have_zero_broken_anchors_and_document_ge_feature_manager(self):
        for fname in ("README.md", "README.en.md"):
            text = read(fname)
            headings, seen, in_code = set(), {}, False
            for line in text.splitlines():
                if line.startswith("```"):
                    in_code = not in_code
                    continue
                if in_code:
                    continue
                m = re.match(r"^#{1,6}\s+(.*)$", line)
                if m:
                    slug = re.sub(r"[^\w\s-]", "", m.group(1).strip().lower(), flags=re.UNICODE).replace(" ", "-")
                    count = seen.get(slug, 0)
                    seen[slug] = count + 1
                    headings.add(f"{slug}-{count}" if count else slug)
            broken = [m.group(1) for m in re.finditer(r"\]\(#([^)]+)\)", text) if m.group(1) not in headings]
            self.assertEqual(broken, [], f"Broken anchors in {fname}: {broken}")
            self.assertIn("CustomAiControlPlaneGeFeatureManager", text, fname)
            self.assertGreaterEqual(text.count("CustomAiControlPlaneGeFeatureManager"), 5, fname)

    def test_probe_iap_bridge_ready_and_retry_quiet(self):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # pylint: disable=invalid-name
                if self.path == "/broken":
                    self.send_response(502)
                    self.send_header("x-goog-iap-generated-response", "true")
                    self.end_headers()
                    self.wfile.write(b"Empty Google Account OAuth client ID(s)/secret(s).")
                else:
                    self.send_response(302)
                    self.send_header("Location", "https://accounts.example.org/o/oauth2/v2/auth")
                    self.end_headers()

            def log_message(self, *args):  # pylint: disable=arguments-differ
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                sleep_stub = os.path.join(tmp, "sleep")
                with open(sleep_stub, "w", encoding="utf-8") as fh:
                    fh.write("#!/bin/sh\nexit 0\n")
                os.chmod(sleep_stub, 0o755)
                env = dict(os.environ, PATH=f"{tmp}:{os.environ.get('PATH', '')}")
                common = os.path.join(ROOT, "scripts/lib/common.sh")

                bad = subprocess.run(
                    ["bash", "-c", f'set -euo pipefail; source "{common}"; probe_iap_bridge_ready "http://127.0.0.1:{port}/broken"'],
                    capture_output=True, text=True, env=env, check=False,
                )
                self.assertNotEqual(bad.returncode, 0)

                good = subprocess.run(
                    ["bash", "-c", f'set -euo pipefail; source "{common}"; probe_iap_bridge_ready "http://127.0.0.1:{port}/ok"'],
                    capture_output=True, text=True, env=env, check=False,
                )
                self.assertEqual(good.returncode, 0, msg=(good.stdout, good.stderr))

                # retry_quiet suppresses transient stderr when a later attempt succeeds, and prints it if all attempts fail.
                flag = os.path.join(tmp, "once")
                flaky = os.path.join(tmp, "flaky")
                with open(flaky, "w", encoding="utf-8") as fh:
                    fh.write(f'#!/bin/sh\nif [ ! -f "{flag}" ]; then touch "{flag}"; echo "ERROR: transient NOT_FOUND" >&2; exit 1; fi\nexit 0\n')
                os.chmod(flaky, 0o755)
                ok_rq = subprocess.run(
                    ["bash", "-c", f'set -euo pipefail; source "{common}"; retry_quiet "{flaky}"'],
                    capture_output=True, text=True, env=env, check=False,
                )
                self.assertEqual(ok_rq.returncode, 0)
                self.assertEqual(ok_rq.stderr, "")

                always_fail = os.path.join(tmp, "always_fail")
                with open(always_fail, "w", encoding="utf-8") as fh:
                    fh.write('#!/bin/sh\necho "ERROR: permanent failure" >&2\nexit 1\n')
                os.chmod(always_fail, 0o755)
                fail_rq = subprocess.run(
                    ["bash", "-c", f'set -euo pipefail; source "{common}"; retry_quiet "{always_fail}"'],
                    capture_output=True, text=True, env=env, check=False,
                )
                self.assertNotEqual(fail_rq.returncode, 0)
                self.assertIn("ERROR: permanent failure", fail_rq.stderr)
        finally:
            server.shutdown()
            server.server_close()


FAKE_GIT_TEMPLATE = """#!/usr/bin/env python3
import json, os, sys
with open({cfg_path!r}, encoding="utf-8") as fh:
    cfg = json.load(fh)
args = sys.argv[1:]
while args and args[0] in ("-C", "-c"):
    args = args[2:]
sha = cfg.get("sha", "a" * 40)
if args[:2] == ["rev-parse", "--show-toplevel"]:
    print(cfg["root"])
elif args[:2] == ["rev-parse", "HEAD"]:
    print(sha)
elif args[:2] == ["status", "--porcelain"]:
    if cfg.get("status"):
        print(cfg["status"])
elif args[:3] == ["remote", "get-url", "origin"]:
    print(cfg.get("origin", "https://example.org/acme/app.git"))
elif args[:1] == ["ls-remote"]:
    # git_build_source scrubs the environment so local credentials never leak to anonymous probes.
    if os.environ.get("GIT_CONFIG_NOSYSTEM") != "1" or os.environ.get("GIT_TERMINAL_PROMPT") != "0":
        sys.exit(99)
    if cfg.get("private"):
        sys.exit(128)
    print(cfg.get("ls_remote", f"{{sha}}\\trefs/heads/main"))
elif args[:1] == ["for-each-ref"]:
    if cfg.get("tracked"):
        print(cfg["tracked"])
sys.exit(0)
"""


class BuildSourceAndVpcScTest(unittest.TestCase):
    """Cloud Build Git-source mode, --worker-pool, --image, VPC-SC fast-fail, and post-deploy runtime checks."""

    BASE_CFG = (
        'PROJECT_ID="demo-hub-proj"\nADMIN_EMAILS="admin@example.org"\nCREATE_LOG_SINK="false"\n'
        'ENABLE_DATA_ACCESS_LOGS="false"\nENABLE_GE_OBSERVABILITY="false"\n'
        'PUBLIC_BASE_URL="https://dashboard.invalid"\n'
    )

    def _with_fake_git(self, tmp, git_cfg=None, **extra):
        env, log = fake_env(tmp, ACP_VPCSC_POLL_SECONDS="0", **extra)
        cfg_path = os.path.join(tmp, "fake_git.json")
        state = {"root": ROOT, **(git_cfg or {})}
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        git_path = os.path.join(tmp, "fakebin", "git")
        with open(git_path, "w", encoding="utf-8") as fh:
            fh.write(FAKE_GIT_TEMPLATE.format(cfg_path=cfg_path))
        os.chmod(git_path, 0o755)
        return env, log, cfg_path

    def _run_deploy(self, tmp, cli_args=(), cfg_extra="", git_cfg=None, **extra):
        env, log, _ = self._with_fake_git(tmp, git_cfg=git_cfg, **extra)
        cfg = os.path.join(tmp, "deploy.env")
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write(self.BASE_CFG + cfg_extra)
        res = run_script(os.path.join(ROOT, "scripts/deploy.sh"), ["--config", cfg, *cli_args], env)
        return res, read_log(log).splitlines()

    def test_git_build_source_strips_credentials_and_verifies_public_commit(self):
        common = os.path.join(ROOT, "scripts/lib/common.sh")
        sha = "0123456789abcdef0123456789abcdef01234567"
        tok = "test_secret_token_value"
        with tempfile.TemporaryDirectory() as tmp:
            state = {"root": ROOT, "sha": sha, "origin": "https://" + "octocat:" + tok + "@example.org/acme/app.git"}
            env, _, cfg_path = self._with_fake_git(tmp, git_cfg=state)

            def probe():
                with open(cfg_path, "w", encoding="utf-8") as fh:
                    json.dump(state, fh)
                return subprocess.run(
                    ["bash", "-c", f'set -euo pipefail; source "{common}"; git_build_source "{ROOT}"'],
                    capture_output=True, text=True, env=env, check=False)

            # Credentials in origin URL are stripped and never printed.
            res = probe()
            self.assertEqual(res.returncode, 0)
            self.assertEqual(res.stdout.strip(), f"https://example.org/acme/app.git {sha}")
            self.assertNotIn(tok, res.stdout + res.stderr)

            # SSH origin converts to https.
            state["origin"] = "git@example.org:acme/app.git"
            res = probe()
            self.assertEqual(res.stdout.strip(), f"https://example.org/acme/app.git {sha}")

            # Local edits to image sources skip Git source.
            state["status"] = " M src/controlplane/app.py"
            res = probe()
            self.assertEqual(res.stdout.strip(), "")
            self.assertIn("local changes", res.stderr)

            # Unpushed commit skips Git source.
            state["status"] = ""
            state["ls_remote"] = ("b" * 40) + "\trefs/heads/main"
            res = probe()
            self.assertEqual(res.stdout.strip(), "")
            self.assertIn("not pushed", res.stderr)

            # Private repository skips Git source.
            state["private"] = True
            res = probe()
            self.assertEqual(res.stdout.strip(), "")
            self.assertIn("anonymously", res.stderr)

    def test_deploy_uses_git_source_without_storage_upload_or_bucket_grant(self):
        sha = "1234567890abcdef1234567890abcdef12345678"
        with tempfile.TemporaryDirectory() as tmp:
            res, calls = self._run_deploy(tmp, BUILD_SOURCE="auto", git_cfg={"sha": sha})
        self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        submits = [c for c in calls if c.startswith("builds submit ")]
        self.assertEqual(len(submits), 1, submits)
        self.assertIn("--no-source", submits[0])
        self.assertNotIn("--gcs-source-staging-dir", submits[0])
        configs = [c for c in calls if c.startswith("CONFIG ")]
        self.assertEqual(len(configs), 1)
        for token in ("gcr.io/cloud-builders/git", "'init'", "'fetch'", sha, "dir: 'src'",
                      "gcr.io/cloud-builders/docker", "CLOUD_LOGGING_ONLY"):
            self.assertIn(token, configs[0])
        # Build SA never received temporary bucket access when nothing was uploaded.
        self.assertFalse([c for c in calls if "ai-control-plane-build@" in c and "roles/storage.objectAdmin" in c])
        self.assertIn("(Cloud Build, git source)", res.stdout)

    def test_deploy_upload_source_grants_and_cleans_up_bucket_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            res, calls = self._run_deploy(tmp, BUILD_SOURCE="upload")
        self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        submits = [c for c in calls if c.startswith("builds submit ")]
        self.assertEqual(len(submits), 1, submits)
        self.assertIn("--gcs-source-staging-dir", submits[0])
        self.assertTrue([c for c in calls if "storage buckets add-iam-policy-binding" in c
                         and "ai-control-plane-build@" in c and "roles/storage.objectAdmin" in c])
        self.assertTrue([c for c in calls if "storage rm --recursive" in c and "_build/**" in c])
        self.assertTrue([c for c in calls if "storage buckets remove-iam-policy-binding" in c
                         and "ai-control-plane-build@" in c and "roles/storage.objectAdmin" in c])
        self.assertIn("(Cloud Build, upload source)", res.stdout)

    def test_deploy_worker_pool_expands_bare_name_and_uses_upload_in_auto_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            res, calls = self._run_deploy(tmp, cli_args=("--worker-pool", "corp-pool"), BUILD_SOURCE="auto")
        self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        submits = [c for c in calls if c.startswith("builds submit ")]
        self.assertEqual(len(submits), 1, submits)
        self.assertIn("--worker-pool projects/demo-hub-proj/locations/asia-northeast3/workerPools/corp-pool", submits[0])
        self.assertIn("--region asia-northeast3", submits[0])
        self.assertIn("--gcs-source-staging-dir", submits[0])

        with tempfile.TemporaryDirectory() as tmp:
            full = "projects/shared-p/locations/us-central1/workerPools/net-pool"
            res, calls = self._run_deploy(tmp, cli_args=("--worker-pool", full), BUILD_SOURCE="auto")
        self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        submits = [c for c in calls if c.startswith("builds submit ")]
        self.assertIn("--region us-central1", submits[0])
        self.assertIn(f"--worker-pool {full}", submits[0])

        with tempfile.TemporaryDirectory() as tmp:
            res, _ = self._run_deploy(tmp, cli_args=("--worker-pool", "bad/pool/path"), BUILD_SOURCE="auto")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--worker-pool", res.stderr)

    def test_deploy_image_flag_skips_cloud_build(self):
        img = "asia-northeast3-docker.pkg.dev/demo-hub-proj/ai-control-plane/ai-control-plane:v1"
        with tempfile.TemporaryDirectory() as tmp:
            res, calls = self._run_deploy(tmp, cli_args=("--image", img), BUILD_SOURCE="auto")
        self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
        self.assertEqual([c for c in calls if c.startswith("builds submit ")], [])
        self.assertIn(f"Container     : {img} (prebuilt)", res.stdout)

    def test_vpcsc_build_denial_fails_fast_with_ingress_rule_and_no_retries(self):
        deny_msg = (
            "BUILD FAILURE: fetching object from gcs: HTTP 403 "
            "<Error><Code>SecurityPolicyViolated</Code><Message>Request violates VPC Service Controls.</Message></Error>"
        )
        with tempfile.TemporaryDirectory() as tmp:
            res, calls = self._run_deploy(
                tmp, BUILD_SOURCE="upload",
                FAKE_FAIL_RE=r"^builds submit ",
                FAKE_SAY=json.dumps({r"^builds submit ": deny_msg}),
            )
        self.assertNotEqual(res.returncode, 0)
        # Never retries 3x or falls back to global Cloud Build on a VPC-SC denial.
        submits = [c for c in calls if c.startswith("builds submit ")]
        self.assertEqual(len(submits), 1, submits)
        self.assertIn("serviceAccount:ai-control-plane-build@demo-hub-proj.iam.gserviceaccount.com", res.stderr)
        self.assertIn("serviceName: storage.googleapis.com", res.stderr)
        self.assertIn("- projects/123456789012", res.stderr)
        self.assertIn("--worker-pool", res.stderr)
        self.assertIn("--image", res.stderr)

    def test_vpcsc_runtime_check_reports_denials_with_all_restricted_runtime_services(self):
        common = os.path.join(ROOT, "scripts/lib/common.sh")
        sa = "ai-control-plane-sa@demo-hub-proj.iam.gserviceaccount.com"
        restricted = "storage.googleapis.com,bigquery.googleapis.com,discoveryengine.googleapis.com,aiplatform.googleapis.com,sqladmin.googleapis.com"
        with tempfile.TemporaryDirectory() as tmp:
            # 1. Denial seen -> prints ingress rule covering every restricted service the dashboard calls (not sqladmin).
            env, _ = fake_env(tmp, ACP_VPCSC_POLL_SECONDS="0",
                              FAKE_SAY=json.dumps({r"^logging read ": "storage.googleapis.com"}))
            cmd = (f'set -euo pipefail; source "{common}"; VPCSC_RESTRICTED="{restricted}"; '
                   f'vpcsc_runtime_check demo-hub-proj 123456789012 "{sa}" 15')
            res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env, check=False)
            self.assertEqual(res.returncode, 0)
            for expected in (f"serviceAccount:{sa}", "serviceName: storage.googleapis.com",
                             "serviceName: bigquery.googleapis.com", "serviceName: discoveryengine.googleapis.com",
                             "serviceName: aiplatform.googleapis.com", "- projects/123456789012"):
                self.assertIn(expected, res.stderr)
            self.assertNotIn("sqladmin.googleapis.com", res.stderr)

            # 2. No denial -> prints clear confirmation.
            env, _ = fake_env(tmp, ACP_VPCSC_POLL_SECONDS="0")
            res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env, check=False)
            self.assertEqual(res.returncode, 0)
            self.assertIn("no denial", res.stdout)

            # 3. Unreadable audit logs -> warns instead of falsely claiming no denial.
            env, _ = fake_env(tmp, ACP_VPCSC_POLL_SECONDS="0", FAKE_FAIL="logging read ")
            res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env, check=False)
            self.assertEqual(res.returncode, 0)
            self.assertIn("could not read the audit logs", res.stderr)
            self.assertNotIn("no denial", res.stdout)

    def test_installer_saves_worker_pool_but_keeps_image_one_run_only(self):
        img = "asia-northeast3-docker.pkg.dev/demo-hub-proj/ai-control-plane/ai-control-plane:v9"
        with tempfile.TemporaryDirectory() as tmp:
            copy_scripts(tmp, *SCRIPT_FILES)
            env, _ = fake_env(tmp, ACP_SAVE_ENV_ON_DRY_RUN="1")
            res = run_script(
                os.path.join(tmp, "scripts/install.sh"),
                ["--project", "demo-hub-proj", "--worker-pool", "corp-pool", "--image", img, "--dry-run"],
                env, cwd=tmp,
            )
            self.assertEqual(res.returncode, 0, msg=(res.stdout, res.stderr))
            self.assertIn(f"using prebuilt image {img}", res.stdout)
            with open(os.path.join(tmp, "deploy.demo-hub-proj.env"), encoding="utf-8") as fh:
                saved = fh.read()
            self.assertIn('CLOUD_BUILD_WORKER_POOL="corp-pool"', saved)
            self.assertNotIn("IMAGE=", saved)


if __name__ == "__main__":
    unittest.main()



