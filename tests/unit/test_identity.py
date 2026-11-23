"""Principal identities: Google Accounts, workforce identities, service accounts and other principal types."""

import unittest

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane import identity
from controlplane.security import mask_text_emails

POOL_URI = "principal://iam.googleapis.com/locations/global/workforcePools/corp-pool/subject/"
POOL_KEY = "principal://iam.googleapis.com/locations/global/workforcepools/corp-pool/subject/"


class ParseTest(unittest.TestCase):
    def test_google_accounts_use_the_lowercase_email(self):
        for value in ("user:Alice@Example.com", "alice@example.com", "  USER:alice@example.com ",
                      "principal://goog/subject/Alice@Example.com"):
            found = identity.parse(value)
            self.assertEqual(found.kind, identity.GOOGLE_USER, value)
            self.assertEqual(found.key, "alice@example.com", value)
            self.assertTrue(found.is_person)

    def test_workforce_identities_get_a_lowercase_key_and_keep_the_subject_for_display(self):
        raw = "PRINCIPAL://iam.googleapis.com/locations/global/WORKFORCEPOOLS/Corp-Pool/subject/Kim.Lee@example.com"
        found = identity.parse(raw)
        self.assertEqual(found.kind, identity.WORKFORCE)
        self.assertEqual(found.key, POOL_KEY + "kim.lee@example.com")
        self.assertEqual(found.display, "Kim.Lee@example.com")
        self.assertEqual(identity.person_key(raw.lower()), found.key)  # the BigQuery path lower-cases labels
        self.assertTrue(identity.is_person_key(found.key))
        self.assertFalse(identity.is_person_key(POOL_URI + "kim.lee@example.com"))  # not the key form
        self.assertTrue(identity.is_workforce_key(found.key))
        self.assertFalse(identity.is_workforce_key("alice@example.com"))

    def test_service_accounts_and_other_principals_are_not_people(self):
        for value in ("serviceAccount:Robot@proj.iam.gserviceaccount.com", "robot@proj.iam.gserviceaccount.com"):
            found = identity.parse(value)
            self.assertEqual(found.kind, identity.SERVICE_ACCOUNT)
            self.assertEqual(found.key, "robot@proj.iam.gserviceaccount.com")
            self.assertIsNone(identity.person_key(value))
        for value in ("group:team@example.com", "domain:example.com", "deleted:user:old@example.com?uid=1",
                      "principalSet://iam.googleapis.com/locations/global/workforcePools/p/group/g",
                      "principal://iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/p/subject/s",
                      "principal://goog/subject/robot@proj.iam.gserviceaccount.com", "not an email"):
            found = identity.parse(value)
            self.assertEqual(found.kind, identity.OTHER, value)
            self.assertIsNone(identity.person_key(value), value)
        self.assertIsNone(identity.parse(""))
        self.assertIsNone(identity.parse(None))
        self.assertIsNone(identity.parse(42))

    def test_person_keys_are_stable(self):
        self.assertEqual(identity.person_key("user:Bob@Example.com"), "bob@example.com")
        self.assertTrue(identity.is_person_key("bob@example.com"))
        self.assertFalse(identity.is_person_key("Bob@Example.com"))  # not canonical
        self.assertFalse(identity.is_person_key("user:bob@example.com"))
        self.assertFalse(identity.is_person_key("robot@proj.iam.gserviceaccount.com"))


class PolicyFormsTest(unittest.TestCase):
    def test_allow_and_deny_members(self):
        self.assertEqual(identity.allow_member("bob@example.com"), "user:bob@example.com")
        self.assertEqual(identity.deny_principal("bob@example.com"), "principal://goog/subject/bob@example.com")
        logged = POOL_URI + "S-1"
        self.assertEqual(identity.allow_member(logged), logged)  # the original subject case is kept
        self.assertEqual(identity.deny_principal(logged), logged)
        self.assertEqual(identity.deny_principal(identity.person_key(logged)), POOL_URI + "s-1")
        self.assertIsNone(identity.allow_member("robot@proj.iam.gserviceaccount.com"))
        self.assertIsNone(identity.deny_principal("group:team@example.com"))

    def test_deny_form_round_trips_to_the_same_person(self):
        self.assertEqual(identity.person_key(identity.deny_principal("bob@example.com")), "bob@example.com")
        logged = POOL_URI + "Kim"
        self.assertEqual(identity.person_key(identity.deny_principal(logged)), identity.person_key(logged))


class MaskingTest(unittest.TestCase):
    def test_mask(self):
        self.assertEqual(identity.mask("alice@example.com"), "al***@example.com")
        self.assertEqual(identity.mask(POOL_URI + "kim.lee@example.com"), "ki***@example.com")
        self.assertEqual(identity.mask(POOL_URI + "E12345"), "E1***")

    def test_mask_workforce_in_text(self):
        text = "blocked %s and %s." % (POOL_URI + "E12345", POOL_KEY + "kim@example.com")
        masked = identity.mask_workforce_in_text(text)
        self.assertNotIn("E12345", masked)
        self.assertNotIn("kim@example.com", masked)
        self.assertIn(POOL_URI + "E1***", masked)
        self.assertEqual(identity.mask_workforce_in_text("no principals here"), "no principals here")

    def test_viewer_text_masking_covers_workforce_subjects(self):
        masked = mask_text_emails("user %s used alice@example.com" % (POOL_URI + "E12345"))
        self.assertNotIn("E12345", masked)
        self.assertIn("al***@example.com", masked)


class QueryHelpersTest(unittest.TestCase):
    def test_sql_person_key_matches_the_python_key(self):
        self.assertEqual(identity.sql_person_key("t.labels.user_id"),
                         "LOWER(REGEXP_REPLACE(TRIM(t.labels.user_id), r'(?i)^user:', ''))")

    def test_logging_literal_escapes(self):
        self.assertEqual(identity.logging_literal('a"b\\c'), '"a\\"b\\\\c"')


if __name__ == "__main__":
    unittest.main()
