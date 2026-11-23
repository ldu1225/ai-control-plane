"""Central audit dataset (workspace.audit_project) and the folder / organization scope in services/base.py Ctx.

With a central dataset an organization or folder aggregated log sink writes every project's entries into one dataset;
Source.cte() then keeps the scanned projects' entries through logName (inlined, allow-listed literals).
"""

import unittest
from unittest import mock

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane.gcp import Gcp, GcpError, sql_parameter_names
from controlplane.schema import Source, TableSchema, UnionSource, project_filter_sql
from controlplane.security import Identity
from controlplane.services import base, scope

try:  # Gcp.build_params needs the BigQuery client library (pinned in requirements.txt)
    import google.cloud.bigquery  # noqa: F401
    HAVE_BIGQUERY = True
except ImportError:  # pragma: no cover
    HAVE_BIGQUERY = False

DATASET = "ge_analytics"
LOGICAL = "cloudaudit_googleapis_com_data_access"
FIELDS = [{"name": "logName", "type": "STRING"}, {"name": "timestamp", "type": "TIMESTAMP"},
          {"name": "protopayload_auditlog", "type": "RECORD", "fields": [{"name": "methodName", "type": "STRING"}]}]
NO_LOGNAME = [f for f in FIELDS if f["name"] != "logName"]
PLAIN = ("da AS (SELECT * FROM `central-logs.ge_analytics.cloudaudit_googleapis_com_data_access` "
         "WHERE `timestamp` >= @start_ts AND `timestamp` < @end_ts)")
FILTER = "REGEXP_EXTRACT(`logName`, r'^projects/([^/]+)/') IN (%s)"


def make_source(project="central-logs", kind="table", fields=FIELDS, **kwargs):
    return Source(project, DATASET, LOGICAL, kind, LOGICAL, TableSchema.from_dicts(fields), **kwargs)


class SourceProjectFilterTest(unittest.TestCase):
    def test_without_filter_the_sql_is_unchanged(self):
        self.assertIsNone(make_source().project_filter)
        for value in (None, (), []):
            self.assertEqual(make_source(project_filter=value).cte("da"), PLAIN)

    def test_filter_inlines_validated_literals(self):
        src = make_source(project_filter=("proj-a1", "proj-b2", "example.com:proj-c3", "proj-a1"))
        sql = src.cte("da")
        self.assertEqual(sql, PLAIN[:-1] + " AND " + FILTER % "'proj-a1', 'proj-b2', 'example.com:proj-c3'" + ")")
        self.assertEqual(sql_parameter_names(sql), {"start_ts", "end_ts"})

    def test_invalid_ids_are_dropped(self):
        hostile = ("x') OR TRUE --", "Proj-Upper1", "short", "bad\\id-123", "proj b-123", "proj-b2'", 42, None,
                   "proj-ok-1", "a" * 101)
        sql = make_source(project_filter=hostile).cte("da")
        self.assertTrue(sql.endswith(" AND " + FILTER % "'proj-ok-1'" + ")"), sql)
        self.assertEqual(sql_parameter_names(sql), {"start_ts", "end_ts"})
        none_left = make_source(project_filter=("x') OR TRUE --", "UPPER-CASE")).cte("da")
        self.assertEqual(none_left, PLAIN[:-1] + " AND FALSE)")
        self.assertIsNone(project_filter_sql(make_source().schema, "proj-ok-1"))  # a string is not a list of ids

    def test_no_log_name_column_means_no_filter(self):
        src = make_source(fields=NO_LOGNAME, project_filter=("proj-a1",))
        self.assertEqual(src.cte("da"), PLAIN)
        json_log = [{"name": "logName", "type": "JSON"}] + NO_LOGNAME
        self.assertEqual(make_source(fields=json_log, project_filter=("proj-a1",)).cte("da"), PLAIN)
        repeated = [{"name": "logName", "type": "STRING", "mode": "REPEATED"}] + NO_LOGNAME
        self.assertEqual(make_source(fields=repeated, project_filter=("proj-a1",)).cte("da"), PLAIN)

    def test_exact_column_name_is_quoted(self):
        fields = [{"name": "LogName", "type": "STRING"}] + NO_LOGNAME
        sql = make_source(fields=fields, project_filter=("proj-a1",)).cte("da")
        self.assertIn("REGEXP_EXTRACT(`LogName`, r'^projects/([^/]+)/') IN ('proj-a1')", sql)

    def test_sharded_source_keeps_suffix_pruning(self):
        sql = make_source(kind="sharded", project_filter=("proj-a1",)).cte("da", start="prev_start_ts")
        self.assertEqual(sql, "da AS (SELECT * FROM "
                              "`central-logs.ge_analytics.cloudaudit_googleapis_com_data_access_*` "
                              "WHERE _TABLE_SUFFIX BETWEEN @sfx_start AND @sfx_end AND `timestamp` >= @prev_start_ts "
                              "AND `timestamp` < @end_ts AND " + FILTER % "'proj-a1'" + ")")
        self.assertEqual(sql_parameter_names(sql), {"sfx_start", "sfx_end", "prev_start_ts", "end_ts"})

    def test_union_branches_keep_their_own_filter(self):
        union = UnionSource([make_source("proj-a1", project_filter=("proj-a1",)), make_source("proj-b2")])
        first, second = union.cte("da").split(" UNION ALL ")
        self.assertIn(FILTER % "'proj-a1'", first)
        self.assertNotIn("REGEXP_EXTRACT", second)
        plain_union = UnionSource([make_source("proj-a1"), make_source("proj-b2")]).cte("da")
        self.assertNotIn("REGEXP_EXTRACT", plain_union)

    @unittest.skipUnless(HAVE_BIGQUERY, "google-cloud-bigquery is not installed")
    def test_fixed_parameter_dicts_still_bind(self):
        sql = "WITH %s SELECT COUNT(*) AS n FROM da" % make_source(project_filter=("proj-a1",)).cte("da")
        params = Gcp.build_params(sql, {"start_ts": "2026-11-01T00:00:00Z", "end_ts": "2026-11-02T00:00:00Z",
                                        "sfx_start": "20261031", "sfx_end": "20261103"})
        self.assertEqual({p.name for p in params}, {"start_ts", "end_ts"})


class FakeRegistry:
    """tables / source / dataset_location over {project: [logical tables] or GcpError}; source() hands out the same
    object on every call, like a cache would."""

    def __init__(self, datasets, fields=FIELDS):
        self.datasets = datasets
        self.fields = fields
        self.calls = []
        self.cached = {}

    def tables(self, project, dataset, quota_project, max_bytes):
        self.calls.append(("tables", project))
        value = self.datasets.get(project, GcpError("not_found", status=404))
        if isinstance(value, Exception):
            raise value
        return {name: {"kind": "table", "table": name, "latest": name} for name in value}

    def dataset_location(self, project, dataset, quota_project, max_bytes=None):
        self.calls.append(("location", project))
        self.tables(project, dataset, quota_project, max_bytes)
        return "US"

    def source(self, project, dataset, logical, quota_project, max_bytes):
        self.calls.append(("source", project))
        key = (project, logical)
        if key not in self.cached:
            self.cached[key] = Source(project, dataset, logical, "table", logical, TableSchema.from_dicts(self.fields))
        return self.cached[key]

    def projects_called(self):
        return {project for _, project in self.calls}


def make_ctx(registry=None, params=None, **workspace):
    ws = {"project_id": "acme-prod", "projects": ["acme-dev-01"], "scan_all_projects": False,
          "audit_dataset": DATASET, "audit_project": "central-logs"}
    ws.update(workspace)
    app = support.FakeApp()
    app.registry = registry
    return base.Ctx(app, support.make_settings(workspace=ws), 1,
                    Identity("admin@example.com", "admin", "google", "u:admin"), params=params or {})


class CentralAuditCtxTest(unittest.TestCase):
    def test_central_audit_project(self):
        self.assertEqual(make_ctx().central_audit_project, "central-logs")
        for value in ("", None, "Bad_Project", "x"):
            self.assertEqual(make_ctx(audit_project=value).central_audit_project, "")

    def test_audit_source_reads_only_the_central_dataset_and_returns_a_filtered_copy(self):
        registry = FakeRegistry({"central-logs": [LOGICAL], "acme-prod": [LOGICAL], "acme-dev-01": [LOGICAL]})
        ctx = make_ctx(registry)
        src = ctx.audit_source(LOGICAL)
        cached = registry.cached[("central-logs", LOGICAL)]
        self.assertIsInstance(src, Source)
        self.assertIsNot(src, cached)
        self.assertIs(src.schema, cached.schema)
        self.assertEqual((src.project, src.dataset), ("central-logs", DATASET))
        self.assertEqual(src.project_filter, ("acme-prod", "acme-dev-01"))
        self.assertIsNone(cached.project_filter)
        self.assertEqual(registry.projects_called(), {"central-logs"})
        self.assertIn(FILTER % "'acme-prod', 'acme-dev-01'", src.cte("da"))
        self.assertNotIn("REGEXP_EXTRACT", cached.cte("da"))
        self.assertEqual(ctx.warnings, [])
        again = ctx.audit_source(LOGICAL)
        self.assertIsNot(again, src)
        self.assertIsNone(registry.cached[("central-logs", LOGICAL)].project_filter)

    def test_project_parameter_narrows_the_filter(self):
        registry = FakeRegistry({"central-logs": [LOGICAL]})
        src = make_ctx(registry, params={"project": "acme-dev-01"}).audit_source(LOGICAL)
        self.assertEqual(src.project_filter, ("acme-dev-01",))
        self.assertTrue(src.cte("da").endswith(FILTER % "'acme-dev-01'" + ")"))

    def test_missing_central_dataset(self):
        registry = FakeRegistry({"acme-prod": [LOGICAL]})
        ctx = make_ctx(registry)
        self.assertIsNone(ctx.audit_source(LOGICAL))
        self.assertEqual([(w["source"], w["code"]) for w in ctx.warnings],
                         [("bigquery:%s" % DATASET, "dataset_not_found")])
        self.assertIn("central-logs", ctx.warnings[0]["message"])
        self.assertIsNone(ctx.audit_source(LOGICAL))  # remembered as failed: no second lookup or warning
        self.assertEqual(len(ctx.warnings), 1)
        self.assertEqual(registry.projects_called(), {"central-logs"})
        optional = make_ctx(registry)
        self.assertIsNone(optional.audit_source(LOGICAL, optional=True))
        self.assertEqual(optional.warnings, [])

    def test_missing_table_in_a_readable_dataset_is_silent(self):
        ctx = make_ctx(FakeRegistry({"central-logs": []}))
        self.assertIsNone(ctx.audit_source(LOGICAL))
        self.assertEqual(ctx.warnings, [])

    def test_table_without_timestamp(self):
        registry = FakeRegistry({"central-logs": [LOGICAL]}, fields=[{"name": "logName", "type": "STRING"}])
        ctx = make_ctx(registry)
        self.assertIsNone(ctx.audit_source(LOGICAL))
        self.assertEqual([w["code"] for w in ctx.warnings], ["unsupported_schema"])
        quiet = make_ctx(registry)
        self.assertIsNone(quiet.audit_source(LOGICAL, optional=True))
        self.assertEqual(quiet.warnings, [])

    def test_audit_dataset_ready_checks_only_the_central_dataset(self):
        registry = FakeRegistry({"central-logs": [], "acme-prod": GcpError("denied", 403)})
        ctx = make_ctx(registry)
        self.assertTrue(ctx.audit_dataset_ready())
        self.assertEqual(registry.projects_called(), {"central-logs"})
        missing = FakeRegistry({"acme-prod": [LOGICAL], "acme-dev-01": [LOGICAL]})
        ctx = make_ctx(missing)
        self.assertFalse(ctx.audit_dataset_ready())
        self.assertEqual([w["code"] for w in ctx.warnings], ["dataset_not_found"])
        self.assertEqual(missing.projects_called(), {"central-logs"})

    def test_single_location(self):
        registry = FakeRegistry({"central-logs": [LOGICAL], "acme-prod": [LOGICAL], "acme-dev-01": [LOGICAL]})
        ctx = make_ctx(registry)
        self.assertIsNone(ctx._audit_target_location())  # pylint: disable=protected-access
        self.assertEqual(registry.calls, [])

    def test_per_project_datasets_are_unchanged(self):
        registry = FakeRegistry({"acme-prod": [LOGICAL], "acme-dev-01": [LOGICAL], "central-logs": [LOGICAL]})
        ctx = make_ctx(registry, audit_project="")
        self.assertEqual(ctx.central_audit_project, "")
        src = ctx.audit_source(LOGICAL)
        self.assertIsInstance(src, UnionSource)
        self.assertEqual(src.projects, ["acme-prod", "acme-dev-01"])
        self.assertTrue(all(s.project_filter is None for s in src.sources))
        self.assertIs(src.sources[0], registry.cached[("acme-prod", LOGICAL)])
        self.assertNotIn("REGEXP_EXTRACT", src.cte("da"))
        self.assertNotIn("central-logs", registry.projects_called())
        self.assertTrue(ctx.audit_dataset_ready())


def discovered(count, parent="folders/100"):
    return {"projects": [{"id": "scope-proj-%03d" % i, "name": "Scope %d" % i, "parent": parent, "number": str(i),
                          "labels": {}, "ge": None} for i in range(count)],
            "truncated": False, "folders": 1, "errors": []}


class ScopedProjectListTest(unittest.TestCase):
    def folder_ctx(self, **workspace):
        ws = {"scope_type": "folder", "scope_ids": ["100"], "audit_project": ""}
        ws.update(workspace)
        return make_ctx(**ws)

    def test_folder_scope_has_no_25_project_cap_and_primary_comes_first(self):
        found = discovered(40)
        found["projects"].insert(3, dict(found["projects"][0], id="acme-dev-01"))
        found["projects"].insert(0, dict(found["projects"][0], id="acme-prod", parent="folders/100"))
        calls = []
        with mock.patch.object(scope, "discover", side_effect=lambda ctx, blocking=True: calls.append(blocking)
                               or found):
            ctx = self.folder_ctx()
            ids = ctx.all_projects
            self.assertEqual(ids[:3], ["acme-prod", "acme-dev-01", "scope-proj-000"])
            self.assertEqual(len(ids), 42)
            self.assertEqual(len(set(ids)), 42)
            self.assertEqual(ctx.known_projects, ids)
            details = ctx.project_details
        self.assertEqual(calls, [True, False, True])  # known_projects reads the cached discovery only
        self.assertEqual([d["source"] for d in details[:3]], ["primary", "configured", "scope"])
        self.assertEqual(details[2], {"id": "scope-proj-000", "name": "Scope 0", "parent": "folders/100",
                                      "sameOrg": True, "source": "scope"})
        self.assertEqual(details[0]["parent"], "folders/100")
        self.assertEqual(len(details), 42)

    def test_scope_max_projects_bounds_the_discovered_part(self):
        with mock.patch.object(scope, "discover", return_value=discovered(40)):
            ids = self.folder_ctx(scope_max_projects=10).all_projects
        self.assertEqual(ids[:2], ["acme-prod", "acme-dev-01"])
        self.assertEqual(len(ids), 12)

    def test_configured_projects_are_not_capped(self):
        configured = ["conf-proj-%03d" % i for i in range(30)]
        with mock.patch.object(scope, "discover", return_value=None):
            ids = self.folder_ctx(projects=configured).all_projects
        self.assertEqual(ids, ["acme-prod"] + configured)

    def test_folder_scope_without_scope_ids_behaves_like_projects(self):
        with mock.patch.object(scope, "discover", side_effect=AssertionError("no discovery")):
            ctx = self.folder_ctx(scope_ids=[])
            self.assertEqual(ctx.all_projects, ["acme-prod", "acme-dev-01"])
            self.assertEqual(ctx.known_projects, ["acme-prod", "acme-dev-01"])
            self.assertEqual([d["source"] for d in ctx.project_details], ["primary", "configured"])

    def test_fake_application_makes_no_calls(self):
        ctx = self.folder_ctx()  # support.FakeApp: no Gcp client at all
        self.assertEqual(ctx.all_projects, ["acme-prod", "acme-dev-01"])
        self.assertEqual(ctx.known_projects, ["acme-prod", "acme-dev-01"])
        self.assertEqual([d["source"] for d in ctx.project_details], ["primary", "configured"])

    def test_projects_scope_keeps_the_25_cap(self):
        crm = [{"id": "crm-proj-%03d" % i, "name": "p", "parent": "organizations/1"} for i in range(40)]
        with mock.patch.object(scope, "discover", side_effect=AssertionError("no scope discovery")), \
                mock.patch.object(base.Ctx, "_crm_discovered", return_value=crm):
            ctx = make_ctx(audit_project="")
            self.assertEqual(len(ctx.all_projects), 25)
            self.assertEqual(ctx.all_projects[:3], ["acme-prod", "acme-dev-01", "crm-proj-000"])
            self.assertEqual(len(ctx.known_projects), 25)
            self.assertEqual(ctx.project_details[2]["source"], "auto_discovered")


if __name__ == "__main__":
    unittest.main()
