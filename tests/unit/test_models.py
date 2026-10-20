"""Model family normalization, SKU classification and Code Assist exclusion."""

import datetime as dt
import unittest
from unittest import mock

import support

from controlplane.gcp import GcpError
from controlplane.services import billing, models, monitoring


def key_of(sku):
    return models.model_key(models.family_name(sku))


class FamilyNameTest(unittest.TestCase):
    def test_model_id_examples(self):
        self.assertEqual(models.family_name("Gemini 3.8 Flash Text Input Tokens"), "Gemini 3.8 Flash")
        self.assertEqual(key_of("Gemini 3.8 Flash Text Input Tokens"), "gemini-3.8-flash")
        self.assertEqual(key_of("Claude Sonnet 4.5 Output Tokens"), "claude-sonnet-4.5")
        self.assertEqual(key_of("Veo 3.1 Video Generation with audio"), "veo-3.1")

    def test_no_hard_coded_versions(self):
        cases = {
            "Gemini 3.8 Pro Text Output Tokens >200K": "gemini-3.8-pro",
            "Gemini 4 Argon Cached Input Tokens": "gemini-4-argon",
            "Gemini 3.8 Flash - Long Context Input Tokens": "gemini-3.8-flash",
            "Gemini 3.8 Flash Thinking Output Tokens (Priority)": "gemini-3.8-flash",
            "Claude Sonnet 4.5 Input Tokens Batch": "claude-sonnet-4.5",
            "Imagen 4 Image Generation": "imagen-4",
            "Llama 4 Maverick Input Tokens": "llama-4-maverick",
            "Gemini 9.9 Ultra Audio Input Tokens": "gemini-9.9-ultra",
        }
        for sku, key in cases.items():
            self.assertEqual(key_of(sku), key, sku)

    def test_vendor(self):
        self.assertEqual(models.vendor_for("Gemini 3.8 Flash"), "Google")
        self.assertEqual(models.vendor_for("Veo 3.1"), "Google")
        self.assertEqual(models.vendor_for("Claude Sonnet 4.5"), "Anthropic")
        self.assertEqual(models.vendor_for("Llama 4 Maverick"), "Meta")
        self.assertEqual(models.vendor_for("Mistral Large 3"), "Mistral AI")
        self.assertEqual(models.vendor_for("Jamba 2"), "Partner")

    def test_monitoring_model_ids(self):
        self.assertEqual(models.normalize_model_id("publishers/google/models/gemini-3.8-flash-001"), "gemini-3.8-flash")
        self.assertEqual(models.normalize_model_id("claude-sonnet-4-5@20250929"), "claude-sonnet-4.5")
        self.assertEqual(models.normalize_model_id("gemini-4-argon"), "gemini-4-argon")
        self.assertEqual(models.display_from_key("gemini-3.8-flash"), "Gemini 3.8 Flash")

    def test_measure_classification(self):
        self.assertEqual(models.classify_measure("Gemini 3.8 Flash Text Input Tokens", "count"), "input_tokens")
        self.assertEqual(models.classify_measure("Gemini 3.8 Flash Text Output Tokens", "count"), "output_tokens")
        self.assertEqual(models.classify_measure("Gemini 4 Argon Cached Input Tokens", "count"), "cached_tokens")
        self.assertEqual(models.classify_measure("Imagen 4 Image Generation", "count"), "images")
        self.assertEqual(models.classify_measure("Veo 3.1 Video Generation with audio", "seconds"), "seconds")
        self.assertEqual(models.classify_measure("Chirp 3 HD Characters", "count"), "characters")
        self.assertEqual(models.classify_measure("Gemini Context Cache Storage", "gibibyte hour"), "other")
        self.assertEqual(models.classify_measure("Something", "count"), "other")


class ExclusionTest(unittest.TestCase):
    def test_code_assist_and_duet_ai_patterns(self):
        patterns = support.make_settings()["model_catalog"]["excluded_sku_patterns"]
        self.assertIn("code assist", patterns)
        self.assertIn("duet ai", patterns)
        self.assertTrue(billing.is_excluded("Gemini Code Assist Enterprise", patterns))
        self.assertTrue(billing.is_excluded("Duet AI for Google Cloud", patterns))
        self.assertFalse(billing.is_excluded("Gemini 3.8 Flash Text Input Tokens", patterns))

    def test_sql_clause_is_parameterized(self):
        clause = billing.excluded_clause({"sku": "b.`sku`.`description`", "service": "b.`service`.`description`"})
        self.assertIn("UNNEST(@excluded)", clause)
        self.assertTrue(clause.startswith("NOT EXISTS"))


class _Src:
    label = "bigquery:billing_detailed_usage.export"


D1, D2 = dt.date(2026, 10, 18), dt.date(2026, 10, 19)
BILLING_ROWS = [
    {"bucket": D1, "sku": "Gemini 3.8 Flash Text Input Tokens", "service": "Vertex AI", "unit": "count",
     "pricing_unit": "count", "amount": 1000000.0, "cost": 0.3, "currency": "USD"},
    {"bucket": D2, "sku": "Gemini 3.8 Flash Text Output Tokens", "service": "Vertex AI", "unit": "count",
     "pricing_unit": "count", "amount": 200000.0, "cost": 0.5, "currency": "USD"},
    {"bucket": D2, "sku": "Claude Sonnet 4.5 Output Tokens", "service": "Claude Sonnet 4.5", "unit": "count",
     "pricing_unit": "count", "amount": 50000.0, "cost": 0.75, "currency": "USD"},
    {"bucket": D1, "sku": "Veo 3.1 Video Generation with audio", "service": "Vertex AI", "unit": "seconds",
     "pricing_unit": "second", "amount": 8.0, "cost": 4.0, "currency": "USD"},
    {"bucket": D2, "sku": "Gemini Code Assist Enterprise", "service": "Gemini Code Assist", "unit": "count",
     "pricing_unit": "month", "amount": 1.0, "cost": 19.0, "currency": "USD"},
    {"bucket": D2, "sku": "Duet AI for Google Cloud", "service": "Duet AI", "unit": "count",
     "pricing_unit": "month", "amount": 1.0, "cost": 30.0, "currency": "USD"},
]


class UsageAssemblyTest(unittest.TestCase):
    def setUp(self):
        from controlplane import timeutil
        self.rng = timeutil.parse_range({"start": "2026-10-18", "end": "2026-10-19"}, "UTC", 14)
        denied = GcpError("denied", status=403)
        self.patches = [mock.patch.object(monitoring, "token_counts", side_effect=denied),
                        mock.patch.object(monitoring, "invocation_counts", side_effect=denied),
                        mock.patch.object(models, "antigravity_rows", return_value=(None, None))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_billing_only_usage_excludes_code_assist(self):
        ctx = support.FakeCtx()
        ctx.billing_source = lambda: _Src()
        with mock.patch.object(models, "billing_rows", return_value=BILLING_ROWS):
            out = models.build_usage(ctx, self.rng)
        keys = {m["key"] for m in out["models"]}
        self.assertEqual(keys, {"gemini-3.8-flash", "claude-sonnet-4.5", "veo-3.1"})
        self.assertFalse(any("code" in k or "duet" in k for k in keys))
        flash = next(m for m in out["models"] if m["key"] == "gemini-3.8-flash")
        self.assertEqual(flash["tokens"], {"input": 1000000, "output": 200000, "cached": 0, "total": 1200000})
        self.assertEqual(flash["vendor"], "Google")
        veo = next(m for m in out["models"] if m["key"] == "veo-3.1")
        self.assertEqual(veo["otherUsage"], [{"unit": "seconds", "amount": 8.0}])
        self.assertAlmostEqual(out["totals"]["cost"], 5.55, places=6)
        self.assertEqual(out["totals"]["tokens"]["total"], 1250000)
        self.assertIsNone(out["totals"]["invocations"])  # Monitoring failed: unknown, not zero
        self.assertEqual(out["tokenSource"], "billing")
        self.assertEqual(out["tokenTrend"]["buckets"], ["2026-10-18", "2026-10-19"])
        self.assertEqual({w["code"] for w in ctx.warnings}, {"denied"})

    def test_all_sources_failed_yields_nulls(self):
        ctx = support.FakeCtx()  # billing_source() returns None
        out = models.build_usage(ctx, self.rng)
        self.assertIsNone(out["models"])
        self.assertEqual(out["totals"]["tokens"], {"input": None, "output": None, "cached": None, "total": None})
        self.assertIsNone(out["totals"]["cost"])
        self.assertIsNone(out["totals"]["modelsTracked"])
        self.assertIsNone(out["tokenTrend"])
        self.assertIsNone(out["costTrend"])
        self.assertIsNone(out["tokenSource"])


class RealSkuFormatTest(unittest.TestCase):
    """SKU description formats observed in a live billing export."""

    def test_naming_styles_merge(self):
        cases = {
            "Gemini 3.8 Flash Global Text Output - Predictions": "gemini-3.8-flash",
            "Generate content input token count gemini 3.8 flash text": "gemini-3.8-flash",
            "Generate content input token count gemini 3.8 flash image": "gemini-3.8-flash",
            "Gemini 3.5 Flash Global Text Input Caching": "gemini-3.5-flash",
            "Generate content cached input token count gemini 3.5 flash text": "gemini-3.5-flash",
            "Gemini 2.5 Flash GA Thinking Text Output - Predictions": "gemini-2.5-flash",
            "Generate content output token count gemini 2.5 flash short input text": "gemini-2.5-flash",
            "Generate_content video output token count for Gemini Omni Flash": "gemini-omni-flash",
            "Gemini Omni Flash Global Video Output": "gemini-omni-flash",
            "Gemini 3.0 Pro Text Input - Predictions": "gemini-3-pro",
            "Generate_content text cached input token count for gemini 3 pro short": "gemini-3-pro",
            "Generate_content_cached_input_token_count_gemini_3_pro_short_image": "gemini-3-pro",
            "Generate_content image output token count for Gemini 3 Pro Image": "gemini-3-pro-image",
            "Gemini 3.1 Flash Image Image Output - Predictions": "gemini-3.1-flash-image",
            "Gemini 3.1 Flash Lite Global Text Input - Predictions": "gemini-3.1-flash-lite",
            "Generate content output token count gemini 3.1 flash lite preview text": "gemini-3.1-flash-lite",
            "Generate_content audio output token count for Gemini 3.1 flash tts": "gemini-3.1-flash-tts",
            "Bidi generate content output token count gemini 3 flash live audio": "gemini-3-flash-live",
            "Claude Sonnet 4.5 \u2014 Input Cache Read Tokens \u2014 global \u2014 Context Window Size from 0 to 200000 Tokens":
                "claude-sonnet-4.5",
            "Veo 3 Audio Video Generation": "veo-3",
        }
        for sku, key in cases.items():
            info = models.parse_sku(sku)
            self.assertIsNotNone(info, sku)
            self.assertEqual(info["key"], key, sku)
            self.assertIsNone(info["product"], sku)

    def test_display_names(self):
        self.assertEqual(models.parse_sku("Generate_content audio output token count for Gemini 3.1 flash tts")["name"],
                         "Gemini 3.1 Flash TTS")
        self.assertEqual(models.display_name_for_id("publishers/google/models/gemini-2.0-flash-001"),
                         "Gemini 2.0 Flash")
        self.assertEqual(models.normalize_model_id("publishers/google/models/gemini-2.0-flash-001"), "gemini-2-flash")

    def test_monitoring_ids_merge_with_billing(self):
        self.assertEqual(models.normalize_model_id("gemini-3-flash-preview"), "gemini-3-flash")
        self.assertEqual(models.normalize_model_id("gemini-3-pro-preview"), "gemini-3-pro")
        self.assertEqual(models.normalize_model_id("gemini-2.5-flash-preview-09-2025"), "gemini-2.5-flash")
        self.assertEqual(models.normalize_model_id("gemini-2.5-flash-preview-tts"), "gemini-2.5-flash-tts")
        self.assertEqual(models.normalize_model_id("text-embedding-005"), "text-embedding-005")
        self.assertEqual(models.parse_sku("Text Embedding 005 Input Tokens")["key"], "text-embedding-005")

    def test_product_skus(self):
        info = models.parse_sku("CodeMender Gemini 3.7 Flash Global Text Input")
        self.assertEqual((info["key"], info["product"]), ("gemini-3.7-flash", "CodeMender"))
        info = models.parse_sku("Gemini Enterprise AlphaEvolve Output Tokens (Flash 3.5)", "Vertex AI Search")
        self.assertEqual((info["key"], info["product"]), ("gemini-3.5-flash", "Gemini Enterprise AlphaEvolve"))

    def test_non_model_skus(self):
        for sku in ("Vertex Colab CPU for GCE usage - E2 Instance Cores in us-central1", "Agent Platform Compute",
                    "Gemini Enterprise Standard License"):
            self.assertIsNone(models.parse_sku(sku, "Vertex AI"), sku)

    def test_partner_model_from_service_name(self):
        info = models.parse_sku("Input Tokens - global", "Jamba 1.5 Large")
        self.assertEqual(info["key"], "jamba-1.5-large")


class TokenMergeTest(unittest.TestCase):
    def setUp(self):
        from controlplane import timeutil
        self.rng = timeutil.parse_range({"start": "2026-10-18", "end": "2026-10-19"}, "UTC", 14)

    def _run(self, billing, mon_counts):
        counts = {raw: {"input": {D2: v[0]}, "output": {D2: v[1]}, "cached": {D2: 0}, "total": {D2: v[0] + v[1]}}
                  for raw, v in mon_counts.items()}
        ctx = support.FakeCtx()
        ctx.billing_source = lambda: _Src()
        with mock.patch.object(monitoring, "token_counts", return_value=(counts, True)), \
                mock.patch.object(monitoring, "invocation_counts", return_value={}), \
                mock.patch.object(models, "antigravity_rows", return_value=(None, None)), \
                mock.patch.object(models, "billing_rows", return_value=billing):
            return models.build_usage(ctx, self.rng)

    def test_monitoring_plus_gemini_api_without_double_counting(self):
        billing = [
            {"bucket": D2, "sku": "Gemini 3.7 Flash Global Text Input - Predictions", "service": "Vertex AI",
             "unit": "requests", "pricing_unit": "count", "amount": 1000.0, "cost": 0.1, "currency": "USD"},
            {"bucket": D2, "sku": "Generate content input token count gemini 3.7 flash text", "service": "Gemini API",
             "unit": "requests", "pricing_unit": "count", "amount": 500.0, "cost": 0.05, "currency": "USD"},
            {"bucket": D2, "sku": "CodeMender Gemini 3.7 Flash Global Text Input", "service": "Vertex AI",
             "unit": "requests", "pricing_unit": "count", "amount": 1000.0, "cost": 0.1, "currency": "USD"},
            {"bucket": D2, "sku": "Vertex Colab CPU for GCE usage - E2 Instance Cores in us-central1",
             "service": "Vertex AI", "unit": "seconds", "pricing_unit": "hour", "amount": 10.0, "cost": 2.0,
             "currency": "USD"},
        ]
        out = self._run(billing, {"publishers/google/models/gemini-3.7-flash": (1000, 200)})
        keys = [m["key"] for m in out["models"]]
        self.assertEqual(keys, ["gemini-3.7-flash"])
        flash = out["models"][0]
        # Monitoring (Vertex AI) 1,200 + Gemini API 500; the Vertex billing line and the CodeMender line are not added.
        self.assertEqual(flash["tokens"]["total"], 1700)
        self.assertEqual(flash["tokens"]["input"], 1500)
        self.assertAlmostEqual(flash["cost"], 0.15, places=6)
        self.assertEqual(out["tokenSource"], "mixed")
        self.assertEqual(out["products"], [{"name": "CodeMender", "cost": 0.1, "tokens": 1000,
                                            "models": [{"key": "gemini-3.7-flash", "name": "Gemini 3.7 Flash"}]}])
        self.assertAlmostEqual(out["totals"]["productCost"], 0.1, places=6)
        self.assertAlmostEqual(out["totals"]["cost"], 0.15, places=6)


class CatalogSnapshotTest(unittest.TestCase):
    def _manager(self):
        from controlplane import config
        from controlplane.settings_store import MemoryStore, SettingsManager
        base = config.finalize(dict(support.make_settings()))
        return SettingsManager(base, MemoryStore())

    def _entries(self, manager):
        return {e["key"]: e for e in manager.overrides()["model_catalog"]["known_models"]}

    def test_first_observation_seeds_baseline(self):
        manager = self._manager()
        manager.record_models(["gemini-3.8-flash"], "2026-10-19", True)
        self.assertTrue(self._entries(manager)["gemini-3.8-flash"]["baseline"])

    def test_old_usage_from_longer_range_joins_baseline(self):
        manager = self._manager()
        manager.record_models(["gemini-3.8-flash"], "2026-10-19", True)
        manager.record_models(["gemini-2.5-pro", "gemini-4-argon"], "2026-10-19", True, recent={"gemini-4-argon"})
        entries = self._entries(manager)
        self.assertTrue(entries["gemini-2.5-pro"]["baseline"])
        self.assertFalse(entries["gemini-4-argon"]["baseline"])

    def test_manual_enablement_disables_only_fresh_models(self):
        manager = self._manager()
        manager.record_models(["gemini-3.8-flash"], "2026-10-19", False)
        manager.record_models(["gemini-2.5-pro", "gemini-4-argon"], "2026-10-19", False, recent={"gemini-4-argon"})
        disabled = manager.overrides()["model_catalog"].get("disabled_models") or []
        self.assertIn("gemini-4-argon", disabled)
        self.assertNotIn("gemini-2.5-pro", disabled)


class GeminiEnterprisePromptSourceTest(unittest.TestCase):
    def test_event_cte_prefers_message_log_and_falls_back_to_activity(self):
        from controlplane.services import gemini_enterprise as ge
        sql = ge.ev_cte(True, True)
        self.assertIn("msg_used AS", sql)
        self.assertIn("IF(is_prompt AND (SELECT prompts FROM msg_used), 1, 0) AS prompt", sql)
        self.assertIn("IF(is_assist AND NOT (SELECT prompts FROM msg_used), 1, 0) AS prompt", sql)
        self.assertIn("IF(is_upload AND NOT (SELECT uploads FROM msg_used), 1, 0) AS upload", sql)
        only_activity = ge.ev_cte(False, True)
        self.assertNotIn("msg_used", only_activity)
        self.assertIn("IF(is_assist AND NOT FALSE, 1, 0) AS prompt", only_activity)

    def test_prompt_caps(self):
        from controlplane.services import gemini_enterprise as ge
        self.assertEqual(ge.prompt_caps(None, {"prompt": True, "upload": True}), (True, True))
        self.assertEqual(ge.prompt_caps({"prompt": True, "upload": False}, None), (True, False))
        self.assertEqual(ge.prompt_caps(None, None), (False, False))

    def test_assist_methods(self):
        import re
        from controlplane.services import gemini_enterprise as ge
        self.assertTrue(re.search(ge.ASSIST_METHODS, "StreamAssist"))
        self.assertFalse(re.search(ge.ASSIST_METHODS, "WriteUserEvent"))
        self.assertTrue(re.search(ge.UPLOAD_METHODS, "UploadSessionFile"))


def _ge_schemas():
    from controlplane.schema import TableSchema
    msg = TableSchema.from_dicts([
        {"name": "timestamp", "type": "TIMESTAMP"},
        {"name": "trace", "type": "STRING"},
        {"name": "jsonPayload", "type": "RECORD", "fields": [
            {"name": "user", "type": "STRING"},
            {"name": "content", "type": "RECORD", "fields": [
                {"name": "role", "type": "STRING"},
                {"name": "parts", "type": "RECORD", "mode": "REPEATED", "fields": [{"name": "text", "type": "STRING"}]},
            ]},
        ]},
    ])
    act = TableSchema.from_dicts([
        {"name": "timestamp", "type": "TIMESTAMP"},
        {"name": "trace", "type": "STRING"},
        {"name": "jsonPayload", "type": "RECORD", "fields": [
            {"name": "useriamprincipal", "type": "STRING"},
            {"name": "logmetadata", "type": "RECORD", "fields": [{"name": "methodname", "type": "STRING"}]},
            {"name": "response", "type": "RECORD", "fields": [{"name": "fileid", "type": "STRING"}]},
        ]},
    ])
    return msg, act


class _GeCtx(support.FakeCtx):
    """Serves canned rows for the message-log file query and the activity-log upload query."""

    def __init__(self, msg_rows=None, act_rows=None, with_msg=True, act_error=None):
        super().__init__()
        from controlplane import timeutil
        from controlplane.schema import Source
        from controlplane.services.base import GE_ACTIVITY, GE_MESSAGE
        self.range = timeutil.parse_range({"start": "2026-10-06", "end": "2026-10-19"}, "UTC", 14)
        msg_schema, act_schema = _ge_schemas()
        self.srcs = {GE_ACTIVITY: Source(support.PROJECT, "audit", GE_ACTIVITY, "table", GE_ACTIVITY, act_schema)}
        if with_msg:
            self.srcs[GE_MESSAGE] = Source(support.PROJECT, "audit", GE_MESSAGE, "table", GE_MESSAGE, msg_schema)
        self.msg_rows = msg_rows or []
        self.act_rows = act_rows or []
        self.act_error = act_error
        self.queries = []

    def audit_source(self, logical, optional=False):
        return self.srcs.get(logical)

    def query(self, sql, params, module, **kwargs):
        self.queries.append(sql)
        if "FROM act WHERE is_upload" in sql:
            if self.act_error:
                raise self.act_error
            return self.act_rows
        return self.msg_rows


class GeminiEnterpriseUploadFallbackTest(unittest.TestCase):
    MSG_ROW = {"ts": dt.datetime(2026, 10, 18, 9, 0, tzinfo=dt.timezone.utc), "email": "a@example.com",
               "file_name": "plan.docx", "mime": "application/vnd.openxmlformats-officedocument", "total": 1}
    ACT_ROWS = [{"ts": dt.datetime(2026, 10, 18, 9, 0, tzinfo=dt.timezone.utc), "email": "a@example.com",
                 "file_name": None, "mime": None, "file_id": "f-1", "total": 2},
                {"ts": dt.datetime(2026, 10, 17, 9, 0, tzinfo=dt.timezone.utc), "email": "b@example.com",
                 "file_name": None, "mime": None, "file_id": "f-2", "total": 2}]

    def _ge(self):
        from controlplane.services import gemini_enterprise as ge
        return ge

    def test_activity_upload_sql(self):
        ge = self._ge()
        _msg, act = _ge_schemas()
        from controlplane.schema import Source
        src = Source(support.PROJECT, "audit", "activity", "table", "activity", act)
        sql = ge.activity_upload_sql(src)
        self.assertIn("FROM act WHERE is_upload", sql)
        self.assertIn("file_id", sql)
        self.assertIn("CAST(NULL AS STRING) AS file_name", sql)
        self.assertIn("is_upload AND email = @email", ge.activity_upload_sql(src, email_filter=True))
        from controlplane.schema import TableSchema
        bare = Source(support.PROJECT, "audit", "activity", "table", "activity",
                      TableSchema.from_dicts([{"name": "timestamp", "type": "TIMESTAMP"}]))
        self.assertIsNone(ge.activity_upload_sql(bare))  # no method column: uploads cannot be identified

    def test_message_log_rows_win(self):
        ge = self._ge()
        ctx = _GeCtx(msg_rows=[self.MSG_ROW], act_rows=self.ACT_ROWS)
        out = ge.file_uploads(ctx)
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["items"][0]["fileName"], "plan.docx")
        self.assertEqual(out["items"][0]["category"], "document")
        self.assertEqual(len(ctx.queries), 1)  # the activity log is not consulted
        self.assertEqual(ctx.warnings, [])

    def test_empty_message_log_falls_back_to_activity(self):
        ge = self._ge()
        ctx = _GeCtx(msg_rows=[], act_rows=self.ACT_ROWS)
        out = ge.file_uploads(ctx)
        self.assertEqual(out["total"], 2)
        self.assertEqual([i["fileId"] for i in out["items"]], ["f-1", "f-2"])
        self.assertTrue(all(i["fileName"] is None and i["category"] == "other" for i in out["items"]))
        self.assertEqual([(w["code"], w["message"]) for w in ctx.warnings],
                         [("partial_data", ge.ACTIVITY_UPLOAD_NOTE)])

    def test_activity_only_workspace(self):
        ge = self._ge()
        ctx = _GeCtx(act_rows=self.ACT_ROWS, with_msg=False)
        out = ge.file_uploads(ctx)
        self.assertEqual(out["total"], 2)
        self.assertEqual(len(ctx.queries), 1)

    def test_no_uploads_is_empty_not_null(self):
        ge = self._ge()
        ctx = _GeCtx(msg_rows=[], act_rows=[])
        self.assertEqual(ge.file_uploads(ctx), {"items": [], "total": 0})
        self.assertEqual(ctx.warnings, [])

    def test_failed_activity_query(self):
        ge = self._ge()
        denied = GcpError("denied", status=403)
        ctx = _GeCtx(msg_rows=[], act_error=denied)
        self.assertEqual(ge.file_uploads(ctx), {"items": [], "total": 0})  # the message log answered: empty
        self.assertEqual([w["code"] for w in ctx.warnings], ["denied"])
        ctx = _GeCtx(act_error=denied, with_msg=False)
        self.assertEqual(ge.file_uploads(ctx), {"items": None, "total": None})  # nothing answered: unknown


def _full_act_schema():
    from controlplane.schema import TableSchema
    return TableSchema.from_dicts([
        {"name": "timestamp", "type": "TIMESTAMP"},
        {"name": "insertId", "type": "STRING"},
        {"name": "trace", "type": "STRING"},
        {"name": "jsonPayload", "type": "RECORD", "fields": [
            {"name": "useriamprincipal", "type": "STRING"},
            {"name": "logmetadata", "type": "RECORD", "fields": [{"name": "methodname", "type": "STRING"}]},
            {"name": "request", "type": "RECORD", "fields": [
                {"name": "query", "type": "RECORD", "fields": [
                    {"name": "text", "type": "STRING"},
                    {"name": "parts", "type": "RECORD", "mode": "REPEATED",
                     "fields": [{"name": "text", "type": "STRING"}]},
                ]},
                {"name": "agentsspec", "type": "RECORD", "fields": [
                    {"name": "agentspecs", "type": "RECORD", "mode": "REPEATED", "fields": [
                        {"name": "agentid", "type": "STRING"}, {"name": "scheduleid", "type": "STRING"}]},
                ]},
            ]},
            {"name": "response", "type": "RECORD", "fields": [
                {"name": "fileid", "type": "STRING"}, {"name": "name", "type": "STRING"},
                {"name": "displayname", "type": "STRING"}]},
        ]},
    ])


class ActivitySemanticsTest(unittest.TestCase):
    def _sql(self, schema=None):
        from controlplane.schema import Source
        from controlplane.services import gemini_enterprise as ge
        src = Source(support.PROJECT, "audit", "activity", "table", "activity", schema or _full_act_schema())
        ctes, caps = ge.act_ctes(src, start_param="start_ts")
        return "\n".join(ctes), caps

    def test_duplicate_deliveries_are_dropped(self):
        sql, _ = self._sql()
        self.assertIn("QUALIFY ROW_NUMBER() OVER (PARTITION BY a.`insertId`, a.`timestamp`) = 1", sql)

    def test_prompts_exclude_product_calls_and_scheduled_runs(self):
        sql, caps = self._sql()
        self.assertIn("LIKE '%session_to_summarize%'", sql)
        self.assertIn("LIKE '%For context:%'", sql)
        self.assertIn("q_text IS NOT NULL AND schedule_id IS NULL AS is_assist", sql)
        self.assertIn("$.request.agentsspec.agentspecs[0].scheduleid", sql)
        self.assertTrue(caps["prompt"])

    def test_agent_calls_come_from_agent_addressed_assistant_calls(self):
        sql, caps = self._sql()
        self.assertIn("$.request.agentsspec.agentspecs[0].agentid", sql)
        self.assertRegex(sql, r"IF\(IFNULL\(REGEXP_CONTAINS\(method, r'[^']+'\), FALSE\) AND NOT IFNULL\(q_text LIKE "
                              r"'%session_to_summarize%' OR q_text LIKE '%For context:%', FALSE\), spec_agent, NULL\) "
                              r"AS agent_id")
        self.assertTrue(caps["agent"])

    def test_uploads_count_the_response_only(self):
        sql, caps = self._sql()
        self.assertIn("FALSE) AND file_id IS NOT NULL AS is_upload", sql)
        self.assertTrue(caps["upload"])

    def test_missing_fields_make_metrics_unavailable(self):
        from controlplane.schema import TableSchema
        bare = TableSchema.from_dicts([
            {"name": "timestamp", "type": "TIMESTAMP"},
            {"name": "jsonPayload", "type": "RECORD", "fields": [
                {"name": "logmetadata", "type": "RECORD", "fields": [{"name": "methodname", "type": "STRING"}]}]},
        ])
        sql, caps = self._sql(bare)
        self.assertNotIn("QUALIFY", sql)
        self.assertEqual((caps["prompt"], caps["agent"], caps["upload"]), (False, False, False))

    def test_agent_label(self):
        from controlplane.services import gemini_enterprise as ge
        self.assertEqual(ge.agent_label("123", "Weekly Reporter"), "Weekly Reporter")
        self.assertEqual(ge.agent_label("123", None, {"123": "From Directory"}), "From Directory")
        self.assertEqual(ge.agent_label("skill_generation_agent"), "Skill generation agent")
        self.assertEqual(ge.agent_label("18099188549180822270"), "18099188549180822270")


class EnvironmentListOverrideTest(unittest.TestCase):
    def test_comma_separated_lists_and_locale(self):
        from controlplane import config
        layer = config.env_layer({"DISCOVERY_ENGINE_LOCATIONS": "global, us", "AGENT_ENGINE_REGIONS": "us-central1",
                                  "DEFAULT_LOCALE": "ko"})
        self.assertEqual(layer["workspace"]["discovery_engine_locations"], ["global", "us"])
        self.assertEqual(layer["workspace"]["agent_engine_regions"], ["us-central1"])
        self.assertEqual(layer["branding"]["default_locale"], "ko")


if __name__ == "__main__":
    unittest.main()
