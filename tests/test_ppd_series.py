"""Weekly Wheel PPD series: wheel/metrics.py `weekly_ppd_series`."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_engine import tx  # noqa: E402
from wheel.engine import build_cycles  # noqa: E402
from wheel.metrics import (  # noqa: E402
    portfolio_metrics,
    realized_pl_series,
    weekly_ppd_series,
)
from wheel.parser import BTC, EXPIRED, STO  # noqa: E402


class TestShape(unittest.TestCase):
    def test_empty_when_nothing_has_closed(self):
        cycles, _ = build_cycles([tx("2025-01-06", STO, "-MU250131P100", -1, 1.0, 99.34, row_id=1)])
        self.assertEqual(weekly_ppd_series(realized_pl_series(cycles), date(2025, 1, 6), date(2025, 3, 1)), [])

    def test_one_row_per_iso_week_zero_filled(self):
        # STO 2025-01-06 (Mon), BTC 2025-01-27 (Mon) -> weeks of Jan 6, 13, 20, 27.
        cycles, _ = build_cycles(
            [
                tx("2025-01-06", STO, "-MU250207P100", -1, 2.0, 199.34, row_id=1),
                tx("2025-01-27", BTC, "-MU250207P100", 1, 0.5, -50.66, row_id=2),
            ]
        )
        rows = weekly_ppd_series(realized_pl_series(cycles), date(2025, 1, 6), date(2025, 1, 31))
        # Only the close week has P/L; earlier weeks are present at zero.
        self.assertEqual([r["week_start"] for r in rows][0], "2025-01-27")  # first *close* week
        self.assertEqual(len(rows), 1)

    def test_weekly_and_cumulative_tracks(self):
        cycles, _ = build_cycles(
            [
                tx("2025-01-06", STO, "-MU250110P100", -1, 1.0, 100.0, row_id=1),
                tx("2025-01-10", EXPIRED, "-MU250110P100", 1, None, 0.0, row_id=2, as_of="2025-01-10"),
                tx("2025-01-13", STO, "-MU250117P100", -1, 2.0, 200.0, row_id=3),
                tx("2025-01-17", EXPIRED, "-MU250117P100", 1, None, 0.0, row_id=4, as_of="2025-01-17"),
            ]
        )
        rows = weekly_ppd_series(realized_pl_series(cycles), date(2025, 1, 6), date(2025, 1, 19))
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]["option_pl"], 100.0)
        self.assertAlmostEqual(rows[0]["weekly_ppd"], 100.0 / 7, places=2)
        self.assertAlmostEqual(rows[1]["option_pl"], 200.0)
        self.assertAlmostEqual(rows[1]["cum_option_pl"], 300.0)
        # cumulative days run from the denominator start (2025-01-06), through
        # `through` on the last row.
        self.assertEqual(rows[1]["cum_days"], (date(2025, 1, 19) - date(2025, 1, 6)).days)
        self.assertAlmostEqual(rows[1]["cum_ppd"], 300.0 / rows[1]["cum_days"], places=2)

    def test_last_cum_ppd_equals_headline_ppd(self):
        # The chart's right edge must match the Performance tile.
        txns = [
            tx("2025-02-03", STO, "-MU250214P100", -1, 3.0, 300.0, row_id=1),
            tx("2025-02-14", BTC, "-MU250214P100", 1, 0.4, -40.0, row_id=2),
            tx("2025-03-10", STO, "-MU250321P95", -1, 2.5, 250.0, row_id=3),
            tx("2025-03-21", EXPIRED, "-MU250321P95", 1, None, 0.0, row_id=4, as_of="2025-03-21"),
        ]
        cycles, _ = build_cycles(txns)
        through = date(2025, 3, 31)
        pm = portfolio_metrics(cycles, through)
        first = min(c.start_date for c in cycles)
        rows = weekly_ppd_series(realized_pl_series(cycles), first, through)
        self.assertAlmostEqual(rows[-1]["cum_ppd"], pm.profit_per_day, places=2)

    def test_non_wheel_cycles_are_excluded_from_the_series(self):
        # A real wheel + a lone directional call: the PPD numerator is
        # wheel-only, matching portfolio.profit_per_day after the is_wheel split.
        from wheel.parser import BTO

        cycles, _ = build_cycles(
            [
                tx("2025-02-03", STO, "-MU250214P100", -1, 3.0, 300.0, row_id=1),
                tx("2025-02-14", BTC, "-MU250214P100", 1, 0.4, -40.0, row_id=2),
                tx("2025-02-05", BTO, "-NVDA250214C130", 1, 2.0, -200.0, row_id=3),
                tx("2025-02-14", EXPIRED, "-NVDA250214C130", -1, None, 0.0, row_id=4, as_of="2025-02-14"),
            ]
        )
        through = date(2025, 2, 28)
        pm = portfolio_metrics(cycles, through)
        wheel_only = realized_pl_series([c for c in cycles if c.is_wheel])
        first = min(c.start_date for c in cycles)
        rows = weekly_ppd_series(wheel_only, first, through)
        self.assertAlmostEqual(rows[-1]["cum_ppd"], pm.profit_per_day, places=2)
        # The -$200 directional loss is not in the series total.
        self.assertAlmostEqual(rows[-1]["cum_option_pl"], 260.0, places=2)

    def test_denominator_start_defaults_when_first_date_missing(self):
        cycles, _ = build_cycles(
            [
                tx("2025-01-06", STO, "-MU250110P100", -1, 1.0, 100.0, row_id=1),
                tx("2025-01-10", EXPIRED, "-MU250110P100", 1, None, 0.0, row_id=2, as_of="2025-01-10"),
            ]
        )
        rows = weekly_ppd_series(realized_pl_series(cycles), None, date(2025, 1, 12))
        self.assertTrue(rows)  # falls back to the first active week, no crash


if __name__ == "__main__":
    unittest.main(verbosity=2)
