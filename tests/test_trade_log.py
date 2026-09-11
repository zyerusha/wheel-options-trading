"""Trade Log payload: per-wheel summary + transaction ledger (wheel/api.py)."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_engine import tx  # noqa: E402
from wheel.api import Dashboard, _cc_strike_floor, _profit_target, _trade_log_entry  # noqa: E402
from wheel.engine import build_cycles  # noqa: E402
from wheel.parser import ASSIGNED, BTC, BTO, BUY_STOCK, EXPIRED, OTHER, SELL_STOCK, STC, STO  # noqa: E402


def _trade_log(transactions, names=None, prices=None) -> dict:
    """Run ``Dashboard._build_trade_log`` against a hand-built transaction list,
    without touching disk or the network (the ``__init__`` pipeline).
    """
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.transactions = transactions
    dashboard.all_cycles, dashboard.engine = build_cycles(transactions)
    dashboard._company_names = names or {}
    return Dashboard._build_trade_log(dashboard, prices or {})


class TestTransactionRows(unittest.TestCase):
    def test_type_mapping_and_csp_collateral(self):
        rows = _trade_log(
            [
                tx("2025-01-06", STO, "-MU250117P100", -2, 1.00, 199.34, row_id=1, commission=1.30, fees=0.02),
                tx("2025-01-15", BTC, "-MU250117P100", 2, 0.20, -40.02, row_id=2, commission=0.0, fees=0.02),
            ]
        )
        (wheel,) = rows["wheels"]
        sell, buy = wheel["transactions"]

        self.assertEqual(sell["type"], "Sell Put")
        self.assertEqual(buy["type"], "Buy Put")
        # Long/bought positive, short/sold negative; `quantity` stays unsigned.
        self.assertEqual(sell["signed_quantity"], -2)
        self.assertEqual(buy["signed_quantity"], 2)
        self.assertEqual(sell["quantity"], 2)
        # Fees and commissions stay in their own columns.
        self.assertEqual(sell["fees"], 0.02)
        self.assertEqual(sell["commission"], 1.30)
        # Initial CSP collateral is set only on the cash-secured-put open.
        self.assertEqual(sell["initial_csp_collateral"], 100 * 100 * 2)
        self.assertIsNone(buy["initial_csp_collateral"])
        # Net cash flow is the broker's own Amount, already fee/commission-net.
        self.assertEqual(sell["net_cash_flow"], 199.34)
        self.assertEqual(buy["net_cash_flow"], -40.02)
        # Cumulative is the running sum.
        self.assertEqual(sell["running_cash_flow"], 199.34)
        self.assertAlmostEqual(buy["running_cash_flow"], 159.32)

    def test_share_row_greys_when_its_own_lot_is_sold_not_only_when_cycle_flat(self):
        rows = _trade_log(
            [
                # CSP -> assigned keeps this one continuous wheel cycle alive
                # while shares rotate through it.
                tx("2025-06-02", STO, "-GILD250620P100", -1, 2.0, 200.0, row_id=1),
                tx("2025-06-20", ASSIGNED, "-GILD250620P100", 1, None, 0.0, row_id=2, as_of="2025-06-20"),
                tx("2025-07-10", BUY_STOCK, "GILD", 100, 118.0, -11800.0, row_id=3),
                tx("2025-07-20", BUY_STOCK, "GILD", 100, 119.0, -11900.0, row_id=4),
                # FIFO: retires the assigned 2025-06-20 lot.
                tx("2025-08-01", SELL_STOCK, "GILD", -100, 130.0, 13000.0, row_id=5),
                # FIFO: retires the 2025-07-10 lot.
                tx("2025-08-10", SELL_STOCK, "GILD", -100, 131.0, 13100.0, row_id=6),
                # Bought again -- the cycle is NOT flat, it still holds 200 shares.
                tx("2025-09-01", BUY_STOCK, "GILD", 100, 125.0, -12500.0, row_id=7),
            ]
        )
        (wheel,) = rows["wheels"]
        self.assertEqual(wheel["status"], "ACTIVE")
        self.assertEqual(wheel["shares_held"], 200.0)

        buys = [r for r in wheel["transactions"] if r["type"] == "Buy Shares"]
        self.assertEqual([b["date"] for b in buys], ["2025-07-10", "2025-07-20", "2025-09-01"])
        # The 2025-07-10 lot is gone even though the cycle still holds shares -> greyed.
        self.assertTrue(buys[0]["is_settled"])
        # The 2025-07-20 and 2025-09-01 lots are still held.
        self.assertFalse(buys[1]["is_settled"])
        self.assertFalse(buys[2]["is_settled"])

        # The assigned lot is also gone -> its row greys too.
        assigned = [r for r in wheel["transactions"] if r["type"] == "Shares Assigned"]
        self.assertEqual(len(assigned), 1)
        self.assertTrue(assigned[0]["is_settled"])

        # A sale is always settled on arrival.
        sells = [r for r in wheel["transactions"] if r["type"] == "Sell Shares"]
        self.assertTrue(all(s["is_settled"] for s in sells))

    def test_dividend_row_is_settled_immediately(self):
        rows = _trade_log(
            [
                tx("2025-01-02", STO, "-MU250207P100", -1, 3.00, 300.0, row_id=1),
                tx("2025-01-17", ASSIGNED, "-MU250207P100", 1, None, 0.0, row_id=2, as_of="2025-01-17"),
                tx("2025-02-01", OTHER, "MU", 0, None, 12.34, row_id=3, action_raw="DIVIDEND RECEIVED MICRON"),
            ]
        )
        (wheel,) = rows["wheels"]
        div = [r for r in wheel["transactions"] if r["type"] == "Dividend"]
        self.assertEqual(len(div), 1)
        # A dividend is cash received, complete on arrival -> greyed like a
        # closed leg (frontend keys the row style off is_settled).
        self.assertTrue(div[0]["is_settled"])
        self.assertEqual(div[0]["net_cash_flow"], 12.34)

    def test_synthetic_assignment_row_when_export_has_no_equity_leg(self):
        rows = _trade_log(
            [
                tx("2025-01-06", STO, "-MU250117P100", -1, 2.00, 199.33, row_id=1),
                tx("2025-01-17", ASSIGNED, "-MU250117P100", 1, None, 0.0, row_id=2, as_of="2025-01-17"),
            ]
        )
        (wheel,) = rows["wheels"]
        synth = [r for r in wheel["transactions"] if r["synthetic"]]
        self.assertEqual(len(synth), 1)
        self.assertEqual(synth[0]["type"], "Shares Assigned")
        self.assertEqual(synth[0]["quantity"], 100.0)
        self.assertEqual(synth[0]["signed_quantity"], 100.0)  # shares acquired -> +
        self.assertEqual(synth[0]["strike"], 100.0)
        self.assertAlmostEqual(synth[0]["net_cash_flow"], -10000.0)
        self.assertTrue(wheel["is_open"])  # still holding the shares
        self.assertEqual(wheel["status"], "ACTIVE")

    def test_flat_wheel_summary_fields(self):
        rows = _trade_log(
            [
                tx("2025-01-06", STO, "-MU250117P100", -1, 2.00, 199.33, row_id=1, commission=0.65, fees=0.02),
                tx("2025-01-17", EXPIRED, "-MU250117P100", 1, None, 0.0, row_id=2, as_of="2025-01-17"),
            ]
        )
        (wheel,) = rows["wheels"]
        self.assertFalse(wheel["is_open"])
        # Flat, but the book's latest trade is still 2025 -> dormant, not terminal.
        self.assertEqual(wheel["status"], "NO_ACTIVITY")
        self.assertEqual(wheel["capital_committed_now"], 0.0)
        self.assertEqual(wheel["gross_premium_received"], 199.33)
        self.assertIsNone(wheel["cost_basis_per_share"])
        self.assertIsNone(wheel["break_even_per_share"])
        self.assertAlmostEqual(wheel["total_fees_commissions"], 0.67)

    def test_pl_per_day_held(self):
        rows = _trade_log(
            [
                # 5-day short put: +99.34 open, -10.66 close -> +88.68
                tx("2025-01-01", STO, "-MU250110P100", -1, 1.0, 99.34, row_id=1),
                tx("2025-01-06", BTC, "-MU250110P100", 1, 0.10, -10.66, row_id=2),
                # 10-day short put: +199.34 open, -50.66 close -> +148.68
                tx("2025-02-01", STO, "-MU250228P100", -1, 2.0, 199.34, row_id=3),
                tx("2025-02-11", BTC, "-MU250228P100", 1, 0.50, -50.66, row_id=4),
            ]
        )
        (wheel,) = rows["wheels"]
        self.assertEqual(wheel["closed_leg_count"], 2)
        self.assertEqual(wheel["total_days_held"], 15)
        self.assertAlmostEqual(wheel["closed_leg_pl"], 237.36, places=2)
        # total P&L / total days held: 237.36 / 15
        self.assertAlmostEqual(wheel["pl_per_day_held"], 15.82, places=2)

    def test_lone_directional_long_is_flagged_non_wheel_and_withholds_ratios(self):
        """A single bought-and-expired long option is not a wheel: its loss is
        real (closed_leg_pl, net_realized_pl) but the wheel-framed ratios --
        P&L / day held, win rate, Wheel ROC -- come back None (see
        Cycle.is_wheel), so a two-day premium bet cannot skew the wheel stats.
        """
        rows = _trade_log(
            [
                tx("2025-03-01", BTO, "-MU250321P90", 1, 1.0, -100.66, row_id=1),
                tx("2025-03-21", EXPIRED, "-MU250321P90", -1, None, 0.0, row_id=2, as_of="2025-03-21"),
            ]
        )
        (wheel,) = rows["wheels"]
        self.assertFalse(wheel["is_wheel"])
        self.assertAlmostEqual(wheel["closed_leg_pl"], -100.66, places=2)
        self.assertAlmostEqual(wheel["net_realized_pl"], -100.66, places=2)
        self.assertIsNone(wheel["pl_per_day_held"])
        self.assertIsNone(wheel["win_rate_pct"])
        self.assertIsNone(wheel["annualized_wheel_roc_pct"])
        self.assertIsNone(wheel["roi_on_avg_wheel_pct"])

    def test_pl_per_day_held_is_none_when_no_leg_has_closed(self):
        rows = _trade_log([tx("2025-01-01", STO, "-MU250131P100", -1, 1.0, 99.34, row_id=1)])
        (wheel,) = rows["wheels"]
        self.assertIsNone(wheel["pl_per_day_held"])
        self.assertEqual(wheel["closed_leg_count"], 0)

    def test_mark_to_market_pl_and_break_even_price(self):
        # STO put -> assigned 100 sh @ $100 (kept $300 premium), then STO a
        # covered call still open for +$150, stock now at $92.
        rows = _trade_log(
            [
                tx("2025-01-02", STO, "-MU250117P100", -1, 3.0, 300.0, row_id=1),
                tx("2025-01-17", ASSIGNED, "-MU250117P100", 1, None, 0.0, row_id=2, as_of="2025-01-17"),
                tx("2025-01-20", STO, "-MU250221C105", -1, 1.5, 150.0, row_id=3),
            ],
            prices={"MU": 92.0},
        )
        (wheel,) = rows["wheels"]
        self.assertEqual(wheel["shares_held"], 100.0)
        # break-even: $100 cost - ($300 kept put premium + $150 open call) / 100
        self.assertAlmostEqual(wheel["break_even_price"], 100.0 - 4.5, places=2)
        # mark-to-market: +300 realized option, +150 open call, shares -$800 (92 vs 100)
        self.assertAlmostEqual(wheel["stock_unrealized_pl"], -800.0, places=2)
        self.assertAlmostEqual(wheel["mark_to_market_pl"], 300.0 + 150.0 - 800.0, places=2)
        # ~ shares_held * (current - break_even)
        self.assertAlmostEqual(
            wheel["mark_to_market_pl"], 100.0 * (92.0 - wheel["break_even_price"]), places=2
        )

    def test_running_break_even_progression_and_final_row(self):
        # STO put +$300 -> assigned 100 sh @ $100 -> STO covered call +$150,
        # still open. Break-even should be a dash until shares land, step down
        # $3/sh on assignment and another $1.50/sh on the call, and the last
        # share-holding row must equal the summary's break_even_price.
        rows = _trade_log(
            [
                tx("2025-01-02", STO, "-MU250117P100", -1, 3.0, 300.0, row_id=1),
                tx("2025-01-17", ASSIGNED, "-MU250117P100", 1, None, 0.0, row_id=2, as_of="2025-01-17"),
                tx("2025-01-20", STO, "-MU250221C105", -1, 1.5, 150.0, row_id=3),
            ],
            prices={"MU": 92.0},
        )
        (wheel,) = rows["wheels"]
        txns = wheel["transactions"]
        by_type = {r["type"]: r for r in txns}
        # Put sold before any shares exist -> no break-even yet.
        self.assertIsNone(by_type["Sell Put"]["running_break_even"])
        # 100 sh assigned at $100, $300 premium already banked -> $97.00.
        self.assertAlmostEqual(by_type["Shares Assigned"]["running_break_even"], 97.0, places=2)
        # Covered call adds $150 / 100 sh -> $95.50.
        self.assertAlmostEqual(by_type["Sell Call"]["running_break_even"], 95.5, places=2)
        # Final populated row is exactly the summary figure.
        last_with_shares = [r for r in txns if r["running_break_even"] is not None][-1]
        self.assertAlmostEqual(
            last_with_shares["running_break_even"], wheel["break_even_price"], places=2
        )
        # And it tracks -cumulative cash flow / shares held.
        self.assertAlmostEqual(
            by_type["Sell Call"]["running_break_even"],
            -by_type["Sell Call"]["running_cash_flow"] / wheel["shares_held"],
            places=2,
        )

    def test_running_break_even_is_none_after_shares_are_sold_off(self):
        rows = _trade_log(
            [
                tx("2025-01-02", STO, "-MU250117P100", -1, 3.0, 300.0, row_id=1),
                tx("2025-01-17", ASSIGNED, "-MU250117P100", 1, None, 0.0, row_id=2, as_of="2025-01-17"),
                tx("2025-02-01", SELL_STOCK, "MU", -100, 101.0, 10100.0, row_id=3),
            ],
        )
        (wheel,) = rows["wheels"]
        txns = wheel["transactions"]
        by_type = {r["type"]: r for r in txns}
        self.assertEqual(wheel["shares_held"], 0.0)
        # While the 100 sh were held the row still showed the break-even -- the
        # historical progression is not erased just because the wheel is flat now.
        self.assertAlmostEqual(by_type["Shares Assigned"]["running_break_even"], 97.0, places=2)
        # Flat again -> the closing row has no break-even, matching the summary.
        self.assertIsNone(txns[-1]["running_break_even"])
        self.assertIsNone(wheel["break_even_price"])

    # ---- Profit Target / CC Strike Floor / Preferred CSP Entry split -------

    def _cc_phase_fixture(self, price):
        """STO put -> assigned 100 sh @ $100 (banked $300) -> STO covered call
        still open (+$150). cost_basis=100, break_even/break_even_price=95.5,
        so the profit-target floor is cost_basis (100) regardless of price."""
        return _trade_log(
            [
                tx("2025-01-02", STO, "-MU250117P100", -1, 3.0, 300.0, row_id=1),
                tx("2025-01-17", ASSIGNED, "-MU250117P100", 1, None, 0.0, row_id=2, as_of="2025-01-17"),
                tx("2025-01-20", STO, "-MU250221C105", -1, 1.5, 150.0, row_id=3),
            ],
            prices={"MU": price},
        )

    def test_profit_target_stable_under_a_price_rally(self):
        expected = _profit_target(100.0)  # cost_basis dominates the floor here
        for price in (92.0, 150.0, 500.0):
            (wheel,) = self._cc_phase_fixture(price)["wheels"]
            self.assertAlmostEqual(wheel["profit_target"], expected, places=2)

    def test_profit_target_formula_excludes_current_price(self):
        for price in (92.0, 150.0, 500.0):
            (wheel,) = self._cc_phase_fixture(price)["wheels"]
            floor = max(
                v
                for v in (
                    wheel["cost_basis_per_share"],
                    wheel["break_even_per_share"],
                    wheel["break_even_price"],
                )
                if v is not None
            )
            self.assertAlmostEqual(wheel["profit_target"], _profit_target(floor), places=2)

    def test_cc_strike_floor_available_with_an_open_cc(self):
        # This wheel holds shares AND already has an open covered call (the
        # STO call above is never bought back) -- unlike _build_cc_candidates'
        # target_cc_strike, which would exclude a wheel like this one entirely
        # (its cc_cycle_ids filter), cc_strike_floor is still populated: it's
        # useful for planning the *next* roll of an already-open call.
        (wheel,) = self._cc_phase_fixture(92.0)["wheels"]
        self.assertIsNotNone(wheel["cc_strike_floor"])

    def test_cc_strike_floor_matches_shared_helper(self):
        (wheel,) = self._cc_phase_fixture(92.0)["wheels"]
        expected = _cc_strike_floor(
            wheel["cost_basis_per_share"],
            wheel["break_even_per_share"],
            wheel["break_even_price"],
            92.0,
        )
        self.assertAlmostEqual(wheel["cc_strike_floor"], expected, places=2)

    def test_crox_style_price_rally_case(self):
        # A plain share buy (no premium banked yet) at ~$95.43, price now
        # ~$112.48 -- reproduces the real CROX numbers that exposed the
        # original "stuck target" problem: Profit Target stays low/stable,
        # CC Strike Floor tracks the current price, and they differ.
        rows = _trade_log(
            [tx("2025-07-18", BUY_STOCK, "CROX", 290, 95.43, -290 * 95.43, row_id=1)],
            prices={"CROX": 112.48},
        )
        (wheel,) = rows["wheels"]
        self.assertAlmostEqual(wheel["profit_target"], 97.5, places=2)
        self.assertAlmostEqual(wheel["cc_strike_floor"], 112.5, places=2)
        self.assertNotAlmostEqual(wheel["profit_target"], wheel["cc_strike_floor"], places=2)

    def test_csp_preferred_entry_formula(self):
        rows = _trade_log(
            [tx("2025-01-02", STO, "-MU250117P100", -1, 3.0, 300.0, row_id=1)],
            prices={"MU": 120.0},
        )
        (wheel,) = rows["wheels"]
        self.assertEqual(wheel["wheel_phase"], "csp")
        self.assertAlmostEqual(wheel["preferred_csp_entry"], 111.5, places=2)  # floor(120*0.93 -> $0.50)

    def test_csp_phase_has_no_cc_strike_floor(self):
        rows = _trade_log(
            [tx("2025-01-02", STO, "-MU250117P100", -1, 3.0, 300.0, row_id=1)],
            prices={"MU": 120.0},
        )
        (wheel,) = rows["wheels"]
        self.assertIsNone(wheel["cc_strike_floor"])

    def test_cc_phase_has_no_preferred_csp_entry(self):
        (wheel,) = self._cc_phase_fixture(92.0)["wheels"]
        self.assertEqual(wheel["wheel_phase"], "cc")
        self.assertIsNone(wheel["preferred_csp_entry"])

    def test_historical_running_profit_target_unchanged(self):
        # Same fixture/expectations as test_running_break_even_progression_
        # and_final_row -- confirms the renamed running_profit_target field
        # still builds off running_break_even the same way, and the pin still
        # lands exactly on the summary's profit_target.
        rows = _trade_log(
            [
                tx("2025-01-02", STO, "-MU250117P100", -1, 3.0, 300.0, row_id=1),
                tx("2025-01-17", ASSIGNED, "-MU250117P100", 1, None, 0.0, row_id=2, as_of="2025-01-17"),
                tx("2025-01-20", STO, "-MU250221C105", -1, 1.5, 150.0, row_id=3),
            ],
            prices={"MU": 92.0},
        )
        (wheel,) = rows["wheels"]
        txns = wheel["transactions"]
        by_type = {r["type"]: r for r in txns}
        self.assertIsNone(by_type["Sell Put"]["running_profit_target"])
        self.assertAlmostEqual(
            by_type["Shares Assigned"]["running_profit_target"], _profit_target(100.0), places=2
        )
        last_with_target = [r for r in txns if r["running_profit_target"] is not None][-1]
        self.assertAlmostEqual(last_with_target["running_profit_target"], wheel["profit_target"], places=2)

    def test_phase_transitions_csp_to_cc_and_back_to_csp(self):
        """Same underlying's story told at three snapshots: CSP-only, then CC
        after assignment, then flat again once the call is called away (which
        closes the cycle) -- wheel_phase switches correctly at each snapshot,
        never blending a prior phase's fields into the current one."""
        all_txns = [
            tx("2025-11-17", STO, "-MU251121P230", -1, 4.00, 399.33, row_id=1),
            tx("2025-11-21", ASSIGNED, "-MU251121P230", 1, None, 0.0, row_id=2, as_of="2025-11-20"),
            tx("2025-11-24", STO, "-MU251128C235", -1, 2.00, 199.33, row_id=3),
            tx("2025-12-01", ASSIGNED, "-MU251128C235", 1, None, 0.0, row_id=4, as_of="2025-11-28"),
        ]

        # Stage 1: CSP only, no shares yet.
        (wheel1,) = _trade_log(all_txns[:1], prices={"MU": 240.0})["wheels"]
        self.assertEqual(wheel1["wheel_phase"], "csp")
        self.assertAlmostEqual(wheel1["preferred_csp_entry"], 223.0, places=2)  # floor(240*0.93 -> $0.50)
        self.assertIsNone(wheel1["profit_target"])
        self.assertIsNone(wheel1["cc_strike_floor"])

        # Stage 2: assigned, holding shares, no CC written yet.
        (wheel2,) = _trade_log(all_txns[:2], prices={"MU": 232.0})["wheels"]
        self.assertEqual(wheel2["wheel_phase"], "cc")
        self.assertIsNotNone(wheel2["profit_target"])
        self.assertIsNotNone(wheel2["cc_strike_floor"])
        self.assertIsNone(wheel2["preferred_csp_entry"])

        # Stage 3: CC written and called away -- cycle closes, flat again.
        # wheel_phase reports "csp" for this now-closed cycle too (is_open
        # marks it terminal separately) -- not a new behavior, the pre-split
        # target_price/target_phase worked the same way here.
        (wheel3,) = _trade_log(all_txns, prices={"MU": 236.0})["wheels"]
        self.assertFalse(wheel3["is_open"])
        self.assertEqual(wheel3["wheel_phase"], "csp")
        self.assertIsNotNone(wheel3["preferred_csp_entry"])
        self.assertIsNone(wheel3["profit_target"])
        self.assertIsNone(wheel3["cc_strike_floor"])

    def test_pl_bridge_sums_to_mark_to_market(self):
        rows = _trade_log(
            [
                tx("2025-01-02", STO, "-MU250117P100", -1, 3.0, 300.0, row_id=1),
                tx("2025-01-10", BTC, "-MU250117P100", 1, 1.0, -100.0, row_id=2),
                tx("2025-01-12", STO, "-MU250207P95", -1, 2.5, 250.0, row_id=3),
                tx("2025-02-07", ASSIGNED, "-MU250207P95", 1, None, 0.0, row_id=4, as_of="2025-02-07"),
                tx("2025-02-10", STO, "-MU250307C100", -1, 1.5, 150.0, row_id=5),
            ],
            prices={"MU": 90.0},
        )
        (wheel,) = rows["wheels"]
        bridge = wheel["pl_bridge"]
        self.assertEqual(bridge[0]["label"], "Premium sold")
        self.assertEqual(bridge[-1]["kind"], "total")
        # every step's running is the prior running plus its own delta
        run = 0.0
        for step in bridge:
            if step["kind"] == "step":
                run += step["delta"]
                self.assertAlmostEqual(step["running"], round(run, 2), places=2)
            else:
                self.assertAlmostEqual(step["running"], round(run, 2), places=2)
        self.assertAlmostEqual(bridge[-1]["running"], wheel["mark_to_market_pl"], places=2)

    def test_mark_to_market_pl_is_none_without_a_price(self):
        rows = _trade_log(
            [
                tx("2025-01-02", STO, "-MU250117P100", -1, 3.0, 300.0, row_id=1),
                tx("2025-01-17", ASSIGNED, "-MU250117P100", 1, None, 0.0, row_id=2, as_of="2025-01-17"),
            ]
        )  # no price supplied
        (wheel,) = rows["wheels"]
        self.assertEqual(wheel["shares_held"], 100.0)
        self.assertIsNone(wheel["mark_to_market_pl"])

    def test_close_return_pct(self):
        rows = _trade_log(
            [
                # short: sold 0.50, bought back 0.25 -> kept 50%
                tx("2025-03-03", STO, "-MU250321P100", -1, 0.50, 49.34, row_id=1),
                tx("2025-03-10", BTC, "-MU250321P100", 1, 0.25, -25.66, row_id=2),
                # long: bought 1.00, sold 0.20 -> lost 80%
                tx("2025-03-11", BTO, "-MU250418P90", 1, 1.00, -100.66, row_id=3),
                tx("2025-03-20", STC, "-MU250418P90", -1, 0.20, 19.34, row_id=4),
                # short expiring worthless -> kept 100%
                tx("2025-03-21", STO, "-MU250328P95", -1, 0.40, 39.34, row_id=5),
                tx("2025-03-28", EXPIRED, "-MU250328P95", 1, None, 0.0, row_id=6, as_of="2025-03-28"),
            ]
        )
        by_row = {(r["type"], r["date"]): r for w in rows["wheels"] for r in w["transactions"]}
        self.assertIsNone(by_row[("Sell Put", "2025-03-03")]["close_return_pct"])  # an open
        self.assertAlmostEqual(by_row[("Buy Put", "2025-03-10")]["close_return_pct"], 50.0)
        self.assertAlmostEqual(by_row[("Sell Put", "2025-03-20")]["close_return_pct"], -80.0)
        self.assertAlmostEqual(by_row[("Put Expired", "2025-03-28")]["close_return_pct"], 100.0)

    def test_company_name_is_surfaced_when_available(self):
        rows = _trade_log(
            [tx("2025-01-06", STO, "-MU250117P100", -1, 2.00, 199.33, row_id=1)],
            names={"MU": "Micron Technology"},
        )
        self.assertEqual(rows["wheels"][0]["name"], "Micron Technology")
        self.assertEqual(rows["wheels"][0]["underlying"], "MU")


class TestEngineExactPath(unittest.TestCase):
    def test_engine_exact_entry_combines_fees_and_notes_it(self):
        cycles, _ = build_cycles(
            [
                tx("2025-01-06", STO, "-MU250117P100", -1, 2.00, 199.33, row_id=1, commission=0.65, fees=0.02),
                tx("2025-01-10", BTC, "-MU250117P100", 1, 0.50, -50.67, row_id=2, commission=0.65, fees=0.02),
            ]
        )
        entry = _trade_log_entry(
            cycles[0],
            [],  # engine-exact path never reads the raw list
            date(2025, 1, 17),
            name=None,
            dividend_row_ids=set(),
            dividends=0.0,
            engine_exact=True,
        )
        self.assertIsNotNone(entry["attribution_note"])
        self.assertEqual([r["type"] for r in entry["transactions"]], ["Sell Put", "Buy Put"])
        for row in entry["transactions"]:
            self.assertIsNone(row["commission"])  # folded into fees on this path
        # open leg fee is commission + fees combined
        self.assertAlmostEqual(entry["transactions"][0]["fees"], 0.67)

    def test_engine_exact_entry_still_carries_dividend_rows(self):
        # Engine-exact derivation is legs/closes/assignments only; a dividend is
        # not an engine structure, so it has to be pulled from the raw ledger or
        # the cash column (and the running break-even) would drop it.
        div = tx("2025-01-08", OTHER, "MU", 0, None, 12.34, row_id=9, action_raw="DIVIDEND RECEIVED MICRON")
        cycles, _ = build_cycles(
            [
                tx("2025-01-06", STO, "-MU250117P100", -1, 2.00, 199.33, row_id=1),
                tx("2025-01-10", BTC, "-MU250117P100", 1, 0.50, -50.67, row_id=2),
            ]
        )
        entry = _trade_log_entry(
            cycles[0],
            [div],
            date(2025, 1, 17),
            name=None,
            dividend_row_ids={9},
            dividends=12.34,
            engine_exact=True,
        )
        div_rows = [r for r in entry["transactions"] if r["type"] == "Dividend"]
        self.assertEqual(len(div_rows), 1)
        self.assertEqual(div_rows[0]["net_cash_flow"], 12.34)


class TestBuildPayload(unittest.TestCase):
    def test_build_exposes_trade_log_with_wheels_and_warnings(self):
        import tempfile

        header = (
            "Run Date,Action,Symbol,Description,Type,Quantity,Price ($),Commission ($),"
            "Fees ($),Accrued Interest ($),Amount ($),Cash Balance ($),Settlement Date"
        )
        row_open = (
            "01/06/2025,YOU SOLD OPENING TRANSACTION,-MU250117P100,"
            "PUT (MU) MICRON TECHNOLOGY JAN 17 25 $100 (100 SHS),Cash,-1,2.00,0,0,,199.33,10000,01/06/2025"
        )
        row_exp = (
            "01/17/2025,EXPIRED as of 01/17/2025,-MU250117P100,"
            "PUT (MU) MICRON TECHNOLOGY JAN 17 25 $100 (100 SHS),Cash,1,,0,0,,0.00,10000,01/17/2025"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "History_for_Account.csv")
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                handle.write(header + "\n" + row_open + "\n" + row_exp + "\n")
            payload = Dashboard(path, position_paths=[]).build()

        self.assertIn("trade_log", payload)
        self.assertEqual(payload["trade_log"]["warnings"], [])
        (wheel,) = payload["trade_log"]["wheels"]
        self.assertEqual(wheel["underlying"], "MU")
        self.assertEqual(wheel["name"], "Micron Technology")
        self.assertEqual([r["type"] for r in wheel["transactions"]], ["Sell Put", "Put Expired"])


class TestRenamedTicker(unittest.TestCase):
    def test_pre_rename_option_rows_appear_in_the_merged_wheel(self):
        # Put sold as AXL, assigned as DCH -> one DCH wheel. Its ledger must
        # still carry the AXL sell-to-open rows, not just the DCH side.
        rows = _trade_log(
            [
                tx("2026-01-30", STO, "-AXL260220P8", -10, 0.55, 543.30, row_id=1),
                tx("2026-02-23", ASSIGNED, "-DCH260220P8", 10, None, 0.0, row_id=2, as_of="2026-02-20"),
            ]
        )
        (wheel,) = rows["wheels"]
        self.assertEqual(wheel["underlying"], "DCH")
        types = [r["type"] for r in wheel["transactions"]]
        self.assertIn("Sell Put", types)  # the AXL open
        self.assertIn("Put Assigned", types)  # the DCH assignment
        self.assertAlmostEqual(wheel["gross_premium_received"], 543.30, places=2)

    def test_symbol_change_bookkeeping_rows_are_not_shown(self):
        # A "DISTRIBUTION NAME/SYMBOL CHANGE" pair nets to $0 and is not a trade.
        rows = _trade_log(
            [
                tx("2026-01-30", STO, "-AXL260220P8", -1, 0.55, 54.33, row_id=1),
                tx("2026-02-05", OTHER, "-AXL260220P8", 1, None, 300.0, row_id=2,
                   action_raw="DISTRIBUTION NAME/SYMBOL CHANGE PUT (AXL)"),
                tx("2026-02-05", OTHER, "-DCH260220P8", -1, None, -300.0, row_id=3,
                   action_raw="DISTRIBUTION NAME/SYMBOL CHANGE PUT (DCH)"),
                tx("2026-02-23", ASSIGNED, "-DCH260220P8", 1, None, 0.0, row_id=4, as_of="2026-02-20"),
            ]
        )
        (wheel,) = rows["wheels"]
        self.assertNotIn("Other", [r["type"] for r in wheel["transactions"]])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
