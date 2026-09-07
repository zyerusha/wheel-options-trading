"""Assignment-risk panel: wheel/assignment.py `assignment_risk`."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.assignment import assignment_risk  # noqa: E402


def _leg(**kw):
    base = {
        "cycle_id": "MU-2026-1",
        "underlying": "MU",
        "name": "MU Inc",
        "type": "CSP",
        "side": "SHORT",
        "strike": 100.0,
        "contracts": 1.0,
        "expiration": "2026-10-17",
        "days_to_expiry": 20,
        "in_the_money": False,
        "moneyness_pct": 5.0,
        "collateral": 10000.0,
        "shares_tracked": True,
        "net_premium": 150.0,
    }
    base.update(kw)
    return base


NW = {"available": True, "cash_total": 25000.0}


class TestPuts(unittest.TestCase):
    def test_itm_put_is_counted_with_obligation(self):
        out = assignment_risk([_leg(in_the_money=True, moneyness_pct=-3.0)], NW)
        self.assertEqual(out["count"], 1)
        self.assertEqual(len(out["itm_puts"]), 1)
        self.assertEqual(out["itm_puts"][0]["obligation"], 10000.0)
        self.assertEqual(out["assignment_obligation"], 10000.0)
        self.assertEqual(out["shares_committed_if_all_puts_assigned"], 100.0)

    def test_otm_put_is_excluded(self):
        out = assignment_risk([_leg(in_the_money=False, moneyness_pct=6.0)], NW)
        self.assertEqual(out["count"], 0)
        self.assertEqual(out["assignment_obligation"], 0.0)

    def test_shortfall_is_zero_when_cash_covers_it(self):
        out = assignment_risk([_leg(in_the_money=True, strike=100.0, contracts=1.0)], NW)
        self.assertEqual(out["potential_shortfall"], 0.0)

    def test_shortfall_is_positive_when_obligation_exceeds_cash(self):
        out = assignment_risk(
            [_leg(in_the_money=True, strike=100.0, contracts=3.0, collateral=30000.0)],
            {"available": True, "cash_total": 25000.0},
        )
        self.assertEqual(out["assignment_obligation"], 30000.0)
        self.assertEqual(out["potential_shortfall"], 5000.0)

    def test_cash_available_none_without_a_snapshot(self):
        out = assignment_risk([_leg(in_the_money=True)], {"available": False})
        self.assertIsNone(out["cash_available"])
        self.assertIsNone(out["potential_shortfall"])


class TestCalls(unittest.TestCase):
    def test_itm_call_tracked_vs_proxy(self):
        tracked = assignment_risk([_leg(type="CC", in_the_money=True, shares_tracked=True)], NW)
        proxy = assignment_risk([_leg(type="CC", in_the_money=True, shares_tracked=False)], NW)
        self.assertTrue(tracked["itm_calls"][0]["shares_tracked"])
        self.assertFalse(proxy["itm_calls"][0]["shares_tracked"])
        self.assertEqual(tracked["itm_calls"][0]["proceeds_if_called"], 10000.0)
        self.assertEqual(tracked["shares_at_risk_of_call"], 100.0)
        # ITM calls do not add to the put cash obligation
        self.assertEqual(tracked["assignment_obligation"], 0.0)


class TestNearTheMoney(unittest.TestCase):
    def test_within_two_percent_but_not_itm(self):
        out = assignment_risk([_leg(in_the_money=False, moneyness_pct=1.5)], NW)
        self.assertEqual(out["count"], 0)
        self.assertEqual(len(out["near_the_money"]), 1)
        self.assertEqual(out["near_the_money"][0]["kind"], "put")

    def test_boundary_at_two_percent_is_included_and_above_is_not(self):
        at = assignment_risk([_leg(in_the_money=False, moneyness_pct=2.0)], NW)
        above = assignment_risk([_leg(in_the_money=False, moneyness_pct=2.1)], NW)
        self.assertEqual(len(at["near_the_money"]), 1)
        self.assertEqual(len(above["near_the_money"]), 0)


class TestMisc(unittest.TestCase):
    def test_soonest_itm_expiry_and_sort(self):
        out = assignment_risk(
            [
                _leg(underlying="A", in_the_money=True, expiration="2026-11-01"),
                _leg(underlying="B", in_the_money=True, expiration="2026-09-19"),
            ],
            NW,
        )
        self.assertEqual(out["soonest_itm_expiry"], "2026-09-19")
        self.assertEqual([r["underlying"] for r in out["itm_puts"]], ["B", "A"])

    def test_empty_book(self):
        out = assignment_risk([], NW)
        self.assertEqual(out["count"], 0)
        self.assertEqual(out["itm_puts"], [])
        self.assertEqual(out["potential_shortfall"], 0.0)

    def test_long_legs_are_ignored(self):
        out = assignment_risk([_leg(side="LONG", type="LP", in_the_money=True)], NW)
        self.assertEqual(out["count"], 0)


if __name__ == "__main__":
    unittest.main()
