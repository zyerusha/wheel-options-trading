"""Market data tests: Stooq parsing, cache round-trip, offline degrade behavior.

No test in this file makes a real network call -- ``fetch`` is always injected.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.marketdata import (  # noqa: E402
    MarketDataError,
    PricePoint,
    get_price_series,
    load_cache,
    parse_stooq_csv,
    price_on_or_before,
    save_cache,
)

STOOQ_SAMPLE = (
    "Date,Open,High,Low,Close,Volume\n"
    "2026-01-02,470.00,472.00,469.00,471.50,1000000\n"
    "2026-01-05,471.50,475.00,470.00,474.25,900000\n"
    "2026-01-06,474.25,476.00,473.00,475.00,850000\n"
)


class TestParseStooqCsv(unittest.TestCase):
    def test_parses_ascending(self):
        points = parse_stooq_csv(STOOQ_SAMPLE)
        self.assertEqual([p.day for p in points], [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)])
        self.assertEqual(points[0].close, 471.50)

    def test_out_of_order_input_is_sorted(self):
        shuffled = "Date,Open,High,Low,Close,Volume\n2026-01-06,1,1,1,475.00,1\n2026-01-02,1,1,1,471.50,1\n"
        points = parse_stooq_csv(shuffled)
        self.assertEqual([p.day for p in points], [date(2026, 1, 2), date(2026, 1, 6)])

    def test_malformed_rows_are_skipped_not_fatal(self):
        text = STOOQ_SAMPLE + "not,a,real,row\n2026-01-07,x,x,x,not-a-number,1\n"
        points = parse_stooq_csv(text)
        self.assertEqual(len(points), 3)


class TestPriceOnOrBefore(unittest.TestCase):
    def setUp(self):
        self.points = parse_stooq_csv(STOOQ_SAMPLE)

    def test_exact_match(self):
        self.assertEqual(price_on_or_before(self.points, date(2026, 1, 5)).close, 474.25)

    def test_gap_between_trading_days_resolves_to_prior_close(self):
        # No point exists for 2026-01-08 -- the nearest prior close should win.
        result = price_on_or_before(self.points, date(2026, 1, 8))
        self.assertEqual(result.close, 475.00)

    def test_before_series_start_is_none(self):
        self.assertIsNone(price_on_or_before(self.points, date(2025, 12, 1)))

    def test_empty_series_is_none(self):
        self.assertIsNone(price_on_or_before([], date(2026, 1, 5)))


class TestCacheRoundTrip(unittest.TestCase):
    def test_save_then_load(self):
        points = parse_stooq_csv(STOOQ_SAMPLE)
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, "cache.csv")
            save_cache(points, cache_path)
            loaded = load_cache(cache_path)
        self.assertEqual([(p.day, p.close) for p in loaded], [(p.day, p.close) for p in points])

    def test_missing_cache_file_is_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_cache(os.path.join(tmp, "nope.csv")), [])


class TestGetPriceSeries(unittest.TestCase):
    def _cache_path(self, tmp):
        return os.path.join(tmp, "cache.csv")

    def test_cold_start_successful_fetch_populates_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = self._cache_path(tmp)
            points, warnings = get_price_series(fetch=lambda: STOOQ_SAMPLE, cache_path=cache_path)
            self.assertEqual(warnings, [])
            self.assertEqual(len(points), 3)
            self.assertTrue(os.path.isfile(cache_path))

    def test_fresh_cache_does_not_refetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = self._cache_path(tmp)
            save_cache([PricePoint(day=date.today(), close=100.0)], cache_path)

            calls = []

            def fetch():
                calls.append(1)
                return STOOQ_SAMPLE

            points, warnings = get_price_series(fetch=fetch, cache_path=cache_path, max_age_days=1)
            self.assertEqual(calls, [])
            self.assertEqual(warnings, [])
            self.assertEqual(len(points), 1)

    def test_stale_cache_with_failing_fetch_falls_back_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = self._cache_path(tmp)
            save_cache([PricePoint(day=date(2020, 1, 1), close=100.0)], cache_path)

            def failing_fetch():
                raise MarketDataError("boom")

            points, warnings = get_price_series(fetch=failing_fetch, cache_path=cache_path)
            self.assertEqual(len(points), 1)
            self.assertEqual(len(warnings), 1)
            self.assertIn("boom", warnings[0])

    def test_no_cache_and_failing_fetch_is_empty_with_warning_not_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = self._cache_path(tmp)

            def failing_fetch():
                raise MarketDataError("offline")

            points, warnings = get_price_series(fetch=failing_fetch, cache_path=cache_path)
            self.assertEqual(points, [])
            self.assertEqual(len(warnings), 1)
            self.assertIn("offline", warnings[0])

    def test_force_refresh_bypasses_freshness_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = self._cache_path(tmp)
            save_cache([PricePoint(day=date.today(), close=100.0)], cache_path)

            calls = []

            def fetch():
                calls.append(1)
                return STOOQ_SAMPLE

            points, warnings = get_price_series(fetch=fetch, cache_path=cache_path, force_refresh=True)
            self.assertEqual(len(calls), 1)
            self.assertEqual(warnings, [])
            self.assertEqual(len(points), 3)


if __name__ == "__main__":
    unittest.main()
