"""Expiration calendar: wheel/expiration.py `expiration_calendar`."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.expiration import expiration_calendar  # noqa: E402


def _leg(**kw):
    base = {
        "underlying": "MU",
        "type": "CSP",
        "side": "SHORT",
        "strike": 100.0,
        "contracts": 1.0,
        "expiration": "2026-10-16",  # a Friday
        "in_the_money": False,
        "collateral": 10000.0,
        "net_premium": 150.0,
        "breakeven": 98.0,
        "wheel_breakeven": None,
        "last_close": 105.0,
        "moneyness_pct": 5.0,
        "days_to_expiry": 30,
        "cycle_id": "MU-2026-1",
    }
    base.update(kw)
    return base


class TestBucketing(unittest.TestCase):
    def test_days_group_by_exact_date_with_start_and_sparse_no_zero_fill(self):
        out = expiration_calendar(
            [
                _leg(expiration="2026-10-16", collateral=10000.0),
                _leg(expiration="2026-10-16", underlying="WFC", collateral=8000.0),
                _leg(expiration="2026-11-20", collateral=5000.0),
            ]
        )
        self.assertEqual([d["date"] for d in out["days"]], ["2026-10-16", "2026-11-20"])
        self.assertEqual([d["start"] for d in out["days"]], ["2026-10-16", "2026-11-20"])
        first = out["days"][0]
        self.assertEqual(first["count"], 2)
        self.assertEqual(first["capital_exposure"], 18000.0)
        self.assertEqual({p["underlying"] for p in first["positions"]}, {"MU", "WFC"})

    def test_weeks_group_by_monday_sparse(self):
        out = expiration_calendar(
            [_leg(expiration="2026-10-16"), _leg(expiration="2026-11-20")]
        )
        self.assertEqual([w["week_start"] for w in out["weeks"]], ["2026-10-12", "2026-11-16"])
        self.assertEqual([w["start"] for w in out["weeks"]], ["2026-10-12", "2026-11-16"])
        self.assertEqual(out["weeks"][0]["count"], 1)

    def test_months_group_by_key_with_first_of_month_start_sparse(self):
        out = expiration_calendar(
            [_leg(expiration="2026-10-16"), _leg(expiration="2027-01-15")]
        )
        self.assertEqual([m["month"] for m in out["months"]], ["2026-10", "2027-01"])
        self.assertEqual([m["start"] for m in out["months"]], ["2026-10-01", "2027-01-01"])

    def test_capital_sums_shorts_collateral_and_long_debit(self):
        out = expiration_calendar(
            [
                _leg(expiration="2026-10-16", collateral=10000.0),
                _leg(expiration="2026-10-16", side="LONG", type="LP", collateral=None, net_premium=-320.0),
            ]
        )
        self.assertEqual(out["weeks"][0]["capital_exposure"], 10320.0)
        self.assertEqual(out["days"][0]["capital_exposure"], 10320.0)

    def test_one_position_entry_per_leg_with_family_and_capital(self):
        out = expiration_calendar(
            [
                _leg(expiration="2026-10-16", type="CSP", collateral=10000.0),
                _leg(expiration="2026-10-16", underlying="WFC", type="CC", collateral=15000.0,
                     wheel_breakeven=80.0, strike=95.0),
                _leg(expiration="2026-10-16", underlying="GLD", side="LONG", type="LP",
                     collateral=None, net_premium=-320.0),
            ]
        )
        positions = out["days"][0]["positions"]
        fams = {p["underlying"]: p["family"] for p in positions}
        self.assertEqual(fams, {"MU": "csp", "WFC": "cc", "GLD": "long"})
        caps = {p["underlying"]: p["capital"] for p in positions}
        self.assertEqual(caps["MU"], 10000.0)
        self.assertEqual(caps["WFC"], 15000.0)
        self.assertEqual(caps["GLD"], 320.0)
        self.assertEqual(out["days"][0]["capital_exposure"], 25320.0)

    def test_loss_verdict_cc_strike_below_wheel_breakeven(self):
        out = expiration_calendar(
            [_leg(type="CC", strike=90.0, wheel_breakeven=100.0)]
        )
        p = out["days"][0]["positions"][0]
        self.assertTrue(p["at_a_loss"])
        self.assertIn("break-even", p["loss_note"])
        self.assertTrue(out["days"][0]["has_loss"])

    def test_loss_verdict_cc_strike_above_wheel_breakeven_is_fine(self):
        out = expiration_calendar([_leg(type="CC", strike=110.0, wheel_breakeven=100.0)])
        self.assertFalse(out["days"][0]["positions"][0]["at_a_loss"])

    def test_loss_verdict_csp_underwater_vs_breakeven(self):
        under = expiration_calendar([_leg(type="CSP", breakeven=98.0, last_close=90.0)])
        ok = expiration_calendar([_leg(type="CSP", breakeven=98.0, last_close=105.0)])
        self.assertTrue(under["days"][0]["positions"][0]["at_a_loss"])
        self.assertFalse(ok["days"][0]["positions"][0]["at_a_loss"])

    def test_loss_verdict_long_leg_out_of_the_money(self):
        out = expiration_calendar(
            [_leg(side="LONG", type="LP", collateral=None, net_premium=-300.0, in_the_money=False)]
        )
        self.assertTrue(out["days"][0]["positions"][0]["at_a_loss"])

    def test_positions_sorted_losers_first_then_capital(self):
        out = expiration_calendar(
            [
                _leg(underlying="AAA", type="CSP", collateral=5000.0, breakeven=98.0, last_close=105.0),
                _leg(underlying="BBB", type="CC", collateral=1000.0, strike=90.0, wheel_breakeven=100.0),
                _leg(underlying="CCC", type="CSP", collateral=20000.0, breakeven=98.0, last_close=105.0),
            ]
        )
        order = [p["underlying"] for p in out["days"][0]["positions"]]
        self.assertEqual(order[0], "BBB")  # the loser leads
        self.assertEqual(order[1:], ["CCC", "AAA"])  # then by capital desc

    def test_itm_flag_on_every_grain(self):
        out = expiration_calendar([_leg(expiration="2026-10-16", in_the_money=True)])
        for grain in ("days", "weeks", "months"):
            self.assertTrue(out[grain][0]["has_itm"], grain)

    def test_earnings_soon_uses_the_same_14_day_window_as_the_tables(self):
        today = date.today()
        soon = (today + timedelta(days=5)).isoformat()
        far = (today + timedelta(days=40)).isoformat()
        past = (today - timedelta(days=3)).isoformat()
        far_exp = (today + timedelta(days=120)).isoformat()
        out = expiration_calendar(
            [
                _leg(underlying="MU", expiration=far_exp),
                _leg(underlying="WFC", expiration=far_exp),
                _leg(underlying="KO", expiration=far_exp),
            ],
            {"MU": soon, "WFC": far, "KO": past},
        )
        by = {p["underlying"]: p for p in out["days"][0]["positions"]}
        self.assertTrue(by["MU"]["earnings_soon"])
        self.assertEqual(by["MU"]["days_to_earnings"], 5)
        self.assertFalse(by["WFC"]["earnings_soon"])   # 40d out -> outside the window
        self.assertFalse(by["KO"]["earnings_soon"])    # already reported
        # the date is still carried for every one, for the tooltip
        self.assertEqual(by["WFC"]["earnings_date"], far)
        # bucket flag tracks "soon", same as the tables
        self.assertTrue(out["days"][0]["has_earnings"])

    def test_earnings_before_expiry_still_reported_for_reference(self):
        out = expiration_calendar(
            [_leg(underlying="MU", expiration="2027-01-15")],
            {"MU": "2026-12-01"},
        )
        p = out["days"][0]["positions"][0]
        self.assertTrue(p["earnings_before_expiry"])   # Dec 1 is before the Jan 15 expiry
        self.assertEqual(p["earnings_date"], "2026-12-01")

    def test_bare_ticker_iterable_still_flags_before_expiry_without_a_date(self):
        out = expiration_calendar([_leg(underlying="MU")], ["MU"])
        p = out["days"][0]["positions"][0]
        self.assertTrue(p["earnings_before_expiry"])
        self.assertFalse(p["earnings_soon"])   # no real date -> no 14-day warning
        self.assertIsNone(p["earnings_date"])

    def test_as_of_defaults_to_today_and_honours_through(self):
        self.assertEqual(
            expiration_calendar([_leg()], through=date(2026, 9, 1))["as_of"], "2026-09-01"
        )
        self.assertEqual(expiration_calendar([_leg()])["as_of"], date.today().isoformat())

    def test_empty_book(self):
        out = expiration_calendar([])
        self.assertEqual(out["days"], [])
        self.assertEqual(out["weeks"], [])
        self.assertEqual(out["months"], [])
        self.assertIn("as_of", out)


class TestRecentCloses(unittest.TestCase):
    def _close(self, **kw):
        base = {
            "underlying": "MU",
            "type": "CSP",
            "strike": 100.0,
            "contracts": 1.0,
            "close_date": (date.today() - timedelta(days=2)).isoformat(),
            "outcome": "ASSIGNED",
            "capital": 10000.0,
            "realized_pl": 150.0,
        }
        base.update(kw)
        return base

    def test_realized_close_becomes_a_faded_day_and_week_bucket(self):
        cd = (date.today() - timedelta(days=2)).isoformat()
        monday = (
            date.fromisoformat(cd) - timedelta(days=date.fromisoformat(cd).weekday())
        ).isoformat()
        out = expiration_calendar(
            [_leg(expiration="2026-10-16")],
            recent_closes=[self._close(close_date=cd)],
        )
        day = next(d for d in out["days"] if d["start"] == cd)
        self.assertTrue(day["realized"])
        pos = day["positions"][0]
        self.assertTrue(pos["realized"])
        self.assertEqual(pos["outcome"], "ASSIGNED")
        self.assertEqual(pos["capital"], 10000.0)
        self.assertEqual(pos["realized_pl"], 150.0)
        self.assertTrue(any(w["start"] == monday and w["realized"] for w in out["weeks"]))

    def test_current_month_close_is_not_rolled_into_months(self):
        # A close in the current month shares that month's slot with the still-open
        # legs expiring in it, so it is kept out of the month view.
        out = expiration_calendar(
            [_leg(expiration="2026-10-16")],
            recent_closes=[self._close(close_date=date.today().isoformat())],
        )
        self.assertTrue(all(not m.get("realized") for m in out["months"]))

    def test_past_month_close_does_roll_into_months(self):
        old = (date.today().replace(day=1) - timedelta(days=15)).isoformat()
        out = expiration_calendar([], recent_closes=[self._close(close_date=old)])
        old_key = old[:7]
        rolled = next(m for m in out["months"] if m["month"] == old_key)
        self.assertTrue(rolled["realized"])
        self.assertEqual(rolled["positions"][0]["underlying"], "MU")

    def test_realized_buckets_sort_in_with_upcoming_by_date(self):
        cd = (date.today() - timedelta(days=2)).isoformat()
        out = expiration_calendar(
            [_leg(expiration="2026-10-16")],
            recent_closes=[self._close(close_date=cd)],
        )
        self.assertEqual([d["start"] for d in out["days"]], [cd, "2026-10-16"])

    def test_realized_loss_flags_at_a_loss_with_a_note(self):
        out = expiration_calendar([], recent_closes=[self._close(realized_pl=-420.0)])
        pos = out["days"][0]["positions"][0]
        self.assertTrue(pos["at_a_loss"])
        self.assertIn("loss", pos["loss_note"])
        self.assertTrue(out["days"][0]["has_loss"])

    def test_recent_closes_alone_still_produce_a_calendar(self):
        out = expiration_calendar([], recent_closes=[self._close()])
        self.assertEqual(len(out["days"]), 1)


if __name__ == "__main__":
    unittest.main()
