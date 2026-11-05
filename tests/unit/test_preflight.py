"""Organization policy preflight (scripts/lib/common.sh preflight_org_policies) against a fake gcloud.

Each case gives the effective policies a customer organization could enforce and checks what the installer does:
adapt automatically (ORGPOL_* variables) or stop before creating anything, with remediation.
"""

import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()

FAKE_GCLOUD = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    policies = json.loads(os.environ.get("FAKE_POLICIES", "{}"))
    if args[:2] == ["services", "enable"]:
        sys.exit(0)
    if args[:2] == ["storage", "buckets"]:
        err = os.environ.get("FAKE_STORAGE_ERROR", "")
        if err:
            sys.stderr.write(err)
            sys.exit(1)
        sys.exit(0)
    if "describe" in args and "--effective" in args:
        constraint = next(a for a in args if a.startswith("constraints/"))[len("constraints/"):]
        if os.environ.get("FAKE_DENY_ALL"):
            sys.stderr.write(os.environ["FAKE_DENY_ALL"])
            sys.exit(1)
        if constraint in policies:
            print(json.dumps(policies[constraint]))
        else:
            print(json.dumps({"constraint": "constraints/" + constraint}))
        sys.exit(0)
    sys.exit(0)
''')


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self.tmp.name, "gcloud")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(FAKE_GCLOUD)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def tearDown(self):
        self.tmp.cleanup()

    def run_preflight(self, policies=None, region="asia-northeast3", extra_env=None, docker=False):
        env = dict(os.environ, PATH=self.tmp.name + os.pathsep + os.environ.get("PATH", ""),
                   FAKE_POLICIES=json.dumps(policies or {}))
        if not docker:
            # Hide any local docker so the Cloud Build pool case is deterministic.
            env["PATH"] = self.tmp.name + os.pathsep + "/usr/bin:/bin"
        env.update(extra_env or {})
        script = textwrap.dedent('''\
            set -euo pipefail
            source "%s/scripts/lib/common.sh"
            preflight_org_policies demo-project "%s" "%s"
            echo "RESULT DRS=$ORGPOL_DRS INVOKER=$ORGPOL_REQUIRE_INVOKER_IAM BINAUTHZ=$ORGPOL_BINAUTHZ VPC=$ORGPOL_VPC_EGRESS POOL=$ORGPOL_NO_DEFAULT_POOL"
        ''' % (ROOT, region, region))
        res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, check=False)
        return res.returncode, res.stdout, res.stderr

    def test_unrestricted_org_passes(self):
        rc, out, err = self.run_preflight({})
        self.assertEqual(rc, 0, err)
        self.assertIn("RESULT DRS=0 INVOKER=0 BINAUTHZ= VPC=0 POOL=0", out)
        self.assertIn("gcp.resourceLocations: 제한 없음", out)

    def test_typical_korean_enterprise_policies_adapt_automatically(self):
        policies = {
            "gcp.resourceLocations": {"listPolicy": {"allowedValues": ["in:asia-northeast3-locations"]}},
            "iam.allowedPolicyMemberDomains": {"listPolicy": {"allowedValues": ["C0abc123"]}},
            "storage.publicAccessPrevention": {"booleanPolicy": {"enforced": True}},
            "storage.uniformBucketLevelAccess": {"booleanPolicy": {"enforced": True}},
            "iam.disableServiceAccountKeyCreation": {"booleanPolicy": {"enforced": True}},
            "iam.automaticIamGrantsForDefaultServiceAccounts": {"booleanPolicy": {"enforced": True}},
        }
        rc, out, err = self.run_preflight(policies)
        self.assertEqual(rc, 0, err)
        self.assertIn("RESULT DRS=1", out)
        self.assertIn("asia-northeast3 허용", out)
        self.assertIn("도메인 제한 공유", out)
        self.assertIn("storage.publicAccessPrevention", out)

    def test_region_outside_allowed_locations_stops_before_any_change(self):
        policies = {"gcp.resourceLocations": {"listPolicy": {"allowedValues": ["in:us-locations"]}}}
        rc, out, err = self.run_preflight(policies, region="asia-northeast3")
        self.assertEqual(rc, 1)
        self.assertIn("[중단] gcp.resourceLocations", err)
        self.assertIn("--region", err)
        self.assertIn("대시보드 리소스는 아직 하나도 만들지 않았습니다", err)
        self.assertNotIn("RESULT", out)

    def test_expanded_effective_location_values_are_resolved(self):
        # What the effective-policy API really returns for "in:us-locations" (members expanded, no "in:" prefix).
        expanded = ["us-west3-locations", "us-locations", "us-west3-b", "us", "us-west2-b", "us-central6-locations",
                    "us-west4-b", "nam3"]
        rc, out, err = self.run_preflight({"gcp.resourceLocations": {"listPolicy": {"allowedValues": expanded}}})
        self.assertEqual(rc, 1)
        self.assertIn("[중단] gcp.resourceLocations", err)
        self.assertNotIn("asia-northeast3 허용", out)
        seoul = ["asia-northeast3-locations", "asia-northeast3-a", "asia-northeast3", "asia1"]
        rc, out, err = self.run_preflight({"gcp.resourceLocations": {"listPolicy": {"allowedValues": seoul}}})
        self.assertEqual(rc, 0, err)
        self.assertIn("asia-northeast3 허용", out)
        self.assertNotIn("[주의] gcp.resourceLocations", err)

    def test_continent_group_allows_region(self):
        policies = {"gcp.resourceLocations": {"listPolicy": {"allowedValues": ["in:asia-locations"]}}}
        rc, out, err = self.run_preflight(policies, region="asia-northeast3")
        self.assertEqual(rc, 0, err)

    def test_unknown_location_group_warns_instead_of_stopping(self):
        policies = {"gcp.resourceLocations": {"listPolicy": {"allowedValues": ["in:kr-locations"]}}}
        rc, out, err = self.run_preflight(policies)
        self.assertEqual(rc, 0, err)
        self.assertIn("[주의] gcp.resourceLocations", err)

    def test_internal_only_ingress_is_a_blocker(self):
        policies = {"run.allowedIngress": {"listPolicy": {"allowedValues": ["internal", "internal-and-cloud-load-balancing"]}}}
        rc, out, err = self.run_preflight(policies)
        self.assertEqual(rc, 1)
        self.assertIn("[중단] run.allowedIngress", err)

    def test_cmek_requirement_is_a_blocker(self):
        policies = {"gcp.restrictNonCmekServices": {"listPolicy": {"deniedValues": ["bigquery.googleapis.com", "storage.googleapis.com"]}}}
        rc, out, err = self.run_preflight(policies)
        self.assertEqual(rc, 1)
        self.assertIn("CMEK", err)
        self.assertIn("bigquery.googleapis.com", err)

    def test_restricted_services_split_required_and_optional(self):
        policies = {"serviceuser.services": {"listPolicy": {"deniedValues": ["discoveryengine.googleapis.com"]}}}
        rc, out, err = self.run_preflight(policies)
        self.assertEqual(rc, 0, err)
        self.assertIn("discoveryengine.googleapis.com 금지", err)
        policies = {"serviceuser.services": {"listPolicy": {"deniedValues": ["run.googleapis.com"]}}}
        rc, out, err = self.run_preflight(policies)
        self.assertEqual(rc, 1)
        self.assertIn("run.googleapis.com", err)

    def test_vpc_egress_needs_a_subnet(self):
        policies = {"run.allowedVPCEgress": {"listPolicy": {"allowedValues": ["all-traffic"]}}}
        rc, out, err = self.run_preflight(policies)
        self.assertEqual(rc, 1)
        self.assertIn("--vpc-network", err)
        rc, out, err = self.run_preflight(policies, extra_env={"VPC_NETWORK": "corp-vpc", "VPC_SUBNET": "run-subnet"})
        self.assertEqual(rc, 0, err)
        self.assertIn("VPC=1", out)

    def test_binary_authorization_and_build_pool_adapt(self):
        policies = {"run.allowedBinaryAuthorizationPolicies": {"listPolicy": {"allowedValues": ["default"]}},
                    "cloudbuild.allowedWorkerPools": {"listPolicy": {"allowedValues": ["projects/p/locations/asia-northeast3/workerPools/pool"]}}}
        rc, out, err = self.run_preflight(policies, docker=False)
        self.assertEqual(rc, 1)  # no Docker here: Cloud Shell is required
        self.assertIn("Cloud Shell", err)
        rc, out, err = self.run_preflight(policies, extra_env={"IMAGE": "asia-northeast3-docker.pkg.dev/p/r/i:1"})
        self.assertEqual(rc, 0, err)
        self.assertIn("BINAUTHZ=default", out)
        self.assertIn("POOL=0", out)  # a prebuilt image needs no build

    def test_vpc_service_controls_is_detected(self):
        rc, out, err = self.run_preflight({}, extra_env={
            "FAKE_STORAGE_ERROR": "ERROR: Request is prohibited by organization's policy. vpcServiceControlsUniqueIdentifier: X"})
        self.assertEqual(rc, 1)
        self.assertIn("VPC Service Controls", err)

    def test_unreadable_policies_skip_with_a_hint(self):
        rc, out, err = self.run_preflight({}, extra_env={
            "FAKE_DENY_ALL": "ERROR: (gcloud) PERMISSION_DENIED: The caller does not have permission"})
        self.assertEqual(rc, 0, err)
        self.assertIn("[건너뜀]", err)
        self.assertIn("roles/orgpolicy.policyViewer", err)

    def test_strict_mode_can_be_relaxed(self):
        policies = {"run.allowedIngress": {"listPolicy": {"allowedValues": ["internal"]}}}
        rc, out, err = self.run_preflight(policies, extra_env={"PREFLIGHT_STRICT": "false"})
        self.assertEqual(rc, 0, err)
        self.assertIn("PREFLIGHT_STRICT=false", err)

    def test_deploy_runs_the_preflight_before_creating_resources(self):
        text = read("scripts", "deploy.sh")
        self.assertLess(text.index('preflight_org_policies "$PROJECT_ID"'), text.index('log "1/8 '))
        self.assertIn("--skip-preflight-blockers", text)
        install = read("scripts", "install.sh")
        self.assertIn("--skip-preflight-blockers", install)
        self.assertIn("--vpc-network", install)


class AdminPasswordDeployTest(unittest.TestCase):
    def test_no_shared_default_admin_password_anywhere(self):
        for rel in ("scripts/deploy.sh", "deploy.env.example", "src/controlplane/auth.py", "src/web/assets/js/login.js"):
            text = read(rel)
            self.assertNotIn("admin / admin", text, rel)
            self.assertNotIn('or "admin")', text, rel)
            self.assertNotIn("defaultAdminPassword", text, rel)

    def test_deploy_stores_a_random_initial_password_only_for_the_dashboard(self):
        text = read("scripts", "deploy.sh")
        self.assertIn('ADMIN_INITIAL_PASSWORD=${ADMIN_SECRET_NAME}:latest', text)
        self.assertIn("secrets.choice", text)
        bridge = text[text.index('gcloud run deploy "$BRIDGE_NAME"'):]
        self.assertIn('--set-secrets "$SECRETS"', bridge[:600])
        teardown = read("scripts", "teardown.sh")
        self.assertIn('"$ADMIN_SECRET_NAME"', teardown)


if __name__ == "__main__":
    unittest.main()
