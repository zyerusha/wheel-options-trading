"""Covered-call candidates table: wheel/api.py `_build_cc_candidates`
and wheel/accounts.py `_combine_cc_candidates`."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.accounts import _combine_cc_candidates  # noqa: E402
from wheel.api import Dashboard  # noqa: E402


def _wheel(cycle_id, underlying, *, shares, cost=None, be=None, whlbe=None, last=None, is_wheel=True):
    return {
        "cycle_id": cycle_id,
        "underlying": underlying,
        "name": f"{underlying} Inc",
        "is_wheel": is_wheel,
        "shares_held": shares,
        "cost_basis_per_share": cost,
        "break_even_per_share": be,
        "break_even_price": whlbe,
        "current_price": last,
    }


def _candidates(wheels, open_positions=(), earnings=None):
    with mock.patch("wheel.api.load_earnings", return_value=(earnings or {})):
        return Dashboard._build_cc_candidates(None, wheels, open_positions)


class TestSelection(unittest.TestCase):
    def test_position_with_100_plus_shares_and_no_cc_is_a_candidate(self):
        rows = _candidates([_wheel("MU-2025-1", "MU", shares=200, cost=100.0, be=98.0, whlbe=95.0, last=110.0)])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["underlying"], "MU")
        self.assertEqual(row["wheel"], "MU-2025-1")
        self.assertEqual(row["shares_held"], 200)
        self.assertTrue(row["meets_threshold"])
        # negative -- the covered-call position that could be opened
        self.assertEqual(row["contracts_available"], -2)
        self.assertEqual(row["last_close"], 110.0)

    def test_175_shares_reads_minus_one_contract(self):
        rows = _candidates([_wheel("MU-1", "MU", shares=175, cost=100.0, last=110.0)])
        self.assertEqual(rows[0]["contracts_available"], -1)

    def test_under_100_shares_is_listed_but_not_actionable(self):
        rows = _candidates([_wheel("MU-2025-1", "MU", shares=99, cost=100.0, last=105.0)])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertFalse(row["meets_threshold"])
        self.assertIsNone(row["target_cc_strike"])
        self.assertIsNone(row["contracts_available"])
        # the other columns are still populated
        self.assertEqual(row["shares_held"], 99)
        self.assertEqual(row["cost_basis_per_share"], 100.0)
        self.assertAlmostEqual(row["unrealized_pl"], 99 * 5.0, places=2)

    def test_position_with_no_shares_is_dropped_entirely(self):
        self.assertEqual(_candidates([_wheel("MU-1", "MU", shares=0.0, cost=100.0)]), [])

    def test_actionable_rows_sort_ahead_of_sub_100_rows(self):
        rows = _candidates(
            [
                _wheel("ZZZ-1", "ZZZ", shares=50, cost=10.0),
                _wheel("AAA-1", "AAA", shares=200, cost=10.0),
            ]
        )
        self.assertEqual([r["underlying"] for r in rows], ["AAA", "ZZZ"])
        self.assertTrue(rows[0]["meets_threshold"])
        self.assertFalse(rows[1]["meets_threshold"])

    def test_cycle_with_an_open_covered_call_is_excluded(self):
        wheels = [_wheel("SPCX-2026-1", "SPCX", shares=100, cost=140.0)]
        open_positions = [{"cycle_id": "SPCX-2026-1", "type": "CC"}]
        self.assertEqual(_candidates(wheels, open_positions), [])

    def test_open_csp_does_not_exclude_the_shares(self):
        wheels = [_wheel("SPCX-2026-1", "SPCX", shares=100, cost=140.0, last=150.0)]
        open_positions = [{"cycle_id": "SPCX-2026-1", "type": "CSP"}]
        self.assertEqual(len(_candidates(wheels, open_positions)), 1)

    def test_buy_and_hold_shows_with_no_wheel(self):
        rows = _candidates([_wheel("VOO-2025-1", "VOO", shares=300, cost=500.0, is_wheel=False)])
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["wheel"])
        self.assertFalse(rows[0]["is_wheel"])

    def test_rows_sorted_by_symbol(self):
        rows = _candidates(
            [
                _wheel("ZZZ-1", "ZZZ", shares=100, cost=10.0),
                _wheel("AAA-1", "AAA", shares=100, cost=10.0),
                _wheel("MMM-1", "MMM", shares=100, cost=10.0),
            ]
        )
        self.assertEqual([r["underlying"] for r in rows], ["AAA", "MMM", "ZZZ"])


class TestUnrealizedPl(unittest.TestCase):
    def test_gain_is_qty_times_last_close_minus_cost_basis(self):
        rows = _candidates([_wheel("MU-1", "MU", shares=200, cost=100.0, last=110.0)])
        self.assertAlmostEqual(rows[0]["unrealized_pl"], 2000.0, places=2)
        self.assertAlmostEqual(rows[0]["unrealized_pl_pct"], 10.0, places=2)

    def test_loss_is_negative(self):
        rows = _candidates([_wheel("MU-1", "MU", shares=100, cost=100.0, last=88.0)])
        self.assertAlmostEqual(rows[0]["unrealized_pl"], -1200.0, places=2)
        self.assertAlmostEqual(rows[0]["unrealized_pl_pct"], -12.0, places=2)

    def test_uses_raw_cost_basis_not_the_break_even(self):
        # break-evens are far below cost basis (premium banked) but must not
        # affect the gain/loss figure
        rows = _candidates([_wheel("MU-1", "MU", shares=100, cost=100.0, be=80.0, whlbe=70.0, last=100.0)])
        self.assertAlmostEqual(rows[0]["unrealized_pl"], 0.0, places=2)
        self.assertAlmostEqual(rows[0]["unrealized_pl_pct"], 0.0, places=2)

    def test_null_without_a_price_or_a_basis(self):
        no_price = _candidates([_wheel("MU-1", "MU", shares=100, cost=100.0, last=None)])
        self.assertIsNone(no_price[0]["unrealized_pl"])
        self.assertIsNone(no_price[0]["unrealized_pl_pct"])
        no_basis = _candidates([_wheel("MU-1", "MU", shares=100, cost=None, be=90.0, last=95.0)])
        self.assertIsNone(no_basis[0]["unrealized_pl"])
        self.assertIsNone(no_basis[0]["unrealized_pl_pct"])


class TestTargetPrice(unittest.TestCase):
    def test_target_is_the_greatest_of_cost_basis_break_evens_and_last_close(self):
        # premium already banked pulls both break-evens below cost basis;
        # last close sits below cost basis too, so cost basis wins
        rows = _candidates([_wheel("MU-1", "MU", shares=100, cost=100.0, be=92.0, whlbe=88.0, last=95.0)])
        self.assertEqual(rows[0]["target_cc_strike"], 100.0)

    def test_target_never_dips_below_a_break_even_that_sits_above_cost(self):
        # a loss elsewhere in the cycle lifted the wheel break-even past cost
        rows = _candidates([_wheel("MU-1", "MU", shares=100, cost=100.0, be=101.0, whlbe=105.0, last=90.0)])
        self.assertEqual(rows[0]["target_cc_strike"], 105.0)

    def test_target_never_dips_below_the_last_close(self):
        # stock has run well past every cost figure -- don't write a call under market
        rows = _candidates([_wheel("MU-1", "MU", shares=100, cost=90.0, be=85.0, whlbe=80.0, last=120.0)])
        self.assertEqual(rows[0]["target_cc_strike"], 120.0)

    def test_target_falls_back_to_last_close_when_basis_is_unknown(self):
        rows = _candidates([_wheel("MU-1", "MU", shares=100, cost=None, be=None, whlbe=None, last=42.0)])
        self.assertEqual(rows[0]["target_cc_strike"], 42.0)

    def test_target_is_null_only_when_nothing_at_all_is_known(self):
        rows = _candidates([_wheel("MU-1", "MU", shares=100, cost=None, be=None, whlbe=None, last=None)])
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["target_cc_strike"])

    def test_target_ignores_a_missing_break_even(self):
        rows = _candidates([_wheel("MU-1", "MU", shares=100, cost=50.0, be=None, whlbe=48.0, last=45.0)])
        self.assertEqual(rows[0]["target_cc_strike"], 50.0)

    def test_target_is_rounded_up_to_the_next_half_dollar(self):
        rows = _candidates([_wheel("MU-1", "MU", shares=100, cost=175.12, be=170.0, whlbe=168.0, last=174.0)])
        self.assertEqual(rows[0]["target_cc_strike"], 175.5)

    def test_target_already_on_a_half_dollar_is_unchanged(self):
        rows = _candidates([_wheel("MU-1", "MU", shares=100, cost=180.0, be=1.0, whlbe=1.0, last=1.0)])
        self.assertEqual(rows[0]["target_cc_strike"], 180.0)
        rows = _candidates([_wheel("MU-1", "MU", shares=100, cost=180.5, be=1.0, whlbe=1.0, last=1.0)])
        self.assertEqual(rows[0]["target_cc_strike"], 180.5)


class TestSectorAndEarnings(unittest.TestCase):
    def test_sector_from_the_static_map(self):
        rows = _candidates([_wheel("NVDA-1", "NVDA", shares=100, cost=100.0, last=110.0)])
        self.assertEqual(rows[0]["sector"], "Technology")

    def test_no_sector_for_an_unmapped_ticker(self):
        rows = _candidates([_wheel("ZZZZ-1", "ZZZZ", shares=100, cost=100.0, last=110.0)])
        self.assertIsNone(rows[0]["sector"])

    def test_earnings_date_and_days_away(self):
        soon = date.today() + timedelta(days=8)
        rows = _candidates(
            [_wheel("MU-1", "MU", shares=100, cost=100.0, last=110.0)],
            earnings={"MU": soon},
        )
        self.assertEqual(rows[0]["earnings_date"], soon.isoformat())
        self.assertEqual(rows[0]["days_to_earnings"], 8)

    def test_no_earnings_entry_leaves_it_none(self):
        rows = _candidates([_wheel("MU-1", "MU", shares=100, cost=100.0, last=110.0)], earnings={})
        self.assertIsNone(rows[0]["earnings_date"])
        self.assertIsNone(rows[0]["days_to_earnings"])


class TestCombine(unittest.TestCase):
    def test_combine_prefixes_cycle_and_wheel_and_sorts(self):
        payloads = {
            "IRA": {"cc_candidates": [{"cycle_id": "MU-2025-1", "wheel": "MU-2025-1", "underlying": "MU"}]},
            "Joint": {"cc_candidates": [{"cycle_id": "AA-2025-1", "wheel": None, "underlying": "AA"}]},
        }
        combined = _combine_cc_candidates(payloads)
        self.assertEqual([r["underlying"] for r in combined], ["AA", "MU"])
        self.assertEqual(combined[0]["cycle_id"], "Joint:AA-2025-1")
        self.assertIsNone(combined[0]["wheel"])
        self.assertEqual(combined[1]["cycle_id"], "IRA:MU-2025-1")
        self.assertEqual(combined[1]["wheel"], "IRA:MU-2025-1")
        self.assertEqual(combined[1]["account_id"], "IRA")

    def test_combine_tolerates_missing_key(self):
        self.assertEqual(_combine_cc_candidates({"IRA": {}}), [])


if __name__ == "__main__":
    unittest.main()
