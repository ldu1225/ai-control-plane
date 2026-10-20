"""AI Analyst SQL guard: read-only, single statement, allowlisted datasets only."""

import unittest

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane.services.analyst import GuardError, guard_sql

ALLOW = [("demo-project", "ge_analytics"), ("billing-proj", "billing_detailed_usage")]


def check(sql):
    return guard_sql(sql, ALLOW, "demo-project")


class AcceptTest(unittest.TestCase):
    def test_select_and_with_on_allowlisted_datasets(self):
        accepted = [
            "SELECT * FROM ge_analytics.cloudaudit_googleapis_com_activity",
            "SELECT COUNT(*) FROM `demo-project.ge_analytics.modelarmor_googleapis_com_sanitize_operations`",
            "SELECT * FROM `demo-project`.`ge_analytics`.`t`",
            "SELECT * FROM `demo-project.ge_analytics.cloudaudit_googleapis_com_data_access_*` "
            "WHERE _TABLE_SUFFIX >= '20261001'",
            "WITH a AS (SELECT 1 AS x FROM ge_analytics.t) SELECT * FROM a",
            "SELECT service.description, SUM(cost) FROM billing-proj.billing_detailed_usage.gcp_billing_export_v1 "
            "GROUP BY 1",
            "SELECT c.amount FROM `billing-proj.billing_detailed_usage.x` b, UNNEST(b.credits) AS c",
            "SELECT table_name FROM ge_analytics.INFORMATION_SCHEMA.TABLES",
            "SELECT table_name FROM `demo-project.ge_analytics.INFORMATION_SCHEMA.COLUMNS`",
            "SELECT EXTRACT(YEAR FROM timestamp) AS y FROM ge_analytics.t",
            "SELECT * FROM ge_analytics.t WHERE a IS DISTINCT FROM b",
            "(SELECT 1 FROM ge_analytics.t) UNION ALL (SELECT 2 FROM ge_analytics.u)",
        ]
        for sql in accepted:
            self.assertTrue(check(sql), sql)

    def test_comments_and_literals_are_ignored(self):
        self.assertTrue(check("SELECT 'DROP TABLE x; DELETE' AS s FROM ge_analytics.t"))
        self.assertTrue(check("SELECT 1 -- DELETE FROM other.t\nFROM ge_analytics.t"))
        self.assertTrue(check("SELECT 1 /* INSERT INTO x */ FROM ge_analytics.t"))
        self.assertTrue(check("SELECT r'\\d+;' AS rx FROM ge_analytics.t # trailing comment; DROP TABLE x"))

    def test_trailing_semicolons_are_removed(self):
        self.assertEqual(check("SELECT 1 FROM ge_analytics.t;;  "), "SELECT 1 FROM ge_analytics.t")


class RejectTest(unittest.TestCase):
    def assertRejected(self, sql, fragment=None):
        with self.assertRaises(GuardError, msg=sql) as ctx:
            check(sql)
        if fragment:
            self.assertIn(fragment, str(ctx.exception))

    def test_dml_and_ddl(self):
        for sql in ("INSERT INTO ge_analytics.t VALUES (1)", "UPDATE ge_analytics.t SET a = 1 WHERE TRUE",
                    "DELETE FROM ge_analytics.t WHERE TRUE", "CREATE TABLE ge_analytics.x AS SELECT 1",
                    "DROP TABLE ge_analytics.t", "ALTER TABLE ge_analytics.t ADD COLUMN c INT64",
                    "TRUNCATE TABLE ge_analytics.t", "GRANT `roles/bigquery.dataViewer` ON TABLE ge_analytics.t TO x",
                    "MERGE ge_analytics.t T USING ge_analytics.s S ON T.id = S.id WHEN MATCHED THEN DELETE",
                    "EXPORT DATA OPTIONS(uri='gs://b/*') AS SELECT 1", "EXECUTE IMMEDIATE 'SELECT 1'",
                    "CALL ge_analytics.proc()", "BEGIN SELECT 1; END"):
            self.assertRejected(sql)
        self.assertRejected("WITH x AS (SELECT 1) DELETE FROM ge_analytics.t WHERE TRUE", "forbidden keyword")
        self.assertRejected("SELECT 1 FROM ge_analytics.t WHERE TRUE; SET @@query_label = 'x'", "multiple")

    def test_multiple_statements(self):
        self.assertRejected("SELECT 1 FROM ge_analytics.t; DROP TABLE ge_analytics.t", "multiple statements")
        self.assertRejected("SELECT 1 FROM ge_analytics.t; SELECT 2 FROM ge_analytics.t", "multiple statements")
        self.assertRejected("DECLARE x INT64; SELECT 1")

    def test_non_allowlisted_tables(self):
        for sql in ("SELECT * FROM other_ds.t", "SELECT * FROM `evil-project.ge_analytics.t`",
                    "SELECT * FROM ge_analytics.t a JOIN other.t2 b ON a.x = b.x",
                    "SELECT * FROM ge_analytics.t, other.t2",
                    "SELECT * FROM (SELECT * FROM other.t2)",
                    "SELECT * FROM ge_analytics.t WHERE x IN (SELECT y FROM `evil-project.ds.t`)",
                    "SELECT * FROM `other-project.other_ds.INFORMATION_SCHEMA.TABLES`",
                    "(SELECT 1 FROM ge_analytics.t) UNION ALL (SELECT 2 FROM other.t)"):
            self.assertRejected(sql, "not in an allowed dataset")

    def test_information_schema_scoping(self):
        self.assertRejected("SELECT * FROM `region-us`.INFORMATION_SCHEMA.JOBS", "region-qualified")
        self.assertRejected("SELECT * FROM region-us.INFORMATION_SCHEMA.JOBS_BY_PROJECT", "region-qualified")
        self.assertRejected("SELECT * FROM `demo-project.region-eu.INFORMATION_SCHEMA.JOBS`", "region-qualified")
        self.assertRejected("SELECT * FROM demo-project.`region-us`.INFORMATION_SCHEMA.JOBS", "region-qualified")
        self.assertRejected("SELECT * FROM INFORMATION_SCHEMA.SCHEMATA", "unqualified INFORMATION_SCHEMA")

    def test_table_functions(self):
        self.assertRejected("SELECT * FROM ML.PREDICT(MODEL `demo-project.ge_analytics.m`, TABLE ge_analytics.t)",
                            "table functions")
        self.assertRejected("SELECT * FROM EXTERNAL_QUERY('conn', 'SELECT 1')", "table functions")

    def test_malformed_input(self):
        self.assertRejected("", "empty query")
        self.assertRejected("   ", "empty query")
        self.assertRejected("SELECT 'unterminated FROM ge_analytics.t", "unterminated")
        self.assertRejected("SELECT 1 /* unterminated", "unterminated")
        self.assertRejected("SELECT * FROM `ge_analytics.t", "unterminated")
        self.assertRejected("SHOW TABLES", "only SELECT or WITH")
        self.assertRejected("SELECT 1 FROM ge_analytics.t WHERE " + "x" * 20001, "too long")


if __name__ == "__main__":
    unittest.main()
