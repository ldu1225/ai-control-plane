"""Per-user x per-model breakdown across Antigravity status, Users list/detail, and Chargeback."""

import unittest

import support

from controlplane.services import chargeback, governance as gov, users as users_svc


class UserModelsBreakdownTest(unittest.TestCase):
    def test_governance_model_rows_sorted_by_tokens_with_cached_and_output(self):
        pricing = gov.Pricing(
            currency="USD",
            models=(("gemini-4-neon", 3.0, 15.0), ("gemini-4-carbon", 4.0, 20.0)),
            source="manual",
            cached_models=(("gemini-4-neon", 0.15), ("gemini-4-carbon", 0.2)),
        )
        rec = {
            "byModel": {
                "gemini-4-carbon": {
                    "requests": 2, "tokens": 15022, "promptTokens": 14990,
                    "candidateTokens": 12, "cachedTokens": 0,
                },
                "gemini-4-neon": {
                    "requests": 6, "tokens": 102377, "promptTokens": 101328,
                    "candidateTokens": 778, "cachedTokens": 73534,
                },
            }
        }
        rows = gov._model_rows(rec, pricing, ("gemini",))  # pylint: disable=protected-access
        self.assertEqual([r["model"] for r in rows], ["gemini-4-neon", "gemini-4-carbon"])
        neon, carbon = rows[0], rows[1]
        self.assertEqual(neon["cachedTokens"], 73534)
        self.assertEqual(neon["outputTokens"], 1049)  # 778 candidates + 271 thoughts
        self.assertAlmostEqual(neon["cost"], 0.110147, places=6)
        self.assertTrue(neon["credit"])
        self.assertIsNotNone(neon["costShare"])
        self.assertIsNotNone(carbon["costShare"])
        self.assertAlmostEqual(neon["costShare"] + carbon["costShare"], 1.0, places=3)

    def test_users_agy_and_agent_platform_entries_combine_cleanly(self):
        pricing = gov.Pricing(
            currency="USD",
            models=(("claude-opus-5-5", 15.0, 75.0), ("gemini-3.5-flash-lite", 0.1, 0.4)),
            source="manual",
            cached_models=(("claude-opus-5-5", 1.5),),
        )
        agy = users_svc._agy_model_entries({  # pylint: disable=protected-access
            "gemini-3.5-flash-lite": {
                "requests": 10, "tokens": 50000, "promptTokens": 40000,
                "candidateTokens": 10000, "cachedTokens": 0,
            },
            "claude-opus-5-5": {
                "requests": 3, "tokens": 12000, "promptTokens": 10000,
                "candidateTokens": 2000, "cachedTokens": 8000,
            },
        }, pricing, ("gemini",), email="alice@example.com")
        self.assertEqual(len(agy), 2)
        # Cost-first sort puts higher-cost claude-opus-5-5 ahead of higher-token flash-lite.
        self.assertEqual(agy[0]["model"], "claude-opus-5-5")
        self.assertEqual(agy[0]["surface"], "antigravity")
        self.assertEqual(agy[0]["email"], "alice@example.com")
        self.assertEqual(agy[0]["cachedTokens"], 8000)
        self.assertEqual(agy[0]["outputTokens"], 2000)
        self.assertFalse(agy[0]["credit"])
        self.assertAlmostEqual(agy[0]["cost"], 0.192, places=6)  # 2k*15/1M + 8k*1.5/1M + 2k*75/1M
        self.assertGreater(agy[0]["costShare"], 0.95)
        self.assertAlmostEqual(agy[0]["costShare"] + agy[1]["costShare"], 1.0, places=3)

        ap = users_svc._ap_model_entries(  # pylint: disable=protected-access
            [{"email": "alice@example.com", "model": "gemini-3.7-flash", "requests": 4, "lastUsed": "2026-11-19T08:00:00Z"}],
            include_user=True,
        )
        self.assertEqual(len(ap), 1)
        self.assertEqual(ap[0]["surface"], "agent_platform")
        self.assertEqual(ap[0]["requests"], 4)
        self.assertIsNone(ap[0]["tokens"])
        self.assertIsNone(ap[0]["costShare"])
        self.assertEqual(ap[0]["email"], "alice@example.com")

    def test_chargeback_rollup_preserves_uid_and_by_model(self):
        from controlplane.services import org_model

        org = org_model.OrgModel({})
        report_row = {
            "uid": "1234567890abcdef",
            "email": "alice@example.com",
            "requests": 5,
            "tokens": 50000,
            "cost": 0.25,
            "chargeable": 0.10,
            "credit": 0.15,
            "tier": "SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT",
            "creditSource": "license",
            "byModel": [{"model": "gemini-4-neon", "requests": 5, "tokens": 50000, "cost": 0.25}],
            "byProject": [{"project": support.PROJECT, "requests": 5, "tokens": 50000,
                           "cost": 0.25, "credit": 0.15, "chargeable": 0.10}],
        }
        facts = chargeback.usage_facts(report_row, "credit_share")
        for f in facts:
            f["unit"], f["via"] = org_model.UNASSIGNED, "unassigned"
        built = chargeback._rollup(  # pylint: disable=protected-access
            "2026-11", {"complete": False, "usageSource": "bigquery"},
            org, facts, {"alice@example.com": report_row}, None,
            "credit_share", "USD", False, "", {},
        )
        self.assertEqual(len(built["users"]), 1)
        u = built["users"][0]
        self.assertEqual(u["uid"], "1234567890abcdef")
        self.assertEqual(len(u["byModel"]), 1)
        self.assertEqual(u["byModel"][0]["model"], "gemini-4-neon")


if __name__ == "__main__":
    unittest.main()
