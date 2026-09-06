"""Market data tests: Yahoo chart JSON parsing, cache round-trip, offline
degrade behavior.

No test in this file makes a real network call -- ``fetch`` is always injected.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import timedelta  # noqa: E402

from wheel.marketdata import (  # noqa: E402
    MarketDataError,
    PricePoint,
    _default_cache_path,
    _leverage_kind,
    _sessions_elapsed,
    get_fundamentals,
    get_price_series,
    load_cache,
    parse_yahoo_chart,
    parse_yahoo_quotes,
    price_on_or_before,
    save_cache,
    yahoo_chart_url,
)


def _yahoo_payload(rows: list[tuple[str, float | None]]) -> str:
    """Build a minimal Yahoo chart JSON body from (date, close) rows."""
    timestamps = [int(datetime(*map(int, d.split("-")), tzinfo=timezone.utc).timestamp()) for d, _ in rows]
    closes = [c for _, c in rows]
    return json.dumps(
        {
            "chart": {
                "result": [
                    {
                        "timestamp": timestamps,
                        "indicators": {"quote": [{"close": closes}]},
                    }
                ],
                "error": None,
            }
        }
    )


YAHOO_SAMPLE = _yahoo_payload(
    [
        ("2026-01-02", 471.50),
        ("2026-01-05", 474.25),
        ("2026-01-06", 475.00),
    ]
)


class TestParseYahooChart(unittest.TestCase):
    def test_parses_ascending(self):
        points = parse_yahoo_chart(YAHOO_SAMPLE)
        self.assertEqual([p.day for p in points], [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)])
        self.assertEqual(points[0].close, 471.50)

    def test_out_of_order_input_is_sorted(self):
        shuffled = _yahoo_payload([("2026-01-06", 475.00), ("2026-01-02", 471.50)])
        points = parse_yahoo_chart(shuffled)
        self.assertEqual([p.day for p in points], [date(2026, 1, 2), date(2026, 1, 6)])

    def test_null_closes_are_skipped_not_fatal(self):
        text = _yahoo_payload(
            [("2026-01-02", 471.50), ("2026-01-03", None), ("2026-01-06", 475.00)]
        )
        points = parse_yahoo_chart(text)
        self.assertEqual(len(points), 2)

    def test_malformed_json_raises_market_data_error(self):
        with self.assertRaises(MarketDataError):
            parse_yahoo_chart("not json")

    def test_error_payload_raises_market_data_error(self):
        text = json.dumps({"chart": {"result": None, "error": {"code": "Not Found"}}})
        with self.assertRaises(MarketDataError):
            parse_yahoo_chart(text)


class TestPriceOnOrBefore(unittest.TestCase):
    def setUp(self):
        self.points = parse_yahoo_chart(YAHOO_SAMPLE)

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
        points = parse_yahoo_chart(YAHOO_SAMPLE)
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
            points, warnings = get_price_series(fetch=lambda: YAHOO_SAMPLE, cache_path=cache_path)
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
                return YAHOO_SAMPLE

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
                return YAHOO_SAMPLE

            points, warnings = get_price_series(fetch=fetch, cache_path=cache_path, force_refresh=True)
            self.assertEqual(len(calls), 1)
            self.assertEqual(warnings, [])
            self.assertEqual(len(points), 3)

    def test_cache_within_one_session_is_not_stale(self):
        """The cache's last point is one weekday behind today: no refetch --
        the same tolerance the old calendar-day rule gave, now session-aware
        so a Friday->Sunday gap no longer counts as stale.
        """
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = self._cache_path(tmp)
            today = date.today()
            one_session_back = today - timedelta(days=1)
            while one_session_back.weekday() >= 5:  # land on a weekday
                one_session_back -= timedelta(days=1)
            save_cache([PricePoint(day=one_session_back, close=100.0)], cache_path)

            calls = []

            def fetch():
                calls.append(1)
                return YAHOO_SAMPLE

            points, warnings = get_price_series(fetch=fetch, cache_path=cache_path, max_age_days=1)
            self.assertEqual(calls, [])
            self.assertEqual(warnings, [])
            self.assertEqual(points[-1].day, one_session_back)

    def test_local_only_returns_none_when_a_fetch_would_be_needed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = self._cache_path(tmp)
            save_cache([PricePoint(day=date(2020, 1, 1), close=100.0)], cache_path)

            def fetch():
                raise AssertionError("local_only must never fetch")

            self.assertIsNone(
                get_price_series(fetch=fetch, cache_path=cache_path, local_only=True)
            )

    def test_local_only_returns_the_series_when_cache_is_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = self._cache_path(tmp)
            save_cache([PricePoint(day=date.today(), close=100.0)], cache_path)

            result = get_price_series(
                fetch=lambda: YAHOO_SAMPLE, cache_path=cache_path, local_only=True
            )
            self.assertIsNotNone(result)
            points, warnings = result
            self.assertEqual(warnings, [])
            self.assertEqual(points[-1].close, 100.0)


class TestSessionsElapsed(unittest.TestCase):
    def test_same_day_is_zero(self):
        d = date(2026, 8, 26)
        self.assertEqual(_sessions_elapsed(d, d), 0)

    def test_future_cache_clamps_to_zero(self):
        self.assertEqual(_sessions_elapsed(date(2026, 8, 27), date(2026, 8, 26)), 0)

    def test_consecutive_weekdays_count_one(self):
        self.assertEqual(_sessions_elapsed(date(2026, 8, 26), date(2026, 8, 27)), 1)

    def test_friday_to_sunday_is_zero_sessions(self):
        self.assertEqual(_sessions_elapsed(date(2026, 8, 28), date(2026, 8, 30)), 0)

    def test_friday_to_tuesday_counts_only_monday_and_tuesday(self):
        self.assertEqual(_sessions_elapsed(date(2026, 8, 28), date(2026, 9, 1)), 2)


class TestPerTicker(unittest.TestCase):
    def test_yahoo_chart_url_is_ticker_specific(self):
        self.assertEqual(
            yahoo_chart_url("SPY", period2=1700000000),
            "https://query1.finance.yahoo.com/v8/finance/chart/SPY?period1=0&period2=1700000000&interval=1d",
        )
        self.assertEqual(
            yahoo_chart_url("mu", period2=1700000000),
            "https://query1.finance.yahoo.com/v8/finance/chart/MU?period1=0&period2=1700000000&interval=1d",
        )

    def test_spy_keeps_its_original_cache_path(self):
        """SPY's cache stays at the pre-existing path, not under data/prices/,
        so a cache already on disk keeps working unchanged.
        """
        path = _default_cache_path("SPY")
        self.assertTrue(path.replace("\\", "/").endswith("data/spy_daily_closes.csv"))

    def test_other_tickers_get_their_own_file_under_data_prices(self):
        path = _default_cache_path("MU")
        normalized = path.replace("\\", "/")
        self.assertTrue(normalized.endswith("data/prices/MU.csv"))

    def test_default_ticker_is_spy(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, "cache.csv")
            points, warnings = get_price_series(fetch=lambda: YAHOO_SAMPLE, cache_path=cache_path)
            self.assertEqual(warnings, [])
            self.assertEqual(len(points), 3)

    def test_a_second_tickers_fetch_uses_its_own_cache_and_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            spy_cache = os.path.join(tmp, "SPY.csv")
            mu_cache = os.path.join(tmp, "MU.csv")
            save_cache(parse_yahoo_chart(YAHOO_SAMPLE), spy_cache)

            mu_sample = _yahoo_payload([("2026-01-06", 210.50)])
            points, warnings = get_price_series("MU", fetch=lambda: mu_sample, cache_path=mu_cache)
            self.assertEqual(warnings, [])
            self.assertEqual(points[-1].close, 210.50)
            # The SPY cache is untouched by fetching a different ticker.
            self.assertEqual(len(load_cache(spy_cache)), 3)

    def test_one_tickers_failed_fetch_does_not_affect_another_tickers_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            spy_cache = os.path.join(tmp, "SPY.csv")
            mu_cache = os.path.join(tmp, "MU.csv")
            save_cache(parse_yahoo_chart(YAHOO_SAMPLE), spy_cache)

            def failing_fetch():
                raise MarketDataError("MU offline")

            mu_points, mu_warnings = get_price_series("MU", fetch=failing_fetch, cache_path=mu_cache)
            self.assertEqual(mu_points, [])
            self.assertEqual(len(mu_warnings), 1)

            spy_points, spy_warnings = get_price_series(
                "SPY", fetch=lambda: YAHOO_SAMPLE, cache_path=spy_cache, max_age_days=999999
            )
            self.assertEqual(spy_warnings, [])
            self.assertEqual(len(spy_points), 3)


def _quote_payload(rows: list[dict]) -> str:
    return json.dumps({"quoteResponse": {"result": rows, "error": None}})


class TestParseYahooQuotes(unittest.TestCase):
    def test_keys_by_symbol_and_skips_symbolless_rows(self):
        text = _quote_payload([{"symbol": "mu", "marketCap": 1}, {"marketCap": 2}])
        self.assertEqual(list(parse_yahoo_quotes(text)), ["MU"])

    def test_error_payload_raises(self):
        with self.assertRaises(MarketDataError):
            parse_yahoo_quotes('{"finance": {"error": "nope"}}')


class TestLeverageKind(unittest.TestCase):
    def test_equity_is_common(self):
        self.assertEqual(_leverage_kind("Micron Technology, Inc.", "EQUITY"), "common")

    def test_equity_named_fund_is_a_closed_end_fund(self):
        self.assertEqual(_leverage_kind("PIMCO Dynamic Income Fund", "EQUITY"), "closed_end_fund")

    def test_plain_etf(self):
        self.assertEqual(_leverage_kind("Invesco QQQ Trust", "ETF"), "etf")

    def test_leveraged_etf_by_name(self):
        self.assertEqual(_leverage_kind("ProShares UltraPro QQQ", "ETF"), "leveraged_etf")
        self.assertEqual(_leverage_kind("Direxion Daily Semiconductor Bull 3X Shares", "ETF"), "leveraged_etf")

    def test_inverse_etf_by_name(self):
        self.assertEqual(_leverage_kind("ProShares Short QQQ", "ETF"), "inverse_etf")

    def test_mutualfund(self):
        self.assertEqual(_leverage_kind("Fidelity Blue Chip Growth", "MUTUALFUND"), "mutual_fund")

    def test_unknown_quote_type_is_none(self):
        self.assertIsNone(_leverage_kind("Some Index", "INDEX"))


class TestGetFundamentals(unittest.TestCase):
    TODAY = date(2026, 9, 5)

    def _fetch(self, rows):
        payload = _quote_payload(rows)
        return lambda symbols: payload

    def test_cold_fetch_populates_cache_and_maps_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "f.json")
            rows = [{
                "symbol": "MU", "marketCap": 1_148_129_800_000, "averageDailyVolume10Day": 25_069_100,
                "quoteType": "EQUITY", "longName": "Micron Technology, Inc.",
                "earningsTimestampStart": int(datetime(2026, 9, 30, tzinfo=timezone.utc).timestamp()),
            }]
            out, warns = get_fundamentals(["MU"], fetch=self._fetch(rows), cache_path=cache, today=self.TODAY)
            self.assertEqual(warns, [])
            f = out["MU"]
            self.assertEqual(f.kind, "common")
            self.assertAlmostEqual(f.market_cap_b, 1148.1298, places=3)
            self.assertAlmostEqual(f.avg_vol_10d_m, 25.0691, places=3)
            self.assertEqual(f.earnings_date, date(2026, 9, 30))
            # written through
            out2, _ = get_fundamentals(["MU"], fetch=self._boom, cache_path=cache, today=self.TODAY)
            self.assertEqual(out2["MU"].kind, "common")

    def _boom(self, symbols):
        raise MarketDataError("must not fetch")

    def test_fresh_cache_is_not_refetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "f.json")
            earn = int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp())
            rows = [{
                "symbol": "AAA", "marketCap": 5e9, "quoteType": "EQUITY", "longName": "A",
                "earningsTimestampStart": earn,
            }]
            get_fundamentals(["AAA"], fetch=self._fetch(rows), cache_path=cache, today=self.TODAY)
            # a few days later, still inside max_age and earnings still ahead -> no fetch
            out, warns = get_fundamentals(
                ["AAA"], fetch=self._boom, cache_path=cache, today=self.TODAY + timedelta(days=5)
            )
            self.assertEqual(warns, [])
            self.assertEqual(out["AAA"].market_cap_b, 5.0)

    def test_stale_entry_is_refetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "f.json")
            get_fundamentals(
                ["AAA"], fetch=self._fetch([{"symbol": "AAA", "marketCap": 5e9, "quoteType": "EQUITY"}]),
                cache_path=cache, today=self.TODAY,
            )
            out, _ = get_fundamentals(
                ["AAA"], fetch=self._fetch([{"symbol": "AAA", "marketCap": 9e9, "quoteType": "EQUITY"}]),
                cache_path=cache, today=self.TODAY + timedelta(days=40),
            )
            self.assertEqual(out["AAA"].market_cap_b, 9.0)

    def test_past_earnings_date_forces_a_refetch_next_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "f.json")
            old = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
            get_fundamentals(
                ["AAA"], fetch=self._fetch([{"symbol": "AAA", "quoteType": "EQUITY", "earningsTimestampStart": old}]),
                cache_path=cache, today=self.TODAY,
            )
            hit = {"n": 0}

            def counting(symbols):
                hit["n"] += 1
                new = int(datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp())
                return _quote_payload([{"symbol": "AAA", "quoteType": "EQUITY", "earningsTimestampStart": new}])

            out, _ = get_fundamentals(["AAA"], fetch=counting, cache_path=cache, today=self.TODAY + timedelta(days=1))
            self.assertEqual(hit["n"], 1)
            self.assertEqual(out["AAA"].earnings_date, date(2026, 12, 1))

    def test_fetch_failure_keeps_cache_and_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "f.json")
            get_fundamentals(
                ["AAA"], fetch=self._fetch([{"symbol": "AAA", "marketCap": 5e9, "quoteType": "EQUITY"}]),
                cache_path=cache, today=self.TODAY,
            )

            def failing(symbols):
                raise MarketDataError("offline")

            out, warns = get_fundamentals(
                ["AAA"], fetch=failing, cache_path=cache, today=self.TODAY + timedelta(days=99)
            )
            self.assertEqual(out["AAA"].market_cap_b, 5.0)  # stale but usable
            self.assertEqual(len(warns), 1)

    def test_local_only_never_fetches(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "f.json")
            out, warns = get_fundamentals(["AAA"], fetch=self._boom, cache_path=cache, local_only=True)
            self.assertEqual((out, warns), ({}, []))


if __name__ == "__main__":
    unittest.main()
