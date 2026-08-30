"""Engine tests: lifecycle, FIFO partial fills, rolls, assignment, cycle bounds."""

from __future__ import annotations

import itertools
import os
import sys
import unittest
from dataclasses import replace
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.engine import (  # noqa: E402
    ACTIVE,
    ASSIGNED_STATUS,
    CLOSED,
    COVERED_CALL,
    CSP,
    FROM_PRE_HISTORY,
    FROM_PUT_ASSIGNMENT,
    LONG,
    SHORT,
    build_cycles,
)
from wheel.parser import ASSIGNED, BTC, BTO, BUY_STOCK, EXPIRED, STC, STO, Transaction  # noqa: E402

_counter = itertools.count()


def tx(
    day: str,
    action: str,
    symbol: str,
    contracts: float,
    price: float | None = None,
    amount: float = 0.0,
    row_id: int | None = None,
    commission: float = 0.0,
    fees: float = 0.0,
    as_of: str | None = None,
    action_raw: str | None = None,
) -> Transaction:
    """Build a Transaction the way the parser would, with minimal ceremony."""
    from wheel.parser import parse_occ_symbol

    parsed = parse_occ_symbol(symbol)
    underlying, right, strike, expiry = parsed if parsed else (symbol, None, None, None)
    return Transaction(
        row_id=row_id if row_id is not None else next(_counter),
        run_date=date.fromisoformat(day),
        settlement_date=None,
        action=action,
        action_raw=action_raw if action_raw is not None else action,
        description="",
        underlying=underlying,
        occ_symbol=symbol.lstrip("-").upper() if parsed else None,
        right=right,
        strike=strike,
        expiry=expiry,
        contracts=contracts,
        price=price,
        commission=commission,
        fees=fees,
        amount=amount,
        account_type="Cash",
        as_of_date=date.fromisoformat(as_of) if as_of else None,
    )


class TestBasicLifecycle(unittest.TestCase):
    def test_csp_sold_then_bought_back(self):
        cycles, _ = build_cycles(
            [
                tx("2025-09-19", STO, "-MU250926P150", -1, 3.35, 334.33),
                tx("2025-09-25", BTC, "-MU250926P150", 1, 0.95, -95.67),
            ]
        )
        self.assertEqual(len(cycles), 1)
        cycle = cycles[0]
        leg = cycle.legs[0]
        self.assertEqual(leg.strategy, CSP)
        self.assertEqual(leg.side, SHORT)
        self.assertFalse(leg.is_open)
        self.assertEqual(leg.outcome, "CLOSED")
        self.assertAlmostEqual(leg.realized_pl, 238.66, places=2)
        self.assertEqual(leg.days_held, 6)
        self.assertEqual(cycle.status, CLOSED)
        self.assertEqual(cycle.end_date, date(2025, 9, 25))

    def test_expired_short_put_keeps_full_premium(self):
        cycles, _ = build_cycles(
            [
                tx("2025-09-19", STO, "-MU250926P150", -1, 3.35, 334.33),
                tx("2025-09-29", EXPIRED, "-MU250926P150", 1, None, 0.0, as_of="2025-09-26"),
            ]
        )
        leg = cycles[0].legs[0]
        self.assertEqual(leg.outcome, "EXPIRED")
        self.assertAlmostEqual(leg.realized_pl, 334.33, places=2)
        # The expiry is booked on its as-of date, not the ledger run date.
        self.assertEqual(cycles[0].end_date, date(2025, 9, 26))

    def test_long_option_bto_then_stc(self):
        cycles, _ = build_cycles(
            [
                tx("2025-11-21", BTO, "-MU251128C210", 1, 7.70, -770.67),
                tx("2025-11-26", STC, "-MU251128C210", -1, 20.30, 2029.33),
            ]
        )
        leg = cycles[0].legs[0]
        self.assertEqual(leg.side, LONG)
        self.assertAlmostEqual(leg.realized_pl, 1258.66, places=2)
        self.assertEqual(cycles[0].status, CLOSED)


class TestPartialFillsAndFIFO(unittest.TestCase):
    def test_scaled_entry_closed_by_one_larger_fill(self):
        """Two separate 1-lots closed by a single 2-lot buy-back, FIFO."""
        cycles, _ = build_cycles(
            [
                tx("2025-09-22", STO, "-MU250926P155", -1, 3.50, 349.33),
                tx("2025-09-23", STO, "-MU250926P155", -1, 3.50, 349.33),
                tx("2025-09-25", BTC, "-MU250926P155", 2, 0.95, -191.35),
            ]
        )
        legs = cycles[0].legs
        self.assertEqual(len(legs), 2)
        for leg in legs:
            self.assertFalse(leg.is_open)
            self.assertEqual(leg.closed_contracts, 1)
            self.assertAlmostEqual(leg.realized_pl, 349.33 - 95.675, places=2)
        self.assertAlmostEqual(sum(leg.realized_pl for leg in legs), 349.33 * 2 - 191.35, places=2)

    def test_partial_close_leaves_remainder_open(self):
        cycles, _ = build_cycles(
            [
                tx("2025-10-10", STO, "-MU251024P180", -2, 7.40, 1478.66),
                tx("2025-10-17", BTC, "-MU251024P180", 1, 1.12, -112.67),
            ]
        )
        leg = cycles[0].legs[0]
        self.assertEqual(leg.remaining_contracts, 1)
        self.assertTrue(leg.is_open)
        # Only half the opening credit is realized alongside the buy-back.
        self.assertAlmostEqual(leg.realized_pl, 1478.66 / 2 - 112.67, places=2)
        self.assertAlmostEqual(leg.open_premium, 1478.66 / 2, places=2)
        self.assertEqual(cycles[0].status, ACTIVE)

    def test_close_larger_than_position_is_reported(self):
        _, engine = build_cycles(
            [
                tx("2025-10-10", STO, "-MU251024P180", -1, 7.40, 739.33),
                tx("2025-10-17", BTC, "-MU251024P180", 3, 1.12, -338.01),
            ]
        )
        self.assertEqual(len(engine.unmatched_closes), 1)
        self.assertEqual(engine.unmatched_closes[0]["contracts"], 2)

    def test_close_larger_than_position_prorates_cash_by_full_size_not_matched_size(self):
        """The single visible contract must absorb only 1/3 of the buy-back's
        cost, not all of it, even though only 1 of the 3 contracts this BTC
        closes has a known opening leg (the other 2 were opened before this
        export's window). Dividing the row's cash by the matched count instead
        of the requested count would load the whole 3-contract cost onto the
        1 visible contract, tripling its apparent realized P/L.
        """
        cycles, engine = build_cycles(
            [
                tx("2025-10-10", STO, "-MU251024P180", -1, 7.40, 739.33),
                tx("2025-10-17", BTC, "-MU251024P180", 3, 1.12, -338.01),
            ]
        )
        leg = cycles[0].legs[0]
        per_contract_cost = -338.01 / 3
        self.assertAlmostEqual(leg.realized_pl, 739.33 + per_contract_cost, places=2)
        # The excluded 2 contracts' own share of the cash is tracked, not
        # dropped -- the reconciliation total still balances exactly.
        self.assertAlmostEqual(engine.unmatched_cash, per_contract_cost * 2, places=2)
        self.assertAlmostEqual(engine.unmatched_closes[0]["cash"], per_contract_cost * 2, places=2)

    def test_same_day_open_and_close(self):
        """A 0-DTE style open-then-close on one day must still match its lot."""
        cycles, engine = build_cycles(
            [
                tx("2025-11-04", STO, "-MU251107P232.5", -1, 13.10, 1309.33, row_id=1),
                tx("2025-11-04", BTC, "-MU251107P232.5", 1, 9.55, -955.67, row_id=2),
            ]
        )
        self.assertEqual(engine.unmatched_closes, [])
        self.assertAlmostEqual(cycles[0].legs[0].realized_pl, 353.66, places=2)

    def test_open_and_expire_on_the_same_day(self):
        cycles, engine = build_cycles(
            [
                tx("2025-09-17", STO, "-QQQ250917P583", -1, 0.99, 98.33, row_id=1),
                tx("2025-09-18", EXPIRED, "-QQQ250917P583", 1, None, 0.0, row_id=2, as_of="2025-09-17"),
            ]
        )
        self.assertEqual(engine.unmatched_closes, [])
        self.assertEqual(cycles[0].legs[0].outcome, "EXPIRED")


class TestRolls(unittest.TestCase):
    def test_simple_roll_out(self):
        cycles, _ = build_cycles(
            [
                tx("2025-09-22", STO, "-NVDA251003C200", -1, 0.50, 49.33, row_id=1),
                tx("2025-10-01", BTC, "-NVDA251003C200", 1, 0.06, -6.02, row_id=2),
                tx("2025-10-01", STO, "-NVDA251010C200", -1, 0.56, 55.33, row_id=3),
            ]
        )
        # All three legs belong to one cycle; the buy-back plus reopen is a roll.
        self.assertEqual(len(cycles), 1)
        rolls = cycles[0].rolls
        self.assertEqual(len(rolls), 1)
        self.assertEqual(rolls[0].direction, "OUT")
        self.assertAlmostEqual(rolls[0].net_credit, 49.31, places=2)

    def test_same_day_round_trip_is_not_a_roll(self):
        """Closing and re-entering the same contract on one day is not a roll."""
        cycles, _ = build_cycles(
            [
                tx("2025-10-01", STO, "-NVDA251003C200", -1, 0.50, 49.33, row_id=1),
                tx("2025-10-01", BTC, "-NVDA251003C200", 1, 0.06, -6.02, row_id=2),
            ]
        )
        self.assertEqual(cycles[0].rolls, [])

    def test_roll_down_same_expiry(self):
        cycles, _ = build_cycles(
            [
                tx("2025-10-01", STO, "-MU251010P165", -1, 1.26, 125.33, row_id=1),
                tx("2025-10-06", BTC, "-MU251010P165", 1, 0.31, -31.02, row_id=2),
                tx("2025-10-06", STO, "-MU251010P157.5", -1, 0.52, 51.33, row_id=3),
            ]
        )
        self.assertEqual(cycles[0].rolls[0].direction, "DOWN")

    def test_roll_out_and_up(self):
        cycles, _ = build_cycles(
            [
                tx("2025-10-08", STO, "-MU251010C200", -1, 2.02, 201.33, row_id=1),
                tx("2025-10-08", BTC, "-MU251010C200", 1, 1.97, -197.67, row_id=2),
                tx("2025-10-08", STO, "-MU251017C210", -1, 2.69, 268.33, row_id=3),
            ]
        )
        self.assertEqual(cycles[0].rolls[0].direction, "OUT_AND_UP")

    def test_quantity_mismatched_roll_is_still_a_roll(self):
        """Real rolls resize: this book closes 4 contracts and opens 2."""
        cycles, _ = build_cycles(
            [
                tx("2025-10-17", STO, "-MU251024P182.5", -4, 1.42, 565.32, row_id=1),
                tx("2025-10-20", BTC, "-MU251024P182.5", 4, 0.35, -140.07, row_id=2),
                tx("2025-10-20", STO, "-MU251024P190", -2, 0.92, 182.66, row_id=3),
            ]
        )
        rolls = cycles[0].rolls
        self.assertEqual(len(rolls), 1)
        self.assertEqual(rolls[0].closed[0]["contracts"], 4)
        self.assertEqual(rolls[0].opened[0]["contracts"], 2)

    def test_multi_leg_same_day_roll_groups_by_right(self):
        """Puts and calls rolled on one day must not be merged into one roll."""
        cycles, _ = build_cycles(
            [
                tx("2025-10-06", STO, "-MU251010P165", -1, 1.26, 125.33, row_id=1),
                tx("2025-10-06", STO, "-MU251010C200", -1, 2.02, 201.33, row_id=2),
                tx("2025-10-08", BTC, "-MU251010P165", 1, 0.31, -31.02, row_id=3),
                tx("2025-10-08", BTC, "-MU251010C200", 1, 1.97, -197.67, row_id=4),
                tx("2025-10-08", STO, "-MU251017P175", -1, 0.84, 83.33, row_id=5),
                tx("2025-10-08", STO, "-MU251017C210", -1, 2.69, 268.33, row_id=6),
            ]
        )
        rolls = cycles[0].rolls
        self.assertEqual(len(rolls), 2)
        self.assertEqual({roll.right for roll in rolls}, {"P", "C"})
        for roll in rolls:
            self.assertEqual(len(roll.closed), 1)
            self.assertEqual(len(roll.opened), 1)

    def test_close_with_no_reopen_is_not_a_roll(self):
        cycles, _ = build_cycles(
            [
                tx("2025-10-01", STO, "-MU251010P165", -1, 1.26, 125.33, row_id=1),
                tx("2025-10-06", BTC, "-MU251010P165", 1, 0.31, -31.02, row_id=2),
            ]
        )
        self.assertEqual(cycles[0].rolls, [])

    def test_reopen_at_earlier_expiry_is_not_a_roll(self):
        """Opening a nearer-dated leg the same day is a new position, not a roll."""
        cycles, _ = build_cycles(
            [
                tx("2025-10-01", STO, "-MU251024P180", -1, 7.40, 739.33, row_id=1),
                tx("2025-10-06", BTC, "-MU251024P180", 1, 1.12, -112.67, row_id=2),
                tx("2025-10-06", STO, "-MU251010P157.5", -1, 0.52, 51.33, row_id=3),
            ]
        )
        self.assertEqual(cycles[0].rolls, [])


class TestSpreadDetection(unittest.TestCase):
    def test_same_day_short_and_long_pair_into_a_spread(self):
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-XYZ250201P100", -1, 3.00, 300.0, row_id=1),
                tx("2025-01-01", BTO, "-XYZ250201P95", 1, 1.00, -100.0, row_id=2),
            ]
        )
        cycle = cycles[0]
        self.assertEqual(len(cycle.spreads), 1)
        spread = cycle.spreads[0]
        self.assertEqual(spread.right, "P")
        self.assertEqual(spread.short_strike, 100.0)
        self.assertEqual(spread.long_strike, 95.0)
        self.assertEqual(spread.paired_contracts, 1)
        self.assertAlmostEqual(spread.collateral_per_contract, 500.0)  # (100-95)*100
        self.assertAlmostEqual(spread.net_credit, 200.0)  # 300 - 100
        short_leg = next(leg for leg in cycle.legs if leg.strike == 100.0)
        long_leg = next(leg for leg in cycle.legs if leg.strike == 95.0)
        self.assertEqual(short_leg.paired_contracts, {spread.spread_id: 1})
        self.assertEqual(long_leg.paired_contracts, {spread.spread_id: 1})

    def test_different_expiry_does_not_pair(self):
        """A calendar spread -- same strike/right, different expiry -- is not
        paired: the rule is same-day AND same expiry.
        """
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-XYZ250201P100", -1, 3.00, 300.0, row_id=1),
                tx("2025-01-01", BTO, "-XYZ250301P100", 1, 2.00, -200.0, row_id=2),
            ]
        )
        self.assertEqual(cycles[0].spreads, [])

    def test_lone_long_creates_no_spread(self):
        """An ordinary protective put with no same-day short stays unpaired."""
        cycles, _ = build_cycles(
            [tx("2025-01-01", BTO, "-XYZ250201P100", 1, 10.00, -1000.0, row_id=1)]
        )
        self.assertEqual(cycles[0].spreads, [])

    def test_lone_short_creates_no_spread(self):
        cycles, _ = build_cycles(
            [tx("2025-01-01", STO, "-XYZ250201P100", -1, 3.00, 300.0, row_id=1)]
        )
        self.assertEqual(cycles[0].spreads, [])

    def test_quantity_mismatch_pairs_the_smaller_side_and_leaves_the_rest_naked(self):
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-XYZ250201P100", -2, 6.00, 600.0, row_id=1),
                tx("2025-01-01", BTO, "-XYZ250201P95", 1, 1.00, -100.0, row_id=2),
            ]
        )
        cycle = cycles[0]
        self.assertEqual(len(cycle.spreads), 1)
        self.assertEqual(cycle.spreads[0].paired_contracts, 1)
        short_leg = next(leg for leg in cycle.legs if leg.strike == 100.0)
        self.assertEqual(short_leg.contracts, 2)
        self.assertEqual(sum(short_leg.paired_contracts.values()), 1)  # 1 of 2 paired, 1 naked

    def test_ambiguous_multi_candidate_group_stays_naked_with_a_warning(self):
        """One short, two same-day longs at different strikes -- which pairs
        with which is not decidable, so none of them pair (mirrors the real
        MU 2025-11-17 data this rule was designed against).
        """
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-XYZ250201P230", -1, 4.00, 400.0, row_id=1),
                tx("2025-01-01", BTO, "-XYZ250201P245", 1, 1.00, -100.0, row_id=2),
                tx("2025-01-01", BTO, "-XYZ250201P240", 1, 1.00, -100.0, row_id=3),
            ]
        )
        cycle = cycles[0]
        self.assertEqual(cycle.spreads, [])
        self.assertTrue(any("ambiguous" in warning.lower() or "same day" in warning.lower() for warning in cycle.warnings))

    def test_puts_and_calls_opened_same_day_pair_independently(self):
        cycles, _ = build_cycles(
            [
                tx("2025-01-01", STO, "-XYZ250201P100", -1, 3.00, 300.0, row_id=1),
                tx("2025-01-01", BTO, "-XYZ250201P95", 1, 1.00, -100.0, row_id=2),
                tx("2025-01-01", STO, "-XYZ250201C120", -1, 2.50, 250.0, row_id=3),
                tx("2025-01-01", BTO, "-XYZ250201C130", 1, 0.80, -80.0, row_id=4),
            ]
        )
        cycle = cycles[0]
        self.assertEqual(len(cycle.spreads), 2)
        self.assertEqual({spread.right for spread in cycle.spreads}, {"P", "C"})


class TestAssignment(unittest.TestCase):
    def test_put_assignment_creates_share_lot_at_strike(self):
        cycles, _ = build_cycles(
            [
                tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 399.33),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, as_of="2025-11-20"),
            ]
        )
        cycle = cycles[0]
        self.assertEqual(cycle.legs[0].outcome, "ASSIGNED")
        self.assertEqual(len(cycle.share_lots), 1)

        lot = cycle.share_lots[0]
        self.assertEqual(lot.shares, 100)
        self.assertEqual(lot.basis_per_share, 230.0)
        self.assertEqual(lot.source, FROM_PUT_ASSIGNMENT)
        self.assertTrue(lot.synthetic)

        assignment = cycle.assignments[0]
        self.assertEqual(assignment.direction, "ACQUIRE")
        self.assertAlmostEqual(assignment.cash, -23000.0, places=2)
        # Shares are still held, so the campaign stays open.
        self.assertEqual(cycle.status, ACTIVE)

    def test_assigned_shares_flow_into_a_covered_call(self):
        """The call sold after assignment must be recognised as covered."""
        cycles, _ = build_cycles(
            [
                tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 399.33, row_id=1),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=2, as_of="2025-11-20"),
                tx("2025-11-24", STO, "-MU251128C235", -1, 2.00, 199.33, row_id=3),
            ]
        )
        cycle = cycles[0]
        self.assertEqual(len(cycle.legs), 2)
        self.assertEqual(cycle.legs[1].strategy, COVERED_CALL)
        # One continuous campaign: put -> assignment -> covered call.
        self.assertEqual(len(cycles), 1)

    def test_full_wheel_put_to_call_away(self):
        """CSP -> assigned -> covered call -> called away closes the cycle."""
        cycles, _ = build_cycles(
            [
                tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 399.33, row_id=1),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=2, as_of="2025-11-20"),
                tx("2025-11-24", STO, "-MU251128C235", -1, 2.00, 199.33, row_id=3),
                tx("2025-12-01", ASSIGNED, "-MU251128C235", 1, None, 0.0, row_id=4, as_of="2025-11-28"),
            ]
        )
        self.assertEqual(len(cycles), 1)
        cycle = cycles[0]
        self.assertEqual(cycle.status, ASSIGNED_STATUS)
        self.assertEqual(cycle.end_date, date(2025, 11, 28))

        # Stock leg: bought at 230, called away at 235 => $500 on 100 shares.
        lot = cycle.share_lots[0]
        self.assertEqual(lot.remaining, 0)
        self.assertAlmostEqual(lot.disposals[0]["realized"], 500.0, places=2)
        self.assertEqual(len(cycle.assignments), 2)
        self.assertEqual(cycle.assignments[1].direction, "DISPOSE")

    def test_call_away_without_tracked_shares_is_flagged_not_invented(self):
        """Stock bought before the export has unknown basis; P/L must not be faked."""
        cycles, _ = build_cycles(
            [
                tx("2025-09-15", STO, "-QQQ250917C588", -1, 3.32, 331.33, row_id=1),
                tx("2025-09-18", ASSIGNED, "-QQQ250917C588", 1, None, 0.0, row_id=2, as_of="2025-09-17"),
            ]
        )
        cycle = cycles[0]
        # A short call is always covered; this export just cannot see the shares.
        self.assertEqual(cycle.legs[0].strategy, COVERED_CALL)
        self.assertFalse(cycle.legs[0].shares_tracked)

        lot = next(lot for lot in cycle.share_lots if lot.source == FROM_PRE_HISTORY)
        self.assertFalse(lot.basis_known)
        self.assertIsNone(lot.basis_per_share)
        self.assertEqual(lot.disposals[0]["realized"], 0.0)
        self.assertTrue(any("basis unknown" in warning for warning in cycle.warnings))

    def test_assignment_with_no_matching_leg_still_books_shares(self):
        cycles, engine = build_cycles(
            [tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, as_of="2025-11-20")]
        )
        self.assertEqual(len(engine.unmatched_closes), 1)
        self.assertEqual(cycles[0].share_lots[0].shares, 100)
        self.assertTrue(cycles[0].warnings)


class TestBrokerSuppliedShareLegs(unittest.TestCase):
    """Exports that include equity rows must not also get synthesized shares."""

    @staticmethod
    def settlement(day, action, symbol, shares, price, amount, row_id, as_of):
        row = tx(day, action, symbol, shares, price, amount, row_id=row_id, as_of=as_of)
        return replace(row, assignment_settlement=True)

    def test_put_assignment_uses_the_brokers_share_fill(self):
        cycles, _ = build_cycles(
            [
                tx("2026-01-30", STO, "-DCH260220P8", -10, 0.55, 543.30, row_id=1),
                tx("2026-02-23", ASSIGNED, "-DCH260220P8", 10, None, 0.0, row_id=2, as_of="2026-02-20"),
                self.settlement("2026-02-23", "BUY_STOCK", "DCH", 1000, 8.0, -8000.0, 3, "2026-02-20"),
            ]
        )
        cycle = cycles[0]
        # Exactly one share lot: the broker's, not the broker's plus a synthetic one.
        self.assertEqual(len(cycle.share_lots), 1)
        lot = cycle.share_lots[0]
        self.assertEqual(lot.shares, 1000)
        self.assertFalse(lot.synthetic)
        self.assertEqual(lot.basis_per_share, 8.0)

        assignment = cycle.assignments[0]
        self.assertFalse(assignment.synthetic)
        self.assertAlmostEqual(assignment.cash, -8000.0, places=2)

    def test_call_assignment_uses_the_brokers_share_sale(self):
        cycles, _ = build_cycles(
            [
                tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 399.33, row_id=1),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=2, as_of="2025-11-20"),
                self.settlement("2025-11-21", "BUY_STOCK", "MU", 100, 230.0, -23000.0, 3, "2025-11-20"),
                tx("2025-11-24", STO, "-MU251128C235", -1, 2.00, 199.33, row_id=4),
                tx("2025-12-01", ASSIGNED, "-MU251128C235", 1, None, 0.0, row_id=5, as_of="2025-11-28"),
                self.settlement("2025-12-01", "SELL_STOCK", "MU", -100, 235.0, 23500.0, 6, "2025-11-28"),
            ]
        )
        cycle = cycles[0]
        self.assertEqual(len(cycle.share_lots), 1)
        lot = cycle.share_lots[0]
        self.assertEqual(lot.remaining, 0)
        # Bought at 230, called away at 235, on the broker's own numbers.
        self.assertAlmostEqual(lot.disposals[0]["realized"], 500.0, places=2)
        self.assertTrue(all(not a.synthetic for a in cycle.assignments))
        self.assertEqual(cycle.status, ASSIGNED_STATUS)

    def test_synthesis_still_happens_when_no_share_row_exists(self):
        cycles, _ = build_cycles(
            [
                tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 399.33, row_id=1),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=2, as_of="2025-11-20"),
            ]
        )
        lot = cycles[0].share_lots[0]
        self.assertTrue(lot.synthetic)
        self.assertTrue(cycles[0].assignments[0].synthetic)

    def test_one_share_row_cannot_settle_two_assignments(self):
        cycles, _ = build_cycles(
            [
                tx("2025-11-17", STO, "-MU251121P230", -2, 4.00, 798.66, row_id=1),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=2, as_of="2025-11-20"),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=3, as_of="2025-11-20"),
                self.settlement("2025-11-21", "BUY_STOCK", "MU", 100, 230.0, -23000.0, 4, "2025-11-20"),
            ]
        )
        cycle = cycles[0]
        synthetic = [a for a in cycle.assignments if a.synthetic]
        real = [a for a in cycle.assignments if not a.synthetic]
        self.assertEqual(len(real), 1)
        self.assertEqual(len(synthetic), 1)

    def test_repeated_builds_give_identical_results(self):
        """Settlement matching must not leave state on the shared transactions."""
        rows = [
            tx("2026-01-30", STO, "-DCH260220P8", -10, 0.55, 543.30, row_id=1),
            tx("2026-02-23", ASSIGNED, "-DCH260220P8", 10, None, 0.0, row_id=2, as_of="2026-02-20"),
            self.settlement("2026-02-23", "BUY_STOCK", "DCH", 1000, 8.0, -8000.0, 3, "2026-02-20"),
        ]
        first, _ = build_cycles(rows)
        second, _ = build_cycles(rows)
        self.assertEqual(
            [a.synthetic for a in first[0].assignments],
            [a.synthetic for a in second[0].assignments],
        )
        self.assertFalse(second[0].assignments[0].synthetic)


class TestTickerRename(unittest.TestCase):
    def test_assignment_under_a_renamed_ticker_finds_its_lots(self):
        """AXL became DCH mid-contract; the option series is otherwise identical."""
        cycles, engine = build_cycles(
            [
                tx("2026-01-30", STO, "-AXL260220P8", -10, 0.55, 543.30, row_id=1),
                tx("2026-02-23", ASSIGNED, "-DCH260220P8", 10, None, 0.0, row_id=2, as_of="2026-02-20"),
            ]
        )
        self.assertEqual(engine.unmatched_closes, [])
        leg = next(leg for cycle in cycles for leg in cycle.legs)
        self.assertFalse(leg.is_open)
        self.assertEqual(leg.outcome, "ASSIGNED")
        self.assertTrue(any("ticker change" in w for w in engine.warnings))

    def test_ambiguous_series_match_is_left_unmatched(self):
        """Two tickers with the same series is not enough to guess from."""
        cycles, engine = build_cycles(
            [
                tx("2026-01-30", STO, "-AXL260220P8", -1, 0.55, 54.33, row_id=1),
                tx("2026-01-30", STO, "-ZZZ260220P8", -1, 0.55, 54.33, row_id=2),
                tx("2026-02-23", ASSIGNED, "-DCH260220P8", 1, None, 0.0, row_id=3, as_of="2026-02-20"),
            ]
        )
        self.assertEqual(len(engine.unmatched_closes), 1)
        self.assertFalse(any("ticker change" in w for w in engine.warnings))

    def test_a_different_strike_is_not_a_rename(self):
        cycles, engine = build_cycles(
            [
                tx("2026-01-30", STO, "-AXL260220P8", -1, 0.55, 54.33, row_id=1),
                tx("2026-02-23", ASSIGNED, "-DCH260220P9", 1, None, 0.0, row_id=2, as_of="2026-02-20"),
            ]
        )
        self.assertEqual(len(engine.unmatched_closes), 1)


class TestCycleBoundaries(unittest.TestCase):
    def test_flat_then_reopen_next_year_starts_a_second_cycle(self):
        cycles, _ = build_cycles(
            [
                tx("2025-12-05", STO, "-MU251219P150", -1, 3.35, 334.33, row_id=1),
                tx("2025-12-19", EXPIRED, "-MU251219P150", 1, None, 0.0, row_id=2, as_of="2025-12-19"),
                tx("2026-01-06", STO, "-MU260116P175", -1, 0.75, 74.33, row_id=3),
            ]
        )
        self.assertEqual(len(cycles), 2)
        self.assertEqual([cycle.cycle_id for cycle in cycles], ["MU-2025-1", "MU-2026-1"])
        self.assertEqual(cycles[0].status, CLOSED)
        self.assertEqual(cycles[1].status, ACTIVE)

    def test_flat_then_reopen_same_year_stays_one_cycle(self):
        """Selling puts, letting them expire, and selling more months later --
        with a flat gap between -- is one ongoing wheel while the year holds and
        nothing was ever called away (the NBR-2026-1 / -2 case).
        """
        cycles, _ = build_cycles(
            [
                tx("2026-02-26", STO, "-NBR260320P75", -3, 3.75, 1122.98, row_id=1),
                tx("2026-03-20", EXPIRED, "-NBR260320P75", 3, None, 0.0, row_id=2, as_of="2026-03-20"),
                tx("2026-06-09", STO, "-NBR260619P70", -1, 2.0, 199.33, row_id=3),
            ]
        )
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0].cycle_id, "NBR-2026-1")
        self.assertEqual(cycles[0].start_date, date(2026, 2, 26))
        self.assertTrue(cycles[0].is_open)
        self.assertEqual(len(cycles[0].legs), 2)

    def test_called_away_wheel_does_not_resume_same_year(self):
        """A completed rotation -- put assigned, then the covered call assigned
        and the stock called away -- is a finished wheel; the next entry the
        same year is a new cycle.
        """
        cycles, _ = build_cycles(
            [
                tx("2026-01-05", STO, "-MU260116P100", -1, 3.0, 299.33, row_id=1),
                tx("2026-01-16", ASSIGNED, "-MU260116P100", 1, None, 0.0, row_id=2, as_of="2026-01-16"),
                tx("2026-01-20", STO, "-MU260220C110", -1, 2.0, 199.33, row_id=3),
                tx("2026-02-20", ASSIGNED, "-MU260220C110", 1, None, 0.0, row_id=4, as_of="2026-02-20"),
                tx("2026-03-02", STO, "-MU260320P95", -1, 2.5, 249.33, row_id=5),
            ]
        )
        self.assertEqual(len(cycles), 2)
        self.assertEqual(cycles[0].status, ASSIGNED_STATUS)
        self.assertEqual(cycles[0].cycle_id, "MU-2026-1")
        self.assertEqual(cycles[1].cycle_id, "MU-2026-2")

    def test_same_year_resume_only_applies_to_option_reentry(self):
        """A bare stock purchase after a flat gap opens its own cycle."""
        cycles, _ = build_cycles(
            [
                tx("2025-09-05", STO, "-MU250912P150", -1, 3.0, 299.33, row_id=1),
                tx("2025-09-12", EXPIRED, "-MU250912P150", 1, None, 0.0, row_id=2, as_of="2025-09-12"),
                tx("2025-11-20", BUY_STOCK, "MU", 100, 150.0, -15000.0, row_id=3),
            ]
        )
        self.assertEqual(len(cycles), 2)
        self.assertEqual(cycles[0].status, CLOSED)

    def test_different_tickers_never_share_a_cycle(self):
        cycles, _ = build_cycles(
            [
                tx("2025-09-19", STO, "-MU250926P150", -1, 3.35, 334.33, row_id=1),
                tx("2025-09-19", STO, "-QQQ250926P580", -1, 1.00, 99.33, row_id=2),
            ]
        )
        self.assertEqual({cycle.underlying for cycle in cycles}, {"MU", "QQQ"})
        self.assertEqual(len(cycles), 2)

    def test_overlapping_positions_stay_in_one_cycle(self):
        """The ticker never goes flat, so all four legs are one campaign."""
        cycles, _ = build_cycles(
            [
                tx("2025-09-19", STO, "-MU250926P150", -1, 3.35, 334.33, row_id=1),
                tx("2025-09-22", STO, "-MU251003P157.5", -1, 5.55, 554.33, row_id=2),
                tx("2025-09-26", EXPIRED, "-MU250926P150", 1, None, 0.0, row_id=3, as_of="2025-09-26"),
                tx("2025-10-01", BTC, "-MU251003P157.5", 1, 0.04, -4.02, row_id=4),
            ]
        )
        self.assertEqual(len(cycles), 1)
        self.assertEqual(len(cycles[0].legs), 2)
        self.assertEqual(cycles[0].end_date, date(2025, 10, 1))

    def test_multi_year_log_numbers_cycles_per_year(self):
        cycles, _ = build_cycles(
            [
                tx("2024-03-15", STO, "-MU240419P100", -1, 2.00, 199.33, row_id=1),
                tx("2024-04-19", EXPIRED, "-MU240419P100", 1, None, 0.0, row_id=2, as_of="2024-04-19"),
                tx("2025-09-19", STO, "-MU250926P150", -1, 3.35, 334.33, row_id=3),
                tx("2025-09-26", EXPIRED, "-MU250926P150", 1, None, 0.0, row_id=4, as_of="2025-09-26"),
                tx("2026-01-05", STO, "-MU260116P200", -1, 4.00, 399.33, row_id=5),
            ]
        )
        # The sequence restarts each calendar year -- the year already separates
        # them, so every year's first MU cycle is "MU-<year>-1".
        self.assertEqual([cycle.cycle_id for cycle in cycles], ["MU-2024-1", "MU-2025-1", "MU-2026-1"])
        self.assertEqual(cycles[0].legs[0].expiry, date(2024, 4, 19))
        self.assertEqual(cycles[2].legs[0].expiry, date(2026, 1, 16))
        self.assertEqual(cycles[2].status, ACTIVE)

    def test_events_are_ordered_by_event_date_not_file_order(self):
        """An assignment listed later in the file can precede an earlier trade."""
        cycles, engine = build_cycles(
            [
                tx("2025-11-24", STO, "-MU251128C235", -1, 2.00, 199.33, row_id=99),
                tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=50, as_of="2025-11-20"),
                tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 399.33, row_id=10),
            ]
        )
        self.assertEqual(engine.unmatched_closes, [])
        self.assertEqual(cycles[0].start_date, date(2025, 11, 17))
        self.assertEqual(cycles[0].legs[1].strategy, COVERED_CALL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
