"""Realized-gains cross-check: wheel/taxes.py."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.closed_lots import ClosedLot  # noqa: E402
from wheel.parser import ASSIGNED, BTC, EXPIRED  # noqa: E402
from wheel.taxes import (  # noqa: E402
    best_fit_account,
    comparable_option_pl,
    disposition_window,
    reconcile,
)


def _lot(underlying, realized, *, is_option=True, st=None, lt=None, acquired=None, sold=None):
    return ClosedLot(
        symbol=f"{underlying}260101P100",
        cusip="X",
        description=f"PUT ({underlying})",
        underlying=underlying,
        is_option=is_option,
        right="P" if is_option else None,
        strike=100.0 if is_option else None,
        expiry=date(2026, 1, 1) if is_option else None,
        date_acquired=acquired or date(2026, 1, 1),
        date_sold=sold or date(2025, 12, 20),
        quantity=1.0,
        cost_basis=10.0,
        proceeds=10.0 + realized,
        st_gain=st if (st is not None or lt is not None) else (realized if is_option else None),
        lt_gain=lt,
        realized=realized,
        term="SHORT",
        source_file="c.csv",
    )


def _close(action, when, *, contracts=1.0, cash=0.0):
    return SimpleNamespace(action=action, date=when, contracts=contracts, cash=cash)


def _leg(cash_per_contract, closes):
    return SimpleNamespace(cash_per_contract=cash_per_contract, closes=closes)


def _cycle(underlying, legs):
    return SimpleNamespace(underlying=underlying, legs=legs)


class TestReconcile(unittest.TestCase):
    def test_close_when_within_ballpark(self):
        out = reconcile({"MU": 505.0}, [_lot("MU", 500.0)])
        row = out["rows"][0]
        self.assertEqual(row["status"], "close")
        self.assertEqual(row["difference"], 5.0)

    def test_review_when_far_apart(self):
        out = reconcile({"MU": 2000.0}, [_lot("MU", 500.0)])
        self.assertEqual(out["rows"][0]["status"], "review")

    def test_engine_missing_ticker_is_review(self):
        out = reconcile({}, [_lot("CROX", 647.0)])
        row = out["rows"][0]
        self.assertIsNone(row["wheel_engine_pl"])
        self.assertIsNone(row["difference"])
        self.assertEqual(row["status"], "review")

    def test_engine_only_ticker_is_not_a_row(self):
        # A ticker the closed-lots export doesn't cover isn't a discrepancy.
        out = reconcile({"NVDA": 1234.0}, [_lot("MU", 500.0)])
        self.assertEqual([r["underlying"] for r in out["rows"]], ["MU"])

    def test_compares_option_pl_only(self):
        lots = [_lot("MU", 500.0, is_option=True), _lot("MU", 999.0, is_option=False, st=999.0)]
        out = reconcile({"MU": 505.0}, lots)
        # equity lot's 999 must not pull the option comparison
        self.assertEqual(out["rows"][0]["fidelity_realized"], 500.0)
        self.assertEqual(out["rows"][0]["fidelity_equity"], 999.0)
        self.assertEqual(out["rows"][0]["status"], "close")

    def test_totals_st_lt_and_counts(self):
        out = reconcile(
            {"A": 100.0, "B": 5000.0},
            [_lot("A", 100.0, st=100.0), _lot("B", 50.0, st=None, lt=50.0)],
        )
        t = out["totals"]
        self.assertEqual(t["st"], 100.0)
        self.assertEqual(t["lt"], 50.0)
        self.assertEqual(t["close"] + t["review"], 2)
        self.assertEqual(t["close"], 1)
        self.assertEqual(t["review"], 1)

    def test_empty_lots_still_shapes(self):
        out = reconcile({"A": 1.0}, [])
        self.assertEqual(out["rows"], [])
        self.assertIn("cross-check", out["notes"])

    def test_notes_and_totals_quote_the_disposition_window(self):
        lots = [
            _lot("A", 10.0, acquired=date(2026, 3, 20), sold=date(2026, 2, 26)),
            _lot("B", 10.0, acquired=date(2026, 1, 2), sold=date(2025, 11, 10)),
        ]
        out = reconcile({"A": 10.0, "B": 10.0}, lots, window=disposition_window(lots))
        # disposition = max(acquired, sold): 2026-03-20 and 2026-01-02.
        self.assertEqual(out["totals"]["coverage_start"], "2026-01-02")
        self.assertEqual(out["totals"]["coverage_end"], "2026-03-20")
        self.assertIn("2026-01-02 → 2026-03-20", out["notes"])
        # the earliest sell-to-open (2025-11-10) must not widen it
        self.assertNotIn("2025-11-10", out["notes"])


class TestDispositionWindow(unittest.TestCase):
    def test_uses_later_of_the_two_dates_per_lot(self):
        lots = [
            _lot("A", 1.0, acquired=date(2026, 3, 20), sold=date(2026, 2, 26)),  # close 03-20
            _lot("B", 1.0, acquired=date(2026, 1, 2), sold=date(2026, 1, 9)),    # close 01-09
        ]
        self.assertEqual(disposition_window(lots), (date(2026, 1, 9), date(2026, 3, 20)))

    def test_empty(self):
        self.assertEqual(disposition_window([]), (None, None))


class TestComparableOptionPl(unittest.TestCase):
    def test_sums_premium_minus_buyback_per_close(self):
        # premium 100/contract; bought back for -30 -> realized 70
        cyc = _cycle("MU", [_leg(100.0, [_close(BTC, date(2026, 2, 1), cash=-30.0)])])
        self.assertEqual(comparable_option_pl([cyc], date(2026, 1, 1), date(2026, 3, 1)), {"MU": 70.0})

    def test_expiry_keeps_full_premium(self):
        cyc = _cycle("MU", [_leg(120.0, [_close(EXPIRED, date(2026, 2, 6), cash=0.0)])])
        self.assertEqual(comparable_option_pl([cyc], date(2026, 1, 1), date(2026, 3, 1)), {"MU": 120.0})

    def test_assignment_is_excluded(self):
        cyc = _cycle("MU", [_leg(200.0, [_close(ASSIGNED, date(2026, 2, 10), cash=0.0)])])
        self.assertEqual(comparable_option_pl([cyc], date(2026, 1, 1), date(2026, 3, 1)), {})

    def test_partial_assignment_keeps_the_bought_back_contracts(self):
        leg = _leg(
            100.0,
            [
                _close(BTC, date(2026, 2, 1), contracts=2.0, cash=-40.0),
                _close(ASSIGNED, date(2026, 2, 5), contracts=1.0, cash=0.0),
            ],
        )
        # 2 contracts: 200 premium - 40 buyback = 160; the assigned one drops out
        self.assertEqual(comparable_option_pl([_cycle("MU", [leg])], date(2026, 1, 1), date(2026, 3, 1)), {"MU": 160.0})

    def test_closes_outside_the_window_do_not_count(self):
        leg = _leg(
            50.0,
            [
                _close(EXPIRED, date(2025, 12, 1), cash=0.0),  # before start
                _close(EXPIRED, date(2026, 4, 1), cash=0.0),   # after end
                _close(EXPIRED, date(2026, 2, 1), cash=0.0),   # in window
            ],
        )
        self.assertEqual(comparable_option_pl([_cycle("MU", [leg])], date(2026, 1, 1), date(2026, 3, 1)), {"MU": 50.0})


class TestBestFitAccount(unittest.TestCase):
    def test_picks_the_account_whose_figures_match_the_file(self):
        lots = [_lot("MU", 500.0), _lot("QQQ", 300.0)]
        by_account = {
            "ira": {"MU": 501.0, "QQQ": 299.0},          # both within tolerance
            "joint": {"MU": 900.0, "QQQ": 50.0},         # both far off
        }
        self.assertEqual(best_fit_account(by_account, lots), "ira")

    def test_no_accounts(self):
        self.assertIsNone(best_fit_account({}, [_lot("MU", 1.0)]))


if __name__ == "__main__":
    unittest.main()
