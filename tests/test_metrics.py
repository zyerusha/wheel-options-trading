"""Metrics tests: collateral timeline, ROI denominators, annualization."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_engine import tx  # noqa: E402
from wheel.engine import build_cycles  # noqa: E402
from wheel.metrics import (  # noqa: E402
    _time_weighted_average,
    capital_timeline,
    cycle_metrics,
    portfolio_capital_series,
    portfolio_metrics,
    realized_pl_series,
    ticker_summary,
)
from wheel.parser import ASSIGNED, BTC, EXPIRED, STO  # noqa: E402


class TestCapitalTimeline(unittest.TestCase):
    def test_csp_commits_strike_times_multiplier(self):
        cycles, _ = build_cycles(
            [
                tx("2025-09-19", STO, "-MU250926P150", -2, 3.35, 668.66),
                tx("2025-09-26", EXPIRED, "-MU250926P150", 2, None, 0.0, as_of="2025-09-26"),
            ]
        )
        points = capital_timeline(cycles[0], date(2025, 9, 26))
        self.assertEqual(len(points), 8)  # 19th through 26th inclusive
        self.assertAlmostEqual(points[0].put_collateral, 150 * 100 * 2)
        self.assertAlmostEqual(points[0].total, 30000.0)
        # Collateral is released on the day the position closes.
        self.assertAlmostEqual(points[-1].total, 0.0)

    def test_assignment_converts_put_collateral_into_stock_basis(self):
        cycles, _ = build_cycles(
            [
                tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 399.33, row_id=1),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=2, as_of="2025-11-20"),
            ]
        )
        points = {point.day: point for point in capital_timeline(cycles[0], date(2025, 11, 24))}

        before = points[date(2025, 11, 19)]
        self.assertAlmostEqual(before.put_collateral, 23000.0)
        self.assertAlmostEqual(before.stock_basis, 0.0)

        after = points[date(2025, 11, 21)]
        self.assertAlmostEqual(after.put_collateral, 0.0)
        self.assertAlmostEqual(after.stock_basis, 23000.0)
        # Capital committed is unchanged by the assignment itself.
        self.assertAlmostEqual(before.total, after.total)

    def test_covered_call_does_not_double_count_capital(self):
        cycles, _ = build_cycles(
            [
                tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 399.33, row_id=1),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=2, as_of="2025-11-20"),
                tx("2025-11-24", STO, "-MU251128C235", -1, 2.00, 199.33, row_id=3),
            ]
        )
        points = {point.day: point for point in capital_timeline(cycles[0], date(2025, 11, 25))}
        after_call = points[date(2025, 11, 25)]
        self.assertAlmostEqual(after_call.stock_basis, 23000.0)
        self.assertAlmostEqual(after_call.call_collateral, 0.0)
        self.assertAlmostEqual(after_call.total, 23000.0)

    def test_short_call_without_shares_uses_strike_proxy(self):
        cycles, _ = build_cycles([tx("2025-09-15", STO, "-QQQ251017C588", -1, 3.32, 331.33)])
        points = capital_timeline(cycles[0], date(2025, 9, 16))
        self.assertAlmostEqual(points[0].call_collateral, 58800.0)
        self.assertTrue(cycles[0].capital_estimated)


class TestCapitalSeriesGaps(unittest.TestCase):
    """A day with nothing live is a real zero, not a missing row.

    Anything plotting this series draws a straight line between consecutive
    points, so an absent stretch becomes a ramp asserting capital that was never
    committed.  Filtering to a single ticker used to leave months of it.
    """

    def gapped(self):
        """One cycle, a long flat stretch, then another cycle."""
        return build_cycles(
            [
                tx("2025-01-01", STO, "-MU250110P100", -1, 1.00, 99.33, row_id=1),
                tx("2025-01-10", EXPIRED, "-MU250110P100", 1, None, 0.0, row_id=2, as_of="2025-01-10"),
                tx("2025-06-01", STO, "-MU250620P100", -1, 1.00, 99.33, row_id=3),
                tx("2025-06-20", EXPIRED, "-MU250620P100", 1, None, 0.0, row_id=4, as_of="2025-06-20"),
            ]
        )[0]

    def test_gap_days_are_emitted_as_zero(self):
        series = portfolio_capital_series(self.gapped(), date(2025, 6, 20))
        span = (series[-1].day - series[0].day).days + 1
        self.assertEqual(len(series), span)

        days = [point.day for point in series]
        self.assertEqual(days, sorted(days))
        self.assertEqual(len(set(days)), len(days))

        idle = next(p for p in series if p.day == date(2025, 3, 15))
        self.assertEqual(idle.total, 0.0)

    def test_gap_fill_does_not_extend_past_the_last_live_day(self):
        """Extending to `through` would rewrite capital_deployed_now."""
        series = portfolio_capital_series(self.gapped(), date(2025, 12, 31))
        self.assertEqual(series[-1].day, date(2025, 6, 20))
        self.assertEqual(series[0].day, date(2025, 1, 1))

    def test_gap_fill_leaves_average_peak_and_current_unchanged(self):
        """The fix is display-only.

        Compared against only the days a cycle was actually live -- which is
        exactly what this function used to return -- the average, the peak and
        the closing point are all identical.  The mean already skipped zero days
        and a maximum ignores them, so nothing downstream moves.
        """
        cycles = self.gapped()
        filled = portfolio_capital_series(cycles, date(2025, 6, 20))

        live = {point.day for cycle in cycles for point in capital_timeline(cycle, date(2025, 6, 20))}
        before = [point for point in filled if point.day in live]
        self.assertLess(len(before), len(filled))  # there really was a gap to fill

        self.assertAlmostEqual(
            _time_weighted_average(filled), _time_weighted_average(before), places=6
        )
        self.assertAlmostEqual(
            max(p.total for p in filled), max(p.total for p in before), places=6
        )
        self.assertEqual(filled[-1].day, before[-1].day)
        self.assertAlmostEqual(filled[-1].total, before[-1].total, places=6)

    def test_no_cycles_yields_an_empty_series(self):
        self.assertEqual(portfolio_capital_series([], date(2025, 6, 20)), [])

    def test_a_long_only_day_still_reports_a_total(self):
        """Capital can be entirely long-option debit, which the chart does not band.

        On such a day every banded series is zero while the total is not, so the
        total line is the only mark drawn -- without it the day would read as
        "no capital deployed", which is false.  Do not "simplify" it away.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-11-10", "BTO", "-WFC260320P82.5", 3, 3.84, -1154.01, row_id=1),
            ]
        )
        series = portfolio_capital_series(cycles, date(2025, 11, 12))
        point = series[-1]
        self.assertEqual(point.put_collateral, 0.0)
        self.assertEqual(point.stock_basis, 0.0)
        self.assertEqual(point.call_collateral, 0.0)
        self.assertGreater(point.long_premium, 0.0)
        self.assertGreater(point.total, 0.0)


class TestReturnMath(unittest.TestCase):
    def test_roi_and_annualized_roc_on_a_clean_csp(self):
        """$150 strike CSP, 7 days, keeps $334.33 premium."""
        cycles, _ = build_cycles(
            [
                tx("2025-09-19", STO, "-MU250926P150", -1, 3.35, 334.33),
                tx("2025-09-26", EXPIRED, "-MU250926P150", 1, None, 0.0, as_of="2025-09-26"),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 9, 26))

        self.assertEqual(metrics.days_active, 7)
        self.assertAlmostEqual(metrics.net_realized_pl, 334.33, places=2)
        self.assertAlmostEqual(metrics.initial_collateral, 15000.0)
        self.assertAlmostEqual(metrics.peak_collateral, 15000.0)
        # ROI = 334.33 / 15000
        self.assertAlmostEqual(metrics.roi_pct, 2.2289, places=3)
        # Annualized = ROI x 365/7
        self.assertAlmostEqual(metrics.annualized_roc_pct, 2.2289 * 365 / 7, places=2)
        # No stock in this cycle, so the premium-only variant matches the full-wheel one.
        self.assertAlmostEqual(metrics.annualized_roc_premium_pct, metrics.annualized_roc_pct, places=6)

    def test_average_capital_ignores_idle_days(self):
        """A cycle sitting flat between legs must not dilute its denominator."""
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-MU250110P100", -1, 1.00, 99.33, row_id=1),
                tx("2025-01-03", EXPIRED, "-MU250110P100", 1, None, 0.0, row_id=2, as_of="2025-01-03"),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 1, 3))
        # Days 1-2 hold $10,000; day 3 releases it and is excluded from the mean.
        self.assertAlmostEqual(metrics.avg_collateral, 10000.0)

    def test_three_roi_denominators_are_distinct_when_size_changes(self):
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-MU250131P100", -1, 1.00, 99.33, row_id=1),
                tx("2025-01-10", STO, "-MU250131P100", -3, 1.00, 299.01, row_id=2),
                tx("2025-01-31", EXPIRED, "-MU250131P100", 4, None, 0.0, row_id=3, as_of="2025-01-31"),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 1, 31))
        self.assertAlmostEqual(metrics.initial_collateral, 10000.0)
        self.assertAlmostEqual(metrics.peak_collateral, 40000.0)
        self.assertLess(metrics.initial_collateral, metrics.avg_collateral)
        self.assertLess(metrics.avg_collateral, metrics.peak_collateral)
        self.assertGreater(metrics.roi_pct, metrics.roi_on_avg_pct)
        self.assertGreater(metrics.roi_on_avg_pct, metrics.roi_on_peak_pct)

    def test_stock_pl_from_assignment_reaches_net(self):
        cycles, _ = build_cycles(
            [
                tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 399.33, row_id=1),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=2, as_of="2025-11-20"),
                tx("2025-11-24", STO, "-MU251128C235", -1, 2.00, 199.33, row_id=3),
                tx("2025-12-01", ASSIGNED, "-MU251128C235", 1, None, 0.0, row_id=4, as_of="2025-11-28"),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 11, 28))
        self.assertAlmostEqual(metrics.option_realized_pl, 598.66, places=2)
        self.assertAlmostEqual(metrics.stock_realized_pl, 500.0, places=2)
        self.assertAlmostEqual(metrics.net_realized_pl, 1098.66, places=2)
        self.assertEqual(metrics.assignments, 2)
        # Stock P/L moves the full-wheel ROC above the premium-only ROC.
        self.assertGreater(metrics.annualized_roc_pct, metrics.annualized_roc_premium_pct)

    def test_unknown_basis_shares_are_excluded_and_counted(self):
        cycles, _ = build_cycles(
            [
                tx("2025-09-15", STO, "-QQQ250917C588", -1, 3.32, 331.33, row_id=1),
                tx("2025-09-18", ASSIGNED, "-QQQ250917C588", 1, None, 0.0, row_id=2, as_of="2025-09-17"),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 9, 17))
        self.assertAlmostEqual(metrics.stock_realized_pl, 0.0)
        self.assertEqual(metrics.stock_basis_unknown_shares, 100)
        self.assertAlmostEqual(metrics.net_realized_pl, 331.33, places=2)

    def test_zero_collateral_yields_none_not_division_error(self):
        cycles, _ = build_cycles(
            [
                tx("2025-11-21", "BTO", "-MU251128C210", 1, 7.70, -770.67, row_id=1),
                tx("2025-11-26", "STC", "-MU251128C210", -1, 20.30, 2029.33, row_id=2),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 11, 26))
        self.assertIsNotNone(metrics.roi_pct)  # long premium counts as capital
        self.assertGreater(metrics.net_realized_pl, 0)

    def test_open_cycle_measures_through_the_as_of_date(self):
        cycles, _ = build_cycles([tx("2025-09-19", STO, "-MU251226P150", -1, 3.35, 334.33)])
        metrics = cycle_metrics(cycles[0], date(2025, 10, 19))
        self.assertEqual(metrics.status, "ACTIVE")
        self.assertEqual(metrics.days_active, 30)
        self.assertAlmostEqual(metrics.current_collateral, 15000.0)
        # Nothing is realized while the leg is still open.
        self.assertAlmostEqual(metrics.net_realized_pl, 0.0)
        self.assertAlmostEqual(metrics.option_open_premium, 334.33, places=2)


class TestPortfolioRollup(unittest.TestCase):
    def setUp(self):
        self.transactions = [
            tx("2025-09-19", STO, "-MU250926P150", -1, 3.35, 334.33, row_id=1),
            tx("2025-09-26", EXPIRED, "-MU250926P150", 1, None, 0.0, row_id=2, as_of="2025-09-26"),
            tx("2025-09-19", STO, "-QQQ250926P580", -1, 1.00, 99.33, row_id=3),
            tx("2025-09-26", BTC, "-QQQ250926P580", 1, 0.50, -50.02, row_id=4),
        ]
        self.cycles, _ = build_cycles(self.transactions)
        self.through = date(2025, 9, 26)

    def test_capital_is_summed_across_tickers_per_day(self):
        series = portfolio_capital_series(self.cycles, self.through)
        first = series[0]
        self.assertAlmostEqual(first.put_collateral, 15000.0 + 58000.0)

    def test_totals_match_the_sum_of_cycles(self):
        result = portfolio_metrics(self.cycles, self.through)
        self.assertEqual(result.cycles, 2)
        self.assertEqual(result.tickers, 2)
        self.assertEqual(result.active_cycles, 0)
        self.assertAlmostEqual(result.net_realized_pl, 334.33 + 49.31, places=2)
        self.assertAlmostEqual(result.premium_received, 334.33 + 99.33, places=2)

    def test_annualized_uses_portfolio_capital_not_averaged_percentages(self):
        result = portfolio_metrics(self.cycles, self.through)
        expected = 100.0 * result.net_realized_pl / result.avg_capital * (365.0 / result.days_span)
        self.assertAlmostEqual(result.annualized_roc_pct, expected, places=6)
        # No stock in this fixture, so the premium-only figure equals the full-wheel one.
        self.assertAlmostEqual(result.annualized_roc_premium_pct, result.annualized_roc_pct, places=6)

    def test_ticker_summary_is_ranked_by_net_pl(self):
        rows = ticker_summary(self.cycles, self.through)
        self.assertEqual([row["underlying"] for row in rows], ["MU", "QQQ"])
        self.assertGreater(rows[0]["net_realized_pl"], rows[1]["net_realized_pl"])

    def test_cumulative_series_ends_at_the_portfolio_total(self):
        result = portfolio_metrics(self.cycles, self.through)
        series = realized_pl_series(self.cycles)
        self.assertAlmostEqual(series[-1]["cum_total_pl"], result.net_realized_pl, places=6)
        # The "premium" line is net of debits paid to close, not gross credits received.
        self.assertAlmostEqual(series[-1]["cum_option_pl"], result.option_realized_pl, places=6)
        self.assertLess(result.option_realized_pl, result.premium_received)

    def test_empty_input_is_safe(self):
        result = portfolio_metrics([], date(2025, 9, 26))
        self.assertEqual(result.cycles, 0)
        self.assertIsNone(result.annualized_roc_pct)
        self.assertIsNone(result.win_rate_pct)


class TestCapitalWindowContinuity(unittest.TestCase):
    """A date filter must crop the capital chart, not amputate its history.

    A position opened before the requested window is still committing real
    money once the window begins. Rebuilding cycles only from transactions
    inside the window (the naive approach) makes that position appear to spring
    from nothing the moment some *other* in-window trade happens to touch it --
    understating capital for as long as it takes. The fix rebuilds capital from
    the full, start-unrestricted history and only crops the result for display.
    """

    def setUp(self):
        # A put sold and assigned well before the window, then silence -- no
        # further trades on this ticker until long after the window opens.
        self.transactions = [
            tx("2025-01-15", STO, "-MU250201P100", -1, 2.00, 199.33, row_id=1),
            tx("2025-02-01", ASSIGNED, "-MU250201P100", 1, None, 0.0, row_id=2, as_of="2025-02-01"),
            tx("2025-09-10", STO, "-MU250915C120", -1, 1.50, 149.33, row_id=3),
        ]
        self.full_cycles, _ = build_cycles(self.transactions)
        self.through = date(2025, 9, 15)
        # A window that opens well after the assignment but before the next trade.
        self.since = date(2025, 6, 1)

    def _windowed_cycles(self):
        """What a naive implementation would rebuild from: only in-window trades."""
        windowed = [t for t in self.transactions if t.event_date >= self.since]
        cycles, _ = build_cycles(windowed)
        return cycles

    def test_naive_rebuild_loses_the_position(self):
        """Confirms the bug exists before asserting the fix.

        Rebuilt from only the in-window transactions, the only surviving trade
        is the September leg, so the cycle -- and its capital series -- doesn't
        start until then. The window's actual first day, and the $10,000 of
        stock genuinely held on it, isn't wrong in this series; it's just
        absent, which is worse. If this assertion ever stops holding,
        `build_cycles` changed and this whole test class needs review.
        """
        naive = portfolio_capital_series(self._windowed_cycles(), self.through)
        self.assertNotIn(self.since, [p.day for p in naive])
        self.assertEqual(naive[0].day, date(2025, 9, 10))

    def test_since_crops_without_losing_prior_state(self):
        series = portfolio_capital_series(self.full_cycles, self.through, since=self.since)
        self.assertEqual(series[0].day, self.since)
        # The 100 shares from the February assignment are still held on day one.
        self.assertAlmostEqual(series[0].stock_basis, 10000.0, places=2)
        self.assertGreater(series[0].total, 0.0)

    def test_since_none_is_a_no_op(self):
        cropped = portfolio_capital_series(self.full_cycles, self.through, since=None)
        uncropped = portfolio_capital_series(self.full_cycles, self.through)
        self.assertEqual([p.day for p in cropped], [p.day for p in uncropped])

    def test_portfolio_metrics_uses_capital_cycles_not_cycles(self):
        """`cycles` (P&L) stays window-scoped; `capital_cycles` supplies capital."""
        window_cycles = self._windowed_cycles()
        result = portfolio_metrics(
            window_cycles, self.through, capital_cycles=self.full_cycles, since=self.since
        )
        # avg/peak/current reflect the true, uncropped-history capital...
        self.assertGreater(result.avg_capital, 0.0)
        self.assertAlmostEqual(result.capital_deployed_now, 10000.0, places=2)
        # ...while P&L is still whatever `cycles` (the window-scoped set) says --
        # nothing closed in-window, so net realized P/L is zero, not the capital.
        self.assertEqual(result.net_realized_pl, 0.0)

    def test_portfolio_metrics_defaults_capital_cycles_to_cycles(self):
        """Callers that don't pass capital_cycles keep the old, single-set behaviour."""
        with_default = portfolio_metrics(self.full_cycles, self.through)
        explicit = portfolio_metrics(self.full_cycles, self.through, capital_cycles=self.full_cycles)
        self.assertEqual(with_default.avg_capital, explicit.avg_capital)
        self.assertEqual(with_default.capital_deployed_now, explicit.capital_deployed_now)

    def test_ticker_summary_uses_capital_cycles_per_ticker(self):
        window_cycles = self._windowed_cycles()
        rows = ticker_summary(
            window_cycles, self.through, capital_cycles=self.full_cycles, since=self.since
        )
        mu = next(row for row in rows if row["underlying"] == "MU")
        self.assertAlmostEqual(mu["capital_now"], 10000.0, places=2)
        self.assertGreater(mu["avg_capital"], 0.0)

    def test_dormant_ticker_still_appears_with_its_true_capital(self):
        """A ticker with zero trades anywhere in the window must not vanish.

        Push `since` past the September leg too, so MU has literally no
        transaction inside the window and therefore no P&L cycle at all -- the
        naive `for underlying, group in grouped.items()` used to simply skip it,
        which would drop a dormant-but-funded wheel from the P&L and ROC charts
        exactly when there is nothing else to explain where its capital went.
        """
        since = date(2025, 9, 11)  # after every transaction in setUp
        windowed = [t for t in self.transactions if t.event_date >= since]
        window_cycles, _ = build_cycles(windowed) if windowed else ([], None)
        self.assertEqual(window_cycles, [])  # confirms MU has no in-window cycle

        rows = ticker_summary(window_cycles, self.through, capital_cycles=self.full_cycles, since=since)
        mu = next((row for row in rows if row["underlying"] == "MU"), None)
        self.assertIsNotNone(mu, "a dormant-but-funded ticker must still get a row")
        self.assertEqual(mu["net_realized_pl"], 0.0)
        self.assertEqual(mu["cycles"], 0)
        self.assertGreater(mu["avg_capital"], 0.0)
        self.assertIsNotNone(mu["annualized_roc_pct"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
