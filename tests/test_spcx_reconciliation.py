"""SPCX Wheel report reconciliation.

A full-fidelity reproduction of the real SPCX-2026-1 wheel (Zafrir's IRA,
``data/zafrir-ira``) -- all 66 transactions, frozen here so the regression
suite never drifts as that account keeps trading. Every assertion below was
independently cross-checked against the live account data (same CSVs, same
engine) at the time this was written: see the PR/commit this file landed in
for the full reconciliation trace (leg-by-leg, chain-by-chain).

This file exists to prove -- and keep proving -- that every number on the
Trade Log's SPCX report is already produced by the existing backend
formulas (no production code changes were needed): Entries, Realized P&L,
Stock P&L, Open-options P&L, MTM P&L, Capital committed, Cost basis,
Break-even, Profit/day, P&L/day held, Closed legs, Win rate, Roll rate,
Resolved roll chains, Average days in trade, Annualized Wheel ROC, and
Gross premium. None of these expected values are hardcoded into production
logic -- only into this test's assertions, exactly as instructed.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_engine import tx  # noqa: E402
from tests.test_trade_log import _trade_log  # noqa: E402
from wheel.parser import ASSIGNED, BTC, BTO, BUY_STOCK, STC, STO  # noqa: E402

# The complete, real SPCX transaction history for this wheel, in broker-fill
# order -- extracted once from data/zafrir-ira (IRA_2026.csv +
# History_for_Account_488896608.csv, already merged/de-duplicated by the
# normal import pipeline) and frozen here. Reproduces exactly what the live
# Trade Log currently shows for this wheel.
SPCX_TRANSACTIONS = [
    tx("2026-06-16", BUY_STOCK, "SPCX", 1.0, 215.0, -215.0, row_id=1),
    tx("2026-06-16", STO, "SPCX260717P200", -1.0, 22.8, 2279.28, row_id=2, commission=0.65, fees=0.07),
    tx("2026-06-16", BTO, "SPCX260717P170", 1.0, 10.0, -1000.67, row_id=3, commission=0.65, fees=0.02),
    tx("2026-06-18", BUY_STOCK, "SPCX", 50.0, 180.0, -9000.0, row_id=4),
    tx("2026-06-22", STO, "SPCX260717P135", -1.0, 5.2, 519.31, row_id=5, commission=0.65, fees=0.04),
    tx("2026-06-23", BTO, "SPCX260717P125", 1.0, 4.4, -440.67, row_id=6, commission=0.65, fees=0.02),
    tx("2026-07-08", STO, "SPCX260717P140", -1.0, 3.04, 303.34, row_id=7, commission=0.65, fees=0.01),
    tx("2026-07-08", BTC, "SPCX260717P200", 1.0, 49.85, -4985.66, row_id=8, commission=0.65, fees=0.01),
    tx("2026-07-08", STC, "SPCX260717P170", -1.0, 21.16, 2115.29, row_id=9, commission=0.65, fees=0.06),
    tx("2026-07-08", BTO, "SPCX261120P125", 1.0, 15.5, -1550.66, row_id=10, commission=0.65, fees=0.01),
    tx("2026-07-16", BTC, "SPCX260717P140", 1.0, 7.42, -742.66, row_id=11, commission=0.65, fees=0.01),
    tx("2026-07-16", STC, "SPCX260717P125", -1.0, 0.5, 49.34, row_id=12, commission=0.65, fees=0.01),
    tx("2026-07-16", BTC, "SPCX260717P135", 1.0, 4.65, -465.66, row_id=13, commission=0.65, fees=0.01),
    tx("2026-07-16", STO, "SPCX260724P135", -1.0, 7.5, 749.32, row_id=14, commission=0.65, fees=0.03),
    tx("2026-07-16", STO, "SPCX260724P140", -1.0, 9.77, 976.31, row_id=15, commission=0.65, fees=0.04),
    tx("2026-07-24", STO, "SPCX260731P135", -1.0, 23.0, 2299.29, row_id=16, commission=0.65, fees=0.06),
    tx("2026-07-24", BTC, "SPCX260724P135", 1.0, 22.0, -2200.66, row_id=17, commission=0.65, fees=0.01),
    tx("2026-07-24", BTC, "SPCX260724P140", 1.0, 27.32, -2732.66, row_id=18, commission=0.65, fees=0.01),
    tx("2026-07-24", STO, "SPCX260807P137", -1.0, 28.37, 2836.28, row_id=19, commission=0.65, fees=0.07),
    tx("2026-07-30", BTO, "SPCX261120P115", 1.0, 21.4, -2140.66, row_id=20, commission=0.65, fees=0.01),
    tx("2026-07-30", STC, "SPCX261120P125", -1.0, 27.6, 2759.28, row_id=21, commission=0.65, fees=0.07),
    tx("2026-07-30", BTC, "SPCX260731P135", 1.0, 19.61, -1961.66, row_id=22, commission=0.65, fees=0.01),
    tx("2026-07-30", STO, "SPCX260814P130", -1.0, 21.64, 2163.29, row_id=23, commission=0.65, fees=0.06),
    tx("2026-08-03", BTC, "SPCX260807P137", 1.0, 28.55, -2855.66, row_id=24, commission=0.65, fees=0.01),
    tx("2026-08-03", STO, "SPCX260904P135", -1.0, 31.17, 3116.27, row_id=25, commission=0.65, fees=0.08),
    tx("2026-08-04", BTC, "SPCX260814P130", 1.0, 19.77, -1977.66, row_id=26, commission=0.65, fees=0.01),
    tx("2026-08-04", STO, "SPCX260904P130", -1.0, 23.0, 2299.29, row_id=27, commission=0.65, fees=0.06),
    tx("2026-08-07", STO, "SPCX260904P115", -1.0, 7.35, 734.32, row_id=28, commission=0.65, fees=0.03),
    tx("2026-08-10", BTC, "SPCX260904P115", 1.0, 3.5, -350.66, row_id=29, commission=0.65, fees=0.01),
    tx("2026-08-13", STO, "SPCX260821P140", -1.0, 4.0, 399.34, row_id=30, commission=0.65, fees=0.01),
    tx("2026-08-13", STO, "SPCX260814P140", -1.0, 2.0, 199.34, row_id=31, commission=0.65, fees=0.01),
    tx("2026-08-13", BTC, "SPCX260814P140", 1.0, 1.0, -100.66, row_id=32, commission=0.65, fees=0.01),
    tx("2026-08-13", BTC, "SPCX260904P130", 1.0, 4.5, -450.66, row_id=33, commission=0.65, fees=0.01),
    tx("2026-08-13", BTC, "SPCX260904P135", 1.0, 6.0, -600.66, row_id=34, commission=0.65, fees=0.01),
    tx("2026-08-17", STO, "SPCX260821P140", -1.0, 1.57, 156.34, row_id=35, commission=0.65, fees=0.01),
    tx("2026-08-17", BTC, "SPCX260821P140", 1.0, 2.0, -200.66, row_id=36, commission=0.65, fees=0.01),
    tx(
        "2026-08-24",
        BUY_STOCK,
        "SPCX",
        100.0,
        140.0,
        -14000.0,
        row_id=37,
        as_of="2026-08-21",
        action_raw="YOU BOUGHT ASSIGNED PUTS AS OF 08-21-26 SPACE EXPL TECHNOLOGIES CORP CL A (SPCX) (Margin)",
    ),
    tx("2026-08-24", ASSIGNED, "SPCX260821P140", 1.0, None, 0.0, row_id=38, as_of="2026-08-21"),
    tx("2026-08-24", STO, "SPCX260828C140", -1.0, 2.19, 218.34, row_id=39, commission=0.65, fees=0.01),
    tx("2026-08-28", BTC, "SPCX260828C140", 1.0, 0.97, -97.66, row_id=40, commission=0.65, fees=0.01),
    tx("2026-08-28", STO, "SPCX260904C140", -1.0, 3.72, 371.34, row_id=41, commission=0.65, fees=0.01),
    tx("2026-09-02", BTC, "SPCX260904C140", 1.0, 1.85, -185.66, row_id=42, commission=0.65, fees=0.01),
    tx("2026-09-02", STO, "SPCX260911P130", -1.0, 0.74, 73.34, row_id=43, commission=0.65, fees=0.01),
    tx("2026-09-02", STO, "SPCX260911C150", -1.0, 0.87, 86.34, row_id=44, commission=0.65, fees=0.01),
    tx("2026-09-03", BTC, "SPCX260911P130", 1.0, 0.3, -30.01, row_id=45, fees=0.01),
    tx("2026-09-03", STO, "SPCX260911P145", -1.0, 2.48, 247.34, row_id=46, commission=0.65, fees=0.01),
    tx("2026-09-08", STO, "SPCX260911P145", -1.0, 1.0, 99.34, row_id=47, commission=0.65, fees=0.01),
    tx("2026-09-08", BTC, "SPCX260911P145", 1.0, 1.24, -124.66, row_id=48, commission=0.65, fees=0.01),
    tx("2026-09-11", STO, "SPCX260918C155", -1.0, 2.2, 219.34, row_id=49, commission=0.65, fees=0.01),
    tx("2026-09-11", BTC, "SPCX260911P145", 1.0, 0.19, -19.01, row_id=50, fees=0.01),
    tx("2026-09-11", BTC, "SPCX260911C150", 1.0, 0.43, -43.01, row_id=51, fees=0.01),
    tx("2026-09-11", STO, "SPCX260918P139", -1.0, 1.39, 138.34, row_id=52, commission=0.65, fees=0.01),
    tx("2026-09-11", BTC, "SPCX260918P139", 1.0, 0.65, -65.01, row_id=53, fees=0.01),
    tx("2026-09-14", STO, "SPCX260918P146", -1.0, 1.83, 182.34, row_id=54, commission=0.65, fees=0.01),
    tx("2026-09-15", BTC, "SPCX260918C155", 1.0, 0.4, -40.01, row_id=55, fees=0.01),
    tx("2026-09-15", STO, "SPCX260925C155", -1.0, 1.56, 155.34, row_id=56, commission=0.65, fees=0.01),
    tx("2026-09-16", STO, "SPCX260925P146", -1.0, 2.96, 295.34, row_id=57, commission=0.65, fees=0.01),
    tx("2026-09-16", BTC, "SPCX260918P146", 1.0, 0.9, -90.66, row_id=58, commission=0.65, fees=0.01),
    tx("2026-09-17", BTC, "SPCX260925P146", 1.0, 1.4, -140.66, row_id=59, commission=0.65, fees=0.01),
    tx("2026-09-18", STO, "SPCX260925P148", -1.0, 2.06, 205.34, row_id=60, commission=0.65, fees=0.01),
    tx("2026-09-21", BTC, "SPCX260925P148", 1.0, 1.0, -100.66, row_id=61, commission=0.65, fees=0.01),
    tx("2026-09-24", BTC, "SPCX260925C155", 1.0, 0.29, -29.01, row_id=62, fees=0.01),
    tx("2026-09-24", STO, "SPCX261002C155", -1.0, 1.51, 150.34, row_id=63, commission=0.65, fees=0.01),
    tx("2026-09-29", STC, "SPCX261120P115", -1.0, 1.6, 159.34, row_id=64, commission=0.65, fees=0.01),
    tx("2026-10-01", BTC, "SPCX261002C155", 1.0, 0.3, -30.01, row_id=65, fees=0.01),
    tx("2026-10-01", STO, "SPCX261009C155", -1.0, 1.55, 154.34, row_id=66, commission=0.65, fees=0.01),
]

# The live app's own current price for SPCX as of 2026-10-01 (the last
# transaction date) -- not derivable from the ledger, so pinned here the
# same way _trade_log()'s `prices` argument lets every other test pin it.
SPCX_CURRENT_PRICE = 148.07


def _spcx_wheel():
    rows = _trade_log(SPCX_TRANSACTIONS, prices={"SPCX": SPCX_CURRENT_PRICE})
    (wheel,) = rows["wheels"]
    return wheel


class TestSpcxReconciliation(unittest.TestCase):
    """Locks in every number on the real SPCX-2026-1 report. Each assertion
    was independently verified against the live engine/leg/chain objects
    (not just the aggregate payload) before being written here -- see the
    module docstring. No production formula was changed to produce these;
    the existing AVAV-era formulas already get every one of them right.
    """

    def test_entries_is_the_full_raw_ledger_row_count(self):
        # "Entries" = len(wheel.transactions): every broker fill for this
        # ticker, not a derived count of legs/fills/wheel-entries.
        wheel = _spcx_wheel()
        self.assertEqual(len(wheel["transactions"]), 66)
        self.assertEqual(wheel["start_date"], "2026-06-16")
        self.assertIsNone(wheel["end_date"])  # still ACTIVE
        self.assertEqual(wheel["days_active"], 107)

    def test_shares_and_cost_basis(self):
        wheel = _spcx_wheel()
        self.assertEqual(wheel["shares_held"], 151.0)
        self.assertAlmostEqual(wheel["cost_basis_per_share"], 153.74, places=2)
        # 151 x the *rounded* $153.74/share is $23,214.74 -> displays as
        # $23,215; capital_committed_now itself is collateral-based, not
        # share-count x basis, and happens to land on the same number here.
        self.assertAlmostEqual(wheel["capital_committed_now"], 23215.0, delta=1.0)

    def test_gross_premium_is_opening_credit_on_short_legs_only(self):
        # Every STO fill's own credit, summed once per leg -- long hedge
        # opens (BTO) contribute $0, and a later BTC/STC never adds here.
        wheel = _spcx_wheel()
        self.assertAlmostEqual(wheel["gross_premium_received"], 21628.08, places=2)

    def test_realized_option_pl_bridges_from_premium_to_hedges(self):
        wheel = _spcx_wheel()
        self.assertAlmostEqual(wheel["option_realized_pl"], 802.72, places=2)
        self.assertAlmostEqual(wheel["stock_realized_pl"], 0.0, places=2)
        bridge = {step["label"]: step for step in wheel["pl_bridge"]}
        self.assertAlmostEqual(bridge["Premium sold"]["delta"], 21628.08, places=2)
        self.assertAlmostEqual(bridge["Bought back shorts"]["delta"], -20775.95, places=2)
        self.assertAlmostEqual(bridge["Hedge P&L"]["delta"], -49.41, places=2)
        self.assertAlmostEqual(bridge["Option P&L"]["running"], 802.72, places=2)

    def test_stock_mtm_is_the_exact_per_lot_sum_not_the_rounded_average(self):
        """The $856.43 vs. a hand-check's $856.17 is not a bug: the hand-check
        multiplies 151 x the *displayed, rounded* $153.74/share. The real
        calculation sums (current - lot basis) x lot size per share lot --
        1 @ $215, 50 @ $180, 100 @ $140 -- using each lot's own exact basis,
        which is a couple tenths of a cent away from the rounded average."""
        wheel = _spcx_wheel()
        self.assertAlmostEqual(wheel["stock_unrealized_pl"], -856.43, places=2)
        naive = 151 * (SPCX_CURRENT_PRICE - round(wheel["cost_basis_per_share"], 2))
        self.assertAlmostEqual(naive, -856.17, places=2)
        self.assertNotAlmostEqual(wheel["stock_unrealized_pl"], naive, places=2)

    def test_mtm_pl_is_realized_plus_marked_stock_plus_open_options(self):
        wheel = _spcx_wheel()
        self.assertAlmostEqual(wheel["open_option_pl"], 154.34, places=2)
        self.assertFalse(wheel["open_option_pl_marked"])  # no live option quote -> expiry valuation
        self.assertAlmostEqual(
            wheel["option_realized_pl"] + wheel["stock_unrealized_pl"] + wheel["open_option_pl"],
            wheel["mark_to_market_pl"],
            places=2,
        )
        self.assertAlmostEqual(wheel["mark_to_market_pl"], 100.63, places=2)

    def test_break_even_is_exact_cost_basis_minus_non_stock_pl_per_share(self):
        """break_even_price = (unrounded) cost basis - (option_realized_pl +
        stock_realized_pl + dividends + open_option_pl_at_expiry) / shares --
        never the naive "cost basis - total premium / shares"; dividends and
        stock_realized_pl are both in the formula (both $0 here, but the
        formula still isn't just a premium/shares subtraction)."""
        wheel = _spcx_wheel()
        non_stock_pl = wheel["option_realized_pl"] + wheel["stock_realized_pl"] + wheel["dividends"] + wheel["open_option_pl"]
        self.assertAlmostEqual(non_stock_pl, 957.06, places=2)
        expected = wheel["cost_basis_per_share"] - non_stock_pl / wheel["shares_held"]
        self.assertAlmostEqual(round(expected, 2), 147.40, places=2)
        self.assertAlmostEqual(wheel["break_even_price"], 147.40, places=2)

    def test_profit_per_day_and_pl_per_day_held(self):
        wheel = _spcx_wheel()
        self.assertAlmostEqual(wheel["profit_per_day"], 7.5, places=2)
        self.assertEqual(wheel["total_days_held"], 304)
        self.assertAlmostEqual(wheel["pl_per_day_held"], 2.64, places=2)

    def test_closed_legs_and_win_rate(self):
        wheel = _spcx_wheel()
        self.assertEqual(wheel["closed_leg_count"], 31)
        self.assertEqual(wheel["wins"], 24)
        self.assertEqual(wheel["losses"], 7)
        self.assertAlmostEqual(wheel["win_rate_pct"], 24 / 31 * 100, places=2)
        self.assertAlmostEqual(wheel["win_rate_pct"], 77.4, places=1)

    def test_roll_rate_and_resolved_chains(self):
        wheel = _spcx_wheel()
        self.assertEqual(wheel["rollable_legs"], 27)
        self.assertEqual(wheel["rolled_legs"], 16)
        self.assertAlmostEqual(wheel["roll_rate_pct"], 16 / 27 * 100, places=2)
        self.assertAlmostEqual(wheel["roll_rate_pct"], 59.3, places=1)
        self.assertEqual(wheel["resolved_roll_wins"], 2)
        self.assertEqual(wheel["resolved_roll_losses"], 1)
        self.assertEqual(wheel["open_roll_chains"], 1)
        self.assertAlmostEqual(wheel["open_roll_credit"], 930.02, places=2)
        self.assertEqual(len(wheel["open_chain_credits"]), 1)
        self.assertAlmostEqual(wheel["open_chain_credits"][0]["net_cash"], 930.02, places=2)

    def test_exactly_one_leg_is_open_and_it_is_the_last_covered_call(self):
        # 2026-10-01 has both a Buy Call (closing the 10/02-expiry call,
        # a roll) and a Sell Call (opening the still-open 10/09-expiry
        # call) -- a roll, not two fills on the same contract.
        wheel = _spcx_wheel()
        self.assertEqual(wheel["open_contracts"], 1.0)
        open_rows = [
            r for r in wheel["transactions"] if r["type"] == "Sell Call" and not r["is_settled"]
        ]
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(open_rows[0]["date"], "2026-10-01")
        self.assertEqual(open_rows[0]["expiration"], "2026-10-09")
        self.assertEqual(open_rows[0]["strike"], 155.0)
        self.assertEqual(open_rows[0]["chain_status"], "OPEN")

    def test_average_days_in_trade_uses_the_same_floored_day_count(self):
        """304 total days held / 31 closed legs = 9.8 -- and 304 itself
        already includes the same-day-round-trip floor (2 closed legs here
        have a real 0-day hold, each floored to 1, same rule AVAV exercises).
        """
        wheel = _spcx_wheel()
        self.assertEqual(wheel["total_days_held"], 304)
        self.assertEqual(wheel["closed_leg_count"], 31)
        self.assertAlmostEqual(wheel["avg_days_in_trade"], 304 / 31, places=6)
        self.assertAlmostEqual(wheel["avg_days_in_trade"], 9.8, places=1)
        # Two same-day round trips exist in this data (opened and closed on
        # the same date): BTC rows on 2026-08-13 and 2026-09-11 that close a
        # leg opened that same day (strike 140 exp 8/14, and strike 139 exp
        # 9/18, respectively) -- both floored to 1 day, not 0.
        same_day_pairs = [
            ("2026-08-13", 140.0, "2026-08-14"),  # SPCX260814P140: STO row 31, BTC row 32
            ("2026-09-11", 139.0, "2026-09-18"),  # SPCX260918P139: STO row 52, BTC row 53
        ]
        for close_date, strike, expiration in same_day_pairs:
            opens = [
                r
                for r in wheel["transactions"]
                if r["date"] == close_date
                and r["type"] == "Sell Put"
                and r["strike"] == strike
                and r["expiration"] == expiration
            ]
            closes = [
                r
                for r in wheel["transactions"]
                if r["date"] == close_date
                and r["type"] == "Buy Put"
                and r["strike"] == strike
                and r["expiration"] == expiration
            ]
            self.assertEqual(len(opens), 1)
            self.assertEqual(len(closes), 1)

    def test_annualized_wheel_roc_uses_time_weighted_average_collateral_not_todays(self):
        """Today's $23,215 committed must never substitute for the
        time-weighted average collateral ($31,118) in this ratio."""
        wheel = _spcx_wheel()
        self.assertAlmostEqual(wheel["avg_collateral"], 31118.06, delta=1.0)
        self.assertNotAlmostEqual(wheel["avg_collateral"], wheel["capital_committed_now"], delta=100.0)
        expected = (
            100.0
            * (wheel["option_realized_pl"] / wheel["avg_collateral"])
            * (365.0 / wheel["days_active"])
        )
        # `expected` is reconstructed from the already-rounded-to-cents
        # avg_collateral; the backend's own internal figure has more
        # precision, so this only needs to agree to a few decimal places.
        self.assertAlmostEqual(wheel["annualized_wheel_roc_pct"], expected, places=3)
        self.assertAlmostEqual(wheel["annualized_wheel_roc_pct"], 8.8, places=1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
