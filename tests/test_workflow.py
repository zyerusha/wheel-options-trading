"""Workflow buckets: wheel/workflow.py `classify_open_legs`."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.workflow import classify_open_legs  # noqa: E402


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
        "days_to_expiry": 30,
        "in_the_money": False,
        "moneyness_pct": 6.0,
        "collateral": 10000.0,
        "net_premium": 150.0,
        "min_profit_captured_pct": 20.0,
        "wheel_breakeven": None,
        "last_close": 106.0,
    }
    base.update(kw)
    return base


def _bucket_of(leg):
    out = classify_open_legs([leg])
    for name, bucket in out["buckets"].items():
        if bucket["legs"]:
            return name, bucket["legs"][0]["reason"]
    return None, None


class TestRules(unittest.TestCase):
    def test_itm_and_expiring_is_attention(self):
        name, reason = _bucket_of(_leg(in_the_money=True, moneyness_pct=-2.0, days_to_expiry=5))
        self.assertEqual(name, "attention")
        self.assertIn("in the money", reason)

    def test_shares_below_wheel_breakeven_is_attention(self):
        name, _ = _bucket_of(_leg(type="CC", wheel_breakeven=110.0, last_close=100.0))
        self.assertEqual(name, "attention")

    def test_long_hedge_in_last_week_is_attention(self):
        name, _ = _bucket_of(_leg(side="LONG", type="LP", days_to_expiry=4, net_premium=-300.0))
        self.assertEqual(name, "attention")

    def test_high_captured_and_near_expiry_is_take_profit_candidate(self):
        name, reason = _bucket_of(_leg(min_profit_captured_pct=95.0, days_to_expiry=10))
        self.assertEqual(name, "take_profit_candidate")
        self.assertIn("banked", reason)

    def test_high_captured_but_far_from_expiry_is_not_take_profit(self):
        name, _ = _bucket_of(_leg(min_profit_captured_pct=100.0, days_to_expiry=45))
        self.assertEqual(name, "working")

    def test_earnings_before_expiry_is_evaluate(self):
        out = classify_open_legs([_leg()], earnings_before_expiry=["MU"])
        self.assertEqual(out["buckets"]["evaluate"]["count"], 1)
        self.assertIn("earnings", out["buckets"]["evaluate"]["legs"][0]["reason"])

    def test_near_the_money_is_evaluate(self):
        name, _ = _bucket_of(_leg(moneyness_pct=1.2))
        self.assertEqual(name, "evaluate")

    def test_healthy_otm_short_is_working(self):
        name, _ = _bucket_of(_leg(moneyness_pct=8.0, min_profit_captured_pct=30.0))
        self.assertEqual(name, "working")


class TestPrecedence(unittest.TestCase):
    def test_attention_wins_over_take_profit_candidate(self):
        # ITM + expiring AND high captured -> Attention, not Take-Profit Candidate
        name, _ = _bucket_of(
            _leg(in_the_money=True, moneyness_pct=-1.0, days_to_expiry=3, min_profit_captured_pct=99.0)
        )
        self.assertEqual(name, "attention")


class TestShape(unittest.TestCase):
    def test_totals_and_labels(self):
        out = classify_open_legs(
            [_leg(net_premium=150.0, collateral=10000.0), _leg(net_premium=90.0, collateral=5000.0)]
        )
        working = out["buckets"]["working"]
        self.assertEqual(working["count"], 2)
        self.assertEqual(working["capital"], 15000.0)
        self.assertEqual(working["open_premium"], 240.0)
        self.assertEqual(out["buckets"]["attention"]["label"], "Attention")
        self.assertTrue(out["rules"])

    def test_empty(self):
        out = classify_open_legs([])
        self.assertEqual(sum(b["count"] for b in out["buckets"].values()), 0)


if __name__ == "__main__":
    unittest.main()
