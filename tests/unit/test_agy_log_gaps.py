"""Antigravity usage around the BigQuery sink's start, and telemetry detection after a reset.

Log Router copies an entry into the sink table only if the sink existed when the entry arrived, and it creates the
table with the first copied entry. When Metadata logging is turned on before the dashboard is installed (the order
the field guide uses), the first calls exist only in Cloud Logging. Reading the table alone would drop them from that
week's caps and from that month's report, so those time ranges are read from Cloud Logging and added.
"""

import copy
import datetime as dt
import unittest

import support

from controlplane.schema import Source, TableSchema, UnionSource
from controlplane.services import governance as gov
from controlplane.services.base import AGY_SINK
from test_inference_log_users import END, OFFICIAL_SAMPLE, START

UTC = dt.timezone.utc
OTHER = "linked-project"


def sink_source(project=support.PROJECT, created=None):
    schema = TableSchema.from_dicts([
        {"name": "timestamp", "type": "TIMESTAMP"},
        {"name": "logName", "type": "STRING"},
        {"name": "labels", "type": "RECORD", "fields": [
            {"name": name, "type": "STRING"} for name in ("model", "client_name", "user_id")]},
        {"name": "jsonPayload", "type": "RECORD", "fields": [
            {"name": "metadata", "type": "RECORD", "fields": [
                {"name": name, "type": "STRING"}
                for name in ("promptTokenCount", "cachedContentTokenCount", "candidatesTokenCount",
                             "totalTokenCount")]}]},
    ])
    return Source(project, "audit", AGY_SINK, "table", AGY_SINK, schema, created=created)


def entry_at(ts, **labels):
    e = copy.deepcopy(OFFICIAL_SAMPLE)
    e["timestamp"] = ts.isoformat().replace("+00:00", "Z")
    e["labels"].update(labels)
    return e


class GapCtx(support.FakeCtx):
    """BigQuery answers the per-user query with `bq_rows` and the first-entry query with `first`; Cloud Logging
    answers entries:list with the entries inside the requested time range."""

    def __init__(self, src, bq_rows=(), first=None, entries=(), projects=(), usage_source="auto"):
        settings = support.make_settings()
        settings["workspace"]["projects"] = list(projects)
        settings["governance"]["antigravity"]["usage_source"] = usage_source
        super().__init__(settings)
        self.src = src
        self.bq_rows = list(bq_rows)
        self.first = first or {}
        self.entries = list(entries)
        self.queries = []
        self.bodies = []

    def audit_source(self, logical, optional=False):
        return self.src if logical == AGY_SINK else None

    def query(self, sql, params, module, **kwargs):
        self.queries.append(sql)
        if "GROUP BY email, model" in sql:
            return list(self.bq_rows)
        if "AS first_ts" in sql:
            return [{"project": p, "first_ts": ts} for p, ts in self.first.items()]
        if "SELECT 1 FROM s LIMIT 1" in sql:
            return [{"n": 1 if self.bq_rows else 0}]
        raise AssertionError("unexpected query: " + sql[:120])

    def rest(self, method, url, **kwargs):
        body = kwargs.get("json_body") or {}
        self.bodies.append(body)
        flt = body.get("filter", "")
        project = body["resourceNames"][0].split("/", 1)[1]
        out = []
        for e in self.entries:
            if not e["logName"].startswith("projects/%s/" % project) and "[PROJECT_ID]" not in e["logName"]:
                continue
            if 'timestamp>="' in flt and e["timestamp"] < flt.split('timestamp>="', 1)[1].split('"', 1)[0]:
                continue
            if 'timestamp<"' in flt and e["timestamp"] >= flt.split('timestamp<"', 1)[1].split('"', 1)[0]:
                continue
            out.append(copy.deepcopy(e))
        return {"entries": out[: body.get("pageSize") or len(out)]}


def bq_row(requests, email="user@example.com"):
    return {"email": email, "model": "claude-sonnet-5-5", "requests": requests, "tokens": 85040 * requests,
            "prompt_tokens": 70825 * requests, "candidate_tokens": 14215 * requests,
            "cached_tokens": 69079 * requests, "clients": ["antigravity_cli"], "last_ts": None}


class LoggingGapsTest(unittest.TestCase):
    def test_no_gap_when_the_sink_table_predates_the_window(self):
        ctx = GapCtx(sink_source(created=START - dt.timedelta(days=3)))
        self.assertEqual(gov.logging_gaps(ctx, ctx.src, START, END), {})
        self.assertEqual(ctx.queries, [])  # the common case costs no extra query

    def test_unknown_creation_time_keeps_the_table_only(self):
        ctx = GapCtx(sink_source(created=None))
        self.assertEqual(gov.logging_gaps(ctx, ctx.src, START, END), {})

    def test_table_created_mid_window_reads_logging_until_its_first_entry(self):
        first = START + dt.timedelta(days=4, hours=2)
        ctx = GapCtx(sink_source(created=first + dt.timedelta(seconds=3)), first={support.PROJECT: first})
        self.assertEqual(gov.logging_gaps(ctx, ctx.src, START, END), {support.PROJECT: (START, first)})

    def test_window_before_the_table_existed_reads_logging_only(self):
        ctx = GapCtx(sink_source(created=END + dt.timedelta(days=2)))
        self.assertEqual(gov.logging_gaps(ctx, ctx.src, START, END), {support.PROJECT: (START, END)})

    def test_scanned_project_without_a_sink_table_reads_logging(self):
        ctx = GapCtx(sink_source(created=START - dt.timedelta(days=30)), projects=[OTHER])
        self.assertEqual(gov.logging_gaps(ctx, ctx.src, START, END), {OTHER: (START, END)})

    def test_union_source_checks_each_project(self):
        first = START + dt.timedelta(days=1)
        src = UnionSource([sink_source(created=START - dt.timedelta(days=9)),
                           sink_source(project=OTHER, created=first + dt.timedelta(seconds=1))])
        ctx = GapCtx(src, first={support.PROJECT: START + dt.timedelta(hours=1), OTHER: first}, projects=[OTHER])
        self.assertEqual(gov.logging_gaps(ctx, src, START, END), {OTHER: (START, first)})


class MergedUsageTest(unittest.TestCase):
    def test_entries_before_the_table_are_added_once(self):
        first = START + dt.timedelta(days=4)
        before = [entry_at(START + dt.timedelta(days=1)), entry_at(START + dt.timedelta(days=2))]
        after = entry_at(first + dt.timedelta(minutes=5))  # also in BigQuery (the row below): must not count twice
        before[0]["logName"] = before[1]["logName"] = after["logName"] = (
            "projects/%s/logs/businessaicode.googleapis.com%%2Finference_response" % support.PROJECT)
        ctx = GapCtx(sink_source(created=first), bq_rows=[bq_row(1)], first={support.PROJECT: first},
                     entries=before + [after])
        usage = gov.antigravity_usage(ctx, START, END)
        rec = usage.users["user@example.com"]
        self.assertEqual((rec["requests"], rec["tokens"], rec["cachedTokens"]), (3, 3 * 85040, 3 * 69079))
        self.assertEqual(rec["byModel"]["claude-sonnet-5-5"]["requests"], 3)
        self.assertEqual(usage.records, 3)
        self.assertEqual(len(ctx.bodies), 1)
        self.assertIn('timestamp<"%s"' % gov.iso(first), ctx.bodies[0]["filter"])
        self.assertEqual(ctx.sources, {"bigquery:audit.%s" % AGY_SINK,
                                       "logging:businessaicode.googleapis.com/inference_response"})

    def test_bigquery_only_mode_never_reads_logging(self):
        first = START + dt.timedelta(days=4)
        ctx = GapCtx(sink_source(created=first), bq_rows=[bq_row(1)], first={support.PROJECT: first},
                     entries=[entry_at(START + dt.timedelta(days=1))], usage_source="bigquery")
        usage = gov.antigravity_usage(ctx, START, END)
        self.assertEqual(usage.users["user@example.com"]["requests"], 1)
        self.assertEqual(ctx.bodies, [])

    def test_merge_sums_counters_models_clients_and_days(self):
        a, b = gov.Usage("bigquery"), gov.Usage("logging")
        for usage, n in ((a, 1), (b, 2)):
            rec = usage.user("user@example.com")
            rec["requests"] += n
            rec["tokens"] += 10 * n
            rec["clients"].add("antigravity_cli" if n == 1 else "antigravity_ide")
            gov.Usage.add_model(rec, "claude-sonnet-5-5", n, 10 * n, 6 * n, 4 * n, 5 * n)
            usage.records += n
            usage.by_model["claude-sonnet-5-5"] = {"requests": n, "tokens": 10 * n, "promptTokens": 6 * n,
                                                   "candidateTokens": 4 * n, "cachedTokens": 5 * n}
        a.daily[("user@example.com", dt.date(2026, 10, 2))] = (1, 10)
        b.daily[("user@example.com", "2026-10-02")] = (2, 20)
        a.merge(b)
        rec = a.users["user@example.com"]
        self.assertEqual((rec["requests"], rec["tokens"], a.records), (3, 30, 3))
        self.assertEqual(rec["clients"], {"antigravity_cli", "antigravity_ide"})
        self.assertEqual(rec["byModel"]["claude-sonnet-5-5"]["cachedTokens"], 15)
        self.assertEqual(a.by_model["claude-sonnet-5-5"]["requests"], 3)
        self.assertEqual(a.daily, {("user@example.com", dt.date(2026, 10, 2)): (3, 30)})


def real_sink_source():
    """Schema of a real Log Router table of the Antigravity inference log (captured in a test project): the payload
    column is named after the entry's @type and its field names are lower-cased."""
    schema = TableSchema.from_dicts([
        {"name": "logName", "type": "STRING"},
        {"name": "jsonpayload_v1_inferenceresponselog", "type": "RECORD", "fields": [
            {"name": "_type", "type": "STRING"},
            {"name": "metadata", "type": "RECORD", "fields": [
                {"name": name, "type": "STRING"}
                for name in ("prompttokencount", "cachedcontenttokencount", "candidatestokencount",
                             "totaltokencount")]}]},
        {"name": "timestamp", "type": "TIMESTAMP"},
        {"name": "receiveTimestamp", "type": "TIMESTAMP"},
        {"name": "labels", "type": "RECORD", "fields": [
            {"name": name, "type": "STRING"}
            for name in ("request_id", "trajectory_id", "model", "model_provider", "client_name", "user_id")]},
    ])
    return Source(support.PROJECT, "audit", AGY_SINK, "table", AGY_SINK, schema, created=START)


class RealSinkSchemaTest(unittest.TestCase):
    """Reading jsonPayload.metadata only gave every call 0 tokens and US$0 once the sink table existed."""

    def test_token_counts_come_from_the_typed_payload_column(self):
        f = gov.agy_fields(real_sink_source())
        col = "t.`jsonpayload_v1_inferenceresponselog`.`metadata`.`%s`"
        self.assertEqual(f["total"], "SAFE_CAST(%s AS INT64)" % (col % "totaltokencount"))
        self.assertEqual(f["prompt"], "SAFE_CAST(%s AS INT64)" % (col % "prompttokencount"))
        self.assertEqual(f["candidates"], "SAFE_CAST(%s AS INT64)" % (col % "candidatestokencount"))
        self.assertEqual(f["cached"], "SAFE_CAST(%s AS INT64)" % (col % "cachedcontenttokencount"))

    def test_untyped_and_typed_payloads_are_both_read(self):
        src = real_sink_source()
        src.schema.tree.update(sink_source().schema.tree)  # entries without @type land in jsonPayload
        f = gov.agy_fields(src)
        self.assertTrue(f["total"].startswith("COALESCE(SAFE_CAST(t.`jsonpayload_v1_inferenceresponselog`"))
        self.assertIn("t.`jsonPayload`.`metadata`.`totalTokenCount`", f["total"])

    def test_usage_query_uses_the_typed_columns(self):
        ctx = GapCtx(real_sink_source(), bq_rows=[bq_row(2)])
        usage = gov._bq_usage(ctx, ctx.src, START, END)  # pylint: disable=protected-access
        self.assertIn("`jsonpayload_v1_inferenceresponselog`.`metadata`.`totaltokencount`", ctx.queries[0])
        self.assertEqual(usage.users["user@example.com"]["tokens"], 2 * 85040)


class TelemetrySeenTest(unittest.TestCase):
    NOW = END - dt.timedelta(days=1)

    def usage(self, records):
        u = gov.Usage("logging")
        u.ok = True
        u.records = records
        return u

    def test_usage_in_the_window_needs_no_probe(self):
        ctx = GapCtx(None)
        self.assertTrue(gov.telemetry_seen(ctx, self.usage(4), self.NOW))
        self.assertEqual((ctx.queries, ctx.bodies), ([], []))

    def test_empty_window_after_a_reset_still_detects_recent_logs(self):
        ctx = GapCtx(None, entries=[entry_at(self.NOW - dt.timedelta(days=6))])
        self.assertTrue(gov.telemetry_seen(ctx, self.usage(0), self.NOW))
        self.assertIn('timestamp>="%s"' % gov.iso(self.NOW - gov.TELEMETRY_LOOKBACK), ctx.bodies[0]["filter"])

    def test_sink_table_with_recent_rows_counts(self):
        ctx = GapCtx(sink_source(created=START), bq_rows=[bq_row(1)])
        self.assertTrue(gov.telemetry_seen(ctx, self.usage(0), self.NOW))
        self.assertEqual(ctx.bodies, [])

    def test_no_logs_in_30_days_is_not_detected(self):
        ctx = GapCtx(None, entries=[entry_at(self.NOW - dt.timedelta(days=45))])
        self.assertFalse(gov.telemetry_seen(ctx, self.usage(0), self.NOW))

    def test_unreadable_usage_is_not_detected(self):
        u = gov.Usage("logging")
        self.assertFalse(gov.telemetry_seen(GapCtx(None), u, self.NOW))


if __name__ == "__main__":
    unittest.main()
