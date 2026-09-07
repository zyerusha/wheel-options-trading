"""Periodic P/L histogram: wheel/metrics.py `periodic_pl_series`."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_engine import tx  # noqa: E402
from wheel.accounts import _combine_period_pl  # noqa: E402
from wheel.engine import build_cycles  # noqa: E402
from wheel.metrics import periodic_pl_series  # noqa: E402
from wheel.parser import BTC, BUY_STOCK, SELL_STOCK, STO  # noqa: E402


class TestFlows(unittest.TestCase):
    def test_option_premium_lands_in_its_own_week(self):
        cycles, _ = build_cycles(
            [
                tx("2025-06-02", STO, "-MU250620P90", -1, 2.0, 200.0, row_id=1),  # Monday
                tx("2025-06-04", BTC, "-MU250620P90", 1, 0.5, -50.0, row_id=2),
            ]
        )
        rows = periodic_pl_series(cycles, date(2025, 6, 4), "week")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["period"], "2025-06-02")
        self.assertEqual(row["week_start"], "2025-06-02")
        self.assertEqual(row["week_end"], "2025-06-08")
        self.assertAlmostEqual(row["net_premium"], 150.0, places=2)
        self.assertEqual(row["closed_pl"], 0.0)
        self.assertAlmostEqual(row["net_pl"], 150.0, places=2)
        self.assertNotIn("open_pl", row)

    def test_stock_disposal_lands_in_closed_pl(self):
        cycles, _ = build_cycles(
            [
                tx("2025-06-01", BUY_STOCK, "XYZ", 100, 50.0, -5000.0, row_id=1),
                tx("2025-06-10", SELL_STOCK, "XYZ", -100, 60.0, 6000.0, row_id=2),
            ]
        )
        rows = periodic_pl_series(cycles, date(2025, 6, 10), "week")
        by_week = {row["period"]: row for row in rows}
        self.assertAlmostEqual(by_week["2025-06-09"]["closed_pl"], 1000.0, places=2)
        self.assertEqual(by_week["2025-06-09"]["net_premium"], 0.0)
        self.assertAlmostEqual(by_week["2025-06-09"]["net_pl"], 1000.0, places=2)

    def test_zero_fill_between_active_weeks(self):
        cycles, _ = build_cycles(
            [
                tx("2025-06-02", STO, "-MU250620P90", -1, 2.0, 200.0, row_id=1),
                tx("2025-06-04", BTC, "-MU250620P90", 1, 0.5, -50.0, row_id=2),
            ]
        )
        rows = periodic_pl_series(cycles, date(2025, 6, 20), "week")
        self.assertEqual([r["period"] for r in rows], ["2025-06-02", "2025-06-09", "2025-06-16"])
        self.assertEqual(rows[1]["net_pl"], 0.0)
        self.assertEqual(rows[2]["net_pl"], 0.0)

    def test_no_activity_returns_no_rows(self):
        self.assertEqual(periodic_pl_series([], date(2025, 6, 20), "week"), [])

    def test_a_purely_buy_and_hold_cycle_contributes_nothing(self):
        """No option leg ever closed and no share was ever sold, so there is no
        realized flow to bucket -- Open P/L (unrealized on the still-held
        shares) was deliberately removed, so a plain hold produces no rows.
        """
        cycles, _ = build_cycles([tx("2025-06-01", BUY_STOCK, "ABC", 100, 10.0, -1000.0, row_id=1)])
        self.assertEqual(periodic_pl_series(cycles, date(2025, 6, 20), "week"), [])

    def test_monthly_bucketing(self):
        cycles, _ = build_cycles(
            [
                tx("2025-06-02", STO, "-MU250620P90", -1, 2.0, 200.0, row_id=1),
                tx("2025-06-04", BTC, "-MU250620P90", 1, 0.5, -50.0, row_id=2),
                tx("2025-07-10", STO, "-MU250801P90", -1, 1.0, 100.0, row_id=3),
                tx("2025-07-15", BTC, "-MU250801P90", 1, 0.2, -20.0, row_id=4),
            ]
        )
        rows = periodic_pl_series(cycles, date(2025, 7, 15), "month")
        self.assertEqual([r["period"] for r in rows], ["2025-06", "2025-07"])
        self.assertEqual(rows[0]["year"], 2025)
        self.assertEqual(rows[0]["month"], 6)
        self.assertAlmostEqual(rows[0]["net_premium"], 150.0, places=2)
        self.assertAlmostEqual(rows[1]["net_premium"], 80.0, places=2)

    def test_bad_granularity_raises(self):
        with self.assertRaises(ValueError):
            periodic_pl_series([], date(2025, 1, 1), "day")


class TestCombine(unittest.TestCase):
    def test_combine_sums_matching_periods_across_accounts(self):
        payloads = {
            "IRA": {
                "period_pl": {
                    "weeks": [
                        {
                            "period": "2025-06-02",
                            "week_start": "2025-06-02",
                            "week_end": "2025-06-08",
                            "net_premium": 100.0,
                            "closed_pl": 0.0,
                            "net_pl": 100.0,
                        }
                    ],
                    "months": [],
                }
            },
            "Joint": {
                "period_pl": {
                    "weeks": [
                        {
                            "period": "2025-06-02",
                            "week_start": "2025-06-02",
                            "week_end": "2025-06-08",
                            "net_premium": 20.0,
                            "closed_pl": 10.0,
                            "net_pl": 30.0,
                        }
                    ],
                    "months": [],
                }
            },
        }
        combined = _combine_period_pl(payloads, "weeks")
        self.assertEqual(len(combined), 1)
        row = combined[0]
        self.assertEqual(row["period"], "2025-06-02")
        self.assertEqual(row["week_start"], "2025-06-02")
        self.assertAlmostEqual(row["net_premium"], 120.0, places=2)
        self.assertAlmostEqual(row["closed_pl"], 10.0, places=2)
        self.assertAlmostEqual(row["net_pl"], 130.0, places=2)
        self.assertNotIn("open_pl", row)

    def test_combine_tolerates_missing_key(self):
        self.assertEqual(_combine_period_pl({"IRA": {}}, "weeks"), [])


if __name__ == "__main__":
    unittest.main()
