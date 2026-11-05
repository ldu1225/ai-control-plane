"""Optional activity log columns.

Log Router adds a column to a BigQuery table only when the first entry carrying that field arrives. A fresh
gemini_enterprise_user_activity table without agent ids or assist tokens therefore means 0 agent calls and nothing to
attribute yet, not an unsupported schema. (Customer incident: a newly created table showed "Agent fields were not found
in the activity log schema.") Warnings stay for schemas that are really unusable (no method column, no user
identities).
"""

import unittest

import support

from controlplane import timeutil
from controlplane.schema import Source, TableSchema
from controlplane.services import gemini_enterprise as ge
from controlplane.services import model_armor, users
from controlplane.services.base import ARMOR_OPS, GE_ACTIVITY

E = "jsonpayload_v1_sanitizeoperationlogentry"


def activity_source(method=True, user=True, agent=False, token=False):
    payload = []
    if user:
        payload.append({"name": "useriamprincipal", "type": "STRING"})
    if method:
        payload.append({"name": "logmetadata", "type": "RECORD", "fields": [{"name": "methodname", "type": "STRING"}]})
    request = [{"name": "query", "type": "RECORD", "fields": [{"name": "text", "type": "STRING"}]}]
    if agent:
        request.append({"name": "agentsspec", "type": "RECORD", "fields": [
            {"name": "agentspecs", "type": "RECORD", "mode": "REPEATED",
             "fields": [{"name": "agentid", "type": "STRING"}]}]})
    payload.append({"name": "request", "type": "RECORD", "fields": request})
    response = [{"name": "name", "type": "STRING"}]
    if token:
        response.append({"name": "assisttoken", "type": "STRING"})
    payload.append({"name": "response", "type": "RECORD", "fields": response})
    schema = TableSchema.from_dicts([{"name": "timestamp", "type": "TIMESTAMP"},
                                     {"name": "jsonPayload", "type": "RECORD", "fields": payload}])
    return Source(support.PROJECT, "audit", GE_ACTIVITY, "table", GE_ACTIVITY, schema)


def armor_source():
    schema = TableSchema.from_dicts([
        {"name": "timestamp", "type": "TIMESTAMP"},
        {"name": E, "type": "RECORD", "fields": [
            {"name": "operationtype", "type": "STRING"},
            {"name": "sanitizationresult", "type": "RECORD", "fields": [
                {"name": "sanitizationverdict", "type": "STRING"}]}]},
        {"name": "labels", "type": "RECORD", "fields": [
            {"name": "modelarmor_googleapis_com_client_correlation_id", "type": "STRING"}]},
    ])
    return Source(support.PROJECT, "audit", ARMOR_OPS, "table", ARMOR_OPS, schema)


class Ctx(support.FakeCtx):
    """Serves the given audit tables; every query answers with no rows."""

    def __init__(self, sources):
        super().__init__()
        self.range = timeutil.parse_range({"start": "2026-10-06", "end": "2026-10-19"}, "UTC", 14)
        self.srcs = sources
        self.queries = []

    def audit_source(self, logical, optional=False):
        return self.srcs.get(logical)

    def query(self, sql, params, module, **kwargs):
        self.queries.append(sql)
        return []

    def has_transient_warning(self):
        return False


def codes(ctx):
    return [w["code"] for w in ctx.warnings]


class AgentColumnTest(unittest.TestCase):
    def test_fresh_table_without_agent_ids_is_an_empty_series_without_a_notice(self):
        src = activity_source()
        ctx = Ctx({GE_ACTIVITY: src})
        self.assertEqual(ge._agent_usage(ctx, src, ctx.range), [])  # pylint: disable=protected-access
        self.assertEqual(ctx.warnings, [])
        self.assertEqual(ctx.queries, [])  # nothing to count yet

    def test_summary_shows_zero_agent_calls(self):
        ctx = Ctx({GE_ACTIVITY: activity_source()})
        out = ge.summary(ctx)
        self.assertEqual(out["kpis"]["agentCalls"], {"value": 0, "previous": 0})
        self.assertEqual(out["agentUsage"]["series"], [])
        self.assertNotIn("unsupported_schema", codes(ctx))

    def test_agent_ids_are_counted_once_the_column_exists(self):
        src = activity_source(agent=True)
        ctx = Ctx({GE_ACTIVITY: src})
        self.assertEqual(ge._agent_usage(ctx, src, ctx.range), [])  # pylint: disable=protected-access
        self.assertEqual(len(ctx.queries), 1)
        self.assertIn("agent_id IS NOT NULL", ctx.queries[0])

    def test_table_without_a_method_column_still_warns(self):
        src = activity_source(method=False)
        ctx = Ctx({GE_ACTIVITY: src})
        self.assertIsNone(ge._agent_usage(ctx, src, ctx.range))  # pylint: disable=protected-access
        self.assertEqual(ctx.warnings, [{"source": src.label, "code": "unsupported_schema",
                                         "message": "Agent fields were not found in the activity log schema."}])
        out = ge.summary(Ctx({GE_ACTIVITY: src}))
        self.assertIsNone(out["kpis"]["agentCalls"]["value"])


class AssistTokenColumnTest(unittest.TestCase):
    def test_attribution_works_before_the_first_assist_response(self):
        ctes = model_armor.attribution_ctes(activity_source())
        self.assertIsNotNone(ctes)
        sql = "\n".join(ctes)
        self.assertIn("CAST(NULL AS STRING) AS token", sql)
        self.assertIn("act_tok AS (", sql)
        self.assertIsNone(model_armor.attribution_ctes(activity_source(user=False)))

    def test_model_armor_page_has_no_notice_and_empty_user_lists(self):
        ctx = Ctx({ARMOR_OPS: armor_source(), GE_ACTIVITY: activity_source()})
        out = model_armor.summary(ctx)
        self.assertEqual(ctx.warnings, [])
        self.assertEqual(out["byUser"], [])
        self.assertEqual(out["kpis"]["blockedByOrigin"], {"user": 0, "automatic": 0, "unknown": 0})

    def test_missing_user_identities_still_warn(self):
        act = activity_source(user=False)
        ctx = Ctx({ARMOR_OPS: armor_source(), GE_ACTIVITY: act})
        out = model_armor.summary(ctx)
        self.assertIsNone(out["byUser"])
        self.assertEqual(ctx.warnings, [{"source": act.label, "code": "unsupported_schema",
                                         "message": "User identities are not available for user attribution."}])


class UsersTableTest(unittest.TestCase):
    def test_optional_columns_read_zero_not_unknown(self):
        ctx = Ctx({GE_ACTIVITY: activity_source(), ARMOR_OPS: armor_source()})
        _ctes, parts, labels, _agy, available = users.long_ctes(ctx)
        self.assertTrue({"agentCalls", "armorBlocks"} <= available)
        joined = "\n".join(parts)
        self.assertNotIn("agent_id IS NOT NULL", joined)  # no column to count yet
        self.assertNotIn("armorBlocks", joined)
        self.assertNotIn(armor_source().label, labels)

    def test_columns_are_counted_once_they_exist(self):
        ctx = Ctx({GE_ACTIVITY: activity_source(agent=True, token=True), ARMOR_OPS: armor_source()})
        _ctes, parts, labels, _agy, available = users.long_ctes(ctx)
        self.assertTrue({"agentCalls", "armorBlocks"} <= available)
        joined = "\n".join(parts)
        self.assertIn("agent_id IS NOT NULL", joined)
        self.assertIn("'armorBlocks' AS metric", joined)
        self.assertIn(armor_source().label, labels)

    def test_no_method_column_keeps_agent_calls_unknown(self):
        ctx = Ctx({GE_ACTIVITY: activity_source(method=False)})
        available = users.long_ctes(ctx)[4]
        self.assertNotIn("agentCalls", available)


if __name__ == "__main__":
    unittest.main()
