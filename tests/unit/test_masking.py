"""PII minimization for viewers and stable user ids."""

import hashlib
import unittest

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane import security


class MaskingTest(unittest.TestCase):
    def test_mask_email(self):
        self.assertEqual(security.mask_email("admin@example.com"), "ad***@example.com")
        self.assertEqual(security.mask_email("ab@example.com"), "a***@example.com")
        self.assertEqual(security.mask_email("a@example.com"), "a***@example.com")
        sa = "runner@demo-project.iam.gserviceaccount.com"
        self.assertEqual(security.mask_email(sa), sa)
        self.assertIsNone(security.mask_email(None))
        self.assertEqual(security.mask_email("not an email"), "not an email")

    def test_mask_emails_inside_text(self):
        text = "Blocked prompt from alice@example.com and bob.smith@corp.example.com today"
        self.assertEqual(security.mask_text_emails(text),
                         "Blocked prompt from al***@example.com and bo***@corp.example.com today")

    def test_mask_ip(self):
        self.assertEqual(security.mask_ip("10.20.30.40"), "10.20.30.x")
        self.assertEqual(security.mask_ip("2001:db8::1"), "2001:0db8:0000:0000:x")
        self.assertEqual(security.mask_ip("not-an-ip"), "not-an-ip")
        self.assertIsNone(security.mask_ip(None))

    def test_mask_for_viewer(self):
        payload = {"items": [{"email": "alice@example.com", "text": "secret prompt", "callerIp": "10.0.0.7",
                              "fileName": "plan.pdf", "restricted": False, "count": 3},
                             {"email": None, "text": None, "fileName": None, "restricted": False}],
                   "principal": "svc@demo-project.iam.gserviceaccount.com", "payload": "{...}"}
        out = security.mask_for_viewer(payload, restrict_keys=("text", "fileName"), null_keys=("payload",))
        first, second = out["items"]
        self.assertEqual(first["email"], "al***@example.com")
        self.assertIsNone(first["text"])
        self.assertIsNone(first["fileName"])
        self.assertTrue(first["restricted"])
        self.assertEqual(first["callerIp"], "10.0.0.x")
        self.assertEqual(first["count"], 3)
        self.assertFalse(second["restricted"])  # nothing was hidden
        self.assertEqual(out["principal"], "svc@demo-project.iam.gserviceaccount.com")
        self.assertIsNone(out["payload"])
        # The input is not modified (cached responses stay unmasked for admins).
        self.assertEqual(payload["items"][0]["text"], "secret prompt")

    def test_principal_classification(self):
        self.assertTrue(security.is_human("alice@example.com"))
        self.assertFalse(security.is_human("svc@p.iam.gserviceaccount.com"))
        self.assertFalse(security.is_human("principal://iam.googleapis.com/x@y"))
        self.assertFalse(security.is_human("system"))


class UidTest(unittest.TestCase):
    def test_uid_is_salted_lowercase_sha256_prefix(self):
        expected = hashlib.sha256(b"alice@example.comcp-uid-v1").hexdigest()[:16]
        self.assertEqual(security.uid_for("Alice@Example.com "), expected)
        self.assertEqual(len(expected), 16)
        self.assertNotEqual(security.uid_for("bob@example.com"), expected)


class LogSafeTest(unittest.TestCase):
    """Upstream error text is logged without quoted literals or e-mail addresses (NFR-06)."""

    def test_literals_and_emails_removed(self):
        from controlplane.gcp import log_safe
        out = log_safe("Syntax error: Unexpected string literal 'resident number 900101-1234567' at [1:20]")
        self.assertEqual(out, "Syntax error: Unexpected string literal '?' at [1:20]")
        out = log_safe('Unrecognized name: x; Did you mean "secret prompt"? owner alice@example.com')
        self.assertNotIn("secret prompt", out)
        self.assertNotIn("alice@example.com", out)
        self.assertIn("<email>", out)

    def test_unterminated_literal_and_length(self):
        from controlplane.gcp import log_safe
        self.assertEqual(log_safe("Unexpected literal 'never closed text"), "Unexpected literal '?'")
        self.assertEqual(len(log_safe("x" * 1000)), 200)
        self.assertEqual(log_safe(None), "")
        self.assertIn("`proj.ds.tbl`", log_safe("Not found: Table `proj.ds.tbl` was not found"))


if __name__ == "__main__":
    unittest.main()
