"""Gemini Enterprise licenses: seats are distributed per location and must be read from every
configured location (DISCOVERY_ENGINE_LOCATIONS), not only from global."""

import unittest

import support

from controlplane.gcp import GcpError
from controlplane.services import gemini_enterprise as ge
from controlplane.services.base import discovery_host

P = support.PROJECT


def config_entry(location, name, state, count):
    return {"name": "projects/%s/locations/%s/licenseConfigs/%s" % (P, location, name), "state": state,
            "licenseCount": str(count), "subscriptionTier": "SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT",
            "startDate": {"year": 2026, "month": 1, "day": 1}}


class FakeGcp:
    """Answers paginate() from a {url: items | Exception} table and records the URLs."""

    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def paginate(self, url, **kwargs):  # pylint: disable=unused-argument
        self.urls.append(url)
        value = self.pages.get(url, [])
        if isinstance(value, Exception):
            raise value
        return value, False


def configs_url(location):
    return "%s/v1alpha/projects/%s/locations/%s/licenseConfigs" % (discovery_host(location), P, location)


def users_url(location):
    return ("%s/v1alpha/projects/%s/locations/%s/userStores/default_user_store/userLicenses"
            % (discovery_host(location), P, location))


def ctx_for(locations, pages):
    settings = support.make_settings(workspace={"discovery_engine_locations": locations})
    ctx = support.FakeCtx(settings=settings)
    ctx.gcp = FakeGcp(pages)
    return ctx


class LicensesPerLocationTest(unittest.TestCase):
    def test_hosts(self):
        self.assertEqual(discovery_host("global"), "https://discoveryengine.googleapis.com")
        self.assertEqual(discovery_host("us"), "https://us-discoveryengine.googleapis.com")

    def test_seats_are_summed_over_locations(self):
        ctx = ctx_for(["global", "us"], {
            configs_url("global"): [config_entry("global", "plus", "ACTIVE", 20),
                                    config_entry("global", "trial", "EXPIRED", 10000)],
            configs_url("us"): [config_entry("us", "plus", "ACTIVE", 20), config_entry("us", "std", "ACTIVE", 5)],
            users_url("global"): [{"userPrincipal": "a@example.com", "licenseAssignmentState": "ASSIGNED"}],
            users_url("us"): [{"userPrincipal": "A@example.com", "licenseAssignmentState": "ASSIGNED"},
                              {"userPrincipal": "b@example.com", "licenseAssignmentState": "ASSIGNED"}],
        })
        out = ge.licenses(ctx)
        self.assertEqual(out["locations"], ["global", "us"])
        self.assertEqual(out["purchased"], 45)          # active seats only: 20 + 20 + 5
        self.assertEqual(out["assigned"], 3)            # a seat in global and one in us count separately
        self.assertEqual(out["unassigned"], 42)
        self.assertEqual(sorted({c["location"] for c in out["configs"]}), ["global", "us"])
        self.assertEqual(sorted((u["email"], u["location"]) for u in out["users"]),
                         [("a@example.com", "global"), ("a@example.com", "us"), ("b@example.com", "us")])
        self.assertTrue(out["userLicensesAvailable"])
        self.assertEqual(ctx.warnings, [])
        self.assertIn(configs_url("us"), ctx.gcp.urls)  # the regional host is used outside global

    def test_one_failing_location_keeps_the_others(self):
        ctx = ctx_for(["global", "eu"], {
            configs_url("global"): [config_entry("global", "plus", "ACTIVE", 20)],
            configs_url("eu"): GcpError("denied", 403, "permission denied"),
            users_url("global"): [{"userPrincipal": "a@example.com", "licenseAssignmentState": "ASSIGNED"}],
            users_url("eu"): GcpError("denied", 403, "permission denied"),
        })
        out = ge.licenses(ctx)
        self.assertEqual(out["purchased"], 20)
        self.assertEqual(out["assigned"], 1)
        self.assertEqual(len(ctx.warnings), 2)
        self.assertTrue(all("(eu)" in w["message"] for w in ctx.warnings))

    def test_no_configuration_anywhere(self):
        ctx = ctx_for(["global"], {})
        out = ge.licenses(ctx)
        self.assertEqual(out["configs"], [])
        self.assertIsNone(out["purchased"])             # unknown, not zero
        self.assertEqual(out["assigned"], 0)
        self.assertIsNone(out["unassigned"])

    def test_all_locations_failing(self):
        err = GcpError("denied", 403, "permission denied")
        ctx = ctx_for(["global"], {configs_url("global"): err, users_url("global"): err})
        out = ge.licenses(ctx)
        self.assertIsNone(out["configs"])
        self.assertIsNone(out["users"])
        self.assertFalse(out["userLicensesAvailable"])


if __name__ == "__main__":
    unittest.main()
