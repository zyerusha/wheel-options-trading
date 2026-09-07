"""Realized-gains cross-check: wheel/taxes.py `reconcile`."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.closed_lots import ClosedLot  # noqa: E402
from wheel.taxes import reconcile  # noqa: E402


def _lot(underlying, realized, *, is_option=True, st=None, lt=None):
    return ClosedLot(
        symbol=f"{underlying}260101P100",
        cusip="X",
        description=f"PUT ({underlying})",
        underlying=underlying,
        is_option=is_option,
        right="P" if is_option else None,
        strike=100.0 if is_option else None,
        expiry=date(2026, 1, 1) if is_option else None,
        date_acquired=date(2026, 1, 1),
        date_sold=date(2025, 12, 20),
        quantity=1.0,
        cost_basis=10.0,
        proceeds=10.0 + realized,
        st_gain=st if (st is not None or lt is not None) else (realized if is_option else None),
        lt_gain=lt,
        realized=realized,
        term="SHORT",
        source_file="c.csv",
    )


def _tk(underlying, option_realized_pl):
    return {"underlying": underlying, "option_realized_pl": option_realized_pl}


class TestReconcile(unittest.TestCase):
    def test_close_when_within_ballpark(self):
        out = reconcile([_tk("MU", 505.0)], [_lot("MU", 500.0)])
        row = out["rows"][0]
        self.assertEqual(row["status"], "close")
        self.assertEqual(row["difference"], 5.0)

    def test_review_when_far_apart(self):
        out = reconcile([_tk("MU", 2000.0)], [_lot("MU", 500.0)])
        self.assertEqual(out["rows"][0]["status"], "review")

    def test_engine_missing_ticker_is_review(self):
        out = reconcile([], [_lot("CROX", 647.0)])
        row = out["rows"][0]
        self.assertIsNone(row["wheel_engine_pl"])
        self.assertIsNone(row["difference"])
        self.assertEqual(row["status"], "review")

    def test_engine_only_ticker_is_not_a_row(self):
        # A ticker the closed-lots export doesn't cover isn't a discrepancy.
        out = reconcile([_tk("NVDA", 1234.0)], [_lot("MU", 500.0)])
        self.assertEqual([r["underlying"] for r in out["rows"]], ["MU"])

    def test_compares_option_pl_only(self):
        lots = [_lot("MU", 500.0, is_option=True), _lot("MU", 999.0, is_option=False, st=999.0)]
        out = reconcile([_tk("MU", 505.0)], lots)
        # equity lot's 999 must not pull the option comparison
        self.assertEqual(out["rows"][0]["fidelity_realized"], 500.0)
        self.assertEqual(out["rows"][0]["fidelity_equity"], 999.0)
        self.assertEqual(out["rows"][0]["status"], "close")

    def test_totals_st_lt_and_counts(self):
        out = reconcile(
            [_tk("A", 100.0), _tk("B", 5000.0)],
            [_lot("A", 100.0, st=100.0), _lot("B", 50.0, st=None, lt=50.0)],
        )
        t = out["totals"]
        self.assertEqual(t["st"], 100.0)
        self.assertEqual(t["lt"], 50.0)
        self.assertEqual(t["close"] + t["review"], 2)
        self.assertEqual(t["close"], 1)
        self.assertEqual(t["review"], 1)

    def test_empty_lots_still_shapes(self):
        out = reconcile([_tk("A", 1.0)], [])
        self.assertEqual(out["rows"], [])
        self.assertIn("cross-check", out["notes"])


if __name__ == "__main__":
    unittest.main()
