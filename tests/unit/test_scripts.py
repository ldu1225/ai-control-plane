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
        for key in ("DISABLED_MODULES", "ENABLE_GE_OBSERVABILITY", "GE_PROMPT_LOGGING"):
            self.assertRegex(example, r"(?m)^%s=" % key)


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


if __name__ == "__main__":
    unittest.main()

