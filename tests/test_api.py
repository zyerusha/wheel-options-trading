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

            # No live quote for any ticker -> falls back to the daily-close
            # series above, so the expected figures are unchanged.
            original = api_module.marketdata.get_price_series
            original_last = api_module.marketdata.get_last_prices
            api_module.marketdata.get_price_series = fake_get_price_series
            api_module.marketdata.get_last_prices = lambda tickers, **kwargs: ({}, [])
            try:
                prices = dashboard._current_prices()
            finally:
                api_module.marketdata.get_price_series = original
                api_module.marketdata.get_last_prices = original_last

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
            original_last = api_module.marketdata.get_last_prices
            api_module.marketdata.get_price_series = fake_get_price_series
            api_module.marketdata.get_last_prices = lambda tickers, **kwargs: ({}, [])
            try:
                dashboard._current_prices()
                dashboard._current_prices()
            finally:
                api_module.marketdata.get_price_series = original
                api_module.marketdata.get_last_prices = original_last

            self.assertEqual(calls, ["MU"])  # fetched once, not once per call (within the TTL)

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
            original_last = api_module.marketdata.get_last_prices
            api_module.marketdata.get_price_series = slow_get_price_series
            api_module.marketdata.get_last_prices = lambda tickers, **kwargs: ({}, [])
            try:
                start = time.time()
                prices = dashboard._current_prices()
                elapsed = time.time() - start
            finally:
                api_module.marketdata.get_price_series = original
                api_module.marketdata.get_last_prices = original_last

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

    def test_live_quote_wins_over_the_daily_close_when_both_are_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            dashboard = _dashboard_with_held_shares(tmp, ["MU"])

            original = api_module.marketdata.get_price_series
            original_last = api_module.marketdata.get_last_prices
            api_module.marketdata.get_price_series = (
                lambda ticker, **kw: ([PricePoint(day=date(2026, 1, 1), close=210.5)], [])
            )
            api_module.marketdata.get_last_prices = lambda tickers, **kw: ({"MU": 214.02}, [])
            try:
                prices = dashboard._current_prices()
            finally:
                api_module.marketdata.get_price_series = original
                api_module.marketdata.get_last_prices = original_last

            # The live quote is the mark; the daily close becomes "prev" (the
            # "Last Close %" / day-change comparison point), not the mark itself.
            self.assertEqual(prices, {"MU": 214.02})
            self.assertEqual(dashboard._prev_closes, {"MU": 210.5})

    def test_missing_live_quote_falls_back_to_the_daily_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            dashboard = _dashboard_with_held_shares(tmp, ["MU"])

            original = api_module.marketdata.get_price_series
            original_last = api_module.marketdata.get_last_prices
            api_module.marketdata.get_price_series = (
                lambda ticker, **kw: ([PricePoint(day=date(2026, 1, 1), close=210.5)], [])
            )
            # e.g. the quote endpoint's crumb auth failed -- no live price at all.
            api_module.marketdata.get_last_prices = lambda tickers, **kw: ({}, ["auth failed"])
            try:
                prices = dashboard._current_prices()
            finally:
                api_module.marketdata.get_price_series = original
                api_module.marketdata.get_last_prices = original_last

            self.assertEqual(prices, {"MU": 210.5})
            self.assertIn("auth failed", dashboard._price_warnings)

    def test_cache_expires_after_the_live_price_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            dashboard = _dashboard_with_held_shares(tmp, ["MU"])
            calls = []

            def fake_get_last_prices(tickers, **kw):
                calls.append(1)
                return {"MU": 100.0 + len(calls)}, []

            original = api_module.marketdata.get_price_series
            original_last = api_module.marketdata.get_last_prices
            api_module.marketdata.get_price_series = (
                lambda ticker, **kw: ([PricePoint(day=date(2026, 1, 1), close=99.0)], [])
            )
            api_module.marketdata.get_last_prices = fake_get_last_prices
            try:
                first = dashboard._current_prices()
                dashboard._price_cache_at -= api_module._LIVE_PRICE_TTL_SECONDS + 1
                second = dashboard._current_prices()
            finally:
                api_module.marketdata.get_price_series = original
                api_module.marketdata.get_last_prices = original_last

            self.assertEqual(len(calls), 2)  # re-fetched once the TTL elapsed
            self.assertNotEqual(first, second)


class TestBuildRefreshesOnLivePrice(unittest.TestCase):
    """Regression coverage for a bug where _current_prices() refreshed on its
    TTL exactly as designed, but every price-derived table build() actually
    returns (open_positions, cc_candidates, trade_log, ...) stayed frozen at
    whatever they were built from on the Dashboard's very first build() call
    -- each was guarded by a plain ``is None`` memo with no path back to
    ``None`` once set, so a live price ticking underneath was invisible on
    screen no matter how often the frontend polled.
    """

    def test_cc_candidate_last_close_updates_once_the_live_price_ttl_elapses(self):
        with tempfile.TemporaryDirectory() as tmp:
            dashboard = _dashboard_with_held_shares(tmp, ["MU"])

            prices = [100.0]

            def fake_get_last_prices(tickers, **kw):
                return {"MU": prices[0]}, []

            # A real clock backdated by hand (dashboard._price_cache_at -= N)
            # can coincidentally land both builds' *new* _price_cache_at on
            # the same time.monotonic() tick when the test runs fast enough
            # -- real usage never hits this (a refetch only ever happens
            # after TTL seconds of real time), but the test needs a clock it
            # fully controls to not depend on that timing.
            clock = [1_000_000.0]
            original = api_module.marketdata.get_price_series
            original_last = api_module.marketdata.get_last_prices
            original_monotonic = api_module.time.monotonic
            api_module.marketdata.get_price_series = (
                lambda ticker, **kw: ([PricePoint(day=date(2026, 1, 1), close=90.0)], [])
            )
            api_module.marketdata.get_last_prices = fake_get_last_prices
            api_module.time.monotonic = lambda: clock[0]
            try:
                first = dashboard.build()
                first_row = next(r for r in first["cc_candidates"] if r["underlying"] == "MU")
                self.assertEqual(first_row["last_close"], 100.0)

                prices[0] = 105.0
                clock[0] += api_module._LIVE_PRICE_TTL_SECONDS + 1
                second = dashboard.build()
                second_row = next(r for r in second["cc_candidates"] if r["underlying"] == "MU")
                self.assertEqual(second_row["last_close"], 105.0)
            finally:
                api_module.marketdata.get_price_series = original
                api_module.marketdata.get_last_prices = original_last
                api_module.time.monotonic = original_monotonic

    def test_a_second_build_within_the_ttl_does_not_recompute(self):
        with tempfile.TemporaryDirectory() as tmp:
            dashboard = _dashboard_with_held_shares(tmp, ["MU"])

            original = api_module.marketdata.get_price_series
            original_last = api_module.marketdata.get_last_prices
            api_module.marketdata.get_price_series = (
                lambda ticker, **kw: ([PricePoint(day=date(2026, 1, 1), close=90.0)], [])
            )
            api_module.marketdata.get_last_prices = lambda tickers, **kw: ({"MU": 100.0}, [])
            try:
                dashboard.build()
                first_positions = dashboard._open_positions
                dashboard.build()
                # Same object, not just an equal one -- proves the second
                # build() short-circuited on the unchanged price generation
                # rather than rebuilding from scratch.
                self.assertIs(dashboard._open_positions, first_positions)
            finally:
                api_module.marketdata.get_price_series = original
                api_module.marketdata.get_last_prices = original_last


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


class TestCapitalPointSharesSplit(unittest.TestCase):
    """`_capital_point` splits `stock` (all held-share cost basis) into
    `idle_stock` (no call written) + `call_stock` (backing an open covered
    call), so the Capital deployed chart can draw the two as separate bands.
    """

    def test_idle_hold_is_all_idle_stock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "History.csv")
            _write_history_csv(
                path,
                [
                    '11/17/2025,"YOU SOLD OPENING TRANSACTION PUT (MU) ...",-MU251121P230,'
                    '"PUT ...",Cash,-1,4.00,0,0,,399.33,10000.00,11/17/2025',
                    '11/21/2025,"ASSIGNED PUT as of Nov-20-2025",-MU251121P230,"PUT ...",Cash,1,,0,0,,0.00,9700.00,11/21/2025',
                ],
            )
            last = Dashboard(path, position_paths=[]).build()["capital_series"][-1]
            self.assertAlmostEqual(last["stock"], 23000.0, places=2)
            self.assertAlmostEqual(last["idle_stock"], 23000.0, places=2)
            self.assertAlmostEqual(last["call_stock"], 0.0, places=2)

    def test_covered_call_moves_the_basis_to_call_stock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "History.csv")
            _write_history_csv(
                path,
                [
                    '11/17/2025,"YOU SOLD OPENING TRANSACTION PUT (MU) ...",-MU251121P230,'
                    '"PUT ...",Cash,-1,4.00,0,0,,399.33,10000.00,11/17/2025',
                    '11/21/2025,"ASSIGNED PUT as of Nov-20-2025",-MU251121P230,"PUT ...",Cash,1,,0,0,,0.00,9700.00,11/21/2025',
                    '11/24/2025,"YOU SOLD OPENING TRANSACTION CALL (MU) ...",-MU251219C235,'
                    '"CALL ...",Cash,-1,2.00,0,0,,199.33,9900.00,11/24/2025',
                ],
            )
            last = Dashboard(path, position_paths=[]).build()["capital_series"][-1]
            self.assertAlmostEqual(last["idle_stock"] + last["call_stock"], last["stock"], places=2)
            self.assertAlmostEqual(last["idle_stock"], 0.0, places=2)
            self.assertAlmostEqual(last["call_stock"], 23000.0, places=2)


class TestOpenPositionsIsAFullSnapshot(unittest.TestCase):
    """`Dashboard.build()`'s `open_positions` must show every currently-held
    position, not just legged wheels -- a plain buy-and-hold lot (never
    wheeled, no option ever written on it) gets a synthetic no-contract row
    too, same as an assigned-but-uncovered wheel does. Regression coverage
    for the `build()` merge step that used to gate the synthetic row on
    `is_wheel` + a computed `wheel_phase`, which a buy-and-hold lot never has.
    """

    def test_buy_and_hold_lot_gets_a_no_contract_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "History.csv")
            _write_history_csv(
                path,
                [
                    '09/01/2025,"YOU BOUGHT (PAGS) PAGSEGURO DIGITAL LTD",PAGS,'
                    '"PAGSEGURO DIGITAL LTD",Cash,100,10.00,0,0,,-1000.00,9000.00,09/01/2025',
                ],
            )
            data = Dashboard(path, position_paths=[]).build()
            rows = [r for r in data["open_positions"] if r["underlying"] == "PAGS"]

            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0]["type"])
            self.assertAlmostEqual(rows[0]["shares_held"], 100.0, places=4)
            self.assertAlmostEqual(rows[0]["breakeven"], 10.0, places=2)

    def test_wheeled_position_still_gets_a_row_when_legless(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "History.csv")
            _write_history_csv(
                path,
                [
                    '11/17/2025,"YOU SOLD OPENING TRANSACTION PUT (MU) ...",-MU251121P230,'
                    '"PUT ...",Cash,-1,4.00,0,0,,399.33,10000.00,11/17/2025',
                    '11/21/2025,"ASSIGNED PUT as of Nov-20-2025",-MU251121P230,"PUT ...",Cash,1,,0,0,,0.00,9700.00,11/21/2025',
                ],
            )
            data = Dashboard(path, position_paths=[]).build()
            rows = [r for r in data["open_positions"] if r["underlying"] == "MU"]

            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0]["type"])
            self.assertAlmostEqual(rows[0]["shares_held"], 100.0, places=4)


class TestTradeLogUntrackedShares(unittest.TestCase):
    """A still-open wheel whose shares were all bought before the oldest
    loaded export (the IRA's JXN: 0 tracked, 300 real) shows the broker's
    real share count as `shares_untracked`, rolled forward by any share
    trade the Positions snapshot predates so it reflects today's holding.
    """

    # A covered call against shares this history never saw being bought.
    COVERED_CALL_ROW = (
        '09/01/2026,"YOU SOLD OPENING TRANSACTION CALL (XOM) ...",-XOM270115C150,'
        '"CALL ...",Cash,-1,3.00,0,0,,300.00,10000.00,09/02/2026'
    )

    def _xom_wheel(self, history_rows: list[str]) -> dict:
        return self._xom_build(history_rows)[0]

    # Fidelity lists freshly assigned shares as their own Margin row with no
    # cost basis ("--") until they settle.
    UNCOSTED_MARGIN_ROW = (
        '111111111,"Test Account",XOM,"EXXON MOBIL CORP",100,$100.00,,'
        "$10000.00,,,,,25.00%,--,--,Margin,\n"
    )

    def _xom_build(
        self,
        history_rows: list[str],
        cost_basis_total: str = "$18000.00",
        extra_position_rows: tuple[str, ...] | list[str] = (),
    ) -> tuple[dict, dict]:
        """(the XOM Trade Log wheel, its Wheel price targets row). The broker
        file reports 300 shares; ``cost_basis_total`` "" means no cost on file."""
        with tempfile.TemporaryDirectory() as tmp:
            history_path = os.path.join(tmp, "History.csv")
            _write_history_csv(history_path, history_rows)
            positions_path = os.path.join(tmp, "Positions.csv")
            with open(positions_path, "w", encoding="utf-8-sig", newline="") as handle:
                handle.write(POSITIONS_HEADER + "\n")
                handle.write(
                    '111111111,"Test Account",XOM,"EXXON MOBIL CORP",300,$100.00,,'
                    f"$30000.00,,,,,100.00%,{cost_basis_total},,Cash,\n"
                )
                handle.writelines(extra_position_rows)
                handle.write('\n"Date downloaded Sep-05-2026 5:00 p.m ET"\n')
            data = Dashboard(history_path, position_paths=[positions_path]).build()
        (wheel,) = [w for w in data["trade_log"]["wheels"] if w["underlying"] == "XOM"]
        (target,) = [t for t in data["wheel_targets"] if t["underlying"] == "XOM"]
        return wheel, target

    def _sale_row(self, run_date: str, shares: int) -> str:
        return (
            f'{run_date},"YOU SOLD EXXON MOBIL CORP (XOM) (Cash)",XOM,"EXXON MOBIL CORP",Cash,'
            f"-{shares},100.00,0,0,,{shares * 100}.00,20000.00,{run_date}"
        )

    def test_open_wheel_with_no_tracked_shares_shows_the_real_count(self):
        wheel = self._xom_wheel([self.COVERED_CALL_ROW])
        self.assertNotEqual(wheel["status"], "CLOSED")
        self.assertAlmostEqual(wheel["shares_held"], 0.0, places=4)
        self.assertAlmostEqual(wheel["shares_untracked"], 300.0, places=4)

    def test_untracked_shares_put_the_wheel_in_its_covered_call_phase(self):
        wheel, target = self._xom_build([self.COVERED_CALL_ROW])
        self.assertEqual(wheel["wheel_phase"], "cc")
        self.assertNotIn("preferred_csp_entry", wheel)
        self.assertEqual(target["wheel_phase"], "cc")
        self.assertAlmostEqual(target["shares_untracked"], 300.0, places=4)

    def test_untracked_shares_are_costed_at_the_broker_average(self):
        wheel, _ = self._xom_build([self.COVERED_CALL_ROW])
        # $18,000 / 300 shares.
        self.assertEqual(wheel["untracked_cost_per_share"], 60.0)
        self.assertEqual(wheel["cost_basis_per_share"], 60.0)
        # $60 cost less the $300 open call credit (valued at expiry) over 300 shares.
        self.assertAlmostEqual(wheel["break_even_price"], 59.0, places=2)
        # max($60 cost, $59 break-even) x 1.02 = $61.20, rounded up to $0.50.
        self.assertEqual(wheel["profit_target"], 61.5)

    def test_running_break_even_starts_out_holding_the_untracked_shares(self):
        # Feeds the "Break-even price over time" chart, which had no points at
        # all when no share row in the history ever bought the shares.
        wheel, _ = self._xom_build([self.COVERED_CALL_ROW])
        (row,) = [r for r in wheel["transactions"] if r["type"] != "Dividend"]
        # Real cash only: the opening shares' cost never enters this column.
        self.assertAlmostEqual(row["running_cash_flow"], 300.0, places=2)
        # ($18,000 opening cost - $300 credit) / 300 shares, = the summary figure.
        self.assertAlmostEqual(row["running_break_even"], 59.0, places=2)
        self.assertAlmostEqual(row["running_cost_basis"], 60.0, places=2)

    def test_no_broker_cost_leaves_the_targets_explained_but_blank(self):
        wheel, _ = self._xom_build([self.COVERED_CALL_ROW], cost_basis_total="")
        self.assertIsNone(wheel["untracked_cost_per_share"])
        self.assertIsNone(wheel["cost_basis_per_share"])
        self.assertIsNone(wheel["break_even_price"])
        self.assertIsNone(wheel["profit_target"])
        self.assertIn("unknown", wheel["profit_target_explanation"])

    def test_uncosted_row_the_ledger_traces_keeps_the_broker_average(self):
        # The IRA's JXN: 300 old shares with a broker cost, plus 100 just
        # assigned that the broker shows as "--". The ledger costs those 100 at
        # the strike itself, so the 300 still get the broker's average.
        assignment_rows = _assigned_put_rows("-XOM260918P95", "XOM", "09/01/2026", "09/18/2026", "Sep-18-2026")
        wheel, _ = self._xom_build(
            [self.COVERED_CALL_ROW, *assignment_rows],
            extra_position_rows=[self.UNCOSTED_MARGIN_ROW],
        )
        self.assertAlmostEqual(wheel["shares_held"], 100.0, places=4)
        self.assertAlmostEqual(wheel["shares_untracked"], 300.0, places=4)
        # $18,000 / 300 costed shares; the uncosted Margin row is the assignment.
        self.assertEqual(wheel["untracked_cost_per_share"], 60.0)
        # (300 x $60 + 100 x $95) / 400 shares.
        self.assertAlmostEqual(wheel["cost_basis_per_share"], 68.75, places=2)
        # Every row after the opening holds all 300 old shares, so every one
        # has a break-even point for the chart, not just the assignment row.
        self.assertTrue(all(r["running_break_even"] is not None for r in wheel["transactions"]))

    def test_uncosted_row_the_ledger_does_not_trace_leaves_no_cost(self):
        # Nothing in the history bought those 100 shares, so they could be
        # among the untracked ones; averaging over the costed row alone would
        # misprice them.
        wheel, _ = self._xom_build([self.COVERED_CALL_ROW], extra_position_rows=[self.UNCOSTED_MARGIN_ROW])
        self.assertAlmostEqual(wheel["shares_untracked"], 400.0, places=4)
        self.assertIsNone(wheel["untracked_cost_per_share"])

    def test_a_sale_after_the_snapshot_is_subtracted(self):
        wheel = self._xom_wheel([self.COVERED_CALL_ROW, self._sale_row("09/08/2026", 100)])
        self.assertAlmostEqual(wheel["shares_untracked"], 200.0, places=4)

    def test_a_sale_on_the_snapshot_day_is_not_counted_twice(self):
        # Already settled into the Sep-05 snapshot's 300, by definition.
        wheel = self._xom_wheel([self.COVERED_CALL_ROW, self._sale_row("09/05/2026", 100)])
        self.assertAlmostEqual(wheel["shares_untracked"], 300.0, places=4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
