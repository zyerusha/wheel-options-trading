"""Earnings-in-view Planner block: wheel/api.py `_build_earnings_in_view`
and wheel/accounts.py `_combine_earnings_in_view`."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.accounts import _combine_earnings_in_view  # noqa: E402
from wheel.api import Dashboard  # noqa: E402

THROUGH = date(2026, 9, 6)


def _pos(underlying, expiration, *, strike=100.0):
    return {"underlying": underlying, "expiration": expiration, "strike": strike, "type": "CSP"}


def _wheel(underlying, *, shares):
    return {"underlying": underlying, "shares_held": shares}


def _build(open_positions, wheels, earnings, *, through=THROUGH):
    dash = Dashboard.__new__(Dashboard)
    dash._fundamentals = lambda tickers: {t: {"earnings_date": earnings.get(t)} for t in tickers}
    return Dashboard._build_earnings_in_view(dash, open_positions, wheels, through)


class TestBuild(unittest.TestCase):
    def test_empty_when_no_open_legs_or_shares(self):
        self.assertEqual(_build([], [], {}), {"tickers": [], "within_7d": []})

    def test_ticker_with_open_leg_and_a_date_is_listed(self):
        out = _build([_pos("MU", "2026-10-17")], [], {"MU": date(2026, 9, 30)})
        self.assertEqual(len(out["tickers"]), 1)
        row = out["tickers"][0]
        self.assertEqual(row["ticker"], "MU")
        self.assertEqual(row["earnings_date"], "2026-09-30")
        self.assertEqual(row["days_to_earnings"], 24)
        self.assertEqual(row["soonest_leg_expiry"], "2026-10-17")

    def test_before_expiry_true_when_report_precedes_a_leg_expiry(self):
        out = _build([_pos("UNFI", "2026-09-18")], [], {"UNFI": date(2026, 9, 10)})
        self.assertTrue(out["tickers"][0]["before_expiry"])

    def test_before_expiry_false_when_report_is_after_every_open_leg(self):
        out = _build([_pos("UNFI", "2026-09-09")], [], {"UNFI": date(2026, 9, 30)})
        self.assertFalse(out["tickers"][0]["before_expiry"])

    def test_held_shares_with_no_leg_still_listed_but_no_leg_expiry(self):
        out = _build([], [_wheel("GLD", shares=100)], {"GLD": date(2026, 9, 20)})
        self.assertEqual(out["tickers"][0]["ticker"], "GLD")
        self.assertIsNone(out["tickers"][0]["soonest_leg_expiry"])
        self.assertFalse(out["tickers"][0]["before_expiry"])

    def test_ticker_without_a_fetched_date_is_dropped(self):
        out = _build([_pos("MU", "2026-10-17")], [], {})
        self.assertEqual(out["tickers"], [])

    def test_stale_past_date_is_dropped(self):
        out = _build([_pos("MU", "2026-10-17")], [], {"MU": date(2026, 8, 1)})
        self.assertEqual(out["tickers"], [])

    def test_within_7d_and_sort_order(self):
        out = _build(
            [_pos("A", "2026-12-01"), _pos("B", "2026-12-01"), _pos("C", "2026-12-01")],
            [],
            {"A": date(2026, 9, 30), "B": date(2026, 9, 8), "C": date(2026, 9, 25)},
        )
        self.assertEqual([r["ticker"] for r in out["tickers"]], ["B", "C", "A"])
        self.assertEqual(out["within_7d"], ["B"])


class TestCombine(unittest.TestCase):
    def test_union_of_tickers_across_accounts_deduped(self):
        payloads = {
            "IRA": {"earnings_in_view": {"tickers": [
                {"ticker": "MU", "earnings_date": "2026-09-30", "days_to_earnings": 24,
                 "before_expiry": False, "soonest_leg_expiry": "2026-10-17"},
            ], "within_7d": []}},
            "Joint": {"earnings_in_view": {"tickers": [
                {"ticker": "MU", "earnings_date": "2026-09-30", "days_to_earnings": 24,
                 "before_expiry": True, "soonest_leg_expiry": "2026-10-03"},
                {"ticker": "WFC", "earnings_date": "2026-09-09", "days_to_earnings": 3,
                 "before_expiry": False, "soonest_leg_expiry": None},
            ], "within_7d": ["WFC"]}},
        }
        out = _combine_earnings_in_view(payloads)
        by = {r["ticker"]: r for r in out["tickers"]}
        self.assertEqual(set(by), {"MU", "WFC"})
        # before_expiry OR-ed, soonest leg expiry is the earliest across accounts
        self.assertTrue(by["MU"]["before_expiry"])
        self.assertEqual(by["MU"]["soonest_leg_expiry"], "2026-10-03")
        self.assertEqual(out["within_7d"], ["WFC"])
        self.assertEqual([r["ticker"] for r in out["tickers"]], ["WFC", "MU"])

    def test_missing_block_is_tolerated(self):
        self.assertEqual(
            _combine_earnings_in_view({"IRA": {}, "Joint": {"earnings_in_view": None}}),
            {"tickers": [], "within_7d": []},
        )


if __name__ == "__main__":
    unittest.main()
