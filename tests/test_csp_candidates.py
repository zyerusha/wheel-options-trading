"""CSP-candidates table + star recommender: wheel/api.py
`_build_csp_candidates` / `csp_star_score` / `sector_exposure`, and
wheel/accounts.py `_combine_csp_candidates`."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.accounts import _combine_csp_candidates  # noqa: E402
from wheel.api import Dashboard, csp_star_score, sector_exposure  # noqa: E402
from wheel.reference import load_earnings, load_fundamentals, sector_of  # noqa: E402


def _w(
    underlying,
    *,
    pl,
    roc=None,
    is_wheel=True,
    gross_premium=0.0,
    option_pl=0.0,
    avg_collateral=0.0,
    days=0,
    wins=0,
    losses=0,
    end_date="2024-06-01",
    cycle_id=None,
):
    return {
        "cycle_id": cycle_id or f"{underlying}-1",
        "underlying": underlying,
        "is_wheel": is_wheel,
        "net_realized_pl": pl,
        "annualized_wheel_roc_pct": roc,
        "gross_premium_received": gross_premium,
        "option_realized_pl": option_pl,
        "avg_collateral": avg_collateral,
        "days_active": days,
        "wins": wins,
        "losses": losses,
        "end_date": end_date,
        "start_date": end_date,
        "capital_committed_now": 0.0,
    }


def _candidates(wheels, stats=None, names=None, earnings=None, exposure=None, fundamentals=None):
    dash = Dashboard.__new__(Dashboard)
    dash._company_names = names or {}
    dash._price_stats = lambda tickers: (stats or {})

    funds, earns = fundamentals or {}, earnings or {}

    def _fund(tickers):
        out = {}
        for t in tickers:
            row = dict(funds.get(t) or {})
            for k in ("type", "market_cap_b", "avg_vol_10d_m"):
                row.setdefault(k, None)
            row["earnings_date"] = earns.get(t, row.get("earnings_date"))
            out[t] = row
        return out

    dash._fundamentals = _fund
    return Dashboard._build_csp_candidates(dash, wheels, exposure or {})


class TestBuild(unittest.TestCase):
    def test_profitable_past_wheel_becomes_a_row_with_its_last_close(self):
        rows = _candidates(
            [_w("MU", pl=5000.0, roc=40.0)],
            stats={"MU": {"last": 123.45, "vol_annual_pct": 40.0, "price_position": 0.5}},
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["underlying"], "MU")
        self.assertEqual(row["wheels"], 1)
        self.assertEqual(row["net_realized_pl"], 5000.0)
        self.assertEqual(row["avg_annualized_roc_pct"], 40.0)
        self.assertEqual(row["last_close"], 123.45)
        self.assertIsInstance(row["stars"], (int, float))
        self.assertIn("star_breakdown", row)

    def test_net_losing_ticker_is_excluded(self):
        self.assertEqual(_candidates([_w("XYZ", pl=-200.0)]), [])

    def test_non_wheel_cycle_with_no_option_activity_is_ignored(self):
        self.assertEqual(_candidates([_w("XYZ", pl=999.0, is_wheel=False)]), [])

    def test_ticker_with_only_a_bare_csp_and_no_wheel_is_not_listed(self):
        rows = _candidates([_w("XYZ", pl=300.0, is_wheel=False, gross_premium=300.0, option_pl=300.0)])
        self.assertEqual(rows, [])

    def test_a_losing_bare_csp_drags_a_wheeled_tickers_stats(self):
        wheels = [
            _w("MU", pl=5000.0, roc=40.0, gross_premium=1200.0, option_pl=900.0,
               avg_collateral=20_000.0, days=60, wins=4, losses=0, end_date="2024-01-15"),
            # a CSP sold and bought back at a loss, never assigned -> is_wheel False
            _w("MU", pl=-800.0, is_wheel=False, gross_premium=0.0, option_pl=-800.0,
               avg_collateral=18_000.0, days=5, wins=0, losses=1, end_date="2024-09-01"),
        ]
        rows = _candidates(
            wheels, stats={"MU": {"last": 100.0, "vol_annual_pct": 35.0, "price_position": 0.5}}
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["wheels"], 1)  # the bare CSP is not a "wheel"
        self.assertEqual(row["net_realized_pl"], 4200.0)  # 5000 - 800
        self.assertEqual(row["wins"], 4)
        self.assertEqual(row["losses"], 1)  # the loss now shows
        self.assertAlmostEqual(row["win_rate"], 0.8)

    def test_multiple_wheels_on_one_ticker_are_summed(self):
        rows = _candidates(
            [_w("MU", pl=3000.0, roc=30.0), _w("MU", pl=2000.0, roc=50.0)],
            stats={"MU": {"last": 100.0, "vol_annual_pct": 35.0, "price_position": 0.5}},
        )
        self.assertEqual(rows[0]["wheels"], 2)
        self.assertEqual(rows[0]["net_realized_pl"], 5000.0)
        self.assertEqual(rows[0]["avg_annualized_roc_pct"], 40.0)  # (30 + 50) / 2

    def test_missing_stats_still_yields_a_row(self):
        rows = _candidates([_w("MU", pl=5000.0)], stats={})
        self.assertIsNone(rows[0]["last_close"])

    def test_rows_sorted_by_stars_then_realized_pl(self):
        rows = _candidates(
            [
                _w("WEAK", pl=200.0, roc=2.0, days=400, avg_collateral=1_000_000.0, wins=1, losses=3),
                _w("STRONG", pl=40_000.0, roc=70.0, gross_premium=8000.0, option_pl=6000.0,
                   avg_collateral=60_000.0, days=120, wins=9, losses=1),
            ],
            stats={
                "WEAK": {"last": 10.0, "vol_annual_pct": 8.0, "price_position": 0.05},
                "STRONG": {"last": 50.0, "vol_annual_pct": 40.0, "price_position": 0.6},
            },
        )
        self.assertEqual(rows[0]["underlying"], "STRONG")
        self.assertGreater(rows[0]["stars"], rows[1]["stars"])


class TestEligibilityFilters(unittest.TestCase):
    STAT = {"last": 100.0, "vol_annual_pct": 30.0, "price_position": 0.5}

    def _one(self, ticker="ZZ", *, name=None, stat=None, fund=None):
        return _candidates(
            [_w(ticker, pl=5000.0, roc=30.0)],
            stats={ticker: stat or self.STAT},
            names={ticker: name} if name else None,
            fundamentals={ticker: fund} if fund else None,
        )

    def test_common_stock_with_full_data_passes_clean(self):
        rows = self._one(fund={"type": "common", "market_cap_b": 12.0, "avg_vol_10d_m": 4.0})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["vetting"]["unvetted"], [])

    def test_plain_etf_is_allowed(self):
        rows = self._one(fund={"type": "etf", "avg_vol_10d_m": 20.0})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["vetting"]["unvetted"], [])  # no market-cap nag for an ETF

    def test_leveraged_etf_is_dropped(self):
        self.assertEqual(self._one(fund={"type": "leveraged_etf"}), [])

    def test_leveraged_etf_dropped_via_sector_when_type_missing(self):
        self.assertEqual(self._one("TQQQ", stat={**self.STAT, "last": 70.0}), [])

    def test_closed_end_fund_is_dropped(self):
        self.assertEqual(self._one(fund={"type": "closed_end_fund"}), [])

    def test_lp_in_the_name_is_dropped(self):
        self.assertEqual(self._one(name="Enterprise Products Partners L.P.", fund={"type": "common"}), [])
        self.assertEqual(self._one(name="Brookfield Renewable LP", fund={"type": "common"}), [])

    def test_lp_substring_inside_a_word_is_not_matched(self):
        rows = self._one(name="Alpine Immune Sciences", fund={"type": "common", "market_cap_b": 5.0, "avg_vol_10d_m": 2.0})
        self.assertEqual(len(rows), 1)

    def test_price_outside_10_to_350_is_dropped(self):
        self.assertEqual(self._one(stat={**self.STAT, "last": 8.0}), [])
        self.assertEqual(self._one(stat={**self.STAT, "last": 401.0}), [])
        self.assertEqual(len(self._one(stat={**self.STAT, "last": 10.0})), 1)
        self.assertEqual(len(self._one(stat={**self.STAT, "last": 350.0})), 1)

    def test_known_small_cap_or_thin_volume_is_dropped(self):
        self.assertEqual(self._one(fund={"type": "common", "market_cap_b": 0.4, "avg_vol_10d_m": 4.0}), [])
        self.assertEqual(self._one(fund={"type": "common", "market_cap_b": 12.0, "avg_vol_10d_m": 0.3}), [])

    def test_missing_cap_or_volume_shows_but_is_flagged(self):
        rows = self._one(fund={"type": "common"})
        self.assertEqual(len(rows), 1)
        notes = rows[0]["vetting"]["unvetted"]
        self.assertIn("market cap unknown", notes)
        self.assertIn("10d volume unknown", notes)

    def test_no_fundamentals_row_at_all_shows_flagged(self):
        rows = self._one()  # ticker "ZZ" not in fundamentals, not a known fund sector
        self.assertEqual(len(rows), 1)
        self.assertIn("security type unknown", rows[0]["vetting"]["unvetted"])

    def test_adr_is_treated_as_common_stock(self):
        rows = self._one(fund={"type": "adr", "market_cap_b": 20.0, "avg_vol_10d_m": 5.0})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["vetting"]["unvetted"], [])


class TestStarScore(unittest.TestCase):
    def _score(self, **comp):
        base = {
            "roc_pct": 10.0,
            "monthly_premium_pct": 0.5,
            "ppd_yield_pct": 5.0,
            "net_realized_pl": 1000.0,
            "win_rate": 0.6,
            "wheels": 1,
            "days_since_last_wheel": 200,
            "vol_annual_pct": 30.0,
            "price_position": 0.5,
            "sector": "Technology",
        }
        base.update(comp)
        return csp_star_score(base, 0.0, None)

    def test_a_great_ticker_scores_high(self):
        s = self._score(
            roc_pct=90.0, monthly_premium_pct=6.0, ppd_yield_pct=60.0, net_realized_pl=50_000.0,
            win_rate=0.95, wheels=6, days_since_last_wheel=20,
        )
        self.assertGreaterEqual(s["stars"], 4.0)

    def test_a_weak_ticker_scores_low(self):
        s = self._score(
            roc_pct=2.0, monthly_premium_pct=0.1, ppd_yield_pct=0.5, net_realized_pl=150.0,
            win_rate=0.35, wheels=1, days_since_last_wheel=900, vol_annual_pct=6.0, price_position=0.03,
        )
        self.assertLessEqual(s["stars"], 1.5)

    def test_stars_are_whole_numbers_within_range(self):
        for pl in (0, 500, 5_000, 50_000):
            s = self._score(net_realized_pl=pl)["stars"]
            self.assertIsInstance(s, int)
            self.assertTrue(0 <= s <= 5)

    def test_imminent_earnings_docks_stars(self):
        far = csp_star_score(self._score()["values"] | {"sector": "Technology"}, 0.0, 40)
        near = csp_star_score(self._score()["values"] | {"sector": "Technology"}, 0.0, 3)
        self.assertLess(near["stars"], far["stars"])
        self.assertLess(near["modifiers"]["earnings"]["stars"], 0)
        self.assertGreater(far["modifiers"]["earnings"]["stars"], 0)

    def test_sector_concentration_docks_and_diversification_helps(self):
        fresh = csp_star_score({"sector": "Energy", "roc_pct": 20.0}, 0.0, None)
        crowded = csp_star_score({"sector": "Energy", "roc_pct": 20.0}, 0.5, None)
        self.assertGreater(fresh["modifiers"]["sector"]["stars"], 0)
        self.assertLess(crowded["modifiers"]["sector"]["stars"], 0)
        self.assertGreater(fresh["stars"], crowded["stars"])

    def test_realized_pl_moves_the_needle(self):
        low = self._score(net_realized_pl=200.0)["stars"]
        high = self._score(net_realized_pl=40_000.0)["stars"]
        self.assertGreater(high, low)


class TestSectorExposure(unittest.TestCase):
    def test_shares_sum_to_one_and_unknown_is_bucketed(self):
        wheels = [
            {"underlying": "NVDA", "capital_committed_now": 60_000.0},
            {"underlying": "WFC", "capital_committed_now": 20_000.0},
            {"underlying": "ZZZZ", "capital_committed_now": 20_000.0},
            {"underlying": "MU", "capital_committed_now": 0.0},  # nothing committed -> skipped
        ]
        exp = sector_exposure(wheels)
        self.assertAlmostEqual(sum(exp.values()), 1.0, places=6)
        self.assertAlmostEqual(exp["Technology"], 0.6, places=6)
        self.assertAlmostEqual(exp["Financials"], 0.2, places=6)
        self.assertAlmostEqual(exp["Unknown"], 0.2, places=6)

    def test_no_committed_capital_yields_empty(self):
        self.assertEqual(sector_exposure([{"underlying": "MU", "capital_committed_now": 0.0}]), {})


class TestReference(unittest.TestCase):
    def test_sector_of_is_case_insensitive_and_none_for_unknown(self):
        self.assertEqual(sector_of("nvda"), "Technology")
        self.assertIsNone(sector_of("ZZZZ"))

    def test_load_earnings_parses_a_file_and_skips_bad_rows(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "earnings.json"), "w", encoding="utf-8") as fh:
                json.dump({"_comment": "n", "MU": "2026-09-24", "BAD": "x", "nvda": "2026-11-18"}, fh)
            out = load_earnings([d])
        self.assertEqual(out, {"MU": date(2026, 9, 24), "NVDA": date(2026, 11, 18)})

    def test_load_earnings_returns_empty_when_no_file(self):
        self.assertEqual(load_earnings(["/no/such/dir/anywhere"]), {})

    def test_load_fundamentals_parses_normalizes_and_skips_bad_rows(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "fundamentals.json"), "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "_comment": "ignored string row",
                        "mu": {"type": "Common", "market_cap_b": 120, "avg_vol_10d_m": "20"},
                        "spy": {"type": "etf", "market_cap_b": None, "avg_vol_10d_m": "x"},
                    },
                    fh,
                )
            out = load_fundamentals([d])
        self.assertEqual(out["MU"], {"type": "common", "market_cap_b": 120.0, "avg_vol_10d_m": 20.0})
        self.assertEqual(out["SPY"], {"type": "etf", "market_cap_b": None, "avg_vol_10d_m": None})
        self.assertNotIn("_COMMENT", out)

    def test_load_fundamentals_returns_empty_when_no_file(self):
        self.assertEqual(load_fundamentals(["/no/such/dir/anywhere"]), {})


class TestCombine(unittest.TestCase):
    def _row(self, underlying, **kw):
        base = {
            "underlying": underlying,
            "name": "X",
            "wheels": 2,
            "net_realized_pl": 4000.0,
            "avg_annualized_roc_pct": 30.0,
            "monthly_premium_pct": 2.0,
            "ppd": 15.0,
            "last_close": 100.0,
            "sector": "Technology",
            "earnings_date": "2026-10-15",
            "days_to_earnings": 20,
            "stars": 3.5,
            "star_breakdown": {},
            "roc_pct": 30.0,
            "ppd_yield_pct": 18.0,
            "win_rate": 0.8,
            "wins": 8,
            "losses": 2,
            "days_since_last_wheel": 40,
            "vol_annual_pct": 38.0,
            "price_position": 0.6,
        }
        base.update(kw)
        return base

    def test_same_ticker_across_accounts_is_merged_and_re_scored(self):
        payloads = {
            "IRA": {"csp_candidates": [self._row("MU", wheels=2, net_realized_pl=3000.0, roc_pct=30.0, wins=6, losses=1)]},
            "Joint": {"csp_candidates": [self._row("MU", wheels=1, net_realized_pl=1000.0, roc_pct=60.0, wins=3, losses=0)]},
        }
        combined = _combine_csp_candidates(payloads, {})
        self.assertEqual(len(combined), 1)
        row = combined[0]
        self.assertEqual(row["wheels"], 3)
        self.assertEqual(row["net_realized_pl"], 4000.0)
        self.assertEqual(row["avg_annualized_roc_pct"], 40.0)  # (30*2 + 60*1) / 3
        self.assertEqual(row["wins"], 9)
        self.assertEqual(row["sector"], "Technology")
        self.assertIn("stars", row)
        self.assertIn("star_breakdown", row)

    def test_recency_is_the_soonest_across_accounts(self):
        payloads = {
            "IRA": {"csp_candidates": [self._row("MU", days_since_last_wheel=400)]},
            "Joint": {"csp_candidates": [self._row("MU", days_since_last_wheel=12)]},
        }
        self.assertEqual(_combine_csp_candidates(payloads, {})[0]["days_since_last_wheel"], 12)

    def test_ticker_net_negative_after_merge_is_dropped(self):
        payloads = {
            "IRA": {"csp_candidates": [self._row("MU", net_realized_pl=1000.0)]},
            "Joint": {"csp_candidates": [self._row("MU", net_realized_pl=-3000.0)]},
        }
        self.assertEqual(_combine_csp_candidates(payloads, {}), [])

    def test_tolerates_missing_key(self):
        self.assertEqual(_combine_csp_candidates({"IRA": {}}, {}), [])


if __name__ == "__main__":
    unittest.main()
