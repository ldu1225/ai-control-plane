"""Unit tests for public token pricing/FX sync, reverse cost estimation, and multi-project UnionSource."""

from __future__ import annotations

import datetime as dt
import unittest
from unittest.mock import patch

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane import schema, settings_store, timeutil
from controlplane.gcp import QueryResult
from controlplane.security import Identity
from controlplane.services import base, finops, models, pricing_sync


SAMPLE_MARKDOC = """
## Gemini 2.5 Pro {% id="gemini-2.5-pro" %}

{% tabs %}
{% tab title="Standard" %}
{% table %}
* Feature
* Free Tier
* Paid Tier, per 1M tokens in USD
---
* Input price
* Free of charge
* $1.25, prompts <= 200k tokens
  $2.50, prompts > 200k tokens
---
* Output price (including thinking tokens)
* Free of charge
* $10.00, prompts <= 200k tokens
  $15.00, prompts > 200k
---
* Context caching price
* Free of charge
* $0.125, prompts <= 200k tokens
  $0.25, prompts > 200k
  $4.50 / 1,000,000 tokens per hour (storage price)
{% /table %}
{% /tab %}
{% tab title="Batch" %}
{% table %}
* Feature
* Free Tier
* Paid Tier
---
* Input price
* Not available
* $0.625, prompts <= 200k tokens
{% /table %}
{% /tab %}
{% /tabs %}

## Gemini 2.5 Flash Image {% id="gemini-2.5-flash-image" %}
{% table %}
* Feature
* Paid Tier
---
* Output price
* $30.00
{% /table %}
"""


class PricingSyncTest(unittest.TestCase):
    def test_parse_markdoc_extracts_standard_text_tier_only(self):
        parsed = pricing_sync.parse_markdoc_pricing(SAMPLE_MARKDOC)
        self.assertIn("gemini-2.5-pro", parsed)
        self.assertNotIn("gemini-2.5-flash-image", parsed)
        pro = parsed["gemini-2.5-pro"]
        self.assertAlmostEqual(pro["input"], 1.25)
        self.assertAlmostEqual(pro["output"], 10.00)
        self.assertAlmostEqual(pro["cached"], 0.125)

    def test_ssrf_allowlist_blocks_untrusted_hosts(self):
        settings = support.make_settings()
        fetch = pricing_sync.make_fetcher(pricing_sync.allowed_hosts(settings))
        with self.assertRaises(ValueError):
            fetch("https://evil.example.com/pricing")
        with self.assertRaises(ValueError):
            fetch("http://aistudio.google.com/docs/pricing.md")
        # Only hosts of the configured official price pages and exchange-rate feeds are reachable.
        self.assertIn("aistudio.google.com", pricing_sync.allowed_hosts(settings))
        self.assertNotIn("evil.example.com", pricing_sync.allowed_hosts(settings))

    def test_diff_snapshots_silent_on_first_sync_and_alerts_on_changes(self):
        models_v1 = {
            "gemini-2.5-pro": {
                "input": 1.25, "output": 10.00, "cached": 0.125,
                "source": "google_public_docs", "label": "Gemini 2.5 Pro",
            },
        }
        fx_v1 = {"base": "USD", "rates": {"KRW": {"rate": 1380.0, "source": "open.er-api.com"}}}
        first_alerts = pricing_sync.diff_snapshots({}, models_v1, fx_v1, "2026-04-17T04:00:00Z")
        self.assertEqual(first_alerts, [])

        prev_state = {
            "syncedAt": "2026-04-17T04:00:00Z",
            "fx": fx_v1,
            "models": models_v1,
        }
        models_v2 = {
            "gemini-2.5-pro": {
                "input": 1.00, "output": 10.00, "cached": 0.125,
                "source": "google_public_docs", "label": "Gemini 2.5 Pro",
            },
            "gemini-5-ultra": {
                "input": 4.00, "output": 20.00, "cached": 0.40,
                "source": "google_public_docs", "label": "Gemini 5 Ultra",
            },
        }
        fx_v2 = {"base": "USD", "rates": {"KRW": {"rate": 1415.0, "source": "open.er-api.com"}}}  # +2.54% shift
        alerts = pricing_sync.diff_snapshots(prev_state, models_v2, fx_v2, "2026-04-18T04:00:00Z")
        kinds = {a["kind"] for a in alerts}
        self.assertEqual(kinds, {"fx", "model_price", "new_model"})

        price_alert = next(a for a in alerts if a["kind"] == "model_price")
        self.assertEqual(price_alert["target"], "gemini-2.5-pro")
        self.assertEqual(price_alert["field"], "input")
        self.assertAlmostEqual(price_alert["before"], 1.25)
        self.assertAlmostEqual(price_alert["after"], 1.00)

    def test_acknowledge_alerts_marks_entries_acknowledged(self):
        base_cfg = support.make_settings(workspace={"project_id": "proj-a"})
        mgr = settings_store.SettingsManager(base_cfg, settings_store.MemoryStore())

        def seed(state):
            state["syncedAt"] = "2026-04-18T04:00:00Z"
            state["fx"] = {"usdKrw": 1420.0, "source": "open.er-api.com"}
            state["models"] = {"gemini-2.5-pro": {"input": 1.25, "output": 10.0, "cached": 0.125}}
            state["alerts"] = [{
                "id": "chg_abc123",
                "at": "2026-04-18T04:00:00Z",
                "kind": "fx",
                "target": "USD/KRW",
                "label": "USD/KRW Exchange Rate",
                "field": "usdKrw",
                "before": 1380.0,
                "after": 1420.0,
                "pctChange": 2.9,
                "currency": "KRW",
                "source": "open.er-api.com",
                "acknowledgedAt": None,
                "acknowledgedBy": None,
            }]

        mgr.mutate_pricing(seed, "system")
        ctx = support.FakeCtx(settings=mgr.snapshot()[0], body={"all": True})
        ctx.app.settings = mgr
        before_summary = pricing_sync.pricing_alert_summary(mgr)
        self.assertEqual(before_summary["unacknowledgedCount"], 1)

        res = pricing_sync.acknowledge_alerts(ctx)
        self.assertEqual(res["acknowledged"], 1)
        self.assertEqual(res["unacknowledgedCount"], 0)
        after_summary = pricing_sync.pricing_alert_summary(mgr)
        self.assertEqual(after_summary["unacknowledgedCount"], 0)


class TokenCostEstimationFallbackTest(unittest.TestCase):
    def _build_ctx_without_billing(self, usd_krw=1400.0, params=None):
        base_cfg = support.make_settings(
            workspace={"project_id": "acme-ai-prod", "timezone": "Asia/Seoul"},
            branding={"default_locale": "ko"},
            pricing={"currency": "KRW"},
        )
        mgr = settings_store.SettingsManager(base_cfg, settings_store.MemoryStore())

        def seed(state):
            state["syncedAt"] = "2026-04-17T04:00:00Z"
            state["fx"] = {"base": "USD", "rates": {"KRW": {"rate": usd_krw, "source": "open.er-api.com",
                                                             "asOf": "2026-04-17"}}}
            state["models"] = {
                "gemini-2.5-pro": {
                    "input": 1.25,
                    "output": 10.00,
                    "cached": 0.125,
                    "source": "google_public_docs",
                    "label": "Gemini 2.5 Pro",
                },
            }

        mgr.mutate_pricing(seed, "system")
        eff, _ = mgr.snapshot()
        ctx = support.FakeCtx(settings=eff, params=params or {})
        ctx.app.settings = mgr
        ctx.range = timeutil.DateRange(
            start=dt.date(2026, 4, 10),
            end=dt.date(2026, 4, 16),
            tz_name="Asia/Seoul",
            granularity="day",
        )
        return ctx

    def test_finops_and_models_estimate_from_tokens_without_billing_warnings(self):
        ctx = self._build_ctx_without_billing(usd_krw=1400.0)
        d15 = dt.date(2026, 4, 15)
        d16 = dt.date(2026, 4, 16)
        fake_mon = {
            "gemini-2.5-pro": {
                "input": {d15: 800_000.0, d16: 1_200_000.0},
                "output": {d15: 200_000.0, d16: 300_000.0},
                "cached": {},
                "total": {d15: 1_000_000.0, d16: 1_500_000.0},
            },
        }

        def fake_token_counts(_c, _start_ts, end_ts):
            # Return usage whenever the queried window includes the current range
            if end_ts > ctx.range.start_ts():
                return fake_mon, True
            return {}, True

        with patch("controlplane.services.monitoring.token_counts", side_effect=fake_token_counts), \
             patch("controlplane.services.monitoring.invocation_counts", return_value={}):
            # 2M * $1.25/1M ($2.50) + 0.5M * $10.00/1M ($5.00) = $7.50 USD = 10,500 KRW
            usage = models.usage(ctx)
            self.assertEqual(ctx.warnings, [])
            self.assertEqual(usage["costSource"], "token_estimate")
            # The display currency is the configured pricing.currency (no locale guess): 10,500 KRW
            self.assertEqual(usage["currency"], "KRW")
            self.assertAlmostEqual(usage["totals"]["cost"], 10500.0, places=1)
            pro_row = next(m for m in usage["models"] if m["key"] == "gemini-2.5-pro")
            self.assertTrue(pro_row["costEstimated"])
            self.assertAlmostEqual(pro_row["cost"], 10500.0, places=1)

            # FinOps summary defaults to KRW in token_estimate mode (7.50 * 1400 = 10500 KRW)
            fin_krw = finops.summary(ctx)
            self.assertEqual(ctx.warnings, [])
            self.assertEqual(fin_krw["costSource"], "token_estimate")
            self.assertEqual(fin_krw["currency"], "KRW")
            self.assertAlmostEqual(fin_krw["kpis"]["gross"], 10500.0, places=1)
            self.assertAlmostEqual(fin_krw["kpis"]["aiShare"], 1.0, places=4)
            self.assertEqual(len(fin_krw["topSkus"]), 2)

            # Explicit USD toggle in FinOps
            ctx_usd = self._build_ctx_without_billing(usd_krw=1400.0, params={"currency": "USD"})
            fin_usd = finops.summary(ctx_usd)
            self.assertEqual(fin_usd["currency"], "USD")
            self.assertAlmostEqual(fin_usd["kpis"]["gross"], 7.50, places=2)


class MultiProjectUnionAndAuditZeroStateTest(unittest.TestCase):
    def test_union_source_normalizes_structs_and_preserves_json_field_casing(self):
        s1 = schema.TableSchema([
            schema._DictField({"name": "timestamp", "type": "TIMESTAMP"}),
            schema._DictField({
                "name": "protoPayload",
                "type": "RECORD",
                "fields": [
                    {"name": "serviceName", "type": "STRING"},
                    {
                        "name": "authenticationInfo",
                        "type": "RECORD",
                        "fields": [{"name": "principalEmail", "type": "STRING"}],
                    },
                ],
            }),
        ])
        s2 = schema.TableSchema([
            schema._DictField({"name": "timestamp", "type": "TIMESTAMP"}),
            schema._DictField({
                "name": "protoPayload",
                "type": "RECORD",
                "fields": [{"name": "serviceName", "type": "STRING"}],
            }),
        ])
        t1 = schema.Source("proj-a", "ge_analytics", "cloudaudit_googleapis_com_data_access", "table",
                           "cloudaudit_googleapis_com_data_access", s1)
        t2 = schema.Source("proj-b", "ge_analytics", "cloudaudit_googleapis_com_data_access", "table",
                           "cloudaudit_googleapis_com_data_access", s2)
        union = schema.UnionSource([t1, t2])
        self.assertTrue(union.schema.has("timestamp"))
        self.assertTrue(union.schema.has("protopayload.servicename"))
        self.assertTrue(union.schema.has("protopayload.authenticationinfo.principalemail"))
        self.assertFalse(union.schema.has("protopayload.nonexistentfield"))

        cte_sql = union.cte("events")
        self.assertIn("UNION ALL", cte_sql)
        self.assertIn("TO_JSON(`protoPayload`)", cte_sql)

        # JSON_VALUE path must use exact original field casing (serviceName, authenticationInfo.principalEmail)
        svc_expr = schema.expr("e", union.schema, "protopayload.servicename")
        self.assertIn("$.serviceName", svc_expr)
        email_expr = schema.expr("e", union.schema, "protopayload.authenticationinfo.principalemail")
        self.assertIn("$.authenticationInfo.principalEmail", email_expr)

    def test_audit_source_suppresses_table_not_found_when_dataset_exists(self):
        class EmptyDatasetGcp:
            def query(self, sql, *_args, **_kwargs):
                if "INFORMATION_SCHEMA.TABLES" in sql:
                    return QueryResult([], [], 0, 0)  # Dataset exists and is readable, 0 lazy sink tables yet
                raise AssertionError("unexpected SQL: %s" % sql)

        app = support.FakeApp()
        app.gcp = EmptyDatasetGcp()
        app.registry = schema.SchemaRegistry(app.gcp)
        eff = support.make_settings(workspace={"project_id": "acme-ai-prod", "audit_dataset": "ge_analytics"})
        req_ctx = base.Ctx(
            app=app,
            settings=eff,
            version=1,
            identity=Identity("admin@example.com", "admin", "google", "u:admin"),
        )
        self.assertTrue(req_ctx.audit_dataset_ready())
        src = req_ctx.audit_source("cloudaudit_googleapis_com_data_access")
        self.assertIsNone(src)
        self.assertEqual(req_ctx.warnings, [])

    def test_media_clip_sku_rejected_from_token_pricing(self):
        from controlplane.services import pricing
        skus = [
            {
                "description": "Lyria 3 Output",
                "pricingInfo": [{
                    "currencyConversionRate": 1344.0,
                    "effectiveTime": "2026-11-01T00:00:00Z",
                    "pricingExpression": {
                        "usageUnit": "count",
                        "usageUnitDescription": "count",
                        "baseUnitConversionFactor": 1,
                        "tieredRates": [{"unitPrice": {"units": "107", "nanos": 520000000}}],
                    },
                }],
            },
            {
                "description": "Gemini 2.5 Flash Text Input",
                "pricingInfo": [{
                    "currencyConversionRate": 1344.0,
                    "effectiveTime": "2026-11-01T00:00:00Z",
                    "pricingExpression": {
                        "usageUnit": "count",
                        "usageUnitDescription": "count",
                        "baseUnitConversionFactor": 1,
                        "tieredRates": [{"unitPrice": {"units": "0", "nanos": 403}}],
                    },
                }],
            },
        ]
        parsed = [(k, d) for k, d, *_ in pricing.catalog_entries(skus) if k]
        self.assertEqual(parsed, [("gemini-2.5-flash", "input")])
        self.assertFalse(pricing_sync.valid_token_entry("lyria-3", {"output": 80000.0}))
        _, rate = pricing_sync.lookup_model_rate({"gemini-2.5-flash": {"input": 0.3, "output": 2.5}}, "lyria-3")
        self.assertIsNone(rate)

    def test_audit_source_avoids_cross_region_bigquery_union(self):
        s_ts = schema.TableSchema([schema._DictField({"name": "timestamp", "type": "TIMESTAMP"})])

        class MultiRegionRegistry:
            def dataset_location(self, project, _dataset, _quota):
                return "ASIA-NORTHEAST3" if project == "proj-kr" else "US"

            def tables(self, project, _dataset, _quota, _max_bytes):
                if project == "proj-kr":
                    return {"cloudaudit_googleapis_com_data_access": {"kind": "table"}}
                return {
                    "cloudaudit_googleapis_com_data_access": {"kind": "table"},
                    "discoveryengine_googleapis_com_gen_ai_user_message": {"kind": "table"},
                }

            def source(self, project, dataset, logical, _quota, _max_bytes):
                loc = self.dataset_location(project, dataset, _quota)
                return schema.Source(project, dataset, logical, "table", logical, s_ts, location=loc)

        app = support.FakeApp()
        app.registry = MultiRegionRegistry()
        eff = support.make_settings(workspace={
            "project_id": "proj-kr",
            "projects": ["proj-us"],
            "scan_all_projects": False,
            "audit_dataset": "ge_analytics",
        })
        req_ctx = base.Ctx(
            app=app,
            settings=eff,
            version=1,
            identity=Identity("admin@example.com", "admin", "google", "u:admin"),
        )
        src = req_ctx.audit_source("cloudaudit_googleapis_com_data_access")
        self.assertIsNotNone(src)
        self.assertEqual(src.project, "proj-us")
        self.assertEqual(src.location, "US")


if __name__ == "__main__":
    unittest.main()

