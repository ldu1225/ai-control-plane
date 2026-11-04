"""Daily official price/FX check (configurable time and timezone, no built-in prices or rates), Cloud Billing
Catalog discovery, BigQuery storage prices and the maintenance tick that runs scheduled work idempotently."""

import datetime as dt
import json
import unittest
from unittest import mock

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane import settings_store
from controlplane.services import maintenance, pricing_sync
from test_pricing import catalog_sku
from test_pricing_and_multiproject import SAMPLE_MARKDOC

UTC = dt.timezone.utc


def utc(*args):
    return dt.datetime(*args, tzinfo=UTC)


def make_app(**sections):
    base = support.make_settings(**sections)
    app = support.FakeApp()
    app.settings = settings_store.SettingsManager(base, settings_store.MemoryStore())
    app.gcp = None
    return app


class ScheduleTest(unittest.TestCase):
    def test_midnight_in_the_workspace_timezone_by_default(self):
        settings = support.make_settings(workspace={"timezone": "Asia/Seoul"})
        now = utc(2026, 11, 4, 10, 0)  # 19:00 in Seoul
        info = pricing_sync.schedule_info(settings, now)
        self.assertEqual(info, {"time": "00:00", "timezone": "Asia/Seoul", "nextRunAt": "2026-11-04T15:00:00Z"})
        self.assertEqual(pricing_sync.last_scheduled_time(settings, now), utc(2026, 11, 3, 15, 0))
        self.assertTrue(pricing_sync.is_due(settings, "2026-11-03T14:59:00Z", now))
        self.assertFalse(pricing_sync.is_due(settings, "2026-11-03T15:01:00Z", now))
        self.assertTrue(pricing_sync.is_due(settings, None, now))

    def test_time_and_timezone_are_settings(self):
        settings = support.make_settings(workspace={"timezone": "Asia/Seoul"},
                                         pricing={"sync_time": "06:30", "sync_timezone": "Europe/Berlin"})
        now = utc(2026, 11, 4, 10, 0)  # 11:00 in Berlin (CET)
        info = pricing_sync.schedule_info(settings, now)
        self.assertEqual((info["time"], info["timezone"], info["nextRunAt"]), ("06:30", "Europe/Berlin",
                                                                              "2026-11-05T05:30:00Z"))
        self.assertEqual(pricing_sync.last_scheduled_time(settings, now), utc(2026, 11, 4, 5, 30))


def fx_payload(**rates):
    return json.dumps({"rates": dict({"USD": 1}, **rates), "time_last_update_utc": "Wed, 04 Nov 2026 00:00:01 +0000"})


class Fetcher:
    """Serves the configured official price page and exchange-rate feed; everything else fails."""

    def __init__(self, page=SAMPLE_MARKDOC, fx="default"):
        self.page, self.urls = page, []
        self.fx = fx_payload(KRW=1400.0, EUR=0.9) if fx == "default" else fx

    def __call__(self, url):
        self.urls.append(url)
        if url.startswith("https://aistudio.google.com/") and self.page is not None:
            return self.page
        if url.startswith("https://open.er-api.com/") and self.fx is not None:
            return self.fx
        raise OSError("unreachable in tests")


class SyncTest(unittest.TestCase):
    def test_pages_and_fx_feeds_then_change_alerts(self):
        app = make_app(pricing={"currency": "KRW"})
        res = pricing_sync.sync_pricing(app, trigger="scheduled", fetcher=Fetcher())
        state = app.settings.pricing_state()
        self.assertEqual(state["models"]["gemini-2.5-pro"]["input"], 1.25)
        self.assertEqual(state["models"]["gemini-2.5-pro"]["source"], "page:aistudio.google.com")
        self.assertEqual(state["fx"]["rates"]["KRW"]["rate"], 1400.0)
        self.assertEqual(state["fx"]["rates"]["KRW"]["source"], "open.er-api.com")
        self.assertNotIn("EUR", state["fx"]["rates"])  # only the currencies in use are kept
        self.assertEqual(res["alerts"], [])  # first sync: no alerts
        self.assertEqual(res["displayCurrency"], "KRW")
        pro = next(m for m in res["models"] if m["key"] == "gemini-2.5-pro")
        self.assertEqual(pro["localPerMillion"]["input"], 1750.0)
        statuses = {s["id"]: s["status"] for s in res["sources"]}
        self.assertEqual(statuses["page:aistudio.google.com"], "ok")
        self.assertEqual(statuses["page:ai.google.dev"], "error")
        self.assertEqual(statuses["fx:open.er-api.com"], "ok")

        changed = SAMPLE_MARKDOC.replace("$1.25, prompts <= 200k", "$1.00, prompts <= 200k")
        res2 = pricing_sync.sync_pricing(app, fetcher=Fetcher(page=changed, fx=fx_payload(KRW=1450.0)))
        kinds = sorted(a["kind"] for a in res2["alerts"])
        self.assertEqual(kinds, ["fx", "model_price"])
        self.assertEqual(res2["newAlertsCount"], 2)

        # Everything unreachable: the last official prices and rates are kept, nothing is invented.
        res3 = pricing_sync.sync_pricing(app, fetcher=Fetcher(page=None, fx=None))
        state = app.settings.pricing_state()
        self.assertEqual(state["models"]["gemini-2.5-pro"]["input"], 1.0)
        self.assertEqual(state["fx"]["rates"]["KRW"]["source"], "last_known")
        self.assertEqual(res3["newAlertsCount"], 0)

    def test_no_rate_means_usd_not_a_guess(self):
        app = make_app(pricing={"currency": "JPY"})
        pricing_sync.sync_pricing(app, fetcher=Fetcher(fx=fx_payload(KRW=1400.0)))
        ctx = support.FakeCtx(settings=app.settings.snapshot()[0])
        ctx.app = app
        rates, currency, fx = pricing_sync.effective_token_rates(ctx)
        self.assertEqual(currency, "USD")
        self.assertEqual(fx["rate"], 1.0)
        self.assertIn("fx_unavailable", [w["code"] for w in ctx.warnings])
        self.assertEqual(rates["gemini-2.5-pro"]["input"], 1.25)

    def test_implausible_rate_jump_is_rejected(self):
        app = make_app(pricing={"currency": "KRW"})
        pricing_sync.sync_pricing(app, fetcher=Fetcher())
        pricing_sync.sync_pricing(app, fetcher=Fetcher(fx=fx_payload(KRW=14.0)))
        rate = app.settings.pricing_state()["fx"]["rates"]["KRW"]
        self.assertEqual((rate["rate"], rate["source"]), (1400.0, "last_known"))

    def test_catalog_prices_win_and_storage_prices_are_found(self):
        app = make_app(pricing={"currency": "KRW"})
        app.gcp = object()
        services = {"services": [
            {"serviceId": "C7E2-9256-1C43", "displayName": "Vertex AI"},
            {"serviceId": "AEFD-7695-64FA", "displayName": "Gemini API"},
            {"serviceId": "24E6-581D-38E5", "displayName": "BigQuery"},
            {"serviceId": "6F81-5844-456A", "displayName": "Compute Engine"}]}
        skus = {
            "C7E2-9256-1C43": [catalog_sku("Gemini 2.5 Pro Global Text Input - Online Predictions", 1.3e-06),
                               catalog_sku("Gemini 2.5 Pro Global Text Output - Online Predictions", 1.05e-05),
                               catalog_sku("Gemini 2.5 Pro Global Text Input - Deferred Predictions", 6e-07)],
            "AEFD-7695-64FA": [],
            "24E6-581D-38E5": [
                dict(catalog_sku("Streaming Insert", 0.05, unit="GiBy"), serviceRegions=["asia-northeast3"]),
                dict(catalog_sku("Streaming Insert", 0.06, unit="GiBy"), serviceRegions=["us"]),
                dict(catalog_sku("Active Logical Storage", 0.023, unit="GiBy.mo"), serviceRegions=["asia-northeast3"]),
                dict(catalog_sku("Active Physical Storage", 0.04, unit="GiBy.mo"), serviceRegions=["asia-northeast3"])],
        }
        calls = []

        class CatalogCtx(support.FakeCtx):
            def rest(self, method, url, params=None, **_kw):
                calls.append((url, dict(params or {})))
                if url.endswith("/services"):
                    return services
                sid = url.split("/services/")[1].split("/")[0]
                if (params or {}).get("currencyCode") == "KRW":
                    return {"skus": [catalog_sku("Gemini 2.5 Pro Global Text Input - Online Predictions",
                                                 1.3e-06 * 1390, rate=1390.0)]}
                return {"skus": skus.get(sid, [])}

        settings = app.settings.snapshot()[0]
        settings["telemetry"]["routing"]["dataset_location"] = "asia-northeast3"
        with mock.patch.object(pricing_sync, "_system_ctx", return_value=CatalogCtx(settings=settings)), \
                mock.patch.object(app.settings, "snapshot", return_value=(settings, 1)):
            res = pricing_sync.sync_pricing(app, fetcher=Fetcher(fx=None))
        state = app.settings.pricing_state()
        pro = state["models"]["gemini-2.5-pro"]
        self.assertEqual((pro["input"], pro["output"], pro["source"]), (1.3, 10.5, "cloud_billing_catalog"))
        self.assertEqual(pro["cached"], 0.125)  # the page fills what the catalog does not list
        self.assertEqual(state["fx"]["rates"]["KRW"], {"rate": 1390.0, "previous": None, "source": "cloud_billing_catalog",
                                                       "asOf": "2026-10-20T07:00:00Z"})
        self.assertEqual([s["name"] for s in state["catalogServices"]], ["Vertex AI", "Gemini API"])
        self.assertEqual((res["storage"]["ingestPerGiB"], res["storage"]["storagePerGiBMonth"]), (0.05, 0.023))
        self.assertFalse(any("6F81-5844-456A" in u for u, _ in calls))  # Compute Engine SKUs are never read


class CatalogSelectionTest(unittest.TestCase):
    SERVICES = [{"id": "1", "name": "Vertex AI"}, {"id": "2", "name": "Gemini API"},
                {"id": "3", "name": "Claude Sonnet 4.5"}, {"id": "4", "name": "Cloud Storage"},
                {"id": "5", "name": "BigQuery"}, {"id": "6", "name": "BigQuery Reservation API"}]

    def test_model_services_by_configured_names(self):
        settings = support.make_settings()
        self.assertEqual([s["id"] for s in pricing_sync.select_model_services(self.SERVICES, settings)], ["1", "2", "3"])
        no_partners = support.make_settings(pricing={"include_partner_models": False})
        self.assertEqual([s["id"] for s in pricing_sync.select_model_services(self.SERVICES, no_partners)], ["1", "2"])
        renamed = support.make_settings(pricing={"catalog_services": ["Agent Platform"], "include_partner_models": False})
        self.assertEqual(pricing_sync.select_model_services(self.SERVICES + [{"id": "7", "name": "Agent Platform"}],
                                                            renamed), [{"id": "7", "name": "Agent Platform"}])

    def test_storage_service_is_an_exact_name(self):
        self.assertEqual(pricing_sync.select_storage_service(self.SERVICES, support.make_settings())["id"], "5")
        self.assertIsNone(pricing_sync.select_storage_service(self.SERVICES, support.make_settings(
            pricing={"storage_service": ""})))


class LookupTest(unittest.TestCase):
    RATES = {"gemini-3.8-flash": {"input": 0.3}, "gemini-3.8-flash-lite": {"input": 0.1}}

    def test_exact_alias_and_revision_but_never_another_model(self):
        self.assertEqual(pricing_sync.lookup_model_rate(self.RATES, "gemini-3.8-flash")[1], {"input": 0.3})
        self.assertEqual(pricing_sync.lookup_model_rate(self.RATES, "gemini-3.8-flash-002")[1], {"input": 0.3})
        self.assertEqual(pricing_sync.lookup_model_rate(self.RATES, "my-tuned", {"my-tuned": "gemini-3.8-flash-lite"})[1],
                         {"input": 0.1})
        self.assertIsNone(pricing_sync.lookup_model_rate(self.RATES, "gemini-3.8-pro")[1])
        self.assertIsNone(pricing_sync.lookup_model_rate(self.RATES, "gemini-3.8-flash-image")[1])

    def test_preferred_currency_order(self):
        ctx = support.FakeCtx(settings=support.make_settings(pricing={"currency": "EUR"}, governance={"currency": "KRW"}))
        self.assertEqual(pricing_sync.preferred_currency(ctx), "EUR")
        self.assertEqual(pricing_sync.preferred_currency(ctx, "jpy"), "JPY")
        ctx.params = {"currency": "GBP"}
        self.assertEqual(pricing_sync.preferred_currency(ctx), "GBP")
        ctx = support.FakeCtx(settings=support.make_settings(governance={"currency": "KRW"}))
        self.assertEqual(pricing_sync.preferred_currency(ctx), "KRW")
        self.assertEqual(pricing_sync.preferred_currency(support.FakeCtx()), "USD")

    def test_overrides_and_aliases_from_settings(self):
        app = make_app(pricing={"model_overrides": [{"model": "acme-llm", "input": 2.0, "output": 6.0, "cached": None}],
                                "model_aliases": ["My-Tuned=gemini-2.5-pro"]})
        pricing_sync.sync_pricing(app, fetcher=Fetcher(fx=None))
        ctx = support.FakeCtx(settings=app.settings.snapshot()[0])
        ctx.app = app
        rates, currency, _ = pricing_sync.effective_token_rates(ctx)
        self.assertEqual(currency, "USD")
        self.assertEqual((rates["acme-llm"]["input"], rates["acme-llm"]["source"]), (2.0, "override"))
        self.assertTrue(rates["acme-llm"]["cachedAssumed"])
        _, rate = pricing_sync.lookup_model_rate(rates, "my-tuned", pricing_sync.model_aliases(ctx))
        self.assertEqual(rate["inputUsd"], 1.25)
        listed = pricing_sync.get_pricing(ctx)
        self.assertIn("acme-llm", [m["key"] for m in listed["models"] if m["source"] == "override"])

    def test_storage_prices_converts_miby_streaming_skus_to_gib(self):
        app = make_app()
        eff, _ = app.settings.snapshot()
        ctx = support.FakeCtx(settings=eff)
        skus = [
            {
                "description": "Streaming Insert (asia-northeast3)",
                "serviceRegions": ["asia-northeast3"],
                "pricingInfo": [{
                    "pricingExpression": {
                        "usageUnit": "MiBy",
                        "baseUnit": "By",
                        "baseUnitConversionFactor": 1048576,
                        "tieredRates": [{"startUsageAmount": 0, "unitPrice": {"currencyCode": "USD", "units": "0", "nanos": 50000}}],
                    }
                }],
            },
            {
                "description": "Active Logical Storage (asia-northeast3)",
                "serviceRegions": ["asia-northeast3"],
                "pricingInfo": [{
                    "pricingExpression": {
                        "usageUnit": "GiBy.mo",
                        "tieredRates": [{"startUsageAmount": 0, "unitPrice": {"currencyCode": "USD", "units": "0", "nanos": 23000000}}],
                    }
                }],
            },
        ]
        with mock.patch.object(pricing_sync, "_service_skus", return_value=skus):
            res = pricing_sync.storage_prices(ctx, {"id": "24E6-581D-38E5"}, location="asia-northeast3")
        self.assertAlmostEqual(res["ingestPerGiB"], 0.0512, places=6)
        self.assertAlmostEqual(res["storagePerGiBMonth"], 0.023, places=6)


class MaintenanceTickTest(unittest.TestCase):
    def test_price_sync_runs_once_per_scheduled_time(self):
        app = make_app(workspace={"timezone": "Asia/Seoul"})
        calls = []

        def fake_sync(a, actor="system", trigger="manual", fetcher=None):
            calls.append(trigger)

            def apply(state):
                state["syncedAt"] = dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            a.settings.mutate_pricing(apply, actor)
            return {"models": [], "newAlertsCount": 0}

        with mock.patch.object(pricing_sync, "sync_pricing", side_effect=fake_sync):
            first = maintenance.tick(app)
            second = maintenance.tick(app)
        self.assertEqual(calls, ["scheduled"])
        self.assertIn("pricing_sync", first)
        self.assertNotIn("pricing_sync", second)
        self.assertEqual(maintenance.status(app)["pricing_sync"]["status"], "ok")

    def test_a_running_claim_blocks_until_it_is_stale(self):
        app = make_app()
        started = dt.datetime.now(UTC)

        def seed(minutes_ago):
            def apply(doc):
                doc["runs"] = {"pricing_sync": {"status": "running", "startedAt": (
                    started - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")}}
            settings_store.aux_docs(app).mutate(maintenance.DOC, apply)

        with mock.patch.object(pricing_sync, "sync_pricing", return_value={"models": [], "newAlertsCount": 0}) as sync:
            seed(1)
            maintenance.tick(app)
            self.assertEqual(sync.call_count, 0)
            seed(30)
            maintenance.tick(app)
            self.assertEqual(sync.call_count, 1)

    def test_route_guard_and_daily_upkeep(self):
        app = make_app()
        settings_store.aux_docs(app).mutate("telemetry_routing", lambda doc: doc.update(routes=[{"id": "rt_00000001"}]))
        summary = {"paused": 0, "resumed": 0, "followed": 0, "retention": 0, "errors": 0, "gbToday": 0.0}
        with mock.patch.object(pricing_sync, "sync_pricing", return_value={"models": [], "newAlertsCount": 0}), \
                mock.patch("controlplane.services.telemetry_routing.run_enforcement", return_value=summary) as enf:
            maintenance.tick(app)
            self.assertEqual(enf.call_args.kwargs, {"trigger": "daily", "follow": True})
            maintenance.tick(app)  # within GUARD_MIN_SECONDS of the last guard: skipped
            self.assertEqual(enf.call_count, 1)
            runs = maintenance.status(app)
            self.assertEqual((runs["telemetry_daily"]["status"], runs["telemetry_guard"]["status"]), ("ok", "ok"))

    def test_unconfigured_workspace_is_skipped(self):
        app = make_app(workspace={"project_id": "your-gcp-project-id"})
        self.assertEqual(maintenance.tick(app), {"skipped": "workspace not configured"})


if __name__ == "__main__":
    unittest.main()
