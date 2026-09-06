"""Open option positions table payload: wheel/api.py `_build_open_positions` +
`_open_position_row`, and wheel/accounts.py `_combine_open_positions`."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_engine import tx  # noqa: E402
from wheel.accounts import _combine_open_positions  # noqa: E402
from wheel.api import Dashboard  # noqa: E402
from wheel.engine import build_cycles  # noqa: E402
from wheel.parser import ASSIGNED, BTC, BTO, STC, STO  # noqa: E402


def _positions(transactions, names=None, prices=None, prev=None, wheels=()) -> list[dict]:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.transactions = transactions
    dashboard.all_cycles, dashboard.engine = build_cycles(transactions)
    dashboard._company_names = names or {}
    return Dashboard._build_open_positions(dashboard, prices or {}, prev or {}, wheels)


class TestDetection(unittest.TestCase):
    def test_open_cash_secured_put_is_one_ccp_row(self):
        rows = _positions(
            [tx("2025-06-01", STO, "-MU250801P90", -1, 1.50, 150.0, row_id=1)],
            prices={"MU": 95.0},
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["underlying"], "MU")
        self.assertEqual(row["type"], "CSP")
        self.assertEqual(row["strike"], 90.0)
        self.assertEqual(row["signed_contracts"], -1.0)
        self.assertEqual(row["net_premium"], 150.0)
        # strike - premium/share
        self.assertAlmostEqual(row["breakeven"], 88.5, places=2)
        # 100 * (95 - 90) / 95, positive -> out of the money
        self.assertAlmostEqual(row["moneyness_pct"], 5.26, places=2)
        self.assertFalse(row["in_the_money"])
        self.assertEqual(row["days_to_expiry"], 61)
        # 100 * (150 / 9000) * (365 / 61)
        self.assertAlmostEqual(row["annualized_yield_pct"], 9.97, places=2)

    def test_in_the_money_put_flags_negative_cushion(self):
        rows = _positions(
            [tx("2025-06-01", STO, "-MU250801P90", -1, 1.50, 150.0, row_id=1)],
            prices={"MU": 84.0},
        )
        self.assertTrue(rows[0]["in_the_money"])
        self.assertLess(rows[0]["moneyness_pct"], 0)

    def test_last_close_pct_uses_prior_close(self):
        rows = _positions(
            [tx("2025-06-01", STO, "-MU250801P90", -1, 1.50, 150.0, row_id=1)],
            prices={"MU": 102.0},
            prev={"MU": 100.0},
        )
        self.assertAlmostEqual(rows[0]["last_close_pct"], 2.0, places=2)

    def test_no_price_leaves_moneyness_null(self):
        rows = _positions([tx("2025-06-01", STO, "-MU250801P90", -1, 1.50, 150.0, row_id=1)])
        self.assertIsNone(rows[0]["moneyness_pct"])
        self.assertIsNone(rows[0]["in_the_money"])
        self.assertIsNone(rows[0]["last_close_pct"])

    def test_covered_call_after_assignment_is_a_cc_row(self):
        rows = _positions(
            [
                tx("2025-05-01", STO, "-MU250516P100", -1, 2.0, 200.0, row_id=1),
                tx("2025-05-16", ASSIGNED, "-MU250516P100", 1, None, 0.0, as_of="2025-05-16", row_id=2),
                tx("2025-05-19", STO, "-MU250620C110", -1, 1.0, 100.0, row_id=3),
            ],
            prices={"MU": 105.0},
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["type"], "CC")
        self.assertEqual(row["strike"], 110.0)
        # cost basis 100 (assigned at strike) - premium/share 1.0
        self.assertAlmostEqual(row["breakeven"], 99.0, places=2)
        # call: 100 * (110 - 105) / 105, positive -> out of the money
        self.assertAlmostEqual(row["moneyness_pct"], 4.76, places=2)

    def test_wheel_breakeven_comes_from_the_trade_log_wheels(self):
        txns = [tx("2025-06-01", STO, "-MU250801P90", -1, 1.50, 150.0, row_id=1)]
        cycle_id = build_cycles(txns)[0][0].cycle_id
        rows = _positions(
            txns,
            wheels=[{"cycle_id": cycle_id, "break_even_price": 82.34}],
        )
        self.assertEqual(rows[0]["cycle_id"], cycle_id)
        self.assertEqual(rows[0]["wheel_breakeven"], 82.34)

    def test_wheel_breakeven_is_null_without_a_matching_wheel(self):
        rows = _positions([tx("2025-06-01", STO, "-MU250801P90", -1, 1.50, 150.0, row_id=1)])
        self.assertIsNone(rows[0]["wheel_breakeven"])

    def test_closed_leg_is_excluded(self):
        rows = _positions(
            [
                tx("2025-06-01", STO, "-MU250801P90", -1, 1.50, 150.0, row_id=1),
                tx("2025-06-20", BTC, "-MU250801P90", 1, 0.40, -40.0, row_id=2),
            ]
        )
        self.assertEqual(rows, [])

    def test_open_long_put_is_an_lp_row_with_paid_premium(self):
        rows = _positions(
            [
                tx("2025-06-01", STO, "-MU250801P90", -1, 1.50, 150.0, row_id=1),
                tx("2025-06-02", BTO, "-MU251121P80", 1, 5.0, -500.0, row_id=2),
            ],
            prices={"MU": 95.0},
        )
        self.assertEqual({r["type"] for r in rows}, {"CSP", "LP"})
        lp = next(r for r in rows if r["type"] == "LP")
        self.assertEqual(lp["side"], "LONG")
        self.assertEqual(lp["strike"], 80.0)
        # premium was paid: a debit, so a negative number
        self.assertEqual(lp["net_premium"], -500.0)
        # long put break-even = strike - cost/share
        self.assertAlmostEqual(lp["breakeven"], 75.0, places=2)
        # long: reads positive, unlike a short leg's negative count
        self.assertEqual(lp["signed_contracts"], 1.0)
        # premium paid is a cost, not a yield on committed collateral
        self.assertIsNone(lp["annualized_yield_pct"])
        self.assertIsNone(lp["collateral"])

    def test_long_call_break_even_is_strike_plus_cost(self):
        rows = _positions(
            [tx("2025-06-02", BTO, "-MU251121C120", 2, 3.0, -600.0, row_id=1)],
            prices={"MU": 130.0},
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["type"], "LC")
        self.assertEqual(row["side"], "LONG")
        # cost/share = 600 / (2 * 100) = 3.0  ->  120 + 3
        self.assertAlmostEqual(row["breakeven"], 123.0, places=2)
        self.assertEqual(row["signed_contracts"], 2.0)
        # 130 > 120 -> the call has intrinsic value (in the money)
        self.assertTrue(row["in_the_money"])

    def test_closed_long_leg_is_excluded(self):
        rows = _positions(
            [
                tx("2025-06-02", BTO, "-MU251121P80", 1, 5.0, -500.0, row_id=1),
                tx("2025-06-20", STC, "-MU251121P80", -1, 4.0, 400.0, row_id=2),
            ]
        )
        self.assertEqual(rows, [])

    def test_rows_group_by_symbol_then_expiry(self):
        rows = _positions(
            [
                tx("2025-06-01", STO, "-ZZZ250801P10", -1, 0.5, 50.0, row_id=1),
                tx("2025-06-01", STO, "-AAA250905P20", -1, 0.5, 50.0, row_id=2),
                tx("2025-06-01", STO, "-AAA250801P20", -1, 0.5, 50.0, row_id=3),
            ],
            prices={"AAA": 25.0, "ZZZ": 12.0},
        )
        self.assertEqual(
            [(r["underlying"], r["expiration"]) for r in rows],
            [
                ("AAA", "2025-08-01"),
                ("AAA", "2025-09-05"),
                ("ZZZ", "2025-08-01"),
            ],
        )


class TestCombine(unittest.TestCase):
    def test_combine_prefixes_cycle_id_and_sorts(self):
        payloads = {
            "IRA": {"open_positions": [{"cycle_id": "MU-2025-1", "underlying": "MU", "expiration": "2025-08-01", "strike": 90.0}]},
            "Joint": {"open_positions": [{"cycle_id": "AA-2025-1", "underlying": "AA", "expiration": "2025-07-01", "strike": 10.0}]},
        }
        combined = _combine_open_positions(payloads)
        self.assertEqual([p["underlying"] for p in combined], ["AA", "MU"])
        self.assertEqual(combined[0]["cycle_id"], "Joint:AA-2025-1")
        self.assertEqual(combined[0]["account_id"], "Joint")

    def test_combine_tolerates_missing_key(self):
        self.assertEqual(_combine_open_positions({"IRA": {}}), [])


if __name__ == "__main__":
    unittest.main()
