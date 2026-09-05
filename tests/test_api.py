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
from wheel.api import Dashboard, Filters  # noqa: E402
from wheel.marketdata import PricePoint  # noqa: E402

HISTORY_HEADER = (
    "Run Date,Action,Symbol,Description,Type,Quantity,Price ($),Commission ($),"
    "Fees ($),Accrued Interest ($),Amount ($),Cash Balance ($),Settlement Date"
)
POSITIONS_HEADER = (
    "Account number,Account name,Symbol,Description,Quantity,Last price,Last price change,"
    "Current value,Today's gain/loss dollar,Today's gain/loss percent,Total gain/loss dollar,"
    "Total gain/loss percent,Percent of account,Cost basis total,Average cost basis,Type"
)


def _write_positions_csv(path: str, account_number: str, cash_value: float, as_of: str) -> None:
    """A single cash-only position row -- ``total_value`` == ``cash_value``,
    so the test's expected "not deployed" figure is unambiguous.
    """
    row = (
        f'{account_number},"Test Account",SPAXX**,"MONEY MARKET",{cash_value},$1.00,,'
        f"${cash_value:.2f},,,,,100.00%,${cash_value:.2f},$1.00,Cash,"
    )
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        handle.write(POSITIONS_HEADER + "\n")
        handle.write(row + "\n")
        handle.write("\n")
        handle.write(f'"Date downloaded {as_of} 5:00 p.m ET"\n')


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
        each need a network fetch taking ``delay`` seconds must finish in
        close to ``delay`` total, not ``N * delay`` -- proving the pool
        actually overlaps the waits rather than merely wrapping the same
        serial loop in a pool object.
        """
        tickers = [f"T{i}" for i in range(6)]
        with tempfile.TemporaryDirectory() as tmp:
            dashboard = _dashboard_with_held_shares(tmp, tickers)
            delay = 0.2

            def slow_get_price_series(ticker, *, local_only=False, **kwargs):
                # No local cache/memo -> every ticker is a network miss the
                # caller must batch through the pool.
                if local_only:
                    return None
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


class TestCapitalThroughExtendsToPositionsSnapshot(unittest.TestCase):
    """A same-day account refresh with no trade -- the common case, most days
    nothing happens -- must still land on the capital chart, not vanish
    because `through` is pinned to the last transaction. Regression coverage
    for the `capital_through` plumbing in `Dashboard.build()`.
    """

    def _dashboard(self, tmp: str) -> Dashboard:
        history_path = os.path.join(tmp, "History_for_Account.csv")
        _write_history_csv(
            history_path,
            [
                # $10,000 collateral, never closed.
                '08/01/2026,"YOU SOLD OPENING TRANSACTION PUT (MU) ...",-MU260901P100,'
                '"PUT ...",Cash,-1,3.00,0,0,,300.00,10000.00,08/01/2026',
                # A later, unrelated event -- just to make `self.last_date`
                # (2026-08-07) land before the Positions snapshot
                # (2026-08-08) while staying after the CSP's own open date.
                '08/07/2026,"DIVIDEND RECEIVED MU",MU,"MU DESCRIPTION",Cash,,,0,0,,5.00,10005.00,08/07/2026',
            ],
        )
        positions_path = os.path.join(tmp, "Portfolio_Positions.csv")
        _write_positions_csv(positions_path, "111111111", 50000.0, "Aug-08-2026")
        return Dashboard(history_path, position_paths=[positions_path])

    def test_all_history_view_extends_capital_series_to_the_snapshot_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            dashboard = self._dashboard(tmp)
            data = dashboard.build()  # no filters -- through == self.last_date

            self.assertEqual(data["net_worth"]["timeline"][0]["as_of"], "2026-08-08")
            last_point = data["capital_series"][-1]
            self.assertEqual(last_point["date"], "2026-08-08")
            self.assertAlmostEqual(last_point["total"], 10000.0, places=2)  # unchanged, just carried forward
            self.assertAlmostEqual(data["portfolio"]["capital_deployed_now"], 10000.0, places=2)

            # The extension must not leak into days_span/annualization -- those
            # stay anchored to `through` (2026-08-07), not `capital_through`.
            self.assertEqual(data["portfolio"]["days_span"], 6)  # Aug 1 -> Aug 7

    def test_explicit_historical_end_filter_is_not_overridden(self):
        with tempfile.TemporaryDirectory() as tmp:
            dashboard = self._dashboard(tmp)
            data = dashboard.build(Filters(end=date(2026, 8, 3)))  # before self.last_date

            last_point = data["capital_series"][-1]
            self.assertEqual(last_point["date"], "2026-08-03")  # not stretched to the snapshot date


class TestUntrackedEquityValue(unittest.TestCase):
    """`net_worth.untracked_equity_value` -- the market value of Positions-
    snapshot shares this dashboard's transaction history has no share lot for
    at all: a legacy holding bought before every loaded export begins, with
    nothing since but perhaps a stray dividend-reinvestment fraction of a
    share. This is the single biggest reason `wheel_capital_deployed` can
    undercount `total_value` even for a fully-funded, fully-tracked wheel.
    """

    def _positions_csv(self, path: str, rows: list[str]) -> None:
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            handle.write(POSITIONS_HEADER + "\n")
            handle.write("\n".join(rows) + "\n")
            handle.write("\n")
            handle.write('"Date downloaded Sep-05-2026 5:00 p.m ET"\n')

    def _mu_history(self, path: str) -> None:
        """A CSP assigned into 100 real MU shares -- the one position this
        history actually tracks a share lot for."""
        _write_history_csv(
            path, _assigned_put_rows("-MU251010P100", "MU", "10/01/2025", "10/13/2025", "Oct-10-2025")
        )

    def test_a_position_with_no_history_at_all_is_fully_untracked(self):
        with tempfile.TemporaryDirectory() as tmp:
            history_path = os.path.join(tmp, "History.csv")
            self._mu_history(history_path)
            positions_path = os.path.join(tmp, "Positions.csv")
            self._positions_csv(
                positions_path,
                [
                    # 100 MU shares -- exactly what the assignment above tracks.
                    '111111111,"Test Account",MU,"MICRON TECHNOLOGY INC",100,$100.00,,'
                    "$10000.00,,,,,50.00%,$9700.00,$97.00,Cash,",
                    # XOM: never appears in the transaction history at all.
                    '111111111,"Test Account",XOM,"EXXON MOBIL CORP",50,$100.00,,'
                    "$5000.00,,,,,25.00%,$3000.00,$60.00,Cash,",
                ],
            )
            data = Dashboard(history_path, position_paths=[positions_path]).build()
            self.assertAlmostEqual(data["net_worth"]["untracked_equity_value"], 5000.0, places=2)

    def test_a_partially_tracked_position_only_counts_its_untracked_share(self):
        with tempfile.TemporaryDirectory() as tmp:
            history_path = os.path.join(tmp, "History.csv")
            self._mu_history(history_path)  # tracks 100 of the 150 real shares below
            positions_path = os.path.join(tmp, "Positions.csv")
            self._positions_csv(
                positions_path,
                [
                    '111111111,"Test Account",MU,"MICRON TECHNOLOGY INC",150,$100.00,,'
                    "$15000.00,,,,,50.00%,$14550.00,$97.00,Cash,",
                ],
            )
            data = Dashboard(history_path, position_paths=[positions_path]).build()
            # 50 of 150 shares untracked -> 1/3 of the $15,000 market value.
            self.assertAlmostEqual(data["net_worth"]["untracked_equity_value"], 5000.0, places=2)

    def test_fully_tracked_position_contributes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            history_path = os.path.join(tmp, "History.csv")
            self._mu_history(history_path)
            positions_path = os.path.join(tmp, "Positions.csv")
            self._positions_csv(
                positions_path,
                [
                    '111111111,"Test Account",MU,"MICRON TECHNOLOGY INC",100,$100.00,,'
                    "$10000.00,,,,,50.00%,$9700.00,$97.00,Cash,",
                ],
            )
            data = Dashboard(history_path, position_paths=[positions_path]).build()
            self.assertAlmostEqual(data["net_worth"]["untracked_equity_value"], 0.0, places=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
