"""Trade Log payload: per-wheel summary + transaction ledger (wheel/api.py)."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_engine import tx  # noqa: E402
from wheel.api import Dashboard, _trade_log_entry  # noqa: E402
from wheel.engine import build_cycles  # noqa: E402
from wheel.parser import ASSIGNED, BTC, BTO, EXPIRED, STC, STO  # noqa: E402


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

    def test_closed_wheel_summary_fields(self):
        rows = _trade_log(
            [
                tx("2025-01-06", STO, "-MU250117P100", -1, 2.00, 199.33, row_id=1, commission=0.65, fees=0.02),
                tx("2025-01-17", EXPIRED, "-MU250117P100", 1, None, 0.0, row_id=2, as_of="2025-01-17"),
            ]
        )
        (wheel,) = rows["wheels"]
        self.assertFalse(wheel["is_open"])
        self.assertEqual(wheel["status"], "CLOSED")
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

    def test_pl_per_day_held_is_negative_for_a_losing_long(self):
        rows = _trade_log(
            [
                tx("2025-03-01", BTO, "-MU250321P90", 1, 1.0, -100.66, row_id=1),
                tx("2025-03-21", EXPIRED, "-MU250321P90", -1, None, 0.0, row_id=2, as_of="2025-03-21"),
            ]
        )
        (wheel,) = rows["wheels"]
        self.assertAlmostEqual(wheel["closed_leg_pl"], -100.66, places=2)
        self.assertLess(wheel["pl_per_day_held"], 0)

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
