"""Schema-adaptive SQL building: expressions, source CTEs and sharded tables."""

import unittest

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane import schema
from controlplane.schema import Source, TableSchema

AUDIT_FIELDS = [
    {"name": "timestamp", "type": "TIMESTAMP"},
    {"name": "receiveTimestamp", "type": "TIMESTAMP"},
    {"name": "protopayload_auditlog", "type": "RECORD", "fields": [
        {"name": "serviceName", "type": "STRING"},
        {"name": "methodName", "type": "STRING"},
        {"name": "authenticationInfo", "type": "RECORD", "fields": [{"name": "principalEmail", "type": "STRING"}]},
        {"name": "status", "type": "RECORD", "fields": [{"name": "code", "type": "INTEGER"}]},
        {"name": "authorizationInfo", "type": "RECORD", "mode": "REPEATED", "fields": [
            {"name": "permission", "type": "STRING"}]},
    ]},
    {"name": "jsonPayload", "type": "JSON"},
    {"name": "labels", "type": "RECORD", "fields": [{"name": "user_id", "type": "STRING"}]},
    {"name": "textPayload", "type": "STRING"},
    {"name": "bad-name", "type": "STRING"},
]


def audit_schema(**kwargs):
    return TableSchema.from_dicts(AUDIT_FIELDS, **kwargs)


class ExpressionTest(unittest.TestCase):
    def setUp(self):
        self.s = audit_schema()

    def test_direct_columns_are_quoted_and_case_insensitive(self):
        self.assertEqual(schema.build_expr("t", self.s, "protopayload_auditlog.authenticationinfo.principalemail"),
                         ("t.`protopayload_auditlog`.`authenticationInfo`.`principalEmail`", "STRING"))
        self.assertEqual(schema.build_expr("t", self.s, "protopayload_auditlog.status.code"),
                         ("t.`protopayload_auditlog`.`status`.`code`", "INT64"))

    def test_json_column_paths(self):
        self.assertEqual(schema.build_expr("m", self.s, "jsonPayload.content.parts[0].text"),
                         ("JSON_VALUE(m.`jsonPayload`, '$.content.parts[0].text')", "STRING"))
        self.assertEqual(schema.build_expr("m", self.s, "jsonPayload"), ("JSON_VALUE(m.`jsonPayload`)", "STRING"))

    def test_string_column_treated_as_json_text(self):
        self.assertEqual(schema.build_expr("t", self.s, "textPayload.severity"),
                         ("JSON_VALUE(t.`textPayload`, '$.severity')", "STRING"))

    def test_repeated_ancestor_uses_to_json_string(self):
        self.assertEqual(schema.build_expr("t", self.s, "protopayload_auditlog.authorizationInfo.permission"),
                         ("JSON_VALUE(TO_JSON_STRING(t.`protopayload_auditlog`), '$.authorizationInfo[0].permission')",
                          "STRING"))

    def test_missing_or_invalid_paths_are_never_referenced(self):
        for candidate in ("protopayload_auditlog.nope", "nope", "labels", "protopayload_auditlog.status.code.x",
                          "bad-name", "a;DROP", "jsonPayload.a'b"):
            self.assertIsNone(schema.build_expr("t", self.s, candidate), candidate)
        self.assertIsNone(schema.build_expr("t;", self.s, "timestamp"))
        self.assertIsNone(schema.expr("t", self.s, "missing.one", "missing.two"))
        self.assertIsNone(schema.expr("t", None, "timestamp"))

    def test_coalesce_with_common_type(self):
        self.assertEqual(schema.expr("t", self.s, "missing", "labels.user_id", "protopayload_auditlog.status.code"),
                         "COALESCE(t.`labels`.`user_id`, SAFE_CAST(t.`protopayload_auditlog`.`status`.`code` AS STRING))")
        self.assertEqual(schema.expr("t", self.s, "protopayload_auditlog.status.code", cast="STRING"),
                         "SAFE_CAST(t.`protopayload_auditlog`.`status`.`code` AS STRING)")
        self.assertEqual(schema.expr("t", self.s, "labels.user_id", "labels.user_id"), "t.`labels`.`user_id`")

    def test_leaf_paths_and_types(self):
        self.assertIn("protopayload_auditlog.authenticationInfo.principalEmail", self.s.leaf_paths())
        self.assertNotIn("bad-name", self.s.leaf_paths())
        types = dict(self.s.leaf_types())
        self.assertEqual(types["protopayload_auditlog.authorizationInfo.permission"], "STRING REPEATED")
        self.assertEqual(self.s.first_timestamp(), "timestamp")


class SourceCteTest(unittest.TestCase):
    def test_plain_table_cte(self):
        src = Source("demo-project", "ge_analytics", "cloudaudit_googleapis_com_activity", "table",
                     "cloudaudit_googleapis_com_activity", audit_schema())
        self.assertEqual(src.label, "bigquery:ge_analytics.cloudaudit_googleapis_com_activity")
        self.assertEqual(src.cte("act"),
                         "act AS (SELECT * FROM `demo-project.ge_analytics.cloudaudit_googleapis_com_activity` "
                         "WHERE `timestamp` >= @start_ts AND `timestamp` < @end_ts)")

    def test_sharded_table_cte_prunes_suffixes(self):
        src = Source("demo-project", "ge_analytics", "cloudaudit_googleapis_com_data_access", "sharded",
                     "cloudaudit_googleapis_com_data_access", audit_schema(),
                     latest="cloudaudit_googleapis_com_data_access_20261019")
        self.assertTrue(src.sharded)
        self.assertEqual(src.cte("da", start="prev_start_ts"),
                         "da AS (SELECT * FROM `demo-project.ge_analytics.cloudaudit_googleapis_com_data_access_*` "
                         "WHERE _TABLE_SUFFIX BETWEEN @sfx_start AND @sfx_end AND `timestamp` >= @prev_start_ts "
                         "AND `timestamp` < @end_ts)")

    def test_ingestion_partitioned_table_adds_partition_filter(self):
        src = Source("demo-project", "billing", "export", "table", "export", audit_schema(ingestion_partitioned=True))
        self.assertIn("_PARTITIONTIME >= TIMESTAMP_SUB(@start_ts, INTERVAL 3 DAY)", src.cte("bil"))

    def test_time_column_fallback_and_errors(self):
        no_ts = TableSchema.from_dicts([{"name": "usage_start_time", "type": "TIMESTAMP"}, {"name": "cost",
                                                                                            "type": "FLOAT"}])
        src = Source("demo-project", "billing", "export", "table", "export", no_ts)
        self.assertEqual(src.time_column(), "usage_start_time")
        with self.assertRaises(ValueError):
            src.cte("bad name")
        empty = Source("demo-project", "d", "t", "table", "t", TableSchema.from_dicts([{"name": "x", "type": "STRING"}]))
        with self.assertRaises(ValueError):
            empty.cte("c")

    def test_table_ref_validation(self):
        self.assertEqual(schema.table_ref("demo-project", "ds", "t_*"), "`demo-project.ds.t_*`")
        for args in (("Bad_Project", "ds", "t"), ("demo-project", "d-s", "t"), ("demo-project", "ds", "t`; x")):
            with self.assertRaises(ValueError):
                schema.table_ref(*args)


class GroupTablesTest(unittest.TestCase):
    def test_sharded_detection(self):
        groups = schema.group_tables([
            ("cloudaudit_googleapis_com_activity_20261017", "BASE TABLE"),
            ("cloudaudit_googleapis_com_activity_20261019", "BASE TABLE"),
            ("cloudaudit_googleapis_com_activity_20261018", "BASE TABLE"),
            ("modelarmor_googleapis_com_sanitize_operations", "BASE TABLE"),
            ("events_20261019", "BASE TABLE"), ("events", "BASE TABLE"),
            ("bad name", "BASE TABLE"),
        ])
        act = groups["cloudaudit_googleapis_com_activity"]
        self.assertEqual((act["kind"], act["latest"], act["shards"]),
                         ("sharded", "cloudaudit_googleapis_com_activity_20261019", 3))
        self.assertNotIn("cloudaudit_googleapis_com_activity_20261019", groups)
        self.assertEqual(groups["modelarmor_googleapis_com_sanitize_operations"]["kind"], "table")
        self.assertEqual(groups["events"]["kind"], "table")  # a plain table wins over shards
        self.assertNotIn("bad name", groups)


class _Field:
    def __init__(self, name, field_type, mode="NULLABLE", fields=()):
        self.name, self.field_type, self.mode, self.fields = name, field_type, mode, list(fields)


class _Table:
    def __init__(self, fields):
        self.schema = fields
        self.time_partitioning = None
        self.num_rows = 10
        self.num_bytes = 2048
        self.modified = None


class _Result:
    def __init__(self, rows):
        self.rows = rows


class FakeGcp:
    def __init__(self, table_rows, tables):
        self.table_rows = table_rows
        self.tables = tables
        self.queries = []
        self.got = []

    def query(self, sql, params=None, **kwargs):
        self.queries.append(sql)
        return _Result(self.table_rows)

    def bq_client(self, quota_project):
        gcp = self

        class Client:
            def get_table(self, ref, timeout=None):
                gcp.got.append(ref)
                return gcp.tables[ref]
        return Client()


class SchemaRegistryTest(unittest.TestCase):
    def setUp(self):
        fields = [_Field("timestamp", "TIMESTAMP"), _Field("jsonPayload", "RECORD", fields=[
            _Field("useriamprincipal", "STRING")])]
        self.gcp = FakeGcp(
            [{"table_name": "discoveryengine_googleapis_com_gemini_enterprise_user_activity_20261018",
              "table_type": "BASE TABLE"},
             {"table_name": "discoveryengine_googleapis_com_gemini_enterprise_user_activity_20261019",
              "table_type": "BASE TABLE"},
             {"table_name": "cloudaudit_googleapis_com_activity", "table_type": "BASE TABLE"}],
            {"demo-project.ge_analytics.discoveryengine_googleapis_com_gemini_enterprise_user_activity_20261019":
                _Table(fields),
             "demo-project.ge_analytics.cloudaudit_googleapis_com_activity": _Table(fields)})
        self.reg = schema.SchemaRegistry(self.gcp)

    def test_information_schema_query_and_cache(self):
        self.reg.tables("demo-project", "ge_analytics", "demo-project", 10 ** 9)
        self.reg.tables("demo-project", "ge_analytics", "demo-project", 10 ** 9)
        self.assertEqual(self.gcp.queries, ["SELECT table_name, table_type FROM "
                                            "`demo-project.ge_analytics.INFORMATION_SCHEMA.TABLES`"])
        with self.assertRaises(ValueError):
            self.reg.tables("demo-project", "bad-dataset", "demo-project", 10 ** 9)

    def test_resolve_kinds(self):
        sharded = self.reg.resolve("demo-project", "ge_analytics",
                                   "discoveryengine_googleapis_com_gemini_enterprise_user_activity", "demo-project", 1)
        self.assertEqual(sharded["kind"], "sharded")
        self.assertEqual(sharded["fq"], "demo-project.ge_analytics."
                                        "discoveryengine_googleapis_com_gemini_enterprise_user_activity_*")
        plain = self.reg.resolve("demo-project", "ge_analytics", "cloudaudit_googleapis_com_activity", "demo-project", 1)
        self.assertEqual((plain["kind"], plain["fq"]),
                         ("table", "demo-project.ge_analytics.cloudaudit_googleapis_com_activity"))
        missing = self.reg.resolve("demo-project", "ge_analytics", "modelarmor_googleapis_com_sanitize_operations",
                                   "demo-project", 1)
        self.assertEqual(missing, {"kind": "missing", "fq": None})

    def test_source_uses_latest_shard_schema(self):
        src = self.reg.source("demo-project", "ge_analytics",
                              "discoveryengine_googleapis_com_gemini_enterprise_user_activity", "demo-project", 1)
        self.assertTrue(src.sharded)
        self.assertEqual(self.gcp.got, ["demo-project.ge_analytics."
                                        "discoveryengine_googleapis_com_gemini_enterprise_user_activity_20261019"])
        self.assertEqual(schema.expr("a", src.schema, "jsonPayload.useriamprincipal"),
                         "a.`jsonPayload`.`useriamprincipal`")
        self.assertIsNone(self.reg.source("demo-project", "ge_analytics", "absent_table", "demo-project", 1))


if __name__ == "__main__":
    unittest.main()
