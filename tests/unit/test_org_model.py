"""Organization model: CSV checks and imports, unit ids, attribution precedence, the unit tree and load caching."""

import copy
import hashlib
import json
import types
import unittest
from unittest import mock

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane import identity
from controlplane.httpkit import ApiError
from controlplane.services import governance, org_model
from controlplane.settings_store import StoreConflict, StoreError, aux_docs

HEADER = "principal,unit_path,cost_center\n"
ADMIN = "admin@example.com"


def make_ctx(body=None, app=None, role="admin", settings=None):
    """A minimal request context. The default app is a bare object: no cache, no settings store, no hooks."""
    return types.SimpleNamespace(
        app=app if app is not None else types.SimpleNamespace(), body=body or {}, params={},
        identity=types.SimpleNamespace(email=ADMIN, role=role),
        settings=settings if settings is not None else {"chargeback": {"project_unit_label": "cost-center"}})


def check(text, app=None, **body):
    return org_model.import_csv(make_ctx(dict(body, csv=text), app=app))


def apply(text, app, **body):
    with mock.patch.object(governance, "write_audit", return_value=True):
        return check(text, app=app, dryRun=False, **body)


def codes(result):
    return [(err["row"], err["code"]) for err in result["errors"]]


def expected_id(lowered_path):
    return "u_" + hashlib.sha256(lowered_path.encode("utf-8")).hexdigest()[:10]


def raises(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except ApiError as err:
        return err.code, err.message
    raise AssertionError("ApiError not raised")


class CsvFileTest(unittest.TestCase):
    def test_header_variants(self):
        for text in ("principal,unit_path\nalice@example.com,Sales\n",
                     HEADER + "alice@example.com,Sales,CC-1\n",
                     " Principal , UNIT_PATH ,Cost_Center \r\nalice@example.com,Sales,CC-1\r\n",
                     "\n\n" + HEADER + "alice@example.com,Sales\n"):        # blank lines before the header
            result = check(text)
            self.assertTrue(result["valid"], (text, result["errors"]))
            self.assertEqual((result["rows"], result["units"], result["members"], result["projects"]),
                             (1, 1, 1, 0))

    def test_wrong_headers(self):
        for text in ("email,unit\na@example.com,Sales\n", "principal\na@example.com\n",
                     "principal,unit_path,cost_center,owner\n", "unit_path,principal\nSales,a@example.com\n",
                     "principal;unit_path\na@example.com;Sales\n", "principal,unit_path,\n"):
            self.assertEqual(raises(check, text),
                             ("bad_request", "The CSV header must be principal,unit_path,cost_center."), text)

    def test_byte_order_mark_is_stripped_before_hashing(self):
        text = HEADER + "alice@example.com,Sales,CC-1\n"
        plain, marked = check(text), check("\ufeff" + text)
        self.assertTrue(marked["valid"])
        self.assertEqual(marked["sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertEqual(plain["sha256"], marked["sha256"])

    def test_empty_missing_and_non_text_files(self):
        for body in ({}, {"csv": None}, {"csv": ""}, {"csv": " \r\n\n"}, {"csv": "\ufeff"}, {"csv": ",,\n\n"}):
            self.assertEqual(raises(org_model.import_csv, make_ctx(body)), ("bad_request", "The CSV file is empty."))
        for value in (42, True, ["a"], {"a": 1}):
            self.assertEqual(raises(org_model.import_csv, make_ctx({"csv": value})),
                             ("bad_request", "Field 'csv' must be the text of a CSV file."))
        self.assertEqual(raises(check, HEADER + ",Sales\ud800\n"), ("bad_request", "The CSV file must be UTF-8 text."))

    def test_size_limit_counts_utf8_bytes(self):
        too_large = ("bad_request", "The CSV file is larger than 5 MB.")
        self.assertEqual(raises(check, "a" * (5 * 1024 * 1024 + 1)), too_large)
        self.assertEqual(raises(check, "\uac00" * (5 * 1024 * 1024 // 3 + 1)), too_large)  # 3 bytes per character
        text = HEADER + "alice@example.com,Sales\n"
        size = len(text.encode("utf-8"))
        with mock.patch.object(org_model, "MAX_CSV_BYTES", size):
            self.assertTrue(check(text)["valid"])
        with mock.patch.object(org_model, "MAX_CSV_BYTES", size - 1):
            self.assertEqual(raises(check, text)[0], "bad_request")

    def test_unreadable_quoting_fails_closed(self):
        text = HEADER + 'alice@example.com,"' + "x" * 140000 + '"\n'   # longer than the csv module's field limit
        self.assertEqual(raises(check, text),
                         ("bad_request", "The CSV file could not be read at row 2. Check the quotes in that row."))

    def test_quoted_values(self):
        app = types.SimpleNamespace()
        result = apply(HEADER + '"alice@example.com","R&D, Korea/The ""Lab""","CC-1"\n', app)
        self.assertTrue(result["applied"])
        model = org_model.load(make_ctx(app=app))
        lab = expected_id('r&d, korea/the "lab"')
        self.assertEqual(model.unit(lab)["path"], 'R&D, Korea/The "Lab"')
        self.assertEqual(model.unit(lab)["costCenter"], "CC-1")
        self.assertEqual(model.members, {"alice@example.com": lab})

    def test_rows_are_numbered_by_the_line_where_they_start(self):
        text = (HEADER                                      # row 1
                + 'alice@example.com,"Sales\nKorea"\n'      # rows 2-3: a line break inside quotes
                + "\n"                                      # row 4: blank
                + "bob@example.com,\n"                      # row 5: no unit path
                + ",,\n"                                    # row 6: empty cells only
                + "carol@example.com,Sales,CC 1\n")         # row 7
        result = check(text)
        self.assertEqual(codes(result), [(2, "bad_path"), (5, "bad_path"), (7, "bad_cost_center")])
        self.assertEqual(result["rows"], 3)

    def test_dry_run_flag_must_be_boolean(self):
        text = HEADER + "alice@example.com,Sales\n"
        self.assertEqual(raises(check, text, dryRun="false"),
                         ("bad_request", "Field 'dryRun' must be true or false."))
        self.assertTrue(check(text, dryRun=None)["dryRun"])

    def test_viewers_cannot_import(self):
        ctx = make_ctx({"csv": HEADER + "alice@example.com,Sales\n"}, role="viewer")
        self.assertEqual(raises(org_model.import_csv, ctx)[0], "forbidden")


class RowErrorsTest(unittest.TestCase):
    ROWS = [
        ("a@example.com,Sales,CC-1,extra", "bad_row"),
        ("serviceAccount:sa@demo.iam.gserviceaccount.com,Sales", "bad_principal"),
        ("sa@demo.iam.gserviceaccount.com,Sales", "bad_principal"),
        ("group:team@example.com,Sales", "bad_principal"),
        ("domain:example.com,Sales", "bad_principal"),
        ("not a principal,Sales", "bad_principal"),
        ("x" * 300 + "@example.com,Sales", "bad_principal"),         # longer than an e-mail address can be
        ("y" * 800 + ",Sales", "bad_principal"),
        ("project:Bad_Project!,Sales", "bad_project"),
        ("project:,Sales", "bad_project"),
        (", / / ", "bad_path"),
        (",Sales/\tKorea", "bad_path"),
        (",Sales\x1fKorea", "bad_path"),                              # str.split() would turn this into a space
        (",Sales\x7f", "bad_path"),
        (",Sales/\u202eKorea", "bad_path"),                           # bidirectional override
        (",Sales/\ufffd", "bad_path"),                                # not saved as UTF-8
        (",Sales/" + "x" * 121, "segment_too_long"),
        ("," + "/".join("L%d" % i for i in range(11)), "path_too_deep"),
        (",Sales,CC 1", "bad_cost_center"),
        (",Sales," + "C" * 61, "bad_cost_center"),
        (",Sales,CC#1", "bad_cost_center"),
    ]

    def test_every_row_error_code(self):
        result = check(HEADER + "\n".join(row for row, _ in self.ROWS) + "\n")
        self.assertEqual(codes(result), [(i + 2, code) for i, (_, code) in enumerate(self.ROWS)])
        self.assertEqual((result["valid"], result["errorCount"], result["warningCount"]),
                         (False, len(self.ROWS), 0))
        for err in result["errors"]:
            self.assertEqual(set(err), {"row", "code", "message", "value"})
            self.assertTrue(err["message"].endswith("."), err)
            self.assertLessEqual(len(err["value"]), 200)
        values = {err["row"]: err["value"] for err in result["errors"]}
        self.assertEqual(len(values[8]), 200)
        self.assertEqual(values[13], "Sales/\ufffdKorea")              # control characters are replaced
        self.assertEqual(values[3], "serviceAccount:sa@demo.iam.gserviceaccount.com")

    def test_limits_are_inclusive(self):
        text = (HEADER + "alice@example.com," + "/".join("L%d" % i for i in range(10)) + "\n"
                + "bob@example.com,Sales/" + "x" * 120 + "\n" + ",Finance," + "C" * 60 + "\n")
        result = check(text)
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["units"], 13)

    def test_conflicts_name_both_rows(self):
        text = HEADER + ("alice@example.com,Sales/Korea\n"      # 2
                         "user:Alice@Example.com,Sales/Japan\n"  # 3: the same person in another unit
                         "project:sales-data,Sales\n"            # 4
                         "project:sales-data,Finance\n"          # 5
                         ",Sales/Korea,KR-1\n"                   # 6
                         ",sales/korea,KR-2\n"                   # 7: the same unit with another cost center
                         ",Sales/Korea,kr-1\n")                  # 8: the same cost center apart from case
        result = check(text)
        self.assertEqual(codes(result), [(3, "principal_conflict"), (5, "project_conflict"),
                                         (7, "cost_center_conflict")])
        messages = [err["message"] for err in result["errors"]]
        self.assertEqual(messages[0], "This person is mapped to 'Sales/Korea' in row 2 and to 'Sales/Japan' in row 3.")
        self.assertEqual(messages[1], "This project is mapped to 'Sales' in row 4 and to 'Finance' in row 5.")
        self.assertEqual(messages[2], "The unit 'Sales/Korea' has cost center KR-1 in row 6 and KR-2 in row 7.")
        self.assertEqual(result["errors"][0]["value"], "user:Alice@Example.com")

    def test_repeated_mappings_are_warnings(self):
        workforce = "principal://iam.googleapis.com/locations/global/workforcePools/Corp-Pool/subject/Kim.Lee"
        text = HEADER + ("alice@example.com,Sales\n"
                         "user:ALICE@example.com, Sales \n"
                         "project:sales-data,Sales\n"
                         "project:SALES-DATA,sales\n"
                         + workforce + ",Sales\n"
                         + workforce.replace("Corp-Pool", "corp-pool") + ",Sales\n")
        result = check(text)
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual([(w["row"], w["code"]) for w in result["warnings"]],
                         [(3, "duplicate"), (5, "duplicate"), (7, "duplicate")])
        self.assertEqual(result["warnings"][0]["message"], "Row 2 already maps this person to the same unit.")
        self.assertEqual((result["warningCount"], result["members"], result["projects"]), (3, 2, 1))

    def test_people_use_identity_keys(self):
        workforce = "principal://iam.googleapis.com/locations/global/workforcePools/Corp-Pool/subject/Kim.Lee"
        app = types.SimpleNamespace()
        apply(HEADER + workforce + ",Sales\nprincipal://goog/subject/Bob@example.com,Sales\n", app)
        self.assertEqual(sorted(org_model.load(make_ctx(app=app)).members),
                         sorted([identity.person_key(workforce), "bob@example.com"]))


class LimitsTest(unittest.TestCase):
    def test_row_limit(self):
        with mock.patch.object(org_model, "MAX_ROWS", 3):
            self.assertEqual(check(HEADER + "a@example.com,A\n\n" * 3)["rows"], 3)  # blank lines are not rows
            self.assertEqual(raises(check, HEADER + "a@example.com,A\n" * 4),
                             ("bad_request", "The CSV file has more than 3 rows."))

    def test_unit_limit_counts_implicit_ancestors(self):
        with mock.patch.object(org_model, "MAX_UNITS", 3):
            self.assertEqual(check(HEADER + ",A/B/C\n,a/b\n")["units"], 3)
            self.assertEqual(raises(check, HEADER + ",A/B/C\n,A/D\n"),
                             ("bad_request", "The CSV file defines more than 3 units."))

    def test_depth_limit_constant(self):
        with mock.patch.object(org_model, "MAX_DEPTH", 2):
            self.assertEqual(codes(check(HEADER + ",A/B/C\n,A/B\n")), [(2, "path_too_deep")])

    def test_issue_lists_are_capped_but_counted(self):
        with mock.patch.object(org_model, "MAX_ISSUES", 2):
            result = check(HEADER + "bad,A\n" * 5 + "a@example.com,A\n" * 4)
        self.assertEqual((len(result["errors"]), result["errorCount"]), (2, 5))
        self.assertEqual((len(result["warnings"]), result["warningCount"]), (2, 3))


class UnitIdTest(unittest.TestCase):
    def test_ids_are_deterministic_and_normalized(self):
        self.assertEqual(org_model.unit_id("Sales/Korea"), expected_id("sales/korea"))
        self.assertEqual(org_model.unit_id("  SALES //  Korea \u3000 Team / "), expected_id("sales/korea team"))
        self.assertEqual(org_model.unit_id(["Sales", " Korea "]), expected_id("sales/korea"))
        self.assertEqual(org_model.split_path(" a // b \u00a0 c /"), ["a", "b c"])
        for empty in (" / ", "", []):
            with self.assertRaises(ValueError):
                org_model.unit_id(empty)

    def test_implicit_ancestors_and_first_spelling(self):
        app = types.SimpleNamespace()
        apply(HEADER + "alice@example.com,Sales/Korea/Seoul Team\n,SALES,S-1\nbob@example.com,sales/JAPAN\n", app)
        model = org_model.load(make_ctx(app=app))
        sales, korea = expected_id("sales"), expected_id("sales/korea")
        team, japan = expected_id("sales/korea/seoul team"), expected_id("sales/japan")
        self.assertEqual(set(model.units), {sales, korea, team, japan})
        self.assertEqual(model.units[team], {"id": team, "name": "Seoul Team", "parent": korea,
                                             "path": "Sales/Korea/Seoul Team", "costCenter": "", "depth": 2})
        self.assertEqual(model.unit(sales)["costCenter"], "S-1")      # set by a later row that only names the unit
        self.assertEqual(model.unit(japan)["path"], "Sales/JAPAN")    # an ancestor keeps its first spelling
        self.assertEqual(model.members, {"alice@example.com": team, "bob@example.com": japan})
        doc, _ = aux_docs(app).load(org_model.DOC)
        self.assertEqual(doc["units"][korea], {"name": "Korea", "parent": sales, "path": "Sales/Korea",
                                               "costCenter": ""})


class ApplyTest(unittest.TestCase):
    TEXT = HEADER + "alice@example.com,Sales/Korea,KR-1\nbob@example.com,Sales/Japan\nproject:sales-data,Sales\n"
    CHANGED = HEADER + "alice@example.com,Sales/Japan\ncarol@example.com,Finance\nproject:sales-data,Finance\n"

    def test_dry_run_is_the_default_and_writes_nothing(self):
        app = support.FakeApp()
        with mock.patch.object(governance, "write_audit") as audit:
            result = check(self.TEXT, app=app)
            check(self.TEXT, app=app, dryRun=True)
        audit.assert_not_called()
        self.assertEqual(aux_docs(app).load(org_model.DOC), ({}, 0))
        self.assertEqual(app.invalidations, 0)
        self.assertEqual(result["dryRun"], True)
        self.assertNotIn("applied", result)
        self.assertEqual({k: result[k] for k in ("valid", "rows", "units", "members", "projects", "errorCount",
                                                 "warningCount")},
                         {"valid": True, "rows": 3, "units": 3, "members": 2, "projects": 1, "errorCount": 0,
                          "warningCount": 0})
        self.assertEqual(result["diff"], {"unitsAdded": 3, "unitsRemoved": 0, "membersAdded": 2, "membersRemoved": 0,
                                          "membersMoved": 0, "projectsChanged": 1})

    def test_apply_replaces_the_model_audits_and_invalidates(self):
        app = support.FakeApp()
        digest = hashlib.sha256(self.TEXT.encode("utf-8")).hexdigest()
        with mock.patch.object(governance, "write_audit", return_value=True) as audit:
            result = check(self.TEXT, app=app, dryRun=False, expectSha256=digest.upper())
        self.assertEqual((result["dryRun"], result["applied"], result["version"], result["auditLogged"]),
                         (False, True, 1, True))
        audit.assert_called_once()
        _, action, payload = audit.call_args[0]
        self.assertEqual(action, "org_import")
        self.assertEqual(payload, {"version": 1, "sha256": digest, "rows": 3, "units": 3, "members": 2,
                                   "projects": 1})
        self.assertEqual(app.invalidations, 1)
        doc, generation = aux_docs(app).load(org_model.DOC)
        self.assertEqual(generation, 1)
        self.assertEqual(set(doc), {"version", "updatedAt", "updatedBy", "source", "units", "members", "projects"})
        self.assertEqual((doc["version"], doc["updatedBy"]), (1, ADMIN))
        self.assertEqual(doc["source"], {"kind": "csv", "sha256": digest, "rows": 3, "importedAt": doc["updatedAt"],
                                         "importedBy": ADMIN})
        self.assertEqual(doc["projects"], {"sales-data": expected_id("sales")})

        with mock.patch.object(governance, "write_audit", return_value=True) as audit:
            result = check(self.CHANGED, app=app, dryRun=False, expectSha256="")
        self.assertEqual(result["version"], 2)
        self.assertEqual(audit.call_args[0][2]["version"], 2)
        self.assertEqual(result["diff"], {"unitsAdded": 1, "unitsRemoved": 1, "membersAdded": 1, "membersRemoved": 1,
                                          "membersMoved": 1, "projectsChanged": 1})
        model = org_model.load(make_ctx(app=app))
        self.assertEqual((model.version, dict(model.members)),
                         (2, {"alice@example.com": expected_id("sales/japan"),
                              "carol@example.com": expected_id("finance")}))

    def test_files_with_errors_or_changed_files_are_refused(self):
        app = support.FakeApp()
        with mock.patch.object(governance, "write_audit") as audit:
            self.assertEqual(raises(check, HEADER + "group:team@example.com,Sales\n", app=app, dryRun=False),
                             ("bad_request", "The CSV file has errors. Fix them and run the check again."))
            self.assertEqual(raises(check, self.TEXT, app=app, dryRun=False, expectSha256="0" * 64),
                             ("bad_request", "The file changed after the check. Run the check again."))
        audit.assert_not_called()
        self.assertEqual(aux_docs(app).load(org_model.DOC), ({}, 0))
        self.assertEqual(app.invalidations, 0)

    def test_audit_failure_does_not_undo_the_import(self):
        app = types.SimpleNamespace()
        with mock.patch.object(governance, "write_audit", side_effect=RuntimeError("unavailable")), \
                self.assertLogs("controlplane.org_model", level="WARNING") as logs:
            result = check(self.TEXT, app=app, dryRun=False)
        self.assertEqual((result["applied"], result["auditLogged"]), (True, False))
        self.assertEqual(org_model.load(make_ctx(app=app)).version, 1)
        self.assertEqual(logs.output, ["WARNING:controlplane.org_model:organization model audit entry failed: "
                                       "RuntimeError"])

    def test_logs_hold_counts_only(self):
        with self.assertLogs("controlplane.org_model", level="INFO") as logs:
            apply(self.TEXT, types.SimpleNamespace())
        text = "\n".join(logs.output)
        for secret in ("@", "Sales", "KR-1", "sales-data"):
            self.assertNotIn(secret, text)

    def test_store_failures_fail_closed(self):
        app = types.SimpleNamespace()
        store = aux_docs(app)
        with self.assertLogs("controlplane.org_model", level="WARNING") as logs:
            with mock.patch.object(store, "mutate", side_effect=StoreConflict()):
                self.assertEqual(raises(apply, self.TEXT, app)[0], "conflict")
            with mock.patch.object(store, "mutate", side_effect=StoreError("unreadable")):
                self.assertEqual(raises(apply, self.TEXT, app)[0], "upstream_error")
            with mock.patch.object(store, "load", side_effect=StoreError("unreadable")):
                self.assertEqual(raises(org_model.load, make_ctx(app=app))[0], "upstream_error")
        self.assertEqual(logs.output, ["ERROR:controlplane.org_model:organization model could not be saved: "
                                       "StoreError",
                                       "WARNING:controlplane.org_model:organization model could not be read: "
                                       "StoreError"])
        self.assertEqual(store.load(org_model.DOC), ({}, 0))

    def test_works_with_the_shared_fake_context(self):
        ctx = support.FakeCtx(settings=support.make_settings(chargeback={"project_unit_label": "team"}),
                              body={"csv": self.TEXT, "dryRun": False})
        with mock.patch.object(governance, "write_audit", return_value=True):
            self.assertEqual(org_model.import_csv(ctx)["version"], 1)
        self.assertEqual(ctx.app.invalidations, 1)
        result = org_model.tree(ctx)
        self.assertEqual((result["labelKey"], result["counts"]["members"]), ("team", 2))
        viewer = support.FakeCtx(body={"csv": self.TEXT}, role="viewer")
        self.assertEqual(raises(org_model.import_csv, viewer)[0], "forbidden")


class ResolutionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = types.SimpleNamespace()
        apply(HEADER + "\n".join([
            "alice@example.com,Sales/Korea,KR-100",
            "project:sales-data,Sales/Japan,JP-200",
            ",Finance,shared-cc",
            ",Accounting,Shared-CC",
            ",Sales/Korea/Seoul,shared-cc",
            ",Ops/Infra,SHARED-CC",
        ]) + "\n", cls.app)
        cls.model = org_model.load(make_ctx(app=cls.app))

    def test_precedence(self):
        resolve = self.model.unit_for
        korea, japan = expected_id("sales/korea"), expected_id("sales/japan")
        labels = {"cost-center": "shared-cc"}
        self.assertEqual(resolve("alice@example.com", "sales-data", labels, "cost-center"), (korea, "member"))
        self.assertEqual(resolve("user:ALICE@Example.com", None), (korea, "member"))
        self.assertEqual(resolve("bob@example.com", "sales-data", labels, "cost-center"), (japan, "project"))
        self.assertEqual(resolve("sa@demo.iam.gserviceaccount.com", "SALES-DATA"), (japan, "project"))
        self.assertEqual(resolve("bob@example.com", "other-project", labels, "cost-center"),
                         (expected_id("accounting"), "label"))
        unassigned = (org_model.UNASSIGNED, "unassigned")
        self.assertEqual(resolve("bob@example.com", "other-project", labels, ""), unassigned)
        self.assertEqual(resolve("bob@example.com", "other-project", None, "cost-center"), unassigned)
        self.assertEqual(resolve("bob@example.com", "other-project", {"cost-center": "nope"}, "cost-center"),
                         unassigned)
        self.assertEqual(resolve(None, None), unassigned)

    def test_label_matches_cost_center_case_insensitively_or_unit_id(self):
        resolve = self.model.unit_for
        infra = expected_id("ops/infra")
        self.assertEqual(resolve(None, "other-project", {"cost-center": "kr-100"}, "cost-center"),
                         (expected_id("sales/korea"), "label"))
        # Several units with the cost center: the highest unit wins, then the first path (Accounting < Finance).
        self.assertEqual(resolve(None, "other-project", {"cost-center": " SHARED-cc "}, "cost-center"),
                         (expected_id("accounting"), "label"))
        self.assertEqual(resolve(None, "other-project", {"cost-center": infra}, "cost-center"), (infra, "label"))
        billing_labels = [{"key": "env", "value": "prod"}, {"key": "cost-center", "value": "jp-200"}]
        self.assertEqual(resolve(None, "other-project", billing_labels, "cost-center"),
                         (expected_id("sales/japan"), "label"))

    def test_virtual_unassigned_unit(self):
        virtual = {"id": "u_unassigned", "name": "Unassigned", "parent": None, "path": "Unassigned", "costCenter": "",
                   "depth": 0}
        self.assertEqual(self.model.unit(org_model.UNASSIGNED), virtual)
        self.assertEqual(self.model.unit("u_0000000000"), virtual)
        self.assertEqual(self.model.ancestors(org_model.UNASSIGNED), [org_model.UNASSIGNED])
        self.assertEqual(self.model.ancestors("u_0000000000"), [org_model.UNASSIGNED])
        self.assertNotIn(org_model.UNASSIGNED, self.model.units)


class TreeTest(unittest.TestCase):
    def test_order_ancestors_and_counts(self):
        app = types.SimpleNamespace()
        apply(HEADER + "\n".join([
            "alice@example.com,Sales/Korea/Seoul",
            "bob@example.com,Sales/Korea",
            "carol@example.com,Sales/Japan",
            "dave@example.com,Finance",
            "project:sales-data,Sales/Korea",
            "project:ads-data,Sales/Korea",
            "project:fin-data,Finance,FIN-1",
            ",admin",
        ]) + "\n", app)
        ctx = make_ctx(app=app)
        model = org_model.load(ctx)
        admin, fin, sales = expected_id("admin"), expected_id("finance"), expected_id("sales")
        korea, seoul, japan = expected_id("sales/korea"), expected_id("sales/korea/seoul"), expected_id("sales/japan")
        self.assertEqual(model.roots(), [admin, fin, sales])                  # by name, ignoring case
        self.assertEqual(model.dfs_order(), [admin, fin, sales, japan, korea, seoul])
        self.assertEqual(model.children()[sales], [japan, korea])
        self.assertEqual(model.children()[seoul], [])
        self.assertEqual(model.ancestors(seoul), [seoul, korea, sales])
        self.assertEqual(model.ancestors(admin), [admin])

        result = org_model.tree(ctx)
        self.assertEqual([u["id"] for u in result["units"]], model.dfs_order())
        rows = {u["id"]: u for u in result["units"]}
        self.assertEqual(rows[korea], {"id": korea, "name": "Korea", "parent": sales, "path": "Sales/Korea",
                                       "costCenter": "", "depth": 1, "members": 1, "membersTotal": 2,
                                       "projects": ["ads-data", "sales-data"]})
        self.assertEqual((rows[sales]["members"], rows[sales]["membersTotal"]), (0, 3))
        self.assertEqual((rows[seoul]["members"], rows[seoul]["membersTotal"]), (1, 1))
        self.assertEqual((rows[admin]["members"], rows[admin]["membersTotal"], rows[admin]["projects"]), (0, 0, []))
        self.assertEqual((rows[fin]["costCenter"], rows[fin]["projects"]), ("FIN-1", ["fin-data"]))
        self.assertEqual(result["counts"], {"units": 6, "members": 4, "projects": 3})
        self.assertEqual((result["version"], result["empty"], result["updatedBy"], result["labelKey"]),
                         (1, False, ADMIN, "cost-center"))
        self.assertEqual((result["source"]["kind"], result["source"]["rows"]), ("csv", 8))
        dumped = json.dumps(result)
        for member in ("alice@", "bob@", "carol@", "dave@"):
            self.assertNotIn(member, dumped)

    def test_empty_model(self):
        self.assertEqual(org_model.tree(make_ctx(settings={})),
                         {"version": 0, "updatedAt": None, "updatedBy": None, "source": {}, "empty": True,
                          "labelKey": "", "counts": {"units": 0, "members": 0, "projects": 0}, "units": []})


class LoadTest(unittest.TestCase):
    def test_model_is_rebuilt_only_when_the_document_changes(self):
        app = types.SimpleNamespace()   # no cache attribute
        ctx = make_ctx(app=app)
        with mock.patch.object(org_model, "OrgModel", wraps=org_model.OrgModel) as built:
            first = org_model.load(ctx)
            self.assertTrue(first.empty)
            self.assertIs(org_model.load(ctx), first)
            self.assertEqual(built.call_count, 1)
            apply(HEADER + "alice@example.com,Sales\n", app)
            second = org_model.load(ctx)
            self.assertIs(org_model.load(make_ctx(app=app)), second)
            self.assertEqual((second.version, built.call_count), (1, 2))
            aux_docs(app).mutate(org_model.DOC, lambda doc: doc.update(version=7))   # a write by another path
            third = org_model.load(ctx)
            self.assertEqual((third.version, built.call_count), (7, 3))
            self.assertEqual(dict(third.members), {"alice@example.com": expected_id("sales")})
            self.assertTrue(org_model.load(make_ctx(app=types.SimpleNamespace())).empty)   # another store
        self.assertEqual(built.call_count, 4)

    def test_models_are_read_only_and_json_ready(self):
        app = types.SimpleNamespace()
        apply(HEADER + "alice@example.com,Sales/Korea,KR-1\n", app)
        model = org_model.load(make_ctx(app=app))
        korea = expected_id("sales/korea")
        for change in (lambda: model.members.__setitem__("bob@example.com", korea),
                       lambda: model.units[korea].update(name="X"), lambda: model.projects.clear(),
                       lambda: model.source.pop("sha256")):
            with self.assertRaises(TypeError):
                change()
        copied = model.unit(korea)
        copied["name"] = "X"
        self.assertEqual(model.unit(korea)["name"], "Korea")
        self.assertEqual(json.loads(json.dumps(model.units))[korea]["path"], "Sales/Korea")
        clone = copy.deepcopy(model.units)
        clone[korea]["name"] = "Y"   # deep copies are plain dicts
        self.assertEqual(model.units[korea]["name"], "Korea")


class DamagedDocumentTest(unittest.TestCase):
    def test_invalid_entries_are_left_out(self):
        a, b, x, y, orphan = (expected_id(p) for p in ("a", "a/b", "x", "y", "orphan"))
        doc = {"version": "7", "updatedAt": 5, "source": "csv",
               "units": {a: {"name": "A", "parent": None, "path": "WRONG", "costCenter": "bad cc"},
                         b: {"name": " B   b ", "parent": a, "path": "A/B", "costCenter": "CC-B"},
                         x: {"name": "X", "parent": y}, y: {"name": "Y", "parent": x},     # a parent cycle
                         orphan: {"name": "Orphan", "parent": "u_0000000000"},             # unknown parent
                         "u_unassigned": {"name": "Unassigned"}, "u_ABCDEF0123": {"name": "Upper"},
                         expected_id("slash"): {"name": "Q/R"}, expected_id("ctl"): {"name": "C\x00"},
                         expected_id("blank"): {"name": " "}, expected_id("text"): "Sales"},
               "members": {"alice@example.com": b, "Bob@Example.com": a, "carol@example.com": x,
                           "sa@demo.iam.gserviceaccount.com": a, "dave@example.com": "u_0000000000",
                           "erin@example.com": None},
               "projects": {"good-project": a, "Bad_Project": a, "cycle-project": y, "short": a}}
        model = org_model.OrgModel(doc)
        self.assertEqual((model.version, model.updated_at, dict(model.source)), (0, None, {}))
        self.assertEqual(model.dfs_order(), [a, b, orphan])
        self.assertEqual(model.unit(a), {"id": a, "name": "A", "parent": None, "path": "A", "costCenter": "",
                                         "depth": 0})
        self.assertEqual((model.unit(b)["path"], model.unit(b)["costCenter"]), ("A/B b", "CC-B"))
        self.assertIsNone(model.unit(orphan)["parent"])
        self.assertEqual(dict(model.members), {"alice@example.com": b})
        self.assertEqual(dict(model.projects), {"good-project": a})
        for empty in (None, {}, [], "text"):
            self.assertTrue(org_model.OrgModel(empty).empty)


if __name__ == "__main__":
    unittest.main()
