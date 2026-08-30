"""Open-hedge banner payload: wheel/api.py `_build_open_hedges` + `_open_hedge_entry`."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_engine import tx  # noqa: E402
from wheel.accounts import _combine_open_hedges  # noqa: E402
from wheel.api import Dashboard  # noqa: E402
from wheel.engine import build_cycles  # noqa: E402
from wheel.parser import BTC, BTO, EXPIRED, STC, STO  # noqa: E402


def _hedges(transactions, names=None, prices=None) -> list[dict]:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.transactions = transactions
    dashboard.all_cycles, dashboard.engine = build_cycles(transactions)
    dashboard._company_names = names or {}
    return Dashboard._build_open_hedges(dashboard, prices or {})


class TestDetection(unittest.TestCase):
    def test_open_long_put_in_a_wheel_is_a_hedge(self):
        # last trade 2025-06-02 -> hedge exp 2025-11-20 is ~171 DTE (runway).
        hedges = _hedges(
            [
                tx("2025-06-01", STO, "-MU251017P90", -1, 1.0, 100.0, row_id=1),
                tx("2025-06-02", BTO, "-MU251120P80", 1, 5.0, -500.0, row_id=2),
            ]
        )
        self.assertEqual(len(hedges), 1)
        h = hedges[0]
        self.assertEqual(h["underlying"], "MU")
        self.assertEqual(h["right"], "P")
        self.assertEqual(h["strike"], 80.0)
        self.assertTrue(h["is_wheel"])
        self.assertEqual(h["phase"], "runway")
        self.assertEqual(h["cost"], 500.0)

    def test_closed_long_is_not_a_hedge(self):
        hedges = _hedges(
            [
                tx("2025-06-01", STO, "-MU251017P90", -1, 1.0, 100.0, row_id=1),
                tx("2025-06-02", BTO, "-MU251120P80", 1, 5.0, -500.0, row_id=2),
                tx("2025-06-20", STC, "-MU251120P80", -1, 4.0, 400.0, row_id=3),
            ]
        )
        self.assertEqual(hedges, [])

    def test_short_leg_is_never_a_hedge(self):
        hedges = _hedges([tx("2025-06-01", STO, "-MU251017P90", -1, 1.0, 100.0, row_id=1)])
        self.assertEqual(hedges, [])

    def test_spread_long_leg_is_excluded(self):
        # Same-day short + long put, same expiry -> a Spread; the long leg is
        # risk-defined, not a standalone hedge.
        hedges = _hedges(
            [
                tx("2025-06-01", STO, "-MU251017P90", -1, 2.0, 200.0, row_id=1),
                tx("2025-06-01", BTO, "-MU251017P80", 1, 1.0, -100.0, row_id=2),
            ]
        )
        self.assertEqual(hedges, [])

    def test_lone_directional_long_is_flagged_non_wheel(self):
        hedges = _hedges([tx("2025-06-02", BTO, "-MU251120C130", 1, 4.0, -400.0, row_id=1)])
        self.assertEqual(len(hedges), 1)
        h = hedges[0]
        self.assertFalse(h["is_wheel"])
        self.assertEqual(h["right"], "C")
        self.assertIsNone(h["premium_written_since"])
        self.assertIn("directional", h["message"].lower())


class TestPhasesAndMessage(unittest.TestCase):
    def _one(self, hedge_exp: str):
        # A healthy wheel (short put opened and closed for a net credit, no
        # shares) plus one open long put whose expiry we vary.
        return _hedges(
            [
                tx("2025-06-02", BTO, f"-MU{hedge_exp}P80", 1, 5.0, -500.0, row_id=1),
                tx("2025-06-03", STO, "-MU250620P85", -1, 8.0, 800.0, row_id=2),
                tx("2025-06-18", BTC, "-MU250620P85", 1, 0.2, -20.0, row_id=3),
            ]
        )[0]

    def test_runway_over_two_months(self):
        h = self._one("251120")  # ~171 DTE
        self.assertEqual(h["phase"], "runway")
        self.assertIn("RUNWAY", h["headline"])
        self.assertGreaterEqual(h["wheel_pl_now"], 0)
        self.assertIn("runway", h["message"].lower())

    def test_wind_down_inside_two_months(self):
        h = self._one("250715")  # 43 DTE
        self.assertEqual(h["phase"], "wind_down")
        self.assertIn("WIND DOWN", h["headline"])
        self.assertIn("sell the hedge now", h["message"].lower())

    def test_expiring_within_a_week(self):
        h = self._one("250606")  # 4 DTE
        self.assertEqual(h["phase"], "expiring")
        self.assertIn("EXPIRING", h["headline"])

    def test_premium_written_since_is_a_plain_fact_not_a_verdict(self):
        # Short legs closed after the hedge opened bank premium; it is reported,
        # but never framed as "the hedge is paid for".
        hedges = _hedges(
            [
                tx("2025-06-02", BTO, "-MU251120P80", 1, 5.0, -500.0, row_id=1),
                tx("2025-06-03", STO, "-MU250801P85", -1, 4.0, 400.0, row_id=2),
                tx("2025-06-20", BTC, "-MU250801P85", 1, 0.1, -10.0, row_id=3),
                tx("2025-06-21", STO, "-MU250815P85", -1, 3.0, 300.0, row_id=4),
                tx("2025-07-01", BTC, "-MU250815P85", 1, 0.1, -10.0, row_id=5),
            ]
        )
        h = hedges[0]
        self.assertTrue(h["is_wheel"])
        self.assertGreater(h["premium_written_since"], h["cost"])
        self.assertNotIn("covered", h["message"].lower())
        self.assertNotIn("funded", h["message"].lower())
        self.assertNotIn("house money", h["message"].lower())

    def test_message_leads_with_the_wheel_pl_when_underwater(self):
        # A short put assigned -> shares held far below the mark -> wheel is
        # down; the runway message must surface that, not just "keep going".
        hedges = _hedges(
            [
                tx("2025-06-02", BTO, "-MU251120P80", 1, 5.0, -500.0, row_id=1),
                tx("2025-06-03", STO, "-MU250620P150", -1, 3.0, 300.0, row_id=2),
                tx("2025-06-20", EXPIRED, "-MU250620P150", 1, None, 0.0, row_id=3, as_of="2025-06-20"),
            ],
            prices={"MU": 100.0},
        )
        h = hedges[0]
        self.assertEqual(h["phase"], "runway")
        self.assertLess(h["wheel_pl_now"], 0)
        self.assertIn("down -$", h["message"])
        self.assertIn("do not close it", h["message"].lower())


class TestSorting(unittest.TestCase):
    def test_soonest_expiry_first(self):
        hedges = _hedges(
            [
                tx("2025-06-01", STO, "-MU251017P90", -1, 1.0, 100.0, row_id=1),
                tx("2025-06-02", BTO, "-MU251120P80", 1, 5.0, -500.0, row_id=2),
                tx("2025-06-01", STO, "-AMD251017P90", -1, 1.0, 100.0, row_id=3),
                tx("2025-06-02", BTO, "-AMD250715P80", 1, 5.0, -500.0, row_id=4),
            ]
        )
        self.assertEqual([h["underlying"] for h in hedges], ["AMD", "MU"])


class TestCombine(unittest.TestCase):
    def test_combine_prefixes_cycle_id_and_tags_account(self):
        payloads = {
            "IRA": {"open_hedges": [{"cycle_id": "SPCX-2026-1", "days_to_expiry": 80}]},
            "Joint": {"open_hedges": [{"cycle_id": "MU-2026-1", "days_to_expiry": 30}]},
        }
        combined = _combine_open_hedges(payloads)
        self.assertEqual(combined[0]["cycle_id"], "Joint:MU-2026-1")  # sorted soonest first
        self.assertEqual(combined[0]["account_id"], "Joint")
        self.assertEqual(combined[1]["cycle_id"], "IRA:SPCX-2026-1")

    def test_combine_handles_missing_key(self):
        self.assertEqual(_combine_open_hedges({"IRA": {}}), [])


class TestTradeLogRowFlag(unittest.TestCase):
    def test_open_long_row_is_flagged(self):
        from tests.test_trade_log import _trade_log

        rows = _trade_log(
            [
                tx("2025-06-01", STO, "-MU251017P90", -1, 1.0, 100.0, row_id=1),
                tx("2025-06-02", BTO, "-MU251120P80", 1, 5.0, -500.0, row_id=2),
            ]
        )
        (wheel,) = rows["wheels"]
        flagged = [r for r in wheel["transactions"] if r.get("is_open_long")]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["type"], "Buy Put")
        self.assertFalse(flagged[0]["is_settled"])

    def test_no_flag_once_the_long_is_closed(self):
        from tests.test_trade_log import _trade_log

        rows = _trade_log(
            [
                tx("2025-06-01", STO, "-MU251017P90", -1, 1.0, 100.0, row_id=1),
                tx("2025-06-02", BTO, "-MU251120P80", 1, 5.0, -500.0, row_id=2),
                tx("2025-06-20", STC, "-MU251120P80", -1, 4.0, 400.0, row_id=3),
            ]
        )
        (wheel,) = rows["wheels"]
        self.assertEqual([r for r in wheel["transactions"] if r.get("is_open_long")], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
