"""Log Router -> BigQuery routing with cost controls: filters, estimates, managed sinks, budgets, group following."""

import copy
import datetime as dt
import unittest
import urllib.parse
from unittest import mock

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane import settings_store
from controlplane.gcp import GcpError
from controlplane.httpkit import ApiError
from controlplane.services import telemetry, telemetry_routing as routing

GIB = 2 ** 30
PROJECT = "demo-project"
WRITER = "serviceAccount:service-123@gcp-sa-logging.iam.gserviceaccount.com"
LOG_A = "projects/%s/logs/run.googleapis.com%%2Fstdout" % PROJECT
LOG_B = "projects/%s/logs/custom_rag_worker" % PROJECT


class FakeCloud:
    """In-memory BigQuery datasets and tables, Log Router sinks and exported-bytes metrics."""

    def __init__(self):
        self.datasets, self.tables, self.sinks, self.exported = {}, {}, {}, {}
        self.calls = []

    def rest(self, method, url, json_body=None, params=None, timeout=None):
        self.calls.append((method, url, copy.deepcopy(json_body)))
        parsed = urllib.parse.urlparse(url)
        parts = [urllib.parse.unquote(p) for p in parsed.path.split("/") if p]
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.hostname == "bigquery.googleapis.com":
            proj = parts[3]
            if len(parts) == 5 and method == "POST":  # .../projects/p/datasets
                ref = json_body["datasetReference"]
                meta = dict(json_body, id="%s:%s" % (ref["projectId"], ref["datasetId"]), access=[])
                self.datasets[(proj, ref["datasetId"])] = meta
                return copy.deepcopy(meta)
            key = (proj, parts[5])
            if len(parts) == 6:
                if key not in self.datasets:
                    raise GcpError("not_found", status=404)
                if method == "PATCH":
                    self.datasets[key].update(json_body)
                return copy.deepcopy(self.datasets[key])
            tkey = key + (parts[7],)
            if tkey not in self.tables:
                raise GcpError("not_found", status=404)
            if method == "PATCH":
                self.tables[tkey].update(json_body)
            return copy.deepcopy(self.tables[tkey])
        if parsed.hostname == "logging.googleapis.com":
            proj = parts[2]
            if method == "POST":
                name = json_body["name"]
                if (proj, name) in self.sinks:
                    raise GcpError("conflict", status=409)
                sink = dict(json_body, writerIdentity=WRITER)
                self.sinks[(proj, name)] = sink
                return copy.deepcopy(sink)
            name = parts[4]
            if (proj, name) not in self.sinks:
                raise GcpError("not_found", status=404)
            if method == "DELETE":
                del self.sinks[(proj, name)]
                return {}
            if method in ("PUT", "PATCH"):
                if query.get("uniqueWriterIdentity") != ["true"]:
                    raise GcpError("bad_request", status=400, message="Use uniqueWriterIdentity=true when updating this sink.")
                mask = query.get("updateMask", [""])[0].split(",")
                for field in mask:
                    if field in json_body:
                        self.sinks[(proj, name)][field] = json_body[field]
            return copy.deepcopy(self.sinks[(proj, name)])
        raise AssertionError("unexpected REST call %s %s" % (method, url))

    def request(self, method, url, quota_project=None, params=None, timeout=None):
        series = [{"resource": {"type": "logging_sink", "labels": {"name": name, "project_id": PROJECT}},
                   "points": [{"value": {"int64Value": str(value)}}]} for name, value in self.exported.items()]
        return {"timeSeries": series}, {}


class Registry:
    @staticmethod
    def dataset_location(_project, _dataset, _quota):
        return "asia-northeast3"


class Ctx(support.FakeCtx):
    def __init__(self, cloud, settings=None, body=None, role="admin", mgr=None):
        super().__init__(settings=settings, body=body, role=role)
        self.cloud = cloud
        self.gcp = cloud
        self.registry = Registry()
        if mgr is not None:
            self.app.settings = mgr

    def rest(self, method, url, **kwargs):
        return self.cloud.rest(method, url, **kwargs)


def ctx_with(cloud=None, routing_cfg=None, body=None, role="admin", priced=False):
    cloud = cloud or FakeCloud()
    settings = support.make_settings(telemetry={"routing": dict(support.make_settings()["telemetry"]["routing"],
                                                                **(routing_cfg or {}))})
    mgr = None
    if priced:
        mgr = settings_store.SettingsManager(settings, settings_store.MemoryStore())

        def seed(state):
            state["syncedAt"] = "2026-11-04T00:00:00Z"
            state["storage"] = {"ingestPerGiB": 0.05, "storagePerGiBMonth": 0.02, "service": "BigQuery",
                                "currency": "USD", "source": "cloud_billing_catalog"}
        mgr.mutate_pricing(seed, "system")
        settings = mgr.snapshot()[0]
    return Ctx(cloud, settings=settings, body=body, role=role, mgr=mgr), cloud


class PolicyAndFilterTest(unittest.TestCase):
    def test_defaults_and_route_overrides(self):
        pol = routing.policy(support.make_settings())
        self.assertEqual((pol["dataset"], pol["samplePercent"], pol["retentionDays"], pol["dailyBudgetGb"],
                          pol["sinkDailyGb"], pol["overBudgetAction"], pol["maxSinks"]),
                         ("telemetry_routed", 100.0, 30, 5.0, 1.0, "pause", 20))
        rp = routing.route_policy({"policy": {"samplePercent": 25, "retentionDays": 7}}, pol)
        self.assertEqual((rp["samplePercent"], rp["retentionDays"], rp["dailyGb"]), (25.0, 7, 1.0))
        self.assertEqual(routing._clean_policy({"samplePercent": "10", "minSeverity": "warning", "dailyGb": "0.5"}),
                         {"samplePercent": 10.0, "minSeverity": "WARNING", "dailyGb": 0.5})
        for bad in ({"samplePercent": 0}, {"samplePercent": 101}, {"minSeverity": "LOUD"}, {"retentionDays": -1},
                    {"exclusions": ["x" * 2001]}):
            with self.assertRaises(ApiError, msg=bad):
                routing._clean_policy(bad)

    def test_sink_filter(self):
        self.assertEqual(routing.build_filter([LOG_A]), 'logName="%s"' % LOG_A)
        self.assertEqual(routing.build_filter([LOG_A, LOG_B]), 'logName=("%s" OR "%s")' % (LOG_A, LOG_B))
        self.assertEqual(routing.build_filter([LOG_A], "", "WARNING", 25),
                         'logName="%s" AND severity>=WARNING AND sample(insertId, 0.25)' % LOG_A)
        self.assertEqual(routing.build_filter([], 'resource.type="cloud_run_revision"', "", 0.1),
                         '(resource.type="cloud_run_revision") AND sample(insertId, 0.001)')
        self.assertNotIn("sample(", routing.build_filter([LOG_A], "", "", 100))

    def test_estimate(self):
        prices = {"ingestPerGiB": 0.05, "storagePerGiBMonth": 0.02, "currency": "USD"}
        est = routing.estimate(2 * GIB, 50, 30, prices)
        self.assertEqual((est["gibPerDay"], est["monthlyCost"], est["priced"]), (1.0, 2.1, True))
        self.assertAlmostEqual(routing.estimate(2 * GIB, 50, 90, prices)["monthlyCost"], 3.3)
        self.assertEqual(routing.estimate(2 * GIB, 100, 0, None), {"gibPerDay": 2.0, "monthlyCost": None,
                                                                   "currency": None, "priced": False})
        self.assertIsNone(routing.estimate(None, 100, 30, prices)["gibPerDay"])

    def test_storage_prices_follow_the_display_currency(self):
        ctx, _ = ctx_with(priced=True)
        self.assertEqual(routing.storage_prices(ctx)["ingestPerGiB"], 0.05)

        def fx(state):
            state["fx"] = {"base": "USD", "rates": {"EUR": {"rate": 0.9, "source": "test"}}}
        ctx.app.settings.mutate_pricing(fx, "system")
        ctx.settings = support.make_settings(pricing={"currency": "EUR"})
        prices = routing.storage_prices(ctx)
        self.assertEqual((prices["currency"], prices["ingestPerGiB"]), ("EUR", 0.045))
        self.assertIsNone(routing.storage_prices(support.FakeCtx()))  # nothing synced yet: no guess


class SaveRouteTest(unittest.TestCase):
    def body(self, **extra):
        out = {"title": "RAG pipeline", "project": PROJECT, "logs": [LOG_A, LOG_B], "group": "RAG pipeline",
               "followGroup": True, "policy": {"samplePercent": 50, "minSeverity": "INFO",
                                               "exclusions": ['httpRequest.requestUrl:"/healthz"']}}
        out.update(extra)
        return out

    def test_creates_dataset_sink_writer_grant_and_retention(self):
        ctx, cloud = ctx_with(body=self.body())
        cloud.tables[(PROJECT, "telemetry_routed", "custom_rag_worker")] = {"timePartitioning": {"type": "DAY"}}
        with mock.patch.object(routing, "log_volumes", return_value={"custom_rag_worker": GIB}):
            res = routing.save_route(ctx)
        ds = cloud.datasets[(PROJECT, "telemetry_routed")]
        self.assertEqual(ds["labels"], {"managed-by": "ai-control-plane"})
        self.assertEqual(ds["location"], "asia-northeast3")
        self.assertEqual(ds["defaultPartitionExpirationMs"], str(30 * 86400000))
        self.assertIn({"role": "WRITER", "userByEmail": WRITER.split(":", 1)[1]}, ds["access"])
        route = res["route"]
        self.assertRegex(route["id"], r"^rt_[0-9a-f]{8}$")
        self.assertTrue(route["sink"].startswith("acp-tx-rag-pipeline-"))
        sink = cloud.sinks[(PROJECT, route["sink"])]
        self.assertEqual(sink["destination"], "bigquery.googleapis.com/projects/%s/datasets/telemetry_routed" % PROJECT)
        self.assertEqual(sink["filter"], 'logName=("%s" OR "%s") AND severity>=INFO AND sample(insertId, 0.5)'
                         % (LOG_A, LOG_B))
        self.assertEqual(sink["exclusions"], [{"name": "acp-exclusion-1", "filter": 'httpRequest.requestUrl:"/healthz"',
                                               "disabled": False}])
        self.assertEqual(sink["bigqueryOptions"], {"usePartitionedTables": True})
        self.assertFalse(sink["disabled"])
        self.assertTrue(res["writerGranted"])
        self.assertEqual(cloud.tables[(PROJECT, "telemetry_routed", "custom_rag_worker")]["timePartitioning"],
                         {"type": "DAY", "expirationMs": str(30 * 86400000)})
        state = routing.load_state(ctx)
        self.assertEqual([r["id"] for r in state["routes"]], [route["id"]])
        self.assertEqual(state["events"][-1]["kind"], "created")
        listed = res["routing"]["routes"][0]
        self.assertEqual((listed["live"]["exists"], listed["live"]["disabled"]), (True, False))
        self.assertEqual(listed["effectivePolicy"]["samplePercent"], 50.0)
        self.assertEqual(listed["estimate"]["gibPerDay"], 0.5)
        self.assertIs(res["routing"]["policy"]["webhook"], False)

    def test_update_reuses_the_sink(self):
        ctx, cloud = ctx_with(body=self.body())
        with mock.patch.object(routing, "log_volumes", return_value={}):
            created = routing.save_route(ctx)["route"]
            ctx.body = {"id": created["id"], "logs": [LOG_A], "policy": {"samplePercent": 10}}
            updated = routing.save_route(ctx)["route"]
        self.assertEqual(updated["sink"], created["sink"])
        self.assertEqual(cloud.sinks[(PROJECT, created["sink"])]["filter"], 'logName="%s" AND sample(insertId, 0.1)' % LOG_A)
        self.assertTrue(any(m == "PUT" and "updateMask=" in u for m, u, _ in cloud.calls))
        self.assertEqual(len(routing.load_state(ctx)["routes"]), 1)

    def test_existing_dataset_not_created_by_us_is_left_alone(self):
        ctx, cloud = ctx_with(body=self.body())
        cloud.datasets[(PROJECT, "telemetry_routed")] = {"labels": {}, "access": [], "defaultPartitionExpirationMs": None}
        with mock.patch.object(routing, "log_volumes", return_value={}):
            routing.save_route(ctx)
        self.assertIsNone(cloud.datasets[(PROJECT, "telemetry_routed")]["defaultPartitionExpirationMs"])

    def test_limits(self):
        with mock.patch.object(routing, "log_volumes", return_value={}):
            ctx, _ = ctx_with(body=self.body(logs=["projects/other-project/logs/x"]))
            with self.assertRaises(ApiError):
                routing.save_route(ctx)
            ctx, _ = ctx_with(body=self.body(logs=[], filter=""))
            with self.assertRaises(ApiError):
                routing.save_route(ctx)
            ctx, _ = ctx_with(body=self.body(), role="viewer")
            with self.assertRaises(ApiError):
                routing.save_route(ctx)
            ctx, _ = ctx_with(body=self.body(), routing_cfg={"max_sinks": 1})
            routing.save_route(ctx)
            ctx.body = self.body(title="Second")
            with self.assertRaises(ApiError) as caught:
                routing.save_route(ctx)
            self.assertEqual(caught.exception.code, "conflict")

    def test_follow_group_needs_the_setting(self):
        ctx, _ = ctx_with(body=self.body(), routing_cfg={"follow_groups": False})
        with mock.patch.object(routing, "log_volumes", return_value={}):
            self.assertFalse(routing.save_route(ctx)["route"]["followGroup"])

    def test_estimate_endpoint_checks_budgets(self):
        ctx, _ = ctx_with(body={"logs": [LOG_A], "policy": {"samplePercent": 50}}, routing_cfg={"sink_daily_gb": 1,
                                                                                               "daily_budget_gb": 1.2},
                          priced=True)
        with mock.patch.object(routing, "log_volumes", return_value={"run.googleapis.com/stdout": 3 * GIB}):
            res = routing.estimate_route(ctx)
        self.assertEqual(res["estimate"]["gibPerDay"], 1.5)
        self.assertTrue(res["checks"]["overRouteBudget"])
        self.assertTrue(res["checks"]["overTotalBudget"])
        self.assertEqual(res["filter"], 'logName="%s" AND sample(insertId, 0.5)' % LOG_A)
        self.assertEqual(res["logs"][0]["bytesPerDay"], 3 * GIB)
        # A filter-only route (workload stream) has no per-log volume: unknown, not zero.
        ctx.body = {"logs": [], "filter": 'resource.type="cloud_run_revision"'}
        with mock.patch.object(routing, "log_volumes", return_value={"run.googleapis.com/stdout": 3 * GIB}):
            res = routing.estimate_route(ctx)
        self.assertEqual((res["estimate"]["gibPerDay"], res["estimate"]["monthlyCost"]), (None, None))


class ActionsTest(unittest.TestCase):
    def setUp(self):
        self.ctx, self.cloud = ctx_with(body={"title": "Runtime", "project": PROJECT, "logs": [LOG_A]})
        self.patch = mock.patch.object(routing, "log_volumes", return_value={})
        self.patch.start()
        self.route = routing.save_route(self.ctx)["route"]

    def tearDown(self):
        self.patch.stop()

    def act(self, action):
        self.ctx.body = {"id": self.route["id"], "action": action}
        return routing.route_action(self.ctx)

    def test_pause_resume_apply_delete(self):
        key = (PROJECT, self.route["sink"])
        self.act("pause")
        self.assertTrue(self.cloud.sinks[key]["disabled"])
        self.assertEqual(routing.load_state(self.ctx)["routes"][0]["status"], "paused")
        self.act("resume")
        self.assertFalse(self.cloud.sinks[key]["disabled"])
        self.assertEqual(routing.load_state(self.ctx)["routes"][0]["status"], "active")
        self.cloud.sinks[key]["filter"] = "tampered"
        self.act("apply")
        self.assertEqual(self.cloud.sinks[key]["filter"], 'logName="%s"' % LOG_A)
        self.act("delete")
        self.assertNotIn(key, self.cloud.sinks)
        state = routing.load_state(self.ctx)
        self.assertEqual(state["routes"], [])
        self.assertEqual([e["kind"] for e in state["events"]], ["created", "paused", "resumed", "applied", "deleted"])
        self.ctx.body = {"id": self.route["id"], "action": "explode"}
        with self.assertRaises(ApiError):
            routing.route_action(self.ctx)

    def test_legacy_sink_endpoint_creates_a_managed_route(self):
        self.ctx.body = {"logNames": [LOG_B], "quickAdd": {"name": "RAG worker"}, "createConnector": False}
        res = routing.legacy_create_sink(self.ctx)
        self.assertEqual(res["route"]["logs"], [LOG_B])
        self.assertEqual(res["route"]["title"], "RAG worker")
        self.assertTrue(res["route"]["sink"].startswith("acp-tx-"))


class EnforcementTest(unittest.TestCase):
    def make(self, routing_cfg, routes=(("A", [LOG_A]),)):
        ctx, cloud = ctx_with(routing_cfg=routing_cfg)
        ids = []
        with mock.patch.object(routing, "log_volumes", return_value={}):
            for title, logs in routes:
                ctx.body = {"title": title, "project": PROJECT, "logs": logs}
                ids.append(routing.save_route(ctx)["route"])
        return ctx, cloud, ids

    def test_route_over_budget_is_paused_and_resumed_the_next_day(self):
        ctx, cloud, (route,) = self.make({"sink_daily_gb": 1.0, "daily_budget_gb": 0})
        cloud.exported[route["sink"]] = 2 * GIB
        with mock.patch.object(routing, "notify") as notify:
            summary = routing.run_enforcement(ctx)
        self.assertEqual(summary["paused"], 1)
        self.assertTrue(cloud.sinks[(PROJECT, route["sink"])]["disabled"])
        saved = routing.load_state(ctx)["routes"][0]
        self.assertEqual(saved["status"], "paused_budget")
        self.assertIn("route budget", saved["pausedReason"])
        self.assertEqual(notify.call_count, 1)
        # Still over budget later the same day: nothing changes.
        with mock.patch.object(routing, "notify"):
            self.assertEqual(routing.run_enforcement(ctx)["paused"], 0)
        # Next local day: resumed automatically.

        def yesterday(doc):
            doc["routes"][0]["pausedAt"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1, hours=1)) \
                .strftime("%Y-%m-%dT%H:%M:%SZ")
        routing._mutate(ctx, yesterday)
        cloud.exported[route["sink"]] = 0
        with mock.patch.object(routing, "notify"):
            summary = routing.run_enforcement(ctx)
        self.assertEqual(summary["resumed"], 1)
        self.assertFalse(cloud.sinks[(PROJECT, route["sink"])]["disabled"])
        self.assertEqual(routing.load_state(ctx)["routes"][0]["status"], "active")

    def test_no_auto_resume_keeps_the_route_paused(self):
        ctx, cloud, (route,) = self.make({"sink_daily_gb": 1.0, "auto_resume": False})
        cloud.exported[route["sink"]] = 2 * GIB
        with mock.patch.object(routing, "notify"):
            routing.run_enforcement(ctx)

        def yesterday(doc):
            doc["routes"][0]["pausedAt"] = "2020-01-01T00:00:00Z"
        routing._mutate(ctx, yesterday)
        with mock.patch.object(routing, "notify"):
            self.assertEqual(routing.run_enforcement(ctx)["resumed"], 0)
        self.assertEqual(routing.load_state(ctx)["routes"][0]["status"], "paused_budget")

    def test_alert_mode_reports_once_per_day(self):
        ctx, cloud, (route,) = self.make({"sink_daily_gb": 1.0, "over_budget_action": "alert"})
        cloud.exported[route["sink"]] = 2 * GIB
        with mock.patch.object(routing, "notify") as notify:
            routing.run_enforcement(ctx)
            routing.run_enforcement(ctx)
        self.assertFalse(cloud.sinks[(PROJECT, route["sink"])]["disabled"])
        kinds = [e["kind"] for e in routing.load_state(ctx)["events"]]
        self.assertEqual(kinds.count("over_budget"), 1)
        self.assertEqual(notify.call_count, 1)

    def test_total_budget_pauses_every_route(self):
        ctx, cloud, routes = self.make({"sink_daily_gb": 0, "daily_budget_gb": 1.0},
                                       routes=(("A", [LOG_A]), ("B", [LOG_B])))
        for r in routes:
            cloud.exported[r["sink"]] = int(0.6 * GIB)
        with mock.patch.object(routing, "notify"):
            summary = routing.run_enforcement(ctx)
        self.assertEqual(summary["paused"], 2)
        self.assertAlmostEqual(summary["gbToday"], 1.2, places=3)

    def test_unlimited_budgets_never_pause(self):
        ctx, cloud, (route,) = self.make({"sink_daily_gb": 0, "daily_budget_gb": 0})
        cloud.exported[route["sink"]] = 500 * GIB
        with mock.patch.object(routing, "notify"):
            self.assertEqual(routing.run_enforcement(ctx)["paused"], 0)

    def test_group_following_adds_new_logs_within_the_budget(self):
        ctx, cloud, _ = self.make({"daily_budget_gb": 1.0, "sink_daily_gb": 0}, routes=())
        ctx.body = {"title": "RAG pipeline", "project": PROJECT, "logs": [LOG_A], "group": "RAG pipeline",
                    "followGroup": True}
        with mock.patch.object(routing, "log_volumes", return_value={}):
            route = routing.save_route(ctx)["route"]
        new_log = "projects/%s/logs/rag_reranker" % PROJECT
        huge_log = "projects/%s/logs/rag_debug_dump" % PROJECT
        found = {"logs": [
            {"logName": LOG_A, "shortName": "run.googleapis.com/stdout", "catalog": {"group": "RAG pipeline"}},
            {"logName": new_log, "shortName": "rag_reranker", "catalog": {"group": "RAG pipeline"}},
            {"logName": huge_log, "shortName": "rag_debug_dump", "catalog": {"group": "RAG pipeline"}},
            {"logName": "projects/%s/logs/other" % PROJECT, "shortName": "other", "catalog": {"group": "Security"}},
        ]}
        volumes = {"run.googleapis.com/stdout": 0.1 * GIB, "rag_reranker": 0.2 * GIB, "rag_debug_dump": 5 * GIB}
        with mock.patch.object(telemetry, "discover", return_value=found), \
                mock.patch.object(routing, "log_volumes", return_value=volumes), \
                mock.patch.object(routing, "notify"):
            summary = routing.run_enforcement(ctx, trigger="daily", follow=True)
        self.assertEqual(summary["followed"], 1)
        saved = routing.load_state(ctx)["routes"][0]
        self.assertEqual(saved["logs"], [LOG_A, new_log])
        self.assertIn(new_log, cloud.sinks[(PROJECT, route["sink"])]["filter"])
        kinds = [e["kind"] for e in routing.load_state(ctx)["events"]]
        self.assertIn("followed", kinds)
        self.assertIn("skipped", kinds)  # the 5 GiB/day log would exceed the 1 GiB total budget

    def test_exported_bytes_since_local_midnight(self):
        cloud = FakeCloud()
        cloud.exported = {"acp-tx-a": 1234}
        ctx = Ctx(cloud)
        self.assertEqual(routing.exported_bytes_today(ctx, PROJECT, "Asia/Seoul"), {"acp-tx-a": 1234})

    def test_bootstrap_summary(self):
        ctx, cloud, (route,) = self.make({"sink_daily_gb": 1.0})
        cloud.exported[route["sink"]] = 2 * GIB
        with mock.patch.object(routing, "notify"):
            routing.run_enforcement(ctx)
        summary = routing.alert_summary(ctx.app)
        self.assertEqual((summary["routes"], summary["paused"]), (1, 1))


class GetRoutingFastPathTest(unittest.TestCase):
    def test_empty_or_filter_only_routes_skip_log_volumes(self):
        ctx, cloud = ctx_with()
        with mock.patch.object(routing, "log_volumes", side_effect=AssertionError("should not fetch volumes")) as vol:
            res = routing.get_routing(ctx)
        self.assertEqual(res["routes"], [])
        self.assertEqual(vol.call_count, 0)
        self.assertEqual(cloud.calls, [])

        # Filter-only route (logs == []) also skips log_volumes, only querying exported bytes.
        ctx.body = {"title": "Workload filter", "project": PROJECT, "logs": [], "filter": 'resource.type="cloud_run_revision"'}
        with mock.patch.object(routing, "log_volumes", return_value={}):
            routing.save_route(ctx)
        cloud.calls.clear()
        with mock.patch.object(routing, "log_volumes", side_effect=AssertionError("should not fetch volumes")) as vol:
            res = routing.get_routing(ctx)
        self.assertEqual(len(res["routes"]), 1)
        self.assertEqual(vol.call_count, 0)


if __name__ == "__main__":
    unittest.main()

