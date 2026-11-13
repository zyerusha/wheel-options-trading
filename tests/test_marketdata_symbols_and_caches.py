"""Regression tests for two market-data bugs:

* Class shares (Berkshire B and the like): the ledger writes BRKB and option
  symbols use BRK.B, but Yahoo only knows BRK-B, so every Yahoo call must ask
  for BRK-B while results, cache entries and cache files keep the app's own
  ticker.
* The JSON disk caches (fundamentals, option expirations): an update must not
  drop entries another request saved meanwhile, a failed write must not
  damage the file, and a corrupt file must read as empty with a warning.

No test here makes a real network call. The Yahoo bodies are minimal copies
of real responses' shapes (BRKB and BRK.B answered as unknown, BRK-B with
data; checked live on 2026-11-07).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel import marketdata  # noqa: E402
from wheel.marketdata import (  # noqa: E402
    _default_cache_path,
    fetch_yahoo_option_chain,
    fetch_yahoo_option_expirations,
    fetch_yahoo_quotes,
    get_fundamentals,
    get_last_prices,
    get_option_expirations,
    get_price_series,
    parse_yahoo_option_chain,
    yahoo_chart_url,
    yahoo_symbol,
)

TODAY = date(2026, 11, 7)


def _chart_body(closes: list[tuple[str, float]]) -> str:
    stamps = [int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()) for day, _ in closes]
    quote = {
        "close": [close for _, close in closes],
        "high": [close + 1 for _, close in closes],
        "low": [close - 1 for _, close in closes],
        "volume": [1000 for _ in closes],
    }
    result = {"meta": {"symbol": "BRK-B"}, "timestamp": stamps, "indicators": {"quote": [quote]}}
    return json.dumps({"chart": {"result": [result], "error": None}})


def _quote_body(rows: list[dict]) -> str:
    return json.dumps({"quoteResponse": {"result": rows, "error": None}})


# What Yahoo's quote endpoint returns for a symbol it doesn't know (BRKB).
UNKNOWN_BRKB_ROW = {"symbol": "BRKB", "quoteType": "NONE"}
BRK_B_ROW = {
    "symbol": "BRK-B",
    "quoteType": "EQUITY",
    "regularMarketPrice": 570.89,
    "marketCap": 1_230_000_000_000,
    "averageDailyVolume10Day": 4_100_000,
    "longName": "Berkshire Hathaway Inc.",
}


def _options_body(underlying: str, expirations: list[date]) -> str:
    epochs = [int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()) for d in expirations]
    result = {"underlyingSymbol": underlying, "expirationDates": epochs, "quote": {}, "options": []}
    return json.dumps({"optionChain": {"result": [result], "error": None}})


class YahooSymbolTest(unittest.TestCase):
    def test_class_share_spellings_map_to_yahoo_dash_form(self):
        for spelling in ("BRKB", "BRK.B", "brk/b", "BRK-B", " brk.b "):
            self.assertEqual(yahoo_symbol(spelling), "BRK-B", spelling)
        self.assertEqual(yahoo_symbol("BF.B"), "BF-B")
        self.assertEqual(yahoo_symbol("BFB"), "BF-B")
        self.assertEqual(yahoo_symbol("BRKA"), "BRK-A")

    def test_ordinary_and_ambiguous_tickers_are_left_alone(self):
        # GOOGL and BFH are companies of their own, not GOOG-L or BF-H.
        for ticker in ("GOOGL", "BFH", "SPY", "MU", "^VIX", "TQQQ"):
            self.assertEqual(yahoo_symbol(ticker), ticker)
        self.assertEqual(yahoo_symbol("aapl"), "AAPL")


class ClassShareFetchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_chart_url_asks_yahoo_for_dash_form(self):
        self.assertIn("/chart/BRK-B?", yahoo_chart_url("BRKB", period2=1))
        self.assertIn("/chart/BRK-B?", yahoo_chart_url("BRK.B", period2=1))

    def test_brkb_price_history_loads_and_keeps_its_own_cache_file(self):
        requested: list[str] = []

        def fake_chart(url: str, timeout: float = 10.0) -> str:
            requested.append(url)
            if "/chart/BRK-B?" not in url:
                raise marketdata.MarketDataError("HTTP Error 404: Not Found")
            return _chart_body([("2026-11-05", 568.0), ("2026-11-06", 570.89)])

        cache_path = os.path.join(self.tmp.name, "BRKB.csv")
        with mock.patch.object(marketdata, "fetch_yahoo_chart", fake_chart), mock.patch.object(
            marketdata, "_default_cache_path", lambda ticker: cache_path
        ):
            points, warnings = get_price_series("BRKB")
        self.assertEqual(warnings, [])
        self.assertEqual([p.close for p in points], [568.0, 570.89])
        self.assertTrue(os.path.isfile(cache_path))
        self.assertEqual(len(requested), 1)
        self.assertTrue(_default_cache_path("BRKB").endswith(os.path.join("prices", "BRKB.csv")))

    def test_quote_fetch_sends_dash_form_and_results_keep_app_ticker(self):
        sent: list[dict] = []

        def fake_get(url, params=None, *, timeout, error_context):
            sent.append(params)
            return _quote_body([BRK_B_ROW])

        with mock.patch.object(marketdata, "_yahoo_get", fake_get):
            prices, warnings = get_last_prices(["BRKB"])
        self.assertEqual(sent[0]["symbols"], "BRK-B")
        self.assertEqual(prices, {"BRKB": 570.89})
        self.assertEqual(warnings, [])

    def test_option_urls_use_dash_form(self):
        urls: list[str] = []

        def fake_get(url, params=None, *, timeout, error_context):
            urls.append(url)
            return "{}"

        with mock.patch.object(marketdata, "_yahoo_get", fake_get):
            fetch_yahoo_option_expirations("BRKB")
            fetch_yahoo_option_chain("BRK.B", date(2026, 12, 18))
            fetch_yahoo_quotes(["BRK.B", "MU"])
        self.assertTrue(urls[0].endswith("/options/BRK-B"), urls[0])
        self.assertTrue(urls[1].endswith("/options/BRK-B"), urls[1])

    def test_option_chain_snapshot_can_keep_app_ticker(self):
        expiry = date(2026, 12, 18)
        epoch = int(datetime(2026, 12, 18, tzinfo=timezone.utc).timestamp())
        body = json.dumps(
            {
                "optionChain": {
                    "result": [
                        {
                            "underlyingSymbol": "BRK-B",
                            "quote": {"regularMarketPrice": 570.89},
                            "options": [{"expirationDate": epoch, "calls": [], "puts": []}],
                        }
                    ]
                }
            }
        )
        self.assertEqual(parse_yahoo_option_chain(body, expiry, ticker="BRKB").ticker, "BRKB")
        self.assertEqual(parse_yahoo_option_chain(body, expiry).ticker, "BRK-B")

    def test_expirations_cached_under_app_ticker(self):
        path = os.path.join(self.tmp.name, "option_expirations_cache.json")
        asked: list[str] = []

        def fetch(ticker: str) -> str:
            asked.append(ticker)
            return _options_body("BRK-B", [date(2026, 11, 20), date(2026, 12, 18)])

        result, warnings = get_option_expirations(["BRKB"], fetch=fetch, cache_path=path, today=TODAY)
        self.assertEqual(result, {"BRKB": [date(2026, 11, 20), date(2026, 12, 18)]})
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(list(json.load(handle)), ["BRKB"])

    def test_fundamentals_found_under_dash_form_and_unknown_answer_never_cached(self):
        path = os.path.join(self.tmp.name, "fundamentals_cache.json")
        # The blank BRKB entry the old code cached from Yahoo's "unknown symbol" answer.
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {"BRKB": {"as_of": "2026-11-06", "quote_type": "NONE", "market_cap_b": None, "kind": None}}, handle
            )
        result, warnings = get_fundamentals(
            ["BRKB"], fetch=lambda symbols: _quote_body([BRK_B_ROW]), cache_path=path, today=TODAY
        )
        self.assertEqual(warnings, [])
        self.assertEqual(result["BRKB"].ticker, "BRKB")
        self.assertEqual(result["BRKB"].market_cap_b, 1230.0)
        self.assertEqual(result["BRKB"].quote_type, "EQUITY")

        other = os.path.join(self.tmp.name, "other.json")
        result, _ = get_fundamentals(
            ["XYZB"], fetch=lambda symbols: _quote_body([{"symbol": "XYZB", "quoteType": "NONE"}]),
            cache_path=other, today=TODAY,
        )
        self.assertEqual(result, {})
        self.assertFalse(os.path.exists(other), "an unknown-symbol answer is not fundamentals")


class JsonCacheSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        marketdata._option_expirations_memo.clear()
        self.addCleanup(marketdata._option_expirations_memo.clear)

    def _read(self, path: str) -> dict:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_fundamentals_update_keeps_entries_saved_meanwhile(self):
        # Request A loads the cache, then while A's fetch is in flight request
        # B fetches and saves another ticker. A's save must keep B's entry.
        path = os.path.join(self.tmp.name, "fundamentals_cache.json")

        def row(symbol: str) -> dict:
            return {"symbol": symbol, "quoteType": "EQUITY", "marketCap": 2e9}

        def fetch_a(symbols):
            get_fundamentals(["BBB"], fetch=lambda s: _quote_body([row("BBB")]), cache_path=path, today=TODAY)
            return _quote_body([row("AAA")])

        get_fundamentals(["AAA"], fetch=fetch_a, cache_path=path, today=TODAY)
        self.assertEqual(sorted(self._read(path)), ["AAA", "BBB"])

    def test_option_expirations_update_keeps_entries_saved_meanwhile(self):
        path = os.path.join(self.tmp.name, "option_expirations_cache.json")
        dates = [date(2026, 11, 20), date(2026, 12, 18)]

        def fetch_a(ticker):
            get_option_expirations(
                ["BBB"], fetch=lambda t: _options_body("BBB", dates), cache_path=path, today=TODAY
            )
            return _options_body("AAA", dates)

        get_option_expirations(["AAA"], fetch=fetch_a, cache_path=path, today=TODAY)
        self.assertEqual(sorted(self._read(path)), ["AAA", "BBB"])
        cached, _ = get_option_expirations(["AAA", "BBB"], cache_path=path, local_only=True, today=TODAY)
        self.assertEqual(sorted(cached), ["AAA", "BBB"])

    def test_failed_write_leaves_existing_file_intact(self):
        path = os.path.join(self.tmp.name, "fundamentals_cache.json")
        original = {"OLD": {"as_of": "2026-11-06", "quote_type": "EQUITY", "market_cap_b": 1.0}}
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(original, handle)

        def failing_replace(source, target):
            raise OSError("disk full")

        row = {"symbol": "NEW", "quoteType": "EQUITY", "marketCap": 2e9}
        with mock.patch.object(marketdata.os, "replace", failing_replace):
            result, warnings = get_fundamentals(
                ["NEW"], fetch=lambda s: _quote_body([row]), cache_path=path, today=TODAY
            )
        self.assertEqual(self._read(path), original)
        self.assertEqual(os.listdir(self.tmp.name), ["fundamentals_cache.json"], "no temporary file left behind")
        self.assertIn("NEW", result, "the fresh fetch is still used")
        self.assertTrue(any("could not save cache file" in w for w in warnings))

    def test_corrupt_or_wrong_shaped_cache_reads_as_empty_with_warning(self):
        for content in ('{"AAA": {"as_of": "2026-1', '["not", "an", "object"]'):
            for loader in ("fundamentals", "expirations"):
                with self.subTest(content=content, loader=loader):
                    path = os.path.join(self.tmp.name, f"{loader}.json")
                    with open(path, "w", encoding="utf-8") as handle:
                        handle.write(content)
                    marketdata._option_expirations_memo.clear()
                    if loader == "fundamentals":
                        result, warnings = get_fundamentals(["AAA"], cache_path=path, local_only=True)
                    else:
                        result, warnings = get_option_expirations(["AAA"], cache_path=path, local_only=True)
                    self.assertEqual(result, {})
                    self.assertEqual(len(warnings), 1)
                    self.assertIn("ignored", warnings[0])

    def test_corrupt_cache_is_replaced_by_next_successful_refresh(self):
        path = os.path.join(self.tmp.name, "fundamentals_cache.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{broken")
        row = {"symbol": "AAA", "quoteType": "EQUITY", "marketCap": 2e9}
        result, warnings = get_fundamentals(["AAA"], fetch=lambda s: _quote_body([row]), cache_path=path, today=TODAY)
        self.assertIn("AAA", result)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(list(self._read(path)), ["AAA"])


if __name__ == "__main__":
    unittest.main()
