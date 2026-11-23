"""Prepublish scan: public phrases may contain a listed name; nothing else changes."""

import importlib.util
import os
import unittest

import support  # noqa: F401  (puts src/ on sys.path)

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
SPEC = importlib.util.spec_from_file_location("prepublish_scan", os.path.join(ROOT, "scripts", "prepublish_scan.py"))
scan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scan)

URL = "github.com/octo-owner/ai-control-plane"


class PublicPhraseTest(unittest.TestCase):
    def test_listed_name_inside_the_public_url_passes(self):
        text = "git clone https://%s.git\n" % URL
        problems, _ = scan.check_text("README.md", text, terms={"octo-owner"}, values={"octo-owner"},
                                      allowed={URL})
        self.assertEqual(problems, [])

    def test_listed_name_elsewhere_still_fails(self):
        text = "git clone https://%s.git\nmaintained by octo-owner\n" % URL
        problems, _ = scan.check_text("README.md", text, terms={"octo-owner"}, values={"octo-owner"},
                                      allowed={URL})
        self.assertEqual(sorted(p.split(": ", 1)[1].split(" ")[0] for p in problems), ["listed", "value"])

    def test_without_the_phrase_the_url_fails(self):
        problems, _ = scan.check_text("README.md", "https://%s" % URL, terms={"octo-owner"})
        self.assertEqual(len(problems), 1)

    def test_public_phrases_do_not_hide_secrets_or_addresses(self):
        address = "someone@" + "corp-mail.test"  # built here so the scan does not flag this file
        text = "https://%s %s ghp_%s" % (URL, address, "a" * 36)
        problems, _ = scan.check_text("README.md", text, allowed={URL, address})
        labels = " ".join(problems)
        self.assertIn("GitHub token", labels)
        self.assertIn("e-mail address", labels)


class LocalValueTest(unittest.TestCase):
    """Values from deploy.env that every deployment shares (option values, regions) are not identifiers."""

    def test_option_values_and_regions_are_not_identifiers(self):
        for value in ("iam_deny", "iam_allow_swap", "notify_only", "telemetry_routed", "asia-northeast3",
                      "europe-west1", "us-central1", "projects", "scope", "google_oauth"):
            self.assertFalse(scan._is_identifier(value), value)

    def test_deployment_identifiers_still_count(self):
        for value in ("acme-prod-ai-123", "acme-settings-bucket", "0a1b2c-3d4e5f-6a7b8c", "123456789012",
                      "acme_audit_logs"):
            self.assertTrue(scan._is_identifier(value), value)

    def test_documented_option_value_does_not_fail_the_scan(self):
        values = {v for v in ("iam_deny", "asia-northeast3", "acme-prod-ai-123") if scan._is_identifier(v)}
        text = 'ENFORCEMENT_BACKEND="iam_deny"\nREGION="asia-northeast3"\n'
        problems, _ = scan.check_text("deploy.env.example", text, values=values)
        self.assertEqual(problems, [])
        problems, _ = scan.check_text("README.md", "PROJECT_ID=acme-prod-ai-123", values=values)
        self.assertEqual(len(problems), 1)


if __name__ == "__main__":
    unittest.main()
