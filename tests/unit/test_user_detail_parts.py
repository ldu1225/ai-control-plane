"""User details drawer speed-ups: the address hint of the opened row, the parallel per-person Antigravity queries and
the caches around the Cloud Logging reads (first sink entries, entries the sink table never received)."""

import datetime as dt
import unittest
from unittest import mock

import support
from test_inference_log_users import END, START, sink_source

from controlplane import timeutil
from controlplane.gcp import GcpError
from controlplane.security import uid_for
from controlplane.services import governance as gov
from controlplane.services import users


class _ParamsCtx:
    def __init__(self, params):
        self.params = params


class HintedEmailTest(unittest.TestCase):
    """detail() takes the address sent with the uid only when it is a valid address of exactly that uid."""

    def test_matching_address_is_used_normalised(self):
        uid = uid_for("alice@example.com")
        self.assertEqual(users._hinted_email(_ParamsCtx({"email": " Alice@Example.com "}), uid),  # pylint: disable=protected-access
                         "alice@example.com")

    def test_address_of_another_person_is_ignored(self):
        self.assertIsNone(users._hinted_email(  # pylint: disable=protected-access
            _ParamsCtx({"email": "bob@example.com"}), uid_for("alice@example.com")))

    def test_missing_malformed_or_oversized_address_is_ignored(self):
        uid = uid_for("alice@example.com")
        for params in ({}, {"email": ""}, {"email": "alice"}, {"email": ["alice@example.com"]}, {"email": None}):
            self.assertIsNone(users._hinted_email(_ParamsCtx(params), uid), params)  # pylint: disable=protected-access
        long_address = "a" * 320 + "@example.com"
        self.assertIsNone(users._hinted_email(  # pylint: disable=protected-access
            _ParamsCtx({"email": long_address}), uid_for(long_address)))


class PersonQueryCtx(support.FakeCtx):
    """Answers the per-person Antigravity queries by their GROUP BY; `fail` makes one of them fail."""

    def __init__(self, main_rows, client_rows=(), bucket_rows=(), fail=None):
        super().__init__()
        self.rows = {"main": list(main_rows), "client": list(client_rows), "bucket": list(bucket_rows)}
        self.fail = fail
        self.kinds = []

    def query(self, sql, params, module, **kwargs):
        if "GROUP BY email, model, project" in sql:
            kind = "main"
        elif "GROUP BY k" in sql:
            kind = "client"
        elif "GROUP BY bucket, email" in sql:
            kind = "bucket"
        else:
            raise AssertionError("unexpected query: %s" % sql[-200:])
        self.kinds.append(kind)
        if kind == self.fail:
            raise GcpError("http", message="boom")
        return self.rows[kind]


def person_row(model, project, requests, tokens, prompt, cand, cached):
    return {"email": "alice@example.com", "model": model, "project": project, "requests": requests, "tokens": tokens,
            "prompt_tokens": prompt, "candidate_tokens": cand, "cached_tokens": cached,
            "clients": ["antigravity_cli"], "last_ts": dt.datetime(2026, 10, 5, tzinfo=dt.timezone.utc)}


class PersonUsageTest(unittest.TestCase):
    ROWS = [person_row("claude-sonnet-5-5", "proj-a", 2, 300, 200, 100, 50),
            person_row("claude-sonnet-5-5", "proj-b", 1, 100, 80, 20, 0),
            person_row("gemini-3.8-pro", "proj-a", 4, 1000, 900, 100, 600),
            person_row(None, "proj-a", 1, 10, 8, 2, 0)]

    def test_per_model_totals_come_from_the_person_rows_without_a_separate_job(self):
        ctx = PersonQueryCtx(self.ROWS, client_rows=[{"k": "antigravity_cli", "requests": 8, "tokens": 1410,
                                                      "p": 1188, "c": 222, "ca": 650}])
        usage = gov._bq_usage(ctx, sink_source(), START, END, email="alice@example.com")  # pylint: disable=protected-access
        self.assertEqual(sorted(ctx.kinds), ["client", "main"])  # no per-model query any more
        self.assertEqual(usage.by_model["claude-sonnet-5-5"], {"requests": 3, "tokens": 400, "promptTokens": 280,
                                                               "candidateTokens": 120, "cachedTokens": 50})
        self.assertEqual(usage.by_model["gemini-3.8-pro"]["cachedTokens"], 600)
        self.assertEqual(usage.by_model["unknown"]["requests"], 1)
        person = usage.users["alice@example.com"]
        self.assertEqual(sum(m["requests"] for m in usage.by_model.values()), person["requests"])
        self.assertEqual(sum(m["tokens"] for m in usage.by_model.values()), person["tokens"])
        self.assertEqual(usage.by_client["antigravity_cli"]["tokens"], 1410)

    def test_daily_buckets_run_with_the_person_rows(self):
        rng = timeutil.parse_range({"start": "2026-10-01", "end": "2026-10-31"}, "UTC", 31)
        bucket = rng.buckets()[4]
        ctx = PersonQueryCtx(self.ROWS, bucket_rows=[{"bucket": bucket, "email": "alice@example.com", "requests": 8,
                                                      "tokens": 1410}])
        usage = gov._bq_usage(ctx, sink_source(), START, END, email="alice@example.com", rng=rng)  # pylint: disable=protected-access
        self.assertEqual(sorted(ctx.kinds), ["bucket", "client", "main"])
        self.assertEqual(usage.daily[("alice@example.com", bucket)], (8, 1410))

    def test_a_failed_side_query_fails_the_whole_reading(self):
        for failing in ("main", "client"):
            ctx = PersonQueryCtx(self.ROWS, fail=failing)
            with self.assertRaises(GcpError, msg=failing):
                gov._bq_usage(ctx, sink_source(), START, END, email="alice@example.com")  # pylint: disable=protected-access


class FirstSeenCacheTest(unittest.TestCase):
    def test_a_complete_answer_is_kept_and_a_missing_project_is_read_again(self):
        ctx = support.FakeCtx()
        src = sink_source()
        first_a = START + dt.timedelta(days=2)
        first_b = START + dt.timedelta(days=3)
        answers = [{"proj-a": first_a}, {"proj-a": first_a, "proj-b": first_b}]
        calls = []

        def fake(c, s, start, end):  # pylint: disable=unused-argument
            calls.append(end)
            return dict(answers[min(len(calls), len(answers)) - 1])
        with mock.patch.object(gov, "_bq_first_seen", side_effect=fake):
            one = gov._first_seen(ctx, src, START, ["proj-a", "proj-b"], END)  # pylint: disable=protected-access
            two = gov._first_seen(ctx, src, START, ["proj-a", "proj-b"], END + dt.timedelta(hours=1))  # pylint: disable=protected-access
            three = gov._first_seen(ctx, src, START, ["proj-a", "proj-b"], END + dt.timedelta(hours=2))  # pylint: disable=protected-access
        self.assertNotIn("proj-b", one)
        self.assertEqual(len(calls), 2)  # proj-b had no entry the first time: read again, then kept
        self.assertEqual(two, three)
        self.assertEqual(three["proj-b"], first_b)


class LoggingGapReadTest(unittest.TestCase):
    def test_readings_are_kept_and_a_fresh_read_replaces_them(self):
        ctx = support.FakeCtx()
        src = sink_source()
        reads = []

        def fake_usage(c, start, end, email=None, rng=None, ranges=None):  # pylint: disable=unused-argument
            usage = gov.Usage("logging")
            usage.ok = True
            reads.append(usage)
            return usage
        with mock.patch.object(gov, "logging_gaps", return_value={"proj-b": (START, END)}), \
                mock.patch.object(gov, "_logging_usage", side_effect=fake_usage):
            a, fa = gov._read_logging_gaps(ctx, src, START, END)  # pylint: disable=protected-access
            b, _ = gov._read_logging_gaps(ctx, src, START, END)  # pylint: disable=protected-access
            c, _ = gov._read_logging_gaps(ctx, src, START, END, fresh=True)  # pylint: disable=protected-access
            d, _ = gov._read_logging_gaps(ctx, src, START, END)  # pylint: disable=protected-access
        self.assertIsNone(fa)
        self.assertEqual(len(reads), 2)
        self.assertIs(a, b)
        self.assertIsNot(c, a)
        self.assertIs(d, c)

    def test_failures_are_returned_not_raised(self):
        ctx = support.FakeCtx()
        src = sink_source()
        with mock.patch.object(gov, "logging_gaps", side_effect=GcpError("denied", message="no")):
            extra, failure = gov._read_logging_gaps(ctx, src, START, END)  # pylint: disable=protected-access
        self.assertIsNone(extra)
        self.assertEqual(failure[0], "first")
        with mock.patch.object(gov, "logging_gaps", return_value={"proj-b": (START, END)}), \
                mock.patch.object(gov, "_logging_usage", side_effect=GcpError("timeout", message="slow")):
            extra, failure = gov._read_logging_gaps(ctx, src, START, END, fresh=True)  # pylint: disable=protected-access
        self.assertIsNone(extra)
        self.assertEqual(failure[0], "logging")


if __name__ == "__main__":
    unittest.main()
