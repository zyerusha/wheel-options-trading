"""CSP-candidates table (inside the Cash for CSPs card): wheel/api.py
`_build_csp_candidates` and wheel/accounts.py `_combine_csp_candidates`."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.accounts import _combine_csp_candidates  # noqa: E402
from wheel.api import Dashboard  # noqa: E402
from wheel.reference import load_earnings, sector_of  # noqa: E402


def _w(underlying, *, pl, roc=None, is_wheel=True, gross_premium=0.0, option_pl=0.0, avg_collateral=0.0, days=0):
    return {
        "underlying": underlying,
        "is_wheel": is_wheel,
        "net_realized_pl": pl,
        "annualized_wheel_roc_pct": roc,
        "gross_premium_received": gross_premium,
        "option_realized_pl": option_pl,
        "avg_collateral": avg_collateral,
        "days_active": days,
    }


def _candidates(wheels, closes=None, names=None, earnings=None):
    dash = Dashboard.__new__(Dashboard)
    dash._company_names = names or {}
    dash._last_closes = lambda tickers: (closes or {})
    with mock.patch("wheel.api.load_earnings", return_value=(earnings or {})):
        return Dashboard._build_csp_candidates(dash, wheels)


class TestBuild(unittest.TestCase):
    def test_profitable_past_wheel_becomes_a_row_with_its_last_close(self):
        rows = _candidates([_w("MU", pl=5000.0, roc=40.0)], closes={"MU": 123.45})
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["underlying"], "MU")
        self.assertEqual(row["wheels"], 1)
        self.assertEqual(row["net_realized_pl"], 5000.0)
        self.assertEqual(row["avg_annualized_roc_pct"], 40.0)
        self.assertEqual(row["last_close"], 123.45)

    def test_net_losing_ticker_is_excluded(self):
        self.assertEqual(_candidates([_w("XYZ", pl=-200.0)]), [])

    def test_non_wheel_cycle_is_ignored(self):
        self.assertEqual(_candidates([_w("XYZ", pl=999.0, is_wheel=False)]), [])

    def test_multiple_wheels_on_one_ticker_are_summed(self):
        rows = _candidates(
            [_w("MU", pl=3000.0, roc=30.0), _w("MU", pl=2000.0, roc=50.0)],
            closes={"MU": 100.0},
        )
        self.assertEqual(rows[0]["wheels"], 2)
        self.assertEqual(rows[0]["net_realized_pl"], 5000.0)
        self.assertEqual(rows[0]["avg_annualized_roc_pct"], 40.0)  # (30 + 50) / 2

    def test_missing_last_close_still_yields_a_row(self):
        rows = _candidates([_w("MU", pl=5000.0)], closes={})
        self.assertIsNone(rows[0]["last_close"])

    def test_rows_sorted_by_realized_pl_descending(self):
        rows = _candidates(
            [_w("AAA", pl=100.0), _w("BBB", pl=9000.0), _w("CCC", pl=500.0)],
            closes={"AAA": 1.0, "BBB": 1.0, "CCC": 1.0},
        )
        self.assertEqual([r["underlying"] for r in rows], ["BBB", "CCC", "AAA"])


class TestStrongSignal(unittest.TestCase):
    def test_high_annualized_roc_alone_flags_strong(self):
        rows = _candidates([_w("MU", pl=100.0, roc=45.0)])
        self.assertTrue(rows[0]["strong"])

    def test_high_monthly_premium_alone_flags_strong(self):
        # gross premium 300 on 10k avg collateral over 30 days = 3% / 30d
        rows = _candidates([_w("MU", pl=100.0, roc=5.0, gross_premium=300.0, avg_collateral=10000.0, days=30)])
        self.assertAlmostEqual(rows[0]["monthly_premium_pct"], 3.0, places=2)
        self.assertTrue(rows[0]["strong"])

    def test_high_ppd_alone_flags_strong(self):
        # option P/L 2000 over 40 days = $50/day
        rows = _candidates([_w("MU", pl=100.0, roc=5.0, option_pl=2000.0, days=40, avg_collateral=100000.0)])
        self.assertEqual(rows[0]["ppd"], 50.0)
        self.assertTrue(rows[0]["strong"])

    def test_none_of_the_three_is_not_strong(self):
        rows = _candidates([_w("MU", pl=100.0, roc=8.0, gross_premium=50.0, option_pl=100.0, avg_collateral=100000.0, days=90)])
        self.assertFalse(rows[0]["strong"])


class TestSectorAndEarnings(unittest.TestCase):
    def test_sector_comes_from_the_static_map(self):
        rows = _candidates([_w("NVDA", pl=100.0)])
        self.assertEqual(rows[0]["sector"], "Technology")

    def test_unmapped_ticker_has_no_sector(self):
        rows = _candidates([_w("ZZZZ", pl=100.0)])
        self.assertIsNone(rows[0]["sector"])

    def test_earnings_date_and_days_away_from_the_supplied_map(self):
        in_10 = date.today() + timedelta(days=10)
        rows = _candidates([_w("MU", pl=100.0)], earnings={"MU": in_10})
        self.assertEqual(rows[0]["earnings_date"], in_10.isoformat())
        self.assertEqual(rows[0]["days_to_earnings"], 10)

    def test_no_earnings_entry_leaves_both_none(self):
        rows = _candidates([_w("MU", pl=100.0)], earnings={})
        self.assertIsNone(rows[0]["earnings_date"])
        self.assertIsNone(rows[0]["days_to_earnings"])


class TestReference(unittest.TestCase):
    def test_sector_of_is_case_insensitive_and_none_for_unknown(self):
        self.assertEqual(sector_of("nvda"), "Technology")
        self.assertIsNone(sector_of("ZZZZ"))

    def test_load_earnings_parses_a_file_and_skips_bad_rows(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "earnings.json"), "w", encoding="utf-8") as fh:
                json.dump({"_comment": "note", "MU": "2026-09-24", "BAD": "not-a-date", "nvda": "2026-11-18"}, fh)
            out = load_earnings([d])
        self.assertEqual(out, {"MU": date(2026, 9, 24), "NVDA": date(2026, 11, 18)})

    def test_load_earnings_returns_empty_when_no_file(self):
        self.assertEqual(load_earnings(["/no/such/dir/anywhere"]), {})


class TestCombine(unittest.TestCase):
    def _row(self, underlying, **kw):
        base = {
            "underlying": underlying,
            "wheels": 1,
            "net_realized_pl": 1000.0,
            "avg_annualized_roc_pct": 10.0,
            "monthly_premium_pct": 0.5,
            "ppd": 5.0,
            "strong": False,
            "last_close": 100.0,
            "sector": "Technology",
            "earnings_date": "2026-10-15",
            "days_to_earnings": 20,
            "name": "X",
        }
        base.update(kw)
        return base

    def test_same_ticker_across_accounts_is_merged(self):
        payloads = {
            "IRA": {"csp_candidates": [self._row("MU", wheels=2, net_realized_pl=3000.0, avg_annualized_roc_pct=30.0)]},
            "Joint": {"csp_candidates": [self._row("MU", wheels=1, net_realized_pl=1000.0, avg_annualized_roc_pct=60.0)]},
        }
        combined = _combine_csp_candidates(payloads)
        self.assertEqual(len(combined), 1)
        row = combined[0]
        self.assertEqual(row["wheels"], 3)
        self.assertEqual(row["net_realized_pl"], 4000.0)
        self.assertEqual(row["avg_annualized_roc_pct"], 40.0)  # (30*2 + 60*1) / 3
        self.assertEqual(row["sector"], "Technology")
        self.assertEqual(row["earnings_date"], "2026-10-15")

    def test_strong_recomputed_from_the_merged_roc(self):
        payloads = {
            "IRA": {"csp_candidates": [self._row("MU", wheels=1, avg_annualized_roc_pct=35.0, strong=True)]},
        }
        self.assertTrue(_combine_csp_candidates(payloads)[0]["strong"])

    def test_ticker_net_negative_after_merge_is_dropped(self):
        payloads = {
            "IRA": {"csp_candidates": [self._row("MU", net_realized_pl=1000.0)]},
            "Joint": {"csp_candidates": [self._row("MU", net_realized_pl=-3000.0)]},
        }
        self.assertEqual(_combine_csp_candidates(payloads), [])

    def test_tolerates_missing_key(self):
        self.assertEqual(_combine_csp_candidates({"IRA": {}}), [])


if __name__ == "__main__":
    unittest.main()
