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
import sys
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


if __name__ == "__main__":
    unittest.main()
