"""Date ranges, timezone handling and timeline bucketing plus governance windows."""

import datetime as dt
import unittest

import support  # noqa: F401  (puts src/ on sys.path)

from controlplane import timeutil
from controlplane.httpkit import ApiError

UTC = dt.timezone.utc
# 2026-10-18 16:30 UTC is already 2026-10-19 01:30 in Asia/Seoul (UTC+9).
NOW = dt.datetime(2026, 10, 18, 16, 30, tzinfo=UTC)


class ParseRangeTest(unittest.TestCase):
    def test_default_range_uses_workspace_timezone(self):
        rng = timeutil.parse_range({}, "Asia/Seoul", 14, now=NOW)
        self.assertEqual((rng.start, rng.end), (dt.date(2026, 10, 6), dt.date(2026, 10, 19)))
        self.assertEqual(rng.days, 14)
        utc_rng = timeutil.parse_range({}, "UTC", 14, now=NOW)
        self.assertEqual(utc_rng.end, dt.date(2026, 10, 18))

    def test_timestamps_are_local_midnights(self):
        rng = timeutil.parse_range({"start": "2026-10-06", "end": "2026-10-19"}, "Asia/Seoul", 14)
        self.assertEqual(rng.start_ts(), dt.datetime(2026, 10, 5, 15, 0, tzinfo=UTC))
        self.assertEqual(rng.end_ts(), dt.datetime(2026, 10, 19, 15, 0, tzinfo=UTC))  # exclusive (end + 1 day)
        self.assertEqual(rng.meta(), {"start": "2026-10-06", "end": "2026-10-19", "granularity": "day",
                                      "timezone": "Asia/Seoul"})

    def test_validation(self):
        bad = [{"start": "2026/10/01"}, {"start": "2026-02-30"}, {"start": "2026-10-10", "end": "2026-10-01"},
               {"start": "2025-01-01", "end": "2026-10-01"}, {"granularity": "month"}]
        for params in bad:
            with self.assertRaises(ApiError, msg=str(params)) as ctx:
                timeutil.parse_range(params, "UTC", 14, now=NOW)
            self.assertEqual(ctx.exception.code, "bad_request")

    def test_previous_period_and_suffixes(self):
        rng = timeutil.parse_range({"start": "2026-10-06", "end": "2026-10-19"}, "UTC", 14)
        prev = rng.previous()
        self.assertEqual((prev.start, prev.end), (dt.date(2026, 9, 22), dt.date(2026, 10, 5)))
        self.assertEqual(rng.suffixes(), ("20261005", "20261020"))

    def test_invalid_timezone_falls_back_to_utc(self):
        self.assertFalse(timeutil.valid_timezone("Mars/Base"))
        self.assertFalse(timeutil.valid_timezone("../etc/passwd"))
        self.assertTrue(timeutil.valid_timezone("America/Argentina/Buenos_Aires"))
        self.assertIs(timeutil.get_zone("Mars/Base"), timeutil.UTC)


class BucketTest(unittest.TestCase):
    def test_day_buckets(self):
        rng = timeutil.parse_range({"start": "2026-10-06", "end": "2026-10-08"}, "UTC", 14)
        self.assertEqual(rng.bucket_labels(), ["2026-10-06", "2026-10-07", "2026-10-08"])
        values = timeutil.fold(rng, [(dt.date(2026, 10, 6), 2), ("2026-10-08", 5), (dt.date(2026, 10, 9), 7),
                                     (None, 1), (dt.date(2026, 10, 6), None)], int)
        self.assertEqual(values, [2, 0, 5])

    def test_week_buckets_start_at_range_start(self):
        rng = timeutil.parse_range({"start": "2026-10-01", "end": "2026-10-19", "granularity": "week"}, "UTC", 14)
        self.assertEqual(rng.bucket_labels(), ["2026-10-01", "2026-10-08", "2026-10-15"])
        self.assertEqual(rng.bucket_index(dt.date(2026, 10, 7)), 0)
        self.assertEqual(rng.bucket_index(dt.date(2026, 10, 8)), 1)
        self.assertEqual(rng.bucket_index(dt.date(2026, 10, 19)), 2)
        self.assertIsNone(rng.bucket_index(dt.date(2026, 9, 30)))
        self.assertEqual(timeutil.fold(rng, [("2026-10-02", 1.5), ("2026-10-16", 2.25), ("2026-10-19", 1.0)]),
                         [1.5, 0, 3.25])

    def test_datetime_bucketing_uses_workspace_timezone(self):
        rng = timeutil.parse_range({"start": "2026-10-18", "end": "2026-10-19"}, "Asia/Seoul", 14)
        late_utc = dt.datetime(2026, 10, 18, 20, 0, tzinfo=UTC)  # 2026-10-19 05:00 in Seoul
        self.assertEqual(rng.bucket_index(late_utc), 1)

    def test_timeline_shape(self):
        rng = timeutil.parse_range({"start": "2026-10-06", "end": "2026-10-07"}, "UTC", 14)
        tl = timeutil.timeline(rng, [timeutil.series("prompts", "Prompts", [12, 30])])
        self.assertEqual(tl, {"granularity": "day", "buckets": ["2026-10-06", "2026-10-07"],
                              "series": [{"key": "prompts", "name": "Prompts", "values": [12, 30]}]})


class GovernanceWindowTest(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 15, 13, 45, 30, tzinfo=UTC)  # a Thursday

    def test_calendar_windows_use_utc_boundaries(self):
        self.assertEqual(timeutil.governance_window("daily", "calendar", self.NOW)[0],
                         dt.datetime(2026, 10, 15, tzinfo=UTC))
        self.assertEqual(timeutil.governance_window("weekly", "calendar", self.NOW)[0],
                         dt.datetime(2026, 10, 12, tzinfo=UTC))  # Monday
        self.assertEqual(timeutil.governance_window("monthly", "calendar", self.NOW)[0],
                         dt.datetime(2026, 10, 1, tzinfo=UTC))
        start, end = timeutil.governance_window("all", "calendar", self.NOW)
        self.assertEqual(end - start, dt.timedelta(days=90))

    def test_rolling_windows(self):
        for period, days in (("daily", 1), ("weekly", 7), ("monthly", 30), ("all", 90)):
            start, end = timeutil.governance_window(period, "rolling", self.NOW)
            self.assertEqual(end - start, dt.timedelta(days=days), period)

    def test_invalid(self):
        with self.assertRaises(ApiError):
            timeutil.governance_window("yearly", "calendar", self.NOW)
        with self.assertRaises(ApiError):
            timeutil.governance_window("daily", "sliding", self.NOW)


if __name__ == "__main__":
    unittest.main()
