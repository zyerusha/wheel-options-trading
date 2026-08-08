"""Dashboard-level tests: currently just the parallel price-fetch helper."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel import api as api_module  # noqa: E402
from wheel.api import Dashboard  # noqa: E402
from wheel.marketdata import PricePoint  # noqa: E402

HISTORY_HEADER = (
    "Run Date,Action,Symbol,Description,Type,Quantity,Price ($),Commission ($),"
    "Fees ($),Accrued Interest ($),Amount ($),Cash Balance ($),Settlement Date"
)


def _assigned_put_rows(symbol: str, underlying: str, open_date: str, assign_date: str, as_of: str) -> list[str]:
    """STO then ASSIGNED, so the share lot stays held (remaining > 0) -- the
    condition ``_current_prices`` uses to decide a ticker needs a fetch.
    """
    return [
        f'{open_date},"YOU SOLD OPENING TRANSACTION PUT ({underlying}) ...",{symbol},'
        f'"PUT ...",Cash,-1,3.00,0,0,,300.00,10000.00,{open_date}',
        f'{assign_date},"ASSIGNED PUT as of {as_of}",{symbol},"PUT ...",Cash,1,,0,0,,0.00,9700.00,{assign_date}',
    ]


def _write_history_csv(path: str, rows: list[str]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        handle.write(HISTORY_HEADER + "\n")
        handle.write("\n".join(rows) + "\n")


def _dashboard_with_held_shares(tmp: str, tickers: list[str]) -> Dashboard:
    """One Dashboard holding one assigned (never-sold) share lot per ticker."""
    rows: list[str] = []
    for i, underlying in enumerate(tickers):
        symbol = f"-{underlying}2510{10 + i:02d}P100"  # fixed month (10), day 10..15+
        rows.extend(_assigned_put_rows(symbol, underlying, "10/01/2025", "10/13/2025", "Oct-10-2025"))
    path = os.path.join(tmp, "History_for_Account.csv")
    _write_history_csv(path, rows)
    return Dashboard(path, position_paths=[])


class TestCurrentPrices(unittest.TestCase):
    def test_fetches_every_held_ticker_and_aggregates_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            dashboard = _dashboard_with_held_shares(tmp, ["MU", "QQQ", "IVV"])

            def fake_get_price_series(ticker, **kwargs):
                if ticker == "QQQ":
                    return [], [f"could not fetch {ticker} prices"]
                return [PricePoint(day=date(2026, 1, 1), close={"MU": 210.5, "IVV": 580.0}[ticker])], []

            original = api_module.marketdata.get_price_series
            api_module.marketdata.get_price_series = fake_get_price_series
            try:
                prices = dashboard._current_prices()
            finally:
                api_module.marketdata.get_price_series = original

            self.assertEqual(prices, {"MU": 210.5, "QQQ": None, "IVV": 580.0})
            self.assertEqual(len(dashboard._price_warnings), 1)
            self.assertIn("QQQ", dashboard._price_warnings[0])

    def test_result_is_memoized_not_refetched_on_a_second_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            dashboard = _dashboard_with_held_shares(tmp, ["MU"])
            calls = []

            def fake_get_price_series(ticker, **kwargs):
                calls.append(ticker)
                return [PricePoint(day=date(2026, 1, 1), close=210.5)], []

            original = api_module.marketdata.get_price_series
            api_module.marketdata.get_price_series = fake_get_price_series
            try:
                dashboard._current_prices()
                dashboard._current_prices()
            finally:
                api_module.marketdata.get_price_series = original

            self.assertEqual(calls, ["MU"])  # fetched once, not once per call

    def test_tickers_are_fetched_concurrently_not_one_at_a_time(self):
        """Regression guard for the sequential-fetch slowdown: N tickers that
        each take ``delay`` seconds must finish in close to ``delay`` total,
        not ``N * delay`` -- proving the pool actually overlaps the waits
        rather than merely wrapping the same serial loop in a pool object.
        """
        tickers = [f"T{i}" for i in range(6)]
        with tempfile.TemporaryDirectory() as tmp:
            dashboard = _dashboard_with_held_shares(tmp, tickers)
            delay = 0.2

            def slow_get_price_series(ticker, **kwargs):
                time.sleep(delay)
                return [PricePoint(day=date(2026, 1, 1), close=100.0)], []

            original = api_module.marketdata.get_price_series
            api_module.marketdata.get_price_series = slow_get_price_series
            try:
                start = time.time()
                prices = dashboard._current_prices()
                elapsed = time.time() - start
            finally:
                api_module.marketdata.get_price_series = original

            self.assertEqual(len(prices), len(tickers))
            # Sequential would take ~= len(tickers) * delay (1.2s); parallel
            # across up to 8 workers should take roughly one delay's worth.
            self.assertLess(elapsed, delay * len(tickers) * 0.6)

    def test_no_held_shares_means_no_fetch_at_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "History_for_Account.csv")
            _write_history_csv(
                path,
                [
                    '10/01/2025,"YOU SOLD OPENING TRANSACTION PUT (MU) ...",-MU251010P100,'
                    '"PUT ...",Cash,-1,3.00,0,0,,300.00,10000.00,10/01/2025',
                    '10/13/2025,"ASSIGNED PUT as of Oct-10-2025",-MU251010P100,"PUT ...",Cash,1,,0,0,,0.00,9700.00,10/13/2025',
                    '10/20/2025,"YOU SOLD STOCK",MU,"STOCK ...",Cash,-100,105.00,0,0,,10500.00,20200.00,10/20/2025',
                ],
            )
            dashboard = Dashboard(path, position_paths=[])

            def unexpected_fetch(ticker, **kw):
                raise AssertionError(f"should never fetch a price for {ticker!r} -- no shares are held")

            original = api_module.marketdata.get_price_series
            api_module.marketdata.get_price_series = unexpected_fetch
            try:
                prices = dashboard._current_prices()
            finally:
                api_module.marketdata.get_price_series = original
            self.assertEqual(prices, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
