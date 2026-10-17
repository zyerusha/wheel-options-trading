"""Covered-call candidates table: wheel/api.py `_build_cc_candidates`
and wheel/accounts.py `_combine_cc_candidates`."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.accounts import _combine_cc_candidates  # noqa: E402
from wheel.api import Dashboard  # noqa: E402


def _wheel(
    cycle_id, underlying, *, shares, cost=None, be=None, whlbe=None, last=None, is_wheel=True, covered=0.0, untracked=0.0
):
    """A Trade Log wheel as `_trade_log_entry` shapes it, including the
    CC TO EXIT share counts the candidates table reads (`cc_to_exit_detail`)."""
    held = shares + untracked
    return {
        "cycle_id": cycle_id,
        "underlying": underlying,
        "name": f"{underlying} Inc",
        "is_wheel": is_wheel,
        "shares_held": shares,
        "shares_untracked": untracked or None,
        "cost_basis_per_share": cost,
        "break_even_per_share": be,
        "break_even_price": whlbe,
        "current_price": last,
        "cc_to_exit": 140.0,
        "cc_to_exit_status": "recommended",
        "cc_to_exit_label": None,
        "cc_to_exit_explanation": "CC TO EXIT ...",
        "cc_to_exit_detail": {
            "shares_held": held,
            "covered_shares": covered,
            "uncovered_shares": max(held - covered, 0.0),
        },
    }


def _candidates(wheels, earnings=None):
    earns = earnings or {}
    dash = Dashboard.__new__(Dashboard)
    dash._fundamentals = lambda tickers: {
        t: {"type": None, "market_cap_b": None, "avg_vol_10d_m": None, "earnings_date": earns.get(t)}
        for t in tickers
    }
    return Dashboard._build_cc_candidates(dash, wheels)


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

    def test_cycle_fully_covered_by_an_open_covered_call_is_excluded(self):
        wheels = [_wheel("SPCX-2026-1", "SPCX", shares=100, cost=140.0, covered=100)]
        self.assertEqual(_candidates(wheels), [])

    def test_shares_beyond_an_open_covered_call_are_still_a_candidate(self):
        # JXN: 300 shares, one call open on 100 of them -> 200 free, 2 contracts.
        wheels = [_wheel("JXN-2026-1", "JXN", shares=300, cost=58.0, last=132.0, covered=100)]
        (row,) = _candidates(wheels)
        self.assertEqual(row["shares_held"], 300)
        self.assertEqual(row["shares_covered"], 100)
        self.assertTrue(row["meets_threshold"])
        self.assertEqual(row["contracts_available"], -2)

    def test_under_100_free_shares_beyond_an_open_call_is_not_actionable(self):
        wheels = [_wheel("MU-1", "MU", shares=150, cost=100.0, last=110.0, covered=100)]
        (row,) = _candidates(wheels)
        self.assertFalse(row["meets_threshold"])
        self.assertIsNone(row["contracts_available"])

    def test_untracked_shares_count_toward_the_free_shares(self):
        # 0 shares traced through the history, 300 real (bought before it began).
        wheel = _wheel("JXN-2026-1", "JXN", shares=0.0, last=132.0, untracked=300.0, covered=100)
        (row,) = _candidates([wheel])
        self.assertEqual(row["shares_held"], 300)
        self.assertEqual(row["shares_untracked"], 300)
        self.assertEqual(row["contracts_available"], -2)

    def test_open_csp_does_not_exclude_the_shares(self):
        # An open put ties up cash, not shares, so it never counts as cover.
        wheels = [_wheel("SPCX-2026-1", "SPCX", shares=100, cost=140.0, last=150.0)]
        self.assertEqual(len(_candidates(wheels)), 1)

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


class TestCcToExit(unittest.TestCase):
    def test_cc_to_exit_fields_are_the_wheels_own(self):
        # One calculation (wheel/ccexit.py, run in _trade_log_entry); this
        # table copies it rather than computing its own target.
        wheel = _wheel("MU-1", "MU", shares=100, cost=100.0, last=110.0)
        wheel.update(cc_to_exit=None, cc_to_exit_status="no_qualifying", cc_to_exit_label="NO QUALIFYING CC")
        (row,) = _candidates([wheel])
        for key in ("cc_to_exit", "cc_to_exit_status", "cc_to_exit_label", "cc_to_exit_explanation", "cc_to_exit_detail"):
            self.assertEqual(row[key], wheel[key])
        self.assertNotIn("target_cc_strike", row)


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
