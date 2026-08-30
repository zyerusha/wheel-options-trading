"""Book-level Dashboard commentary: wheel/insights.py `portfolio_insights`."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.insights import portfolio_insights  # noqa: E402


def _text(result: dict) -> str:
    return " || ".join(result["strengths"] + result["improvements"]).lower()


class TestStrengths(unittest.TestCase):
    def test_wheel_beating_its_benchmark(self):
        r = portfolio_insights(
            {},
            [],
            [],
            wheel_return={
                "available": True,
                "xirr_pct": 48.0,
                "value_added": 126843.0,
                "benchmark": {"name": "SPY", "xirr_pct": 16.0},
            },
        )
        self.assertTrue(any("48%" in s and "16%" in s for s in r["strengths"]))
        self.assertIn("$126,843", _text(r))
        # Must be explicit that it is wheel dollars only, not the whole account.
        self.assertIn("committed to the wheel", r["strengths"][0])
        self.assertIn("excluded", r["strengths"][0])

    def test_no_strength_when_wheel_only_matches_benchmark(self):
        r = portfolio_insights(
            {},
            [],
            [],
            wheel_return={"available": True, "xirr_pct": 17.0, "benchmark": {"xirr_pct": 16.0}},
        )
        self.assertEqual(r["strengths"], [])

    def test_win_rate_needs_enough_decided_legs(self):
        low = portfolio_insights({"win_rate_pct": 90.0, "wins": 9, "losses": 1}, [], [])
        self.assertEqual(low["strengths"], [])
        ok = portfolio_insights({"win_rate_pct": 85.0, "wins": 30, "losses": 6}, [], [])
        self.assertTrue(any("85% win rate" in s for s in ok["strengths"]))

    def test_dividends_strength(self):
        r = portfolio_insights({"dividends_received": 7132.0}, [], [])
        self.assertTrue(any("$7,132 in dividends" in s for s in r["strengths"]))

    def test_runway_hedge_is_a_strength(self):
        r = portfolio_insights(
            {}, [], [{"underlying": "SPCX", "phase": "runway", "days_to_expiry": 81}]
        )
        self.assertTrue(any("hedge" in s.lower() and "runway" in s.lower() for s in r["strengths"]))


class TestImprovements(unittest.TestCase):
    def test_whole_account_benchmark_is_not_turned_into_an_insight(self):
        # It blends in idle cash + buy-and-hold and rests on a configured
        # opening balance -- the wheel-vs-SPY comparison is wheel_return, above.
        r = portfolio_insights(
            {},
            [],
            [],
            benchmark={
                "available": True,
                "actual": {"xirr_pct": 14.0},
                "benchmark": {"name": "SPY", "xirr_pct": 20.0},
                "value_added": -125803.0,
            },
        )
        self.assertEqual(r, {"strengths": [], "improvements": []})

    def test_underwater_wheels_are_summed_and_named(self):
        wheels = [
            {"underlying": "TIGR", "status": "ACTIVE", "mark_to_market_pl": -14397.5, "is_wheel": True},
            {"underlying": "DCH", "status": "ACTIVE", "mark_to_market_pl": -2190.0, "is_wheel": True},
            {"underlying": "OK", "status": "ACTIVE", "mark_to_market_pl": 5000.0, "is_wheel": True},
            {"underlying": "CLSD", "status": "CLOSED", "mark_to_market_pl": -9999.0, "is_wheel": True},
        ]
        r = portfolio_insights({}, wheels, [])
        imp = " ".join(r["improvements"])
        self.assertIn("2 active positions are underwater", imp)
        self.assertIn("-$16,588", imp)  # -14397.5 + -2190.0, rounded
        self.assertIn("TIGR", imp)
        self.assertNotIn("CLSD", imp)  # closed wheel excluded

    def test_idle_shares_bucket(self):
        r = portfolio_insights(
            {},
            [],
            [],
            wheel_state={"buckets": {"holding": {"amount": 255324.0, "cycles": 18}}},
        )
        self.assertTrue(any("no covered call written" in s for s in r["improvements"]))
        self.assertIn("$255,324", " ".join(r["improvements"]))

    def test_concentration_needs_size_and_share(self):
        small = portfolio_insights(
            {},
            [{"underlying": "HPQ", "capital_committed_pct": 100.0, "capital_committed_now": 9000.0}],
            [],
        )
        self.assertEqual(small["improvements"], [])
        big = portfolio_insights(
            {},
            [
                {
                    "underlying": "QQQ",
                    "capital_committed_pct": 34.0,
                    "capital_committed_now": 111398.0,
                    "capital_committed_pct_of": "account value",
                }
            ],
            [],
        )
        self.assertTrue(any("QQQ is 34%" in s for s in big["improvements"]))

    def test_directional_losses_are_flagged_buy_and_hold_is_not(self):
        wheels = [
            {"underlying": "TQQQ", "is_wheel": False, "kind": "directional", "net_realized_pl": -398.0},
            {"underlying": "TIGR", "is_wheel": False, "kind": "hold", "net_realized_pl": -900.0},
            {"underlying": "MU", "is_wheel": True, "kind": "wheel", "net_realized_pl": 5000.0},
        ]
        r = portfolio_insights({}, wheels, [])
        joined = " ".join(r["improvements"]).lower()
        self.assertIn("directional (non-wheel) option trades", joined)
        # Only the directional -$398 counts -- the buy-and-hold -$900 does not.
        self.assertIn("-$398", " ".join(r["improvements"]))
        self.assertNotIn("-$1,298", " ".join(r["improvements"]))

    def test_hedge_in_wind_down_window(self):
        r = portfolio_insights(
            {}, [], [{"underlying": "SPCX", "phase": "wind_down", "days_to_expiry": 40}]
        )
        self.assertTrue(any("wind-down window" in s for s in r["improvements"]))

    def test_capital_estimated_caveat(self):
        wheels = [
            {"underlying": "IVV", "status": "ACTIVE", "capital_estimated": True},
            {"underlying": "QQQ", "status": "ACTIVE", "capital_estimated": True},
        ]
        r = portfolio_insights({}, wheels, [])
        self.assertTrue(any("strike-based" in s and "approximate" in s for s in r["improvements"]))


class TestShape(unittest.TestCase):
    def test_caps_at_three_each(self):
        wheels = [
            {"underlying": f"T{i}", "status": "ACTIVE", "mark_to_market_pl": -1000.0 * (i + 1), "is_wheel": True}
            for i in range(4)
        ]
        r = portfolio_insights(
            {
                "win_rate_pct": 90.0,
                "wins": 100,
                "losses": 5,
                "annualized_wheel_roc_pct": 25.0,
                "option_realized_pl": 5000.0,
                "avg_capital": 300000.0,
                "dividends_received": 5000.0,
            },
            wheels,
            [{"underlying": "A", "phase": "runway", "days_to_expiry": 90}],
            wheel_return={"available": True, "xirr_pct": 40.0, "value_added": 100.0, "benchmark": {"xirr_pct": 10.0}},
            wheel_state={"buckets": {"holding": {"amount": 50000.0, "cycles": 5}}},
            benchmark={
                "available": True,
                "actual": {"xirr_pct": 10.0},
                "benchmark": {"xirr_pct": 20.0},
                "value_added": -50000.0,
            },
        )
        self.assertLessEqual(len(r["strengths"]), 3)
        self.assertLessEqual(len(r["improvements"]), 3)

    def test_empty_inputs_are_safe(self):
        r = portfolio_insights({}, [], [])
        self.assertEqual(r, {"strengths": [], "improvements": []})

    def test_none_inputs_are_safe(self):
        r = portfolio_insights(None, None, None, wheel_return=None, benchmark=None, wheel_state=None)
        self.assertEqual(r, {"strengths": [], "improvements": []})

    def test_improvements_ranked_by_dollar_impact(self):
        # A big underwater cluster outranks a smaller idle-shares opportunity.
        r = portfolio_insights(
            {},
            [
                {"underlying": "BIG", "status": "ACTIVE", "mark_to_market_pl": -40000.0, "is_wheel": True},
                {"underlying": "SML", "status": "ACTIVE", "mark_to_market_pl": -900.0, "is_wheel": True},
            ],
            [],
            wheel_state={"buckets": {"holding": {"amount": 60000.0, "cycles": 4}}},
        )
        self.assertIn("underwater", r["improvements"][0])
        self.assertIn("BIG", r["improvements"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
