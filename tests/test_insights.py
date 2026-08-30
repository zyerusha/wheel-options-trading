"""Rule-based wheel commentary (wheel/insights.py)."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_engine import tx  # noqa: E402
from wheel.engine import build_cycles  # noqa: E402
from wheel.insights import wheel_insights  # noqa: E402
from wheel.metrics import cycle_metrics  # noqa: E402
from wheel.parser import ASSIGNED, BTC, EXPIRED, STO  # noqa: E402


def _insights(transactions, *, through, current_price=None, cost_basis=None, break_even_price=None):
    cycles, _ = build_cycles(transactions)
    cycle = cycles[0]
    metrics = cycle_metrics(cycle, through, current_price=current_price)
    return wheel_insights(
        cycle,
        metrics,
        current_price=current_price,
        cost_basis=cost_basis,
        break_even_price=break_even_price,
    )


def _text(result):
    return " ".join(result["strengths"] + result["improvements"]).lower()


class TestStrengths(unittest.TestCase):
    def test_high_win_rate_is_a_strength(self):
        rows = []
        for i in range(6):
            rows.append(tx(f"2025-0{i + 1}-01", STO, f"-MU2506{10 + i:02d}P100", -1, 2.0, 199.33, row_id=2 * i + 1))
            rows.append(
                tx(f"2025-0{i + 1}-15", EXPIRED, f"-MU2506{10 + i:02d}P100", 1, None, 0.0, row_id=2 * i + 2, as_of=f"2025-0{i + 1}-15")
            )
        result = _insights(rows, through=date(2025, 6, 30))
        self.assertTrue(any("win rate" in s for s in result["strengths"]))
        self.assertTrue(any("core wheel kept" in s for s in result["strengths"]))

    def test_shares_above_break_even(self):
        rows = [
            tx("2025-01-02", STO, "-MU250117P100", -1, 3.0, 300.0, row_id=1),
            tx("2025-01-17", ASSIGNED, "-MU250117P100", 1, None, 0.0, row_id=2, as_of="2025-01-17"),
        ]
        result = _insights(
            rows, through=date(2025, 2, 1), current_price=120.0, cost_basis=100.0, break_even_price=97.0
        )
        self.assertTrue(any("above the wheel's break-even" in s for s in result["strengths"]))


class TestImprovements(unittest.TestCase):
    def test_buyback_drag(self):
        rows = [
            tx("2025-01-02", STO, "-MU250117P100", -1, 1.0, 99.33, row_id=1),
            tx("2025-01-10", BTC, "-MU250117P100", 1, 4.0, -400.67, row_id=2),  # closed at a big loss
            tx("2025-01-11", STO, "-MU250207P90", -1, 1.0, 99.33, row_id=3),
            tx("2025-01-20", BTC, "-MU250207P90", 1, 3.0, -300.67, row_id=4),
        ]
        result = _insights(rows, through=date(2025, 2, 1))
        self.assertTrue(any("core wheel is -$" in s for s in result["improvements"]))

    def test_break_even_gap_when_underwater(self):
        rows = [
            tx("2025-01-02", STO, "-MU250117P100", -1, 3.0, 300.0, row_id=1),
            tx("2025-01-17", ASSIGNED, "-MU250117P100", 1, None, 0.0, row_id=2, as_of="2025-01-17"),
        ]
        result = _insights(
            rows, through=date(2025, 2, 1), current_price=85.0, cost_basis=100.0, break_even_price=97.0
        )
        joined = _text(result)
        self.assertIn("must reach $97.00 to close flat", joined)
        self.assertIn("covered calls", joined)

    def test_idle_shares_need_a_round_lot(self):
        # 1 assigned share -> no "write a covered call" nag
        rows = [
            tx("2025-01-02", STO, "-MU250117P100", -1, 3.0, 300.0, row_id=1),
            tx("2025-01-17", ASSIGNED, "-MU250117P100", 1, None, 0.0, row_id=2, as_of="2025-01-17"),
        ]
        cycles, _ = build_cycles(rows)
        # one contract assigns 100 shares, so this fixture *does* have a round lot
        result = wheel_insights(
            cycles[0], cycle_metrics(cycles[0], date(2025, 2, 1)), cost_basis=100.0, break_even_price=97.0
        )
        self.assertTrue(any("no covered call written" in s for s in result["improvements"]))


class TestShape(unittest.TestCase):
    def test_caps_at_two_and_three(self):
        rows = [tx("2025-01-02", STO, "-MU250117P100", -1, 3.0, 300.0, row_id=1)]
        result = _insights(rows, through=date(2025, 1, 20))
        self.assertLessEqual(len(result["strengths"]), 2)
        self.assertLessEqual(len(result["improvements"]), 3)
        self.assertEqual(sorted(result), ["improvements", "strengths"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
