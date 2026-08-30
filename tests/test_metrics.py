"""Metrics tests: collateral timeline, ROI denominators, annualization."""

from __future__ import annotations

import itertools
import os
import string
import sys
import unittest
from dataclasses import replace
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_engine import tx  # noqa: E402
from wheel.cashflow import dividend_transactions  # noqa: E402
from wheel.engine import COVERED_CALL, CSP, LONG_PUT, build_cycles  # noqa: E402
from wheel.metrics import (  # noqa: E402
    capital_timeline,
    cycle_metrics,
    dividends_by_cycle,
    net_adjusted_cost_basis,
    portfolio_capital_series,
    portfolio_metrics,
    realized_pl_series,
    ticker_summary,
    time_weighted_average,
    wheel_cash_flow_events,
    wheel_state_breakdown,
    wheel_terminal_value,
)
from wheel.parser import ASSIGNED, BTC, BTO, EXPIRED, OTHER, STC, STO  # noqa: E402


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

    def test_assigned_shares_with_open_covered_call_are_not_idle(self):
        cycles, _ = build_cycles(
            [
                tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 399.33, row_id=1),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=2, as_of="2025-11-20"),
                tx("2025-11-24", STO, "-MU251128C235", -1, 2.00, 199.33, row_id=3),
            ]
        )
        points = {point.day: point for point in capital_timeline(cycles[0], date(2025, 11, 25))}
        after_call = points[date(2025, 11, 25)]
        self.assertAlmostEqual(after_call.idle_stock_basis, 0.0)
        self.assertAlmostEqual(after_call.working_capital, after_call.total)

    def test_assigned_shares_with_no_covered_call_are_fully_idle(self):
        """The regression this field exists for: a hold with no call written
        against it is real, committed capital (still counted in `total`,
        still shown on "Capital deployed") but contributes nothing to the
        capital that's actually backing an open option -- `working_capital`
        must exclude every dollar of it, not just discount it.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 399.33, row_id=1),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=2, as_of="2025-11-20"),
            ]
        )
        points = {point.day: point for point in capital_timeline(cycles[0], date(2025, 11, 25))}
        held = points[date(2025, 11, 25)]
        self.assertAlmostEqual(held.stock_basis, 23000.0)
        self.assertAlmostEqual(held.idle_stock_basis, 23000.0)
        self.assertAlmostEqual(held.working_capital, 0.0)
        self.assertAlmostEqual(held.total, 23000.0)  # still fully counted here

    def test_a_fresh_csp_stays_working_alongside_an_idle_hold(self):
        """Same cycle, both at once: idle shares from an earlier assignment
        plus a brand-new CSP on a second lot. working_capital must reflect
        only the CSP side.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-09-19", STO, "-MU250926P230", -1, 4.00, 399.33, row_id=1),
                tx("2025-09-26", ASSIGNED, "-MU250926P230", 1, None, 0.0, row_id=2, as_of="2025-09-26"),
                tx("2025-10-01", STO, "-MU251101P220", -1, 3.00, 299.33, row_id=3),
            ]
        )
        points = {point.day: point for point in capital_timeline(cycles[0], date(2025, 10, 2))}
        point = points[date(2025, 10, 2)]
        self.assertAlmostEqual(point.idle_stock_basis, 23000.0)
        self.assertAlmostEqual(point.working_capital, 22000.0)  # the fresh CSP's collateral only
        self.assertAlmostEqual(point.total, 45000.0)

    def test_cycle_opened_after_through_contributes_no_points(self):
        """A cycle that opens two days after the requested `through` must not
        leak a single point at its own start date -- that previously happened
        via the ``end < cycle.start_date: end = cycle.start_date`` guard,
        which silently reported capital from beyond the caller's own horizon.
        This exact shape hit ``_build_net_worth()`` in production: the History
        export can be a few days fresher than the Positions snapshot, so a
        newly opened cycle would outrun the snapshot's own ``as_of`` date and
        get counted as "capital deployed as of the snapshot" when it hadn't
        even existed yet on that day.
        """
        cycles, _ = build_cycles([tx("2025-09-17", STO, "-JXN251016C50", -1, 4.20, 419.33)])
        points = capital_timeline(cycles[0], date(2025, 9, 15))
        self.assertEqual(points, [])

    def test_cycle_closing_after_through_stops_the_walk_at_through(self):
        """A cycle that closes after `through` (e.g. a same-day-fresher
        History export shows the close, but the caller only asked "as of"
        an earlier snapshot date) must stop reporting capital at `through`,
        not walk all the way to the real close date.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-09-15", STO, "-MU250926P150", -1, 3.35, 334.33, row_id=1),
                tx("2025-09-26", EXPIRED, "-MU250926P150", 1, None, 0.0, row_id=2, as_of="2025-09-26"),
            ]
        )
        points = capital_timeline(cycles[0], date(2025, 9, 20))
        self.assertEqual(points[-1].day, date(2025, 9, 20))
        self.assertAlmostEqual(points[-1].total, 15000.0)  # still fully committed at `through`


class TestSpreadCollateral(unittest.TestCase):
    def test_paired_leg_reports_netted_collateral_not_full_csp(self):
        """Short $100 put + long $95 put, both open the same day: collateral
        is the $500 strike distance, not the $10,000 full CSP collateral --
        and none of it leaks into put_collateral or long_premium.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-XYZ250201P100", -1, 3.00, 300.0, row_id=1),
                tx("2025-01-01", BTO, "-XYZ250201P95", 1, 1.00, -100.0, row_id=2),
            ]
        )
        points = capital_timeline(cycles[0], date(2025, 1, 2))
        point = points[0]
        self.assertAlmostEqual(point.spread_collateral, 500.0)  # (100-95)*100
        self.assertAlmostEqual(point.put_collateral, 0.0)
        self.assertAlmostEqual(point.long_premium, 0.0)
        self.assertAlmostEqual(point.total, 500.0)
        self.assertLess(point.total, 100 * 100)  # far less than the unpaired CSP collateral

    def test_unpaired_short_still_gets_full_collateral(self):
        """A short put with no same-day long partner is completely unaffected
        by spreads existing elsewhere in the codebase.
        """
        cycles, _ = build_cycles([tx("2025-01-01", STO, "-XYZ250201P100", -1, 3.00, 300.0, row_id=1)])
        points = capital_timeline(cycles[0], date(2025, 1, 2))
        self.assertAlmostEqual(points[0].put_collateral, 10000.0)
        self.assertAlmostEqual(points[0].spread_collateral, 0.0)
        self.assertAlmostEqual(points[0].total, 10000.0)

    def test_partial_pairing_splits_naked_and_spread_collateral(self):
        """2 short contracts, 1 long: 1 contract nets to spread collateral,
        1 stays a naked CSP -- the two must sum to less than the old
        (pre-spread) full 2-contract CSP collateral.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-XYZ250201P100", -2, 6.00, 600.0, row_id=1),
                tx("2025-01-01", BTO, "-XYZ250201P95", 1, 1.00, -100.0, row_id=2),
            ]
        )
        points = capital_timeline(cycles[0], date(2025, 1, 2))
        point = points[0]
        self.assertAlmostEqual(point.spread_collateral, 500.0)  # 1 contract paired
        self.assertAlmostEqual(point.put_collateral, 10000.0)  # 1 contract naked
        self.assertAlmostEqual(point.total, 10500.0)
        self.assertLess(point.total, 100 * 100 * 2)  # less than full 2-contract CSP collateral

    def test_spread_collateral_lapses_once_the_long_leg_closes_early(self):
        """The netting only holds while both legs are open -- once the long
        side closes, the short reverts to full naked collateral for whatever
        it has left.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-XYZ250301P100", -1, 3.00, 300.0, row_id=1),
                tx("2025-01-01", BTO, "-XYZ250301P95", 1, 1.00, -100.0, row_id=2),
                tx("2025-01-10", STC, "-XYZ250301P95", -1, 0.20, 20.0, row_id=3),
            ]
        )
        points = {point.day: point for point in capital_timeline(cycles[0], date(2025, 1, 15))}
        before = points[date(2025, 1, 5)]
        self.assertAlmostEqual(before.spread_collateral, 500.0)
        self.assertAlmostEqual(before.put_collateral, 0.0)

        after = points[date(2025, 1, 10)]
        self.assertAlmostEqual(after.spread_collateral, 0.0)
        self.assertAlmostEqual(after.put_collateral, 10000.0)  # reverted to full naked CSP

    def test_ambiguous_group_never_nets_and_stays_full_collateral(self):
        """One short, two same-day longs: no spread forms (see
        TestSpreadDetection), so collateral is completely unaffected --
        confirms the capital-timeline side of the conservative-when-ambiguous
        rule, not just the engine's own bookkeeping.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-XYZ250201P230", -1, 4.00, 400.0, row_id=1),
                tx("2025-01-01", BTO, "-XYZ250201P245", 1, 1.00, -100.0, row_id=2),
                tx("2025-01-01", BTO, "-XYZ250201P240", 1, 1.00, -100.0, row_id=3),
            ]
        )
        points = capital_timeline(cycles[0], date(2025, 1, 2))
        point = points[0]
        self.assertAlmostEqual(point.spread_collateral, 0.0)
        self.assertAlmostEqual(point.put_collateral, 23000.0)  # full naked CSP
        self.assertAlmostEqual(point.long_premium, 200.0)  # both longs, at their own debit


class TestNetAdjustedCostBasis(unittest.TestCase):
    def test_assignment_alone_nets_the_put_premium_against_strike(self):
        cycles, _ = build_cycles(
            [
                tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 399.33, row_id=1),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=2, as_of="2025-11-20"),
            ]
        )
        cycle = cycles[0]
        lot = cycle.share_lots[0]
        self.assertEqual(lot.basis_per_share, 230.0)  # tax basis: untouched
        expected = 230.0 - 399.33 / 100.0
        self.assertAlmostEqual(net_adjusted_cost_basis(cycle, lot), expected, places=4)

    def test_a_covered_call_sold_after_assignment_lowers_the_basis_further(self):
        cycles, _ = build_cycles(
            [
                tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 399.33, row_id=1),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=2, as_of="2025-11-20"),
                tx("2025-11-24", STO, "-MU251128C235", -1, 2.00, 199.33, row_id=3),
            ]
        )
        cycle = cycles[0]
        lot = cycle.share_lots[0]
        expected = 230.0 - (399.33 + 199.33) / 100.0
        self.assertAlmostEqual(net_adjusted_cost_basis(cycle, lot), expected, places=4)
        # Tax basis is unmoved by the call -- only the adjusted figure reacts.
        self.assertEqual(lot.basis_per_share, 230.0)

    def test_lower_net_cash_received_raises_the_adjusted_basis(self):
        """Two otherwise-identical assignments, one whose STO premium was
        eaten more by fees (lower ``amount``): the one that netted less cash
        must show the worse (higher) adjusted basis -- confirms fees are
        reflected exactly once (through the already fee-net ``amount``), not
        zero times and not twice.
        """
        higher_net, _ = build_cycles(
            [
                tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 399.33, row_id=1),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=2, as_of="2025-11-20"),
            ]
        )
        lower_net, _ = build_cycles(
            [
                tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 390.00, row_id=1, fees=9.33),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=2, as_of="2025-11-20"),
            ]
        )
        basis_higher_net = net_adjusted_cost_basis(higher_net[0], higher_net[0].share_lots[0])
        basis_lower_net = net_adjusted_cost_basis(lower_net[0], lower_net[0].share_lots[0])
        self.assertGreater(basis_lower_net, basis_higher_net)

    def test_concurrent_lots_split_the_cycles_net_premium_pro_rata(self):
        """Two partial assignments at different strikes, neither sold yet: the
        cycle's total net premium is allocated by share count, not "whichever
        leg happened to produce this lot" -- both lots get the same $/share
        adjustment here since both are 100-share lots.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-XYZ250201P100", -1, 3.00, 300.0, row_id=1),
                tx("2025-02-01", ASSIGNED, "-XYZ250201P100", 1, None, 0.0, row_id=2, as_of="2025-02-01"),
                tx("2025-01-01", STO, "-XYZ250301P105", -1, 2.00, 200.0, row_id=3),
                tx("2025-03-01", ASSIGNED, "-XYZ250301P105", 1, None, 0.0, row_id=4, as_of="2025-03-01"),
            ]
        )
        cycle = cycles[0]
        self.assertEqual(len(cycle.share_lots), 2)
        lot_100 = next(lot for lot in cycle.share_lots if lot.basis_per_share == 100.0)
        lot_105 = next(lot for lot in cycle.share_lots if lot.basis_per_share == 105.0)
        # (300 + 200) split 50/50 across two 100-share lots = 250 each.
        self.assertAlmostEqual(net_adjusted_cost_basis(cycle, lot_100), 100.0 - 250.0 / 100.0, places=4)
        self.assertAlmostEqual(net_adjusted_cost_basis(cycle, lot_105), 105.0 - 250.0 / 100.0, places=4)

    def test_unknown_basis_lot_returns_none(self):
        """A PRE_HISTORY lot (stock the export never saw bought) has no strike
        to net against -- None, not a fabricated number.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-09-15", STO, "-QQQ250917C588", -1, 3.32, 331.33, row_id=1),
                tx("2025-09-18", ASSIGNED, "-QQQ250917C588", 1, None, 0.0, row_id=2, as_of="2025-09-17"),
            ]
        )
        cycle = cycles[0]
        lot = next(lot for lot in cycle.share_lots if lot.basis_per_share is None)
        self.assertIsNone(net_adjusted_cost_basis(cycle, lot))


class TestDividendAttribution(unittest.TestCase):
    def test_dividend_transactions_filters_out_everything_else(self):
        rows = [
            tx("2025-01-15", "OTHER", "XYZ", 0, amount=25.0, row_id=1, action_raw="DIVIDEND RECEIVED XYZ"),
            tx("2025-01-16", "OTHER", "XYZ", 0, amount=-1.50, row_id=2, action_raw="FEE CHARGED"),
            tx("2025-01-17", STO, "-XYZ250201P100", -1, 3.00, 300.0, row_id=3),
        ]
        dividends = dividend_transactions(rows)
        self.assertEqual(len(dividends), 1)
        self.assertEqual(dividends[0].row_id, 1)
        self.assertEqual(dividends[0].action, OTHER)

    def test_dividend_inside_the_cycle_window_is_attributed(self):
        transactions = [
            tx("2025-01-01", STO, "-XYZ250201P100", -1, 3.00, 300.0, row_id=1),
            tx("2025-02-01", ASSIGNED, "-XYZ250201P100", 1, None, 0.0, row_id=2, as_of="2025-02-01"),
            tx("2025-02-15", "OTHER", "XYZ", 0, amount=12.50, row_id=3, action_raw="DIVIDEND RECEIVED XYZ"),
        ]
        cycles, _ = build_cycles(transactions)
        result = dividends_by_cycle(cycles, transactions)
        self.assertAlmostEqual(result[cycles[0].cycle_id], 12.50)

    def test_dividend_before_the_cycle_opened_is_not_attributed(self):
        transactions = [
            tx("2025-01-01", "OTHER", "XYZ", 0, amount=12.50, row_id=1, action_raw="DIVIDEND RECEIVED XYZ"),
            tx("2025-06-01", STO, "-XYZ250701P100", -1, 3.00, 300.0, row_id=2),
            tx("2025-07-01", EXPIRED, "-XYZ250701P100", 1, None, 0.0, row_id=3, as_of="2025-07-01"),
        ]
        cycles, _ = build_cycles(transactions)
        result = dividends_by_cycle(cycles, transactions)
        self.assertEqual(result, {})

    def test_dividend_for_a_different_ticker_is_ignored(self):
        transactions = [
            tx("2025-01-01", STO, "-XYZ250201P100", -1, 3.00, 300.0, row_id=1),
            tx("2025-02-01", EXPIRED, "-XYZ250201P100", 1, None, 0.0, row_id=2, as_of="2025-02-01"),
            tx("2025-01-15", "OTHER", "ABC", 0, amount=12.50, row_id=3, action_raw="DIVIDEND RECEIVED ABC"),
        ]
        cycles, _ = build_cycles(transactions)
        result = dividends_by_cycle(cycles, transactions)
        self.assertEqual(result, {})

    def test_dividend_on_the_cycles_last_day_is_still_included(self):
        """The window's upper bound is inclusive of end_date."""
        transactions = [
            tx("2025-01-01", STO, "-XYZ250201P100", -1, 3.00, 300.0, row_id=1),
            tx("2025-02-01", EXPIRED, "-XYZ250201P100", 1, None, 0.0, row_id=2, as_of="2025-02-01"),
            tx("2025-02-01", "OTHER", "XYZ", 0, amount=12.50, row_id=3, action_raw="DIVIDEND RECEIVED XYZ"),
        ]
        cycles, _ = build_cycles(transactions)
        self.assertEqual(cycles[0].end_date, date(2025, 2, 1))
        result = dividends_by_cycle(cycles, transactions)
        self.assertAlmostEqual(result[cycles[0].cycle_id], 12.50)

    def test_dividend_routes_to_the_correct_one_of_two_sequential_cycles(self):
        """Same ticker, two separate campaigns a year apart: each dividend must
        land in its own cycle's window, never the other's.
        """
        transactions = [
            tx("2025-01-01", STO, "-XYZ250201P100", -1, 3.00, 300.0, row_id=1),
            tx("2025-02-01", EXPIRED, "-XYZ250201P100", 1, None, 0.0, row_id=2, as_of="2025-02-01"),
            tx("2025-01-15", "OTHER", "XYZ", 0, amount=10.0, row_id=3, action_raw="DIVIDEND RECEIVED XYZ"),
            tx("2026-06-01", STO, "-XYZ260701P100", -1, 3.00, 300.0, row_id=4),
            tx("2026-07-01", EXPIRED, "-XYZ260701P100", 1, None, 0.0, row_id=5, as_of="2026-07-01"),
            tx("2026-06-15", "OTHER", "XYZ", 0, amount=20.0, row_id=6, action_raw="DIVIDEND RECEIVED XYZ"),
        ]
        cycles, _ = build_cycles(transactions)
        self.assertEqual(len(cycles), 2)
        result = dividends_by_cycle(cycles, transactions)
        self.assertAlmostEqual(result[cycles[0].cycle_id], 10.0)
        self.assertAlmostEqual(result[cycles[1].cycle_id], 20.0)


class TestDualTrackReturns(unittest.TestCase):
    def test_net_option_yield_uses_initial_collateral_not_the_time_weighted_average(self):
        """Same fixture as test_three_roi_denominators_are_distinct_when_size_changes:
        initial_collateral ($10,000) < avg_collateral, so Net Option Yield (on
        initial) must differ from the existing Wheel ROC (on the average).
        """
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-MU250131P100", -1, 1.00, 99.33, row_id=1),
                tx("2025-01-10", STO, "-MU250131P100", -3, 1.00, 299.01, row_id=2),
                tx("2025-01-31", EXPIRED, "-MU250131P100", 4, None, 0.0, row_id=3, as_of="2025-01-31"),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 1, 31))
        expected = 100.0 * metrics.option_realized_pl / metrics.initial_collateral
        self.assertAlmostEqual(metrics.net_option_yield_pct, expected, places=6)
        self.assertNotAlmostEqual(metrics.net_option_yield_pct, metrics.roi_on_avg_wheel_pct, places=2)

    def test_annualized_net_option_yield_scales_by_365_over_days_active(self):
        cycles, _ = build_cycles(
            [
                tx("2025-09-19", STO, "-MU250926P150", -1, 3.35, 334.33, row_id=1),
                tx("2025-09-26", EXPIRED, "-MU250926P150", 1, None, 0.0, row_id=2, as_of="2025-09-26"),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 9, 26))
        expected = metrics.net_option_yield_pct * (365.0 / metrics.days_active)
        self.assertAlmostEqual(metrics.annualized_net_option_yield_pct, expected, places=6)

    def test_total_position_roi_folds_in_stock_unrealized_and_dividends(self):
        cycles, _ = build_cycles(
            [
                tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 399.33, row_id=1),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=2, as_of="2025-11-20"),
            ]
        )
        cycle = cycles[0]
        through = date(2025, 12, 1)
        metrics = cycle_metrics(cycle, through, current_price=240.0, dividends=15.0)
        self.assertAlmostEqual(metrics.stock_unrealized_pl, (240.0 - 230.0) * 100)
        self.assertAlmostEqual(metrics.dividends_received, 15.0)
        expected_pl = metrics.option_realized_pl + metrics.stock_realized_pl + metrics.stock_unrealized_pl + 15.0
        expected_roi = 100.0 * expected_pl / metrics.initial_collateral
        self.assertAlmostEqual(metrics.total_position_roi_pct, expected_roi, places=6)

    def test_total_position_roi_defaults_ignore_unrealized_and_dividends(self):
        """No current_price/dividends supplied -- Total Position ROI must
        collapse to realized-only, never silently assume a gain.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 399.33, row_id=1),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=2, as_of="2025-11-20"),
            ]
        )
        cycle = cycles[0]
        metrics = cycle_metrics(cycle, date(2025, 12, 1))
        self.assertIsNone(metrics.stock_unrealized_pl)
        self.assertEqual(metrics.dividends_received, 0.0)
        expected_roi = 100.0 * (metrics.option_realized_pl + metrics.stock_realized_pl) / metrics.initial_collateral
        self.assertAlmostEqual(metrics.total_position_roi_pct, expected_roi, places=6)

    def test_stock_unrealized_pl_is_none_without_shares(self):
        cycles, _ = build_cycles(
            [
                tx("2025-09-19", STO, "-MU250926P150", -1, 3.35, 334.33, row_id=1),
                tx("2025-09-26", EXPIRED, "-MU250926P150", 1, None, 0.0, row_id=2, as_of="2025-09-26"),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 9, 26), current_price=200.0)
        self.assertIsNone(metrics.stock_unrealized_pl)

    def test_stock_unrealized_pl_is_none_without_a_price_even_with_shares_held(self):
        cycles, _ = build_cycles(
            [
                tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 399.33, row_id=1),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=2, as_of="2025-11-20"),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 12, 1))  # no current_price
        self.assertIsNone(metrics.stock_unrealized_pl)

    def test_long_leg_unrealized_pl_is_always_none(self):
        """Explicit placeholder -- mark-to-market for an open long option is
        out of scope; the field must never silently become a real number.
        """
        cycles, _ = build_cycles(
            [tx("2025-01-01", BTO, "-XYZ250601P100", 1, 10.00, -1000.0, row_id=1)]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 1, 31))
        self.assertIsNone(metrics.long_leg_unrealized_pl)


class TestCapitalSeriesGaps(unittest.TestCase):
    """A day with nothing live is a real zero, not a missing row.

    Anything plotting this series draws a straight line between consecutive
    points, so an absent stretch becomes a ramp asserting capital that was never
    committed.  Filtering to a single ticker used to leave months of it.
    """

    def gapped(self):
        """One cycle in 2025, a long flat stretch across year-end, another in
        2026 -- the year turn keeps them two cycles, so the portfolio series has
        a real months-long hole to fill.
        """
        return build_cycles(
            [
                tx("2025-01-01", STO, "-MU250110P100", -1, 1.00, 99.33, row_id=1),
                tx("2025-01-10", EXPIRED, "-MU250110P100", 1, None, 0.0, row_id=2, as_of="2025-01-10"),
                tx("2026-06-01", STO, "-MU260619P100", -1, 1.00, 99.33, row_id=3),
                tx("2026-06-19", EXPIRED, "-MU260619P100", 1, None, 0.0, row_id=4, as_of="2026-06-19"),
            ]
        )[0]

    def test_gap_days_are_emitted_as_zero(self):
        series = portfolio_capital_series(self.gapped(), date(2026, 6, 19))
        span = (series[-1].day - series[0].day).days + 1
        self.assertEqual(len(series), span)

        days = [point.day for point in series]
        self.assertEqual(days, sorted(days))
        self.assertEqual(len(set(days)), len(days))

        idle = next(p for p in series if p.day == date(2025, 9, 15))
        self.assertEqual(idle.total, 0.0)

    def test_gap_fill_does_not_extend_past_the_last_live_day(self):
        """Extending to `through` would rewrite capital_deployed_now."""
        series = portfolio_capital_series(self.gapped(), date(2026, 12, 31))
        self.assertEqual(series[-1].day, date(2026, 6, 19))
        self.assertEqual(series[0].day, date(2025, 1, 1))

    def test_gap_fill_leaves_average_peak_and_current_unchanged(self):
        """The fix is display-only.

        Compared against only the days a cycle was actually live -- which is
        exactly what this function used to return -- the average, the peak and
        the closing point are all identical.  The mean already skipped zero days
        and a maximum ignores them, so nothing downstream moves.
        """
        cycles = self.gapped()
        filled = portfolio_capital_series(cycles, date(2026, 6, 19))

        live = {point.day for cycle in cycles for point in capital_timeline(cycle, date(2026, 6, 19))}
        before = [point for point in filled if point.day in live]
        self.assertLess(len(before), len(filled))  # there really was a gap to fill

        self.assertAlmostEqual(
            time_weighted_average(filled), time_weighted_average(before), places=6
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
        # Wheel ROC, annualized = ROI x 365/7 (no stock in this cycle, so option P/L == net P/L)
        self.assertAlmostEqual(metrics.annualized_wheel_roc_pct, 2.2289 * 365 / 7, places=2)

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
        # No stock in this cycle, so option P/L == net P/L and the wheel variant
        # orders identically to the net-based ones.
        self.assertGreater(metrics.roi_pct, metrics.roi_on_avg_wheel_pct)
        self.assertGreater(metrics.roi_on_avg_wheel_pct, metrics.roi_on_peak_pct)

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
        # The Wheel ROC numerator is option P/L only -- the $500 stock gain from
        # the assignment/call-away round trip must not leak into it.
        expected_wheel_roc = (
            100.0 * metrics.option_realized_pl / metrics.avg_collateral * (365.0 / metrics.days_active)
        )
        self.assertAlmostEqual(metrics.annualized_wheel_roc_pct, expected_wheel_roc, places=6)

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
        expected = 100.0 * result.option_realized_pl / result.avg_capital * (365.0 / result.days_span)
        self.assertAlmostEqual(result.annualized_wheel_roc_pct, expected, places=6)

    def test_ticker_summary_is_ranked_by_net_pl(self):
        rows = ticker_summary(self.cycles, self.through)
        self.assertEqual([row["underlying"] for row in rows], ["MU", "QQQ"])
        self.assertGreater(rows[0]["net_realized_pl"], rows[1]["net_realized_pl"])

    def test_ticker_summary_days_span_reproduces_the_annualized_figure(self):
        """`days_span` is exposed so a caller (the dashboard's tooltip, in
        particular) can rebuild ``annualized_wheel_roc_pct`` from
        ``roi_on_avg_wheel_pct`` without guessing the annualizing factor.
        """
        rows = ticker_summary(self.cycles, self.through)
        for row in rows:
            self.assertIsNotNone(row["days_span"])
            self.assertGreaterEqual(row["days_span"], 1)
            expected = row["roi_on_avg_wheel_pct"] * (365.0 / row["days_span"])
            self.assertAlmostEqual(row["annualized_wheel_roc_pct"], expected, places=6)

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
        self.assertIsNone(result.annualized_wheel_roc_pct)
        self.assertIsNone(result.win_rate_pct)


class TestActiveCapital(unittest.TestCase):
    """`avg_active_capital`/`annualized_active_wheel_roc_pct`: the same Wheel
    ROC denominator, narrowed to capital actually backing an open put or
    covered call -- shares held with no covered call written against them
    (an idle hold after assignment) are real, counted capital in
    `avg_capital`, but must not count here.
    """

    def test_idle_holding_shares_dilute_avg_capital_but_not_avg_active_capital(self):
        """MU: CSP assigned (as of Nov 20 -- capital_timeline transitions on
        the assignment's ``as_of`` day, not the transaction row's own date),
        never gets a covered call -- idle from then on. QQQ: a separate CSP,
        open the whole window, providing a steady $5,000 of genuinely
        "working" capital every day so idle days register as diluted (some
        working capital > 0), not excluded entirely (which is what happens
        when NO capital is working that day -- see time_weighted_average's
        engaged-days filter).

        Total capital is a flat $28,000 for all 9 days (Nov 17-25 inclusive)
        -- assignment doesn't change the total, only what's backing it.
        Working capital is $28,000 for the 3 pre-assignment days (17-19) and
        just QQQ's $5,000 for the 6 days after (20-25):
        avg_active_capital = (3*28000 + 6*5000) / 9 = 12666.67, well below
        the flat $28,000 avg_capital.
        """
        transactions = [
            tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 399.33, row_id=1),
            tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=2, as_of="2025-11-20"),
            tx("2025-11-17", STO, "-QQQ251128P50", -1, 1.00, 99.33, row_id=3),
        ]
        cycles, _ = build_cycles(transactions)
        through = date(2025, 11, 25)
        result = portfolio_metrics(cycles, through)

        self.assertAlmostEqual(result.avg_capital, 28000.0, places=2)
        self.assertAlmostEqual(result.avg_active_capital, 114000.0 / 9, places=2)
        self.assertLess(result.avg_active_capital, result.avg_capital)

        self.assertAlmostEqual(
            result.active_capital_deployed_now, 5000.0, places=2
        )  # today: only QQQ's put is "working"
        self.assertAlmostEqual(result.peak_active_capital, 28000.0, places=2)  # the pre-assignment stretch

        expected_active_roc = (
            100.0 * result.option_realized_pl / result.avg_active_capital * (365.0 / result.days_span)
        )
        self.assertAlmostEqual(result.annualized_active_wheel_roc_pct, expected_active_roc, places=6)
        # Same numerator, smaller denominator -- the active figure must read
        # higher than the all-in one whenever idle holding-shares capital
        # exists to exclude.
        self.assertGreater(result.annualized_active_wheel_roc_pct, result.annualized_wheel_roc_pct)

    def test_no_idle_holding_shares_means_the_two_figures_match(self):
        """A portfolio with no idle holds (every held lot backs an open call,
        or nothing is held at all) has nothing to exclude -- active and
        all-in must be identical, not just close.
        """
        cycles, _ = build_cycles([tx("2025-09-19", STO, "-MU250926P150", -1, 3.35, 334.33)])
        result = portfolio_metrics(cycles, date(2025, 9, 26))
        self.assertAlmostEqual(result.avg_active_capital, result.avg_capital, places=6)
        self.assertAlmostEqual(result.annualized_active_wheel_roc_pct, result.annualized_wheel_roc_pct, places=6)


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
        self.assertIsNotNone(mu["annualized_wheel_roc_pct"])


class TestWheelROC(unittest.TestCase):
    """The Wheel ROC is an option-income metric: option P/L over time-weighted
    average capital, annualized. It must never be moved by stock P/L, realized
    or otherwise -- this codebase has no notion of unrealized/mark-to-market
    stock P/L at all (metrics.py only ever sums *realized* disposals), so
    "stock falls/rises while assigned" is exercised here as a realized gain or
    loss on the eventual disposal, which is the only form stock P/L takes here.
    """

    def test_csp_only(self):
        """$10,000 collateral, $300 CSP premium, 30 days -> ROC = 36.5%."""
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-XYZ250131P100", -1, 3.00, 300.0, row_id=1),
                tx("2025-01-31", EXPIRED, "-XYZ250131P100", 1, None, 0.0, row_id=2, as_of="2025-01-31"),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 1, 31))
        self.assertEqual(metrics.days_active, 30)
        self.assertAlmostEqual(metrics.avg_collateral, 10000.0)
        self.assertAlmostEqual(metrics.option_realized_pl, 300.0, places=2)
        self.assertAlmostEqual(metrics.annualized_wheel_roc_pct, 36.5, places=1)

    def test_csp_plus_covered_call_accrues_both_premiums_on_flat_capital(self):
        """30d CSP ($300) -> assignment -> 60d covered call ($300), $10,000 the
        whole way through. Wheel ROC = (300 + 300) / 10,000 * 365 / 90, with no
        stock price movement anywhere in this fixture.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-XYZ250131P100", -1, 3.00, 300.0, row_id=1),
                tx("2025-01-31", ASSIGNED, "-XYZ250131P100", 1, None, 0.0, row_id=2, as_of="2025-01-31"),
                tx("2025-01-31", STO, "-XYZ250401C110", -1, 3.00, 300.0, row_id=3),
                tx("2025-04-01", EXPIRED, "-XYZ250401C110", 1, None, 0.0, row_id=4, as_of="2025-04-01"),
            ]
        )
        through = date(2025, 4, 1)
        metrics = cycle_metrics(cycles[0], through)
        self.assertEqual(metrics.days_active, 90)
        self.assertAlmostEqual(metrics.avg_collateral, 10000.0)
        self.assertAlmostEqual(metrics.option_realized_pl, 600.0, places=2)
        self.assertAlmostEqual(metrics.stock_realized_pl, 0.0)  # shares never sold
        expected = 100.0 * 600.0 / 10000.0 * (365.0 / 90.0)
        self.assertAlmostEqual(metrics.annualized_wheel_roc_pct, expected, places=6)

    def _assigned_and_called_away(self, put_strike: float, call_strike: float):
        """One MU cycle: CSP assigned at ``put_strike``, then called away at
        ``call_strike`` -- the sign and size of the resulting stock P/L is
        controlled entirely by the strike spread.
        """
        return build_cycles(
            [
                tx("2025-11-17", STO, f"-MU251121P{put_strike:g}", -1, 4.00, 399.33, row_id=1),
                tx(
                    "2025-11-21",
                    ASSIGNED,
                    f"-MU251121P{put_strike:g}",
                    1,
                    None,
                    0.0,
                    row_id=2,
                    as_of="2025-11-20",
                ),
                tx("2025-11-24", STO, f"-MU251128C{call_strike:g}", -1, 2.00, 199.33, row_id=3),
                tx(
                    "2025-12-01",
                    ASSIGNED,
                    f"-MU251128C{call_strike:g}",
                    1,
                    None,
                    0.0,
                    row_id=4,
                    as_of="2025-11-28",
                ),
            ]
        )

    def test_assigned_stock_that_falls_does_not_touch_the_wheel_roc_numerator(self):
        """Called away below cost basis -- a realized stock loss -- must not
        make the Wheel ROC numerator anything but the $598.66 of option premium.
        """
        cycles, _ = self._assigned_and_called_away(put_strike=230, call_strike=225)
        metrics = cycle_metrics(cycles[0], date(2025, 11, 28))
        self.assertAlmostEqual(metrics.option_realized_pl, 598.66, places=2)
        self.assertLess(metrics.stock_realized_pl, 0.0)  # sold below cost basis
        expected = (
            100.0 * metrics.option_realized_pl / metrics.avg_collateral * (365.0 / metrics.days_active)
        )
        self.assertAlmostEqual(metrics.annualized_wheel_roc_pct, expected, places=6)

    def test_assigned_stock_that_rises_does_not_touch_the_wheel_roc_numerator(self):
        """Called away above cost basis -- a realized stock gain -- must not
        inflate the Wheel ROC numerator beyond the $598.66 of option premium.
        """
        cycles, _ = self._assigned_and_called_away(put_strike=230, call_strike=235)
        metrics = cycle_metrics(cycles[0], date(2025, 11, 28))
        self.assertAlmostEqual(metrics.option_realized_pl, 598.66, places=2)
        self.assertGreater(metrics.stock_realized_pl, 0.0)  # sold above cost basis
        expected = (
            100.0 * metrics.option_realized_pl / metrics.avg_collateral * (365.0 / metrics.days_active)
        )
        self.assertAlmostEqual(metrics.annualized_wheel_roc_pct, expected, places=6)

    def test_csp_plus_cc_plus_stock_loss_keeps_option_pl_separate_from_net(self):
        """CSP +$300ish, CC +$400ish, a $2,000+ realized stock loss on top: the
        wheel's option P/L must be the (positive) sum of the two premiums, never
        net_realized_pl, which the stock loss drags negative.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-06-02", STO, "-XYZ250704P200", -1, 3.00, 300.0, row_id=1),
                tx("2025-07-04", ASSIGNED, "-XYZ250704P200", 1, None, 0.0, row_id=2, as_of="2025-07-04"),
                # Strike is $20 below the $200 cost basis, so the eventual
                # call-away realizes a $2,000 stock loss.
                tx("2025-07-07", STO, "-XYZ250815C180", -1, 4.00, 400.0, row_id=3),
                tx("2025-08-15", ASSIGNED, "-XYZ250815C180", 1, None, 0.0, row_id=4, as_of="2025-08-15"),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 8, 15))
        self.assertAlmostEqual(metrics.option_realized_pl, 700.0, places=2)
        self.assertLess(metrics.net_realized_pl, 0.0)
        self.assertLess(metrics.net_realized_pl, metrics.option_realized_pl)
        self.assertGreater(metrics.annualized_wheel_roc_pct, 0.0)

    def test_multiple_wheel_cycles_accumulate_option_pl_and_capital_independently(self):
        """Two separate tickers, each a full CSP -> assignment -> covered-call
        -> called-away cycle with opposite-signed stock P/L, roll up so the
        portfolio's option P/L is the plain sum of both cycles' option P/L and
        the capital timeline is the sum of both, independent of what the stock
        did in either.
        """
        loss_cycles, _ = self._assigned_and_called_away(put_strike=230, call_strike=225)
        gain_transactions = [
            tx("2025-11-17", STO, "-QQQ251121P230", -1, 4.00, 399.33, row_id=101),
            tx("2025-11-21", ASSIGNED, "-QQQ251121P230", 1, None, 0.0, row_id=102, as_of="2025-11-20"),
            tx("2025-11-24", STO, "-QQQ251128C240", -1, 2.00, 199.33, row_id=103),
            tx("2025-12-01", ASSIGNED, "-QQQ251128C240", 1, None, 0.0, row_id=104, as_of="2025-11-28"),
        ]
        gain_cycles, _ = build_cycles(gain_transactions)

        through = date(2025, 11, 28)
        cycles = [loss_cycles[0], gain_cycles[0]]
        result = portfolio_metrics(cycles, through)

        per_cycle_option_pl = sum(cycle_metrics(c, through).option_realized_pl for c in cycles)
        self.assertAlmostEqual(result.option_realized_pl, per_cycle_option_pl, places=2)
        # The two stock legs move opposite directions; net_realized_pl reflects
        # that offset while option_realized_pl -- and the ROC built on it -- does not.
        self.assertNotAlmostEqual(result.net_realized_pl, result.option_realized_pl, places=2)
        expected_roc = (
            100.0 * result.option_realized_pl / result.avg_capital * (365.0 / result.days_span)
        )
        self.assertAlmostEqual(result.annualized_wheel_roc_pct, expected_roc, places=6)

    def test_covered_call_on_assigned_shares_adds_no_capital_to_the_metric(self):
        """The metrics-level denominator, not just the raw capital timeline,
        must hold at the $10,000 stock basis once a covered call is sold
        against already-tracked shares -- see also
        TestCapitalTimeline.test_covered_call_does_not_double_count_capital.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 399.33, row_id=1),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=2, as_of="2025-11-20"),
                tx("2025-11-24", STO, "-MU251128C235", -1, 2.00, 199.33, row_id=3),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 11, 25))
        self.assertAlmostEqual(metrics.avg_collateral, 23000.0, places=2)
        self.assertAlmostEqual(metrics.current_collateral, 23000.0, places=2)


def _synthetic_tickers(n: int) -> list[str]:
    """Pure-letter tickers (AAA, AAB, ...), so the OCC-symbol regex never
    confuses a ticker digit for part of the expiry date.
    """
    combos = itertools.product(string.ascii_uppercase, repeat=3)
    return ["".join(letters) for letters in itertools.islice(combos, n)]


def _build_legs(n_win: int = 0, n_loss: int = 0, n_open: int = 0, n_breakeven: int = 0):
    """One single-leg cycle per synthetic ticker, classified by construction:

    * win       -- sold for a credit, expires worthless (fully realized, positive)
    * loss      -- sold for a credit, bought back for more than that credit
    * breakeven -- sold for a credit, bought back for exactly that credit
    * open      -- sold for a credit, never closed

    Each ticker is independent, so none of these can merge into the same cycle.
    """
    tickers = iter(_synthetic_tickers(n_win + n_loss + n_open + n_breakeven))
    txs = []
    row_id = itertools.count(1)

    for _ in range(n_win):
        t = next(tickers)
        txs.append(tx("2025-01-01", STO, f"-{t}250131P100", -1, 2.00, 200.0, row_id=next(row_id)))
        txs.append(
            tx("2025-01-31", EXPIRED, f"-{t}250131P100", 1, None, 0.0, row_id=next(row_id), as_of="2025-01-31")
        )
    for _ in range(n_loss):
        t = next(tickers)
        txs.append(tx("2025-01-01", STO, f"-{t}250131P100", -1, 2.00, 100.0, row_id=next(row_id)))
        txs.append(tx("2025-01-15", BTC, f"-{t}250131P100", 1, 3.00, -300.0, row_id=next(row_id)))
    for _ in range(n_breakeven):
        t = next(tickers)
        txs.append(tx("2025-01-01", STO, f"-{t}250131P100", -1, 1.50, 150.0, row_id=next(row_id)))
        txs.append(tx("2025-01-15", BTC, f"-{t}250131P100", 1, 1.50, -150.0, row_id=next(row_id)))
    for _ in range(n_open):
        t = next(tickers)
        txs.append(tx("2025-01-01", STO, f"-{t}250131P100", -1, 2.00, 200.0, row_id=next(row_id)))

    cycles, _ = build_cycles(txs)
    return cycles


class TestWinRate(unittest.TestCase):
    """Win rate is a secondary, diagnostic figure -- frequency of profitable
    closed legs -- never the primary Wheel performance number (annualized
    Wheel ROC). It excludes open legs and exact break-evens from its
    denominator entirely, rather than counting either as a loss.
    """

    def _win_rate(self, **counts):
        cycles = _build_legs(**counts)
        return portfolio_metrics(cycles, date(2025, 1, 31))

    def test_winners_losers_and_open_legs(self):
        """109 winners, 21 losers, 7 open -- the worked example from the spec."""
        result = self._win_rate(n_win=109, n_loss=21, n_open=7)
        self.assertEqual(result.total_legs, 137)
        self.assertEqual(result.wins, 109)
        self.assertEqual(result.losses, 21)
        self.assertEqual(result.wins + result.losses, 130)  # classified, not 137
        self.assertAlmostEqual(result.win_rate_pct, 100.0 * 109 / 130, places=6)
        self.assertAlmostEqual(result.win_rate_pct, 83.8461538, places=5)

    def test_all_winners_is_100_percent(self):
        result = self._win_rate(n_win=10, n_open=2)
        self.assertEqual(result.total_legs, 12)
        self.assertAlmostEqual(result.win_rate_pct, 100.0)

    def test_all_losers_is_0_percent_not_none(self):
        result = self._win_rate(n_loss=10, n_open=2)
        self.assertEqual(result.total_legs, 12)
        # 0% (a real, decided result) must be distinguishable from "no data".
        self.assertIsNotNone(result.win_rate_pct)
        self.assertAlmostEqual(result.win_rate_pct, 0.0)

    def test_open_legs_only_is_none_not_zero(self):
        """No closed legs at all -- N/A, not 0%, since 0% implies losses existed."""
        result = self._win_rate(n_open=10)
        self.assertEqual(result.total_legs, 10)
        self.assertEqual(result.wins, 0)
        self.assertEqual(result.losses, 0)
        self.assertIsNone(result.win_rate_pct)

    def test_breakeven_legs_excluded_from_denominator(self):
        """10 winners, 10 losers, 5 exact break-evens -> 50%, not 10/25 = 40%."""
        result = self._win_rate(n_win=10, n_loss=10, n_breakeven=5)
        self.assertEqual(result.total_legs, 25)
        self.assertEqual(result.wins, 10)
        self.assertEqual(result.losses, 10)
        self.assertAlmostEqual(result.win_rate_pct, 50.0)

    def test_only_breakeven_legs_is_none(self):
        result = self._win_rate(n_breakeven=6)
        self.assertEqual(result.total_legs, 6)
        self.assertIsNone(result.win_rate_pct)

    def test_open_leg_excluded_from_rate_but_counted_in_total(self):
        """An unresolved leg (here: still open, the only "can't classify P/L"
        state this data model produces) must not read as a loss: one win and
        one open leg is still a 100% win rate over a total of two legs.
        """
        result = self._win_rate(n_win=1, n_open=1)
        self.assertEqual(result.total_legs, 2)
        self.assertEqual(result.wins, 1)
        self.assertEqual(result.losses, 0)
        self.assertAlmostEqual(result.win_rate_pct, 100.0)

    def test_csp_and_covered_call_legs_share_one_win_rate(self):
        """A short put and a short call -- CSP and covered-call strategies --
        both feed the same overall wins/losses tally; the model does not (and
        per spec should not) split them into separate win rates.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-AAA250131P100", -1, 2.00, 200.0, row_id=1),
                tx("2025-01-31", EXPIRED, "-AAA250131P100", 1, None, 0.0, row_id=2, as_of="2025-01-31"),
                tx("2025-01-01", STO, "-BBB250131C100", -1, 2.00, 100.0, row_id=3),
                tx("2025-01-15", BTC, "-BBB250131C100", 1, 3.00, -300.0, row_id=4),
            ]
        )
        strategies = {leg.strategy for cycle in cycles for leg in cycle.legs}
        self.assertEqual(strategies, {CSP, COVERED_CALL})

        result = portfolio_metrics(cycles, date(2025, 1, 31))
        self.assertEqual(result.wins, 1)
        self.assertEqual(result.losses, 1)
        self.assertAlmostEqual(result.win_rate_pct, 50.0)


class TestNonWheelExclusion(unittest.TestCase):
    """A cycle that only ever held long options keeps its P&L in the totals
    but is kept out of every wheel-framed ratio -- see ``Cycle.is_wheel``.
    """

    def _book(self):
        # One real wheel (CSP assigned -> covered call called away, +$1000) and
        # one lone directional call that expired worthless (-$400).
        return build_cycles(
            [
                tx("2025-01-02", STO, "-MU250117P100", -1, 3.0, 300.0, row_id=1),
                tx("2025-01-17", ASSIGNED, "-MU250117P100", 1, None, 0.0, row_id=2, as_of="2025-01-17"),
                tx("2025-01-20", STO, "-MU250221C110", -1, 2.0, 200.0, row_id=3),
                tx("2025-02-21", ASSIGNED, "-MU250221C110", 1, None, 0.0, row_id=4, as_of="2025-02-21"),
                tx("2025-03-03", BTO, "-NVDA250307C130", 4, 1.0, -400.0, row_id=5),
                tx("2025-03-07", EXPIRED, "-NVDA250307C130", -4, None, 0.0, row_id=6, as_of="2025-03-07"),
            ]
        )[0]

    def test_directional_loss_stays_in_realized_pl_totals(self):
        result = portfolio_metrics(self._book(), date(2025, 3, 31))
        # -400 directional + wheel option P&L are both in the total.
        self.assertLess(result.option_realized_pl, result.wheel_core_realized_pl)
        self.assertAlmostEqual(
            result.option_realized_pl, result.wheel_core_realized_pl + result.hedge_realized_pl, places=6
        )
        self.assertAlmostEqual(result.hedge_realized_pl, -400.0, places=2)

    def test_directional_cycle_excluded_from_wheel_roc_and_win_rate(self):
        book = self._book()
        full = portfolio_metrics(book, date(2025, 3, 31))
        wheel_only = portfolio_metrics([c for c in book if c.is_wheel], date(2025, 3, 31))
        # The ROC / win-rate / PPD are identical whether or not the directional
        # cycle is in the input -- it is filtered out internally either way.
        self.assertEqual(full.annualized_wheel_roc_pct, wheel_only.annualized_wheel_roc_pct)
        self.assertEqual(full.win_rate_pct, wheel_only.win_rate_pct)
        self.assertEqual(full.profit_per_day, wheel_only.profit_per_day)
        # ... but net realized P&L is NOT identical -- the -$400 only rides with the full book.
        self.assertNotAlmostEqual(full.net_realized_pl, wheel_only.net_realized_pl, places=2)

    def test_lone_directional_book_has_no_wheel_ratios(self):
        cycles, _ = build_cycles(
            [
                tx("2026-02-04", BTO, "-TQQQ260206C51", 3, 1.32, -398.02, row_id=1),
                tx("2026-02-06", EXPIRED, "-TQQQ260206C51", -3, None, 0.0, row_id=2, as_of="2026-02-06"),
            ]
        )
        result = portfolio_metrics(cycles, date(2026, 2, 28))
        self.assertIsNone(result.annualized_wheel_roc_pct)
        self.assertIsNone(result.roi_on_avg_wheel_pct)
        self.assertIsNone(result.win_rate_pct)
        self.assertAlmostEqual(result.profit_per_day, 0.0)
        self.assertAlmostEqual(result.net_realized_pl, -398.02, places=2)


class TestBuyAndHoldTickerHasNoWheelRatios(unittest.TestCase):
    """A ticker whose only activity is buying (and maybe selling) shares --
    never a put or call written against them -- keeps its capital and stock
    P&L on screen, but its premium-return ratios read N/A, not a misleading
    0%. A genuine wheel that merely sat idle in the window still reports them.
    """

    def test_shares_only_ticker_gets_none_for_wheel_ratios(self):
        cycles, _ = build_cycles(
            [tx("2025-01-06", "BUY_STOCK", "CRESY", 300, 8.0, -2400.0, row_id=1)]
        )
        row = next(r for r in ticker_summary(cycles, date(2025, 6, 30)) if r["underlying"] == "CRESY")
        self.assertGreater(row["avg_capital"], 0.0)  # still real, funded capital
        self.assertIsNone(row["annualized_wheel_roc_pct"])
        self.assertIsNone(row["roi_on_avg_wheel_pct"])
        self.assertIsNone(row["annualized_net_option_yield_pct"])
        self.assertIsNone(row["profit_per_day"])

    def test_assignment_row_with_no_option_leg_is_not_enough(self):
        """A stray ASSIGNED row with no CSP/CC leg behind it (an incomplete
        export, or a stock position with an odd corporate-action row) is not
        evidence the wheel was ever run -- ratios stay N/A.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-02-10", "BUY_STOCK", "DCH", 1000, 8.0, -8000.0, row_id=1),
                tx("2025-03-15", ASSIGNED, "-DCH250321P8", 1, None, 0.0, row_id=2, as_of="2025-03-15"),
            ]
        )
        row = next(r for r in ticker_summary(cycles, date(2025, 6, 30)) if r["underlying"] == "DCH")
        self.assertIsNone(row["annualized_wheel_roc_pct"])
        self.assertIsNone(row["profit_per_day"])

    def test_one_covered_call_makes_the_ratios_defined_again(self):
        cycles, _ = build_cycles(
            [
                tx("2025-01-06", "BUY_STOCK", "CRESY", 300, 8.0, -2400.0, row_id=1),
                tx("2025-02-03", STO, "-CRESY250321C9", -3, 0.40, 119.0, row_id=2),
            ]
        )
        row = next(r for r in ticker_summary(cycles, date(2025, 6, 30)) if r["underlying"] == "CRESY")
        self.assertIsNotNone(row["annualized_wheel_roc_pct"])
        self.assertIsNotNone(row["profit_per_day"])

    def test_dormant_real_wheel_still_reports_ratios(self):
        # CSP sold and assigned last year; the display window is later, so the
        # ticker has no in-window premium -- but it did run the wheel, so the
        # ratios stay defined (here 0%-ish, not None).
        cycles, _ = build_cycles(
            [
                tx("2025-01-02", STO, "-MU250117P100", -1, 3.0, 300.0, row_id=1),
                tx("2025-01-17", ASSIGNED, "-MU250117P100", 1, None, 0.0, row_id=2, as_of="2025-01-17"),
            ]
        )
        since = date(2025, 3, 1)
        row = next(
            r
            for r in ticker_summary([], date(2025, 6, 30), capital_cycles=cycles, since=since)
            if r["underlying"] == "MU"
        )
        self.assertIsNotNone(row["annualized_wheel_roc_pct"])


class TestHedgePL(unittest.TestCase):
    """Protective puts and credit-spread hedges are ordinary option legs to the
    engine -- LONG_PUT/LONG_CALL for the long side, CSP/COVERED_CALL for a
    spread's short side -- and were already summed into ``option_realized_pl``
    by the unfiltered ``sum(leg.realized_pl for leg in cycle.legs)``. These
    tests lock that in explicitly and exercise the new ``wheel_core_realized_pl``
    / ``hedge_realized_pl`` breakdown (CSP+covered-call vs. everything else).
    """

    def test_protective_put_sold_at_a_loss(self):
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", BTO, "-XYZ250201P100", 1, 10.00, -1000.0, row_id=1),
                tx("2025-01-10", STC, "-XYZ250201P100", -1, 7.00, 700.0, row_id=2),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 1, 10))
        self.assertEqual(cycles[0].legs[0].strategy, LONG_PUT)
        self.assertAlmostEqual(metrics.hedge_realized_pl, -300.0, places=2)
        self.assertAlmostEqual(metrics.wheel_core_realized_pl, 0.0)
        self.assertAlmostEqual(metrics.option_realized_pl, -300.0, places=2)

    def test_protective_put_sold_for_a_partial_recovery(self):
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", BTO, "-XYZ250201P100", 1, 10.00, -1000.0, row_id=1),
                tx("2025-01-10", STC, "-XYZ250201P100", -1, 4.00, 400.0, row_id=2),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 1, 10))
        self.assertAlmostEqual(metrics.hedge_realized_pl, -600.0, places=2)
        self.assertAlmostEqual(metrics.option_realized_pl, -600.0, places=2)

    def test_protective_put_sold_at_a_profit(self):
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", BTO, "-XYZ250201P100", 1, 5.00, -500.0, row_id=1),
                tx("2025-01-10", STC, "-XYZ250201P100", -1, 9.00, 900.0, row_id=2),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 1, 10))
        self.assertAlmostEqual(metrics.hedge_realized_pl, 400.0, places=2)
        self.assertAlmostEqual(metrics.option_realized_pl, 400.0, places=2)

    def test_protective_put_expires_worthless(self):
        """The realized loss is the full premium paid -- never $0 and never
        left dangling as an unrealized number once the leg is actually closed.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", BTO, "-XYZ250201P100", 1, 10.00, -1000.0, row_id=1),
                tx("2025-02-01", EXPIRED, "-XYZ250201P100", 1, None, 0.0, row_id=2, as_of="2025-02-01"),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 2, 1))
        self.assertAlmostEqual(metrics.hedge_realized_pl, -1000.0, places=2)
        self.assertAlmostEqual(metrics.option_realized_pl, -1000.0, places=2)

    def test_credit_spread_closes_for_a_profit(self):
        """Short leg (CSP) sold for $300, long leg (LONG_PUT) bought for $100,
        both expire worthless -- net spread P/L +$200, split $300 wheel-core /
        -$100 hedge, but summed the same either way.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-XYZ250201P100", -1, 3.00, 300.0, row_id=1),
                tx("2025-01-01", BTO, "-XYZ250201P95", 1, 1.00, -100.0, row_id=2),
                tx("2025-02-01", EXPIRED, "-XYZ250201P100", 1, None, 0.0, row_id=3, as_of="2025-02-01"),
                tx("2025-02-01", EXPIRED, "-XYZ250201P95", 1, None, 0.0, row_id=4, as_of="2025-02-01"),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 2, 1))
        self.assertAlmostEqual(metrics.wheel_core_realized_pl, 300.0, places=2)
        self.assertAlmostEqual(metrics.hedge_realized_pl, -100.0, places=2)
        self.assertAlmostEqual(metrics.option_realized_pl, 200.0, places=2)

    def test_credit_spread_closes_for_a_loss(self):
        """Short leg costs more to close than it collected; long leg recovers
        only part of its cost -- net spread P/L -$400.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-XYZ250201P100", -1, 5.00, 500.0, row_id=1),
                tx("2025-01-01", BTO, "-XYZ250201P95", 1, 3.00, -300.0, row_id=2),
                tx("2025-01-20", BTC, "-XYZ250201P100", 1, 7.00, -700.0, row_id=3),
                tx("2025-01-20", STC, "-XYZ250201P95", -1, 1.00, 100.0, row_id=4),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 1, 20))
        self.assertAlmostEqual(metrics.wheel_core_realized_pl, -200.0, places=2)  # 500 - 700
        self.assertAlmostEqual(metrics.hedge_realized_pl, -200.0, places=2)  # 100 - 300
        self.assertAlmostEqual(metrics.option_realized_pl, -400.0, places=2)

    def test_multiple_protective_puts_accumulate(self):
        """Two separate hedges over the life of one wheel: -$400 then -$300,
        for a combined -$700 -- not the cost of either one alone.

        An anchor leg that never closes keeps both hedges in the same cycle
        (a cycle closes once it goes flat -- see wheel/engine.py); its own
        contribution to every realized-P/L figure here is exactly $0.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-XYZ250601P80", -1, 0.50, 50.0, row_id=0),
                tx("2025-01-01", BTO, "-XYZ250201P100", 1, 10.00, -1000.0, row_id=1),
                tx("2025-01-10", STC, "-XYZ250201P100", -1, 6.00, 600.0, row_id=2),
                tx("2025-01-15", BTO, "-XYZ250301P100", 1, 8.00, -800.0, row_id=3),
                tx("2025-01-25", STC, "-XYZ250301P100", -1, 5.00, 500.0, row_id=4),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 1, 25))
        self.assertAlmostEqual(metrics.hedge_realized_pl, -700.0, places=2)
        self.assertAlmostEqual(metrics.option_realized_pl, -700.0, places=2)

    def test_multiple_credit_spreads_accumulate(self):
        """Two independent spreads: the first nets +$150, the second -$50 --
        combined option P/L is their sum, correctly split across the four legs.
        Anchored open so both spreads land in the same cycle (see above).
        """
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-XYZ250601P80", -1, 0.50, 50.0, row_id=0),
                # Spread 1: short +250, long -100 -> net +150.
                tx("2025-01-01", STO, "-XYZ250201P100", -1, 2.50, 250.0, row_id=1),
                tx("2025-01-01", BTO, "-XYZ250201P95", 1, 1.00, -100.0, row_id=2),
                tx("2025-02-01", EXPIRED, "-XYZ250201P100", 1, None, 0.0, row_id=3, as_of="2025-02-01"),
                tx("2025-02-01", EXPIRED, "-XYZ250201P95", 1, None, 0.0, row_id=4, as_of="2025-02-01"),
                # Spread 2: short +200, long -250 -> net -50.
                tx("2025-02-05", STO, "-XYZ250301P100", -1, 2.00, 200.0, row_id=5),
                tx("2025-02-05", BTO, "-XYZ250301P95", 1, 2.50, -250.0, row_id=6),
                tx("2025-03-01", EXPIRED, "-XYZ250301P100", 1, None, 0.0, row_id=7, as_of="2025-03-01"),
                tx("2025-03-01", EXPIRED, "-XYZ250301P95", 1, None, 0.0, row_id=8, as_of="2025-03-01"),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 3, 1))
        # The anchor leg never closes, so it contributes $0 to realized P/L --
        # only capital and leg counts, not this sum.
        self.assertAlmostEqual(metrics.wheel_core_realized_pl, 450.0, places=2)  # 250 + 200
        self.assertAlmostEqual(metrics.hedge_realized_pl, -350.0, places=2)  # -100 + -250
        self.assertAlmostEqual(metrics.option_realized_pl, 100.0, places=2)  # 150 + -50

    def test_open_protective_put_excluded_from_realized_roc(self):
        """An open hedge contributes nothing to realized P/L -- its cost only
        counts once the position actually closes -- but it does show up as
        committed capital while open (the *actual* debit paid, not notional).
        """
        cycles, _ = build_cycles(
            [tx("2025-01-01", BTO, "-XYZ250601P100", 1, 10.00, -1000.0, row_id=1)]
        )
        through = date(2025, 1, 31)
        metrics = cycle_metrics(cycles[0], through)
        self.assertAlmostEqual(metrics.hedge_realized_pl, 0.0)
        self.assertAlmostEqual(metrics.option_realized_pl, 0.0)
        # A lone long put is not a wheel (no CSP/covered call, no shares, no
        # assignment), so wheel-framed ratios are withheld entirely.
        self.assertFalse(metrics.is_wheel)
        self.assertIsNone(metrics.annualized_wheel_roc_pct)
        self.assertIsNone(metrics.roi_on_avg_wheel_pct)

        points = capital_timeline(cycles[0], through)
        self.assertAlmostEqual(points[0].long_premium, 1000.0)
        self.assertAlmostEqual(points[0].total, 1000.0)

    def test_csp_plus_protective_put_plus_covered_call(self):
        """+$400 CSP, -$300 hedge, +$500 covered call -> $600 total, $900 of
        it wheel-core and -$300 of it hedge.

        An anchor leg that never closes keeps all three in one cycle (see
        ``test_multiple_protective_puts_accumulate``) -- it contributes $0 to
        every realized figure below.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-XYZ250601P80", -1, 0.50, 50.0, row_id=0),
                tx("2025-01-01", STO, "-XYZ250201P100", -1, 4.00, 400.0, row_id=1),
                tx("2025-02-01", EXPIRED, "-XYZ250201P100", 1, None, 0.0, row_id=2, as_of="2025-02-01"),
                tx("2025-01-01", BTO, "-XYZ250201P90", 1, 10.00, -1000.0, row_id=3),
                tx("2025-01-20", STC, "-XYZ250201P90", -1, 7.00, 700.0, row_id=4),
                tx("2025-02-05", STO, "-XYZ250305C110", -1, 5.00, 500.0, row_id=5),
                tx("2025-03-05", EXPIRED, "-XYZ250305C110", 1, None, 0.0, row_id=6, as_of="2025-03-05"),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 3, 5))
        self.assertAlmostEqual(metrics.wheel_core_realized_pl, 900.0, places=2)
        self.assertAlmostEqual(metrics.hedge_realized_pl, -300.0, places=2)
        self.assertAlmostEqual(metrics.option_realized_pl, 600.0, places=2)

    def test_end_to_end_csp_put_spread_and_cc_matches_worked_example(self):
        """The full scenario from the design doc: CSP +$400, protective put
        net -$300, covered call +$500, credit spread net +$200 -> $800 total.
        Annualized Wheel ROC is then whatever the formula gives on this
        cycle's own capital/duration -- checked against the same formula the
        implementation uses, not a hand-picked capital figure. An anchor leg
        again keeps all four instruments in one cycle.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-XYZ250601P80", -1, 0.50, 50.0, row_id=0),
                tx("2025-01-01", STO, "-XYZ250201P100", -1, 4.00, 400.0, row_id=1),
                tx("2025-02-01", EXPIRED, "-XYZ250201P100", 1, None, 0.0, row_id=2, as_of="2025-02-01"),
                tx("2025-01-01", BTO, "-XYZ250201P90", 1, 10.00, -1000.0, row_id=3),
                tx("2025-01-20", STC, "-XYZ250201P90", -1, 7.00, 700.0, row_id=4),
                tx("2025-02-05", STO, "-XYZ250305C110", -1, 5.00, 500.0, row_id=5),
                tx("2025-03-05", EXPIRED, "-XYZ250305C110", 1, None, 0.0, row_id=6, as_of="2025-03-05"),
                tx("2025-03-06", STO, "-XYZ250401P100", -1, 3.00, 300.0, row_id=7),
                tx("2025-03-06", BTO, "-XYZ250401P95", 1, 1.00, -100.0, row_id=8),
                tx("2025-04-01", EXPIRED, "-XYZ250401P100", 1, None, 0.0, row_id=9, as_of="2025-04-01"),
                tx("2025-04-01", EXPIRED, "-XYZ250401P95", 1, None, 0.0, row_id=10, as_of="2025-04-01"),
            ]
        )
        through = date(2025, 4, 1)
        metrics = cycle_metrics(cycles[0], through)
        self.assertAlmostEqual(metrics.option_realized_pl, 800.0, places=2)

        expected_roc = (
            100.0 * metrics.option_realized_pl / metrics.avg_collateral * (365.0 / metrics.days_active)
        )
        self.assertAlmostEqual(metrics.annualized_wheel_roc_pct, expected_roc, places=6)

        # The formula itself, on the design doc's own $10,000 / 90-day figures,
        # independent of whatever capital this particular fixture produces.
        self.assertAlmostEqual(100.0 * 800.0 / 10000.0 * (365.0 / 90.0), 32.44, places=2)

    def test_multi_leg_spread_does_not_double_count(self):
        """Two legs, two cash flows each -- exactly four realized numbers sum
        to the net spread P/L; nothing is counted an extra time.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-XYZ250201P100", -1, 3.00, 300.0, row_id=1),
                tx("2025-01-01", BTO, "-XYZ250201P95", 1, 1.00, -100.0, row_id=2),
                tx("2025-01-15", BTC, "-XYZ250201P100", 1, 1.00, -100.0, row_id=3),
                tx("2025-01-15", STC, "-XYZ250201P95", -1, 0.20, 20.0, row_id=4),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 1, 15))
        short_leg_pl = 300.0 - 100.0  # 200
        long_leg_pl = -100.0 + 20.0  # -80
        self.assertAlmostEqual(metrics.wheel_core_realized_pl, short_leg_pl, places=2)
        self.assertAlmostEqual(metrics.hedge_realized_pl, long_leg_pl, places=2)
        self.assertAlmostEqual(metrics.option_realized_pl, short_leg_pl + long_leg_pl, places=2)
        # Not the sum of the raw cash flows counted independently (300 - 100 -
        # 100 + 20 double-counted some other way, or the opening/closing legs
        # mistaken for four unrelated trades): exactly leg_1 + leg_2.
        self.assertNotAlmostEqual(metrics.option_realized_pl, 300.0 - 100.0 + 100.0 - 20.0, places=2)

    def test_hedge_pl_moves_roc_but_stock_pl_does_not(self):
        """A hedge loss on top of an assignment that later profits on the
        stock: the Wheel ROC numerator must move with the hedge, not the
        stock gain -- mirroring TestWheelROC's assigned-stock tests, but with
        a hedge leg layered on.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 399.33, row_id=1),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=2, as_of="2025-11-20"),
                tx("2025-11-17", BTO, "-MU251205P210", 1, 3.00, -300.0, row_id=3),
                tx("2025-11-24", STC, "-MU251205P210", -1, 1.00, 100.0, row_id=4),
                tx("2025-11-24", STO, "-MU251128C235", -1, 2.00, 199.33, row_id=5),
                tx("2025-12-01", ASSIGNED, "-MU251128C235", 1, None, 0.0, row_id=6, as_of="2025-11-28"),
            ]
        )
        metrics = cycle_metrics(cycles[0], date(2025, 11, 28))
        self.assertAlmostEqual(metrics.hedge_realized_pl, -200.0, places=2)  # -300 + 100
        self.assertAlmostEqual(metrics.wheel_core_realized_pl, 598.66, places=2)  # 399.33 + 199.33
        self.assertAlmostEqual(metrics.option_realized_pl, 398.66, places=2)
        self.assertGreater(metrics.stock_realized_pl, 0.0)  # the assignment/call-away round trip gained
        # ROC reflects the hedge loss and the option gains, never the stock gain.
        expected_roc = (
            100.0 * metrics.option_realized_pl / metrics.avg_collateral * (365.0 / metrics.days_active)
        )
        self.assertAlmostEqual(metrics.annualized_wheel_roc_pct, expected_roc, places=6)
        self.assertNotAlmostEqual(
            metrics.annualized_wheel_roc_pct,
            100.0 * metrics.net_realized_pl / metrics.avg_collateral * (365.0 / metrics.days_active),
            places=2,
        )

    def test_hedge_premium_is_not_notional_and_clears_on_close(self):
        """Capital for a $50-strike protective put (notional $5,000) is the
        $320 actually paid for it, not $5,000 -- and it drops to $0 the day
        the leg closes, because by then its cost lives in realized P/L instead.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", BTO, "-XYZ250201P50", 1, 3.20, -320.0, row_id=1),
                tx("2025-01-15", STC, "-XYZ250201P50", -1, 1.00, 100.0, row_id=2),
            ]
        )
        points = {point.day: point for point in capital_timeline(cycles[0], date(2025, 1, 20))}
        self.assertAlmostEqual(points[date(2025, 1, 2)].long_premium, 320.0, places=2)
        self.assertLess(points[date(2025, 1, 2)].long_premium, 50 * 100)  # nowhere near notional
        self.assertAlmostEqual(points[date(2025, 1, 15)].long_premium, 0.0, places=2)  # closed
        metrics = cycle_metrics(cycles[0], date(2025, 1, 20))
        self.assertAlmostEqual(metrics.hedge_realized_pl, -220.0, places=2)  # -320 + 100


class TestWheelStateBreakdown(unittest.TestCase):
    """``wheel_state_breakdown`` must be called on the *capital*-scoped cycle
    set (``capital_cycles`` in wheel/api.py), never the P&L/date-filtered
    ``cycles`` -- these tests exercise the classification and aggregation in
    isolation, independent of that scoping choice (which lives in api.py's
    wiring, not here).
    """

    def test_naked_csp_lands_entirely_in_puts(self):
        cycles, _ = build_cycles([tx("2025-09-19", STO, "-MU250926P150", -1, 3.35, 334.33)])
        result = wheel_state_breakdown(cycles, date(2025, 9, 20))
        self.assertAlmostEqual(result["buckets"]["puts"]["amount"], 15000.0, places=2)
        self.assertEqual(result["buckets"]["puts"]["cycles"], 1)
        self.assertEqual(result["buckets"]["puts"]["tickers"], ["MU"])
        self.assertAlmostEqual(result["buckets"]["calls"]["amount"], 0.0)
        self.assertAlmostEqual(result["buckets"]["holding"]["amount"], 0.0)
        self.assertAlmostEqual(result["buckets"]["other"]["amount"], 0.0)
        self.assertEqual(result["active_cycles"], 1)

    def test_assignment_with_open_covered_call_routes_stock_basis_to_calls(self):
        cycles, _ = build_cycles(
            [
                tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 399.33, row_id=1),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=2, as_of="2025-11-20"),
                tx("2025-11-24", STO, "-MU251128C235", -1, 2.00, 199.33, row_id=3),
            ]
        )
        result = wheel_state_breakdown(cycles, date(2025, 11, 25))
        self.assertAlmostEqual(result["buckets"]["calls"]["amount"], 23000.0, places=2)
        self.assertAlmostEqual(result["buckets"]["holding"]["amount"], 0.0)
        self.assertAlmostEqual(result["buckets"]["puts"]["amount"], 0.0)
        self.assertEqual(result["active_cycles"], 1)

    def test_assignment_without_open_covered_call_lands_in_holding(self):
        cycles, _ = build_cycles(
            [
                tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 399.33, row_id=1),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=2, as_of="2025-11-20"),
            ]
        )
        result = wheel_state_breakdown(cycles, date(2025, 11, 25))
        self.assertAlmostEqual(result["buckets"]["holding"]["amount"], 23000.0, places=2)
        self.assertAlmostEqual(result["buckets"]["calls"]["amount"], 0.0)

    def test_covered_call_on_untracked_shares_uses_strike_proxy(self):
        cycles, _ = build_cycles([tx("2025-09-15", STO, "-QQQ251017C588", -1, 3.32, 331.33)])
        result = wheel_state_breakdown(cycles, date(2025, 9, 16))
        self.assertAlmostEqual(result["buckets"]["calls"]["amount"], 58800.0, places=2)
        self.assertEqual(result["buckets"]["calls"]["tickers"], ["QQQ"])

    def test_protective_put_lands_in_other(self):
        cycles, _ = build_cycles([tx("2025-01-01", BTO, "-XYZ250601P100", 1, 10.00, -1000.0)])
        result = wheel_state_breakdown(cycles, date(2025, 1, 2))
        self.assertAlmostEqual(result["buckets"]["other"]["amount"], 1000.0, places=2)
        self.assertAlmostEqual(result["buckets"]["puts"]["amount"], 0.0)
        self.assertAlmostEqual(result["buckets"]["calls"]["amount"], 0.0)
        self.assertAlmostEqual(result["buckets"]["holding"]["amount"], 0.0)

    def test_closed_cycle_contributes_nothing(self):
        cycles, _ = build_cycles(
            [
                tx("2025-09-19", STO, "-MU250926P150", -1, 3.35, 334.33, row_id=1),
                tx("2025-09-26", EXPIRED, "-MU250926P150", 1, None, 0.0, row_id=2, as_of="2025-09-26"),
            ]
        )
        result = wheel_state_breakdown(cycles, date(2025, 9, 27))
        self.assertEqual(result["active_cycles"], 0)
        for bucket in result["buckets"].values():
            self.assertAlmostEqual(bucket["amount"], 0.0)

    def test_held_shares_and_a_fresh_csp_split_across_two_buckets_in_one_cycle(self):
        """The regression this function exists to prevent: a cycle that is
        classified whole (rather than per capital-component) would hide one
        side entirely. Here the same MU cycle holds shares from an earlier
        assignment (no covered call written against them -- Holding Shares)
        *and* has a brand-new CSP open on a second lot at a different strike
        (Cash-Secured Puts) at the same time. Both must show up, from the one
        cycle, simultaneously.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-09-19", STO, "-MU250926P230", -1, 4.00, 399.33, row_id=1),
                tx("2025-09-26", ASSIGNED, "-MU250926P230", 1, None, 0.0, row_id=2, as_of="2025-09-26"),
                tx("2025-10-01", STO, "-MU251101P220", -1, 3.00, 299.33, row_id=3),
            ]
        )
        result = wheel_state_breakdown(cycles, date(2025, 10, 2))
        self.assertAlmostEqual(result["buckets"]["holding"]["amount"], 23000.0, places=2)
        self.assertAlmostEqual(result["buckets"]["puts"]["amount"], 22000.0, places=2)
        self.assertEqual(result["buckets"]["holding"]["cycles"], 1)
        self.assertEqual(result["buckets"]["puts"]["cycles"], 1)
        # One cycle, contributing to two buckets -- not double-counted here.
        self.assertEqual(result["active_cycles"], 1)


class TestWheelCashFlowEvents(unittest.TestCase):
    """wheel_cash_flow_events/wheel_terminal_value: the wheel's own dated
    contribution/withdrawal timeline, feeding a money-weighted (XIRR) return
    isolated from every other holding in the account. Sign convention: a
    contribution (capital committed) is positive, a withdrawal (capital plus
    whatever it earned or lost, returned) is negative -- matching
    wheel.benchmark.CashFlowEvent.
    """

    def test_expired_csp_is_one_contribution_and_one_withdrawal(self):
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-XYZ250131P100", -1, 3.00, 300.0, row_id=1),
                tx("2025-01-31", EXPIRED, "-XYZ250131P100", 1, None, 0.0, row_id=2, as_of="2025-01-31"),
            ]
        )
        events = wheel_cash_flow_events(cycles)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0], (date(2025, 1, 1), 10000.0, "XYZ CSP open"))
        # Collateral released ($10,000) + premium kept ($300) = $10,300.
        self.assertEqual(events[1], (date(2025, 1, 31), -10300.0, "XYZ CSP close"))
        self.assertAlmostEqual(wheel_terminal_value(cycles, date(2025, 1, 31), {}), 0.0)

    def test_open_csp_is_only_a_contribution_with_no_close(self):
        cycles, _ = build_cycles([tx("2025-01-01", STO, "-XYZ250201P100", -1, 3.00, 300.0)])
        events = wheel_cash_flow_events(cycles)
        self.assertEqual(events, [(date(2025, 1, 1), 10000.0, "XYZ CSP open")])
        # Still committed -- shows up in terminal value, not as a withdrawal.
        self.assertAlmostEqual(wheel_terminal_value(cycles, date(2025, 1, 15), {}), 10000.0)

    def test_full_round_trip_loses_no_premium_across_the_assignment(self):
        """The regression this test exists for: an earlier version of
        wheel_cash_flow_events skipped BOTH the put's and the covered call's
        assignment-closes entirely (to avoid double-counting the stock's
        capital), which silently discarded their premium along with it. The
        capital must be skipped (it converts to a ShareLot, not withdrawn),
        but the premium is real, realized income and must still appear.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-XYZ250201P100", -1, 4.00, 400.0, row_id=1),
                tx("2025-02-01", ASSIGNED, "-XYZ250201P100", 1, None, 0.0, row_id=2, as_of="2025-02-01"),
                tx("2025-02-05", STO, "-XYZ250305C105", -1, 2.00, 200.0, row_id=3),
                tx("2025-03-05", ASSIGNED, "-XYZ250305C105", 1, None, 0.0, row_id=4, as_of="2025-03-05"),
            ]
        )
        events = wheel_cash_flow_events(cycles)
        self.assertEqual(
            events,
            [
                (date(2025, 1, 1), 10000.0, "XYZ CSP open"),
                (date(2025, 2, 1), -400.0, "XYZ CSP close"),  # premium only -- capital converts
                (date(2025, 3, 5), -200.0, "XYZ COVERED_CALL close"),  # premium only -- $0 collateral
                (date(2025, 3, 5), -10500.0, "XYZ shares sold"),  # the converted capital, finally returned
            ],
        )
        contributions = sum(amount for _, amount, _ in events if amount > 0)
        withdrawals = sum(-amount for _, amount, _ in events if amount < 0)
        # $400 put premium + $200 call premium + $500 stock gain (sold at
        # $105, assigned at $100, 100 shares) = $1,100 net.
        self.assertAlmostEqual(withdrawals - contributions, 1100.0, places=2)
        self.assertAlmostEqual(wheel_terminal_value(cycles, date(2025, 3, 5), {}), 0.0)

    def test_brokers_own_settlement_row_does_not_double_the_contribution(self):
        """The regression this test exists for: a real Fidelity export almost
        always carries an explicit "YOU BOUGHT ASSIGNED PUTS ..." stock
        row alongside the option's own ASSIGNED notification -- so
        WheelEngine routes the share lot through the ordinary stock-purchase
        path (to avoid double-booking the *shares*), which tags it
        ``FROM_PURCHASE`` even though the cash is the same dollars as the
        put's own contribution. Trusting ``ShareLot.source`` alone here
        would count that contribution twice -- once for the put's open,
        once again for the "purchase" -- inflating total contributions well
        past total withdrawals and producing a deeply, wrongly negative
        XIRR. See ``_assignment_lot_dates``.
        """

        def settlement(day, action, symbol, shares, price, amount, row_id, as_of):
            row = tx(day, action, symbol, shares, price, amount, row_id=row_id, as_of=as_of)
            return replace(row, assignment_settlement=True)

        cycles, _ = build_cycles(
            [
                tx("2026-01-30", STO, "-DCH260220P8", -10, 0.55, 543.30, row_id=1),
                tx("2026-02-23", ASSIGNED, "-DCH260220P8", 10, None, 0.0, row_id=2, as_of="2026-02-20"),
                settlement("2026-02-23", "BUY_STOCK", "DCH", 1000, 8.0, -8000.0, 3, "2026-02-20"),
            ]
        )
        cycle = cycles[0]
        # Confirms the fixture actually exercises the broker-settlement path
        # this test is about, not the synthetic one another test already covers.
        self.assertEqual(cycle.share_lots[0].source, "PURCHASE")

        events = wheel_cash_flow_events(cycles)
        self.assertEqual(
            events,
            [
                (date(2026, 1, 30), 8000.0, "DCH CSP open"),
                (date(2026, 2, 20), -543.30, "DCH CSP close"),  # premium only, not a second $8,000
            ],
        )
        self.assertAlmostEqual(wheel_terminal_value(cycles, date(2026, 2, 25), {}), 8000.0)

    def test_covered_call_on_untracked_shares_gets_a_normal_open_and_close(self):
        """A covered call with no tracked shares behind it (shares_tracked
        False, the pre-export-start proxy case) is real, standalone
        collateral -- unlike the tracked case above, both its open and its
        close are ordinary events, no assignment carve-out.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-09-15", STO, "-QQQ251017C588", -1, 3.32, 331.33, row_id=1),
                tx("2025-10-17", EXPIRED, "-QQQ251017C588", 1, None, 0.0, row_id=2, as_of="2025-10-17"),
            ]
        )
        events = wheel_cash_flow_events(cycles)
        self.assertEqual(events[0], (date(2025, 9, 15), 58800.0, "QQQ COVERED_CALL open"))
        self.assertEqual(events[1], (date(2025, 10, 17), -59131.33, "QQQ COVERED_CALL close"))

    def test_protective_put_open_and_close_use_the_debit_paid(self):
        """A hedge leg *inside a wheel* contributes its actual debit, not its
        notional. The anchor CSP never closes, so the cycle stays a wheel and
        the long put rides in it.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-XYZ250601P90", -1, 1.00, 100.0, row_id=1),
                tx("2025-01-01", BTO, "-XYZ250201P100", 1, 10.00, -1000.0, row_id=2),
                tx("2025-01-10", STC, "-XYZ250201P100", -1, 7.00, 700.0, row_id=3),
            ]
        )
        events = wheel_cash_flow_events(cycles)
        self.assertIn((date(2025, 1, 1), 1000.0, "XYZ LONG_PUT open"), events)
        self.assertIn((date(2025, 1, 10), -700.0, "XYZ LONG_PUT close"), events)

    def test_lone_directional_long_is_not_wheel_capital(self):
        """A cycle that only ever bought options -- no CSP, covered call,
        shares or assignment -- is not the wheel; its dollars stay out of the
        wheel cash-flow ledger and terminal value entirely (see Cycle.is_wheel).
        """
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", BTO, "-XYZ250201P100", 1, 10.00, -1000.0, row_id=1),
                tx("2025-01-10", STC, "-XYZ250201P100", -1, 7.00, 700.0, row_id=2),
            ]
        )
        self.assertEqual(wheel_cash_flow_events(cycles), [])
        self.assertAlmostEqual(wheel_terminal_value(cycles, date(2025, 2, 15), {}), 0.0)

    def test_plain_stock_purchase_with_no_covered_call_is_excluded(self):
        """Shares bought outright, with no covered call ever written against
        them, are not wheel capital -- an ordinary buy-and-hold position no
        different from a non-wheel ETF sitting in the same account.
        """
        cycles, _ = build_cycles([tx("2025-01-01", "BUY_STOCK", "XYZ", 100, 50.00, -5000.0, row_id=1)])
        self.assertEqual(wheel_cash_flow_events(cycles), [])
        self.assertAlmostEqual(wheel_terminal_value(cycles, date(2025, 2, 15), {}), 0.0)

    def test_stock_bought_outright_then_covered_becomes_wheel_capital(self):
        """The regression this covers: a real account often buys shares
        directly and writes calls against them, never having sold a put to
        acquire them -- assignment-only tracking would silently drop the
        entire cost of that stock from both the cash-flow ledger and the
        terminal value, while still counting the call's own premium, making
        the wheel look like it generates income from zero capital. The
        moment a cycle writes even one covered call, the stock backing it
        counts too, regardless of how it was acquired.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", "BUY_STOCK", "XYZ", 100, 50.00, -5000.0, row_id=1),
                tx("2025-02-01", STO, "-XYZ250301C55", -1, 2.00, 199.33, row_id=2),
                tx("2025-03-01", EXPIRED, "-XYZ250301C55", 1, None, 0.0, row_id=3, as_of="2025-03-01"),
                tx("2025-04-01", "SELL_STOCK", "XYZ", -100, 55.00, 5500.0, row_id=4),
            ]
        )
        events = wheel_cash_flow_events(cycles)
        self.assertIn((date(2025, 1, 1), 5000.0, "XYZ shares bought"), events)
        # The call's own collateral is $0 (tracked shares) -- no open event --
        # but its premium is real, realized income once it closes.
        self.assertIn((date(2025, 3, 1), -199.33, "XYZ COVERED_CALL close"), events)
        self.assertIn((date(2025, 4, 1), -5500.0, "XYZ shares sold"), events)
        contributions = sum(amount for _, amount, _ in events if amount > 0)
        withdrawals = sum(-amount for _, amount, _ in events if amount < 0)
        # $199.33 call premium + $500 stock gain (bought at $50, sold at $55).
        self.assertAlmostEqual(withdrawals - contributions, 199.33 + 500.0, places=2)
        self.assertAlmostEqual(wheel_terminal_value(cycles, date(2025, 4, 15), {}), 0.0)  # fully closed out

    def test_terminal_value_marks_held_shares_to_current_price_when_available(self):
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-XYZ250201P100", -1, 4.00, 400.0, row_id=1),
                tx("2025-02-01", ASSIGNED, "-XYZ250201P100", 1, None, 0.0, row_id=2, as_of="2025-02-01"),
            ]
        )
        # No price supplied for XYZ -- falls back to cost basis ($100/share).
        self.assertAlmostEqual(wheel_terminal_value(cycles, date(2025, 2, 15), {}), 10000.0)
        # A live price marks the same shares to market instead.
        self.assertAlmostEqual(
            wheel_terminal_value(cycles, date(2025, 2, 15), {"XYZ": 120.0}), 12000.0
        )
        # A failed fetch (key present, value None) must fall back too, not crash.
        self.assertAlmostEqual(
            wheel_terminal_value(cycles, date(2025, 2, 15), {"XYZ": None}), 10000.0
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
