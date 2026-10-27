"""Automatic token prices for per-user cost caps: SKU selection, billed effective prices, the public
price list and currency conversion (services.pricing). No network: REST and queries are faked."""

import unittest
from unittest import mock

import support

from controlplane.gcp import GcpError
from controlplane.services import governance as gov
from controlplane.services import pricing
from test_governance_actions import Harness, make_usage
from test_governance_caps import ctx_with


def catalog_sku(description, price, rate=1.0, unit="count", effective="2026-10-20T07:00:00Z"):
    units, nanos = int(price), int(round((price - int(price)) * 1e9))
    return {"description": description, "pricingInfo": [{
        "effectiveTime": effective, "currencyConversionRate": rate,
        "pricingExpression": {"usageUnit": unit, "usageUnitDescription": unit, "baseUnitConversionFactor": 1,
                              "tieredRates": [{"startUsageAmount": 0,
                                               "unitPrice": {"currencyCode": "X", "units": str(units), "nanos": nanos}}]}}]}


class SkuSelectionTest(unittest.TestCase):
    def test_standard_text_token_skus(self):
        self.assertEqual(pricing.token_sku("Gemini 3.8 Flash Global Text Input - Predictions")[:2],
                         ("gemini-3.8-flash", "input"))
        self.assertEqual(pricing.token_sku("Generate content output token count gemini 4 argon text")[:2],
                         ("gemini-4-argon", "output"))
        self.assertEqual(pricing.token_sku("CodeMender Gemini 3.7 Flash Global Text Input")[2], "CodeMender")

    def test_variants_are_not_the_standard_price(self):
        for desc in ("Gemini 3.8 Flash Global Text Input - Batch Predictions",
                     "Gemini 4 Argon Global Text Input - Deferred Predictions",
                     "Gemini 3.8 Flash Regional Audio Input Priority - Predictions",
                     "Generate content input token count gemini 4 argon text flex",
                     "Generate content cached input token count gemini 4 argon text",
                     "Gemini 3.7 Flash Global Text Input Caching",
                     "Generate content input token count gemini 3.8 flash image",
                     "Gemini 3.8 Live Audio Output - Online Predictions",
                     "Generate content output token count gemini 4 argon text contributor",
                     "Gemini Code Assist monthly subscription"):
            self.assertIsNone(pricing.token_sku(desc), desc)


class EffectivePriceTest(unittest.TestCase):
    def test_weighted_price_per_million_and_product_fallback(self):
        rows = [
            {"sku": "Gemini 3.8 Flash Global Text Input - Predictions", "service": "Vertex AI", "unit": "requests",
             "pricing_unit": "count", "amount": 1_000_000, "cost": 1.5, "currency": "USD", "fx": 1.0},
            {"sku": "Generate content input token count gemini 3.8 flash text", "service": "Gemini API",
             "unit": "requests", "pricing_unit": "count", "amount": 1_000_000, "cost": 0.75, "currency": "USD", "fx": 1.0},
            {"sku": "CodeMender Gemini 3.7 Flash Global Text Output", "service": "Vertex AI", "unit": "requests",
             "pricing_unit": "count", "amount": 2_000_000, "cost": 15.0, "currency": "USD", "fx": 1.0},
            {"sku": "Gemini 3.8 Flash Global Text Output - Predictions", "service": "Vertex AI", "unit": "requests",
             "pricing_unit": "count", "amount": 0, "cost": 0, "currency": "USD", "fx": 1.0},
        ]
        prices, currency, rate = pricing.effective_prices(rows)
        self.assertEqual(prices["gemini-3.8-flash"], {"input": 1.125})  # (1.5 + 0.75) / 2M tokens * 1M
        self.assertEqual(prices["gemini-3.7-flash"], {"output": 7.5})
        self.assertEqual((currency, rate), ("USD", 1.0))


class CatalogTest(unittest.TestCase):
    def test_global_online_price_in_the_requested_currency(self):
        pages = {"C7E2-9256-1C43": [{"skus": [
            catalog_sku("Gemini 4 Argon Global Text Input - Online Predictions", 4e-06 * 1358.75, rate=1358.75),
            catalog_sku("Gemini 4 Argon Regional Text Input - Online Predictions", 4.4e-06 * 1358.75, rate=1358.75),
            catalog_sku("Gemini 4 Argon Global Text Input - Deferred Predictions", 2e-06 * 1358.75, rate=1358.75),
            catalog_sku("Gemini 4 Argon Global Text Output - Online Predictions", 2e-05 * 1358.75, rate=1358.75),
            catalog_sku("Vertex AI GPU hour", 900.0, rate=1358.75, unit="h"),
        ]}], "AEFD-7695-64FA": [{"skus": [
            catalog_sku("Generate content input token count gemini 4 argon text", 4e-06 * 1358.75, rate=1358.75)]}]}
        ctx = mock.MagicMock()
        ctx.rest.side_effect = lambda method, url, **kw: pages[url.split("/services/")[1].split("/")[0]].pop(0)
        prices, rate, effective = pricing.catalog_prices(ctx, "KRW")
        self.assertAlmostEqual(prices["gemini-4-argon"]["input"], 4 * 1358.75, places=3)
        self.assertAlmostEqual(prices["gemini-4-argon"]["output"], 20 * 1358.75, places=3)
        self.assertEqual((rate, effective), (1358.75, "2026-10-20T07:00:00Z"))
        self.assertEqual(ctx.rest.call_args.kwargs["params"]["currencyCode"], "KRW")


class CombineTest(unittest.TestCase):
    def test_billed_prices_are_converted_and_win(self):
        table, warning = pricing.combine(
            "KRW", {"gemini-3.8-flash": {"input": 1.5, "output": 7.5}}, "USD", 1.0,
            {"gemini-3.8-flash": {"input": 2000.0, "output": 9000.0}, "gemini-4-argon": {"input": 5435.0, "output": 27175.0}},
            1358.75, "2026-10-20T07:00:00Z")
        self.assertIsNone(warning)
        self.assertEqual(table.lookup("models/gemini-3.8-flash@001"),
                         {"input": 1.5 * 1358.75, "output": 7.5 * 1358.75, "source": "billing"})
        self.assertEqual(table.lookup("gemini-4-argon")["source"], "catalog")
        self.assertEqual(table.fx, {"from": "USD", "to": "KRW", "rate": 1358.75, "asOf": "2026-10-20T07:00:00Z"})
        self.assertEqual(table.counts, {"billing": 1, "catalog": 1})

    def test_same_currency_needs_no_rate_and_missing_rate_falls_back(self):
        table, warning = pricing.combine("USD", {"m-1": {"input": 1.0, "output": 2.0}}, "USD", None, {}, None, None)
        self.assertIsNone(warning)
        self.assertEqual(table.lookup("m-1")["input"], 1.0)
        self.assertIsNone(table.fx)
        table, warning = pricing.combine("KRW", {"m-1": {"input": 1.0, "output": 2.0}}, "USD", None, {}, None, None)
        self.assertIn("could not be converted", warning)
        self.assertIsNone(table.lookup("m-1"))

    def test_build_never_raises_and_warns(self):
        ctx = support.FakeCtx()
        ctx.billing_source = lambda: None
        ctx.rest = mock.MagicMock(side_effect=GcpError("denied", status=403))
        table = pricing.build_price_table(ctx, "KRW")
        self.assertEqual((table.currency, table.prices), ("KRW", {}))
        codes = {w["code"] for w in ctx.warnings}
        self.assertIn("no_prices", codes)


class AutoPricedStatusTest(unittest.TestCase):
    def test_cost_in_won_from_the_automatic_table(self):
        table = pricing.PriceTable("KRW", {"gemini-3.8-pro": {"input": 2717.5, "output": 16305.0, "source": "billing"}},
                                   {"from": "USD", "to": "KRW", "rate": 1358.75, "asOf": "2026-10-20T07:00:00Z"},
                                   {"billing": 1, "catalog": 0})
        usage = make_usage([("heavy@example.com", 2_000_000, 40)])
        usage.users["heavy@example.com"]["byModel"] = {
            "gemini-3.8-pro": {"tokens": 2_000_000, "promptTokens": 1_000_000, "candidateTokens": 900_000}}
        ctx = ctx_with(currency="KRW", cost_cap=10000.0, token_cap=0, request_cap=0)
        with Harness(usage=usage) as h:
            h.price_table.return_value = table
            payload, _ = gov.compute_status(ctx, "monthly", "calendar", 0, 0)
        heavy = next(u for u in payload["users"] if u["email"] == "heavy@example.com")
        self.assertEqual(heavy["estimatedCost"], 2717.5 + 16305.0)  # 1M input + 1M output tokens
        self.assertEqual((heavy["overCap"], heavy["overCapReasons"]), (True, ["cost"]))
        pub = payload["policy"]["pricing"]
        self.assertEqual((pub["source"], pub["currency"], pub["fx"]["rate"]), ("auto", "KRW", 1358.75))
        self.assertEqual(pub["used"], [{"model": "gemini-3.8-pro", "price": {"input": 2717.5, "output": 16305.0,
                                                                            "source": "billing", "complete": True}}])
        self.assertEqual(payload["policy"]["currency"], "KRW")
        h.price_table.assert_called_once()
        self.assertEqual(h.price_table.call_args.args[1], "KRW")


if __name__ == "__main__":
    unittest.main()
