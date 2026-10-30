"""Credit spreads: wheel/creditspreads.py and Dashboard.build_credit_spreads.
Pure, no network: chains are hand-built, and a strike's implied volatility is
solved here so the model delta lands where a test wants it (delta is always
derived by wheel.optionsmath, never supplied). Ledger positions come from real
Transactions run through the engine, the same path imported spreads take."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_engine import tx  # noqa: E402
from wheel import creditspreads as cs  # noqa: E402
from wheel.api import Dashboard, Filters  # noqa: E402
from wheel.engine import build_cycles  # noqa: E402
from wheel.marketdata import OptionChainSnapshot, OptionContractQuote, PricePoint  # noqa: E402
from wheel.optionsmath import CALL, PUT, black_scholes_greeks, probability_finish_above  # noqa: E402
from wheel.parser import BTC, BTO, EXPIRED, STC, STO  # noqa: E402

RATE = 0.045
FEE = 0.65
TODAY = date(2026, 10, 27)
NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
EXPIRY = TODAY + timedelta(days=35)
SPOT = 100.0
DEFAULTS = cs.SpreadSettings()


def iv_for_delta(spot: float, strike: float, days: int, target: float, right: str) -> float:
    """The implied volatility at which an out-of-the-money option's model delta is ``target``."""
    low, high = 0.01, 3.0
    for _ in range(100):
        mid = (low + high) / 2
        delta = abs(
            black_scholes_greeks(
                spot=spot, strike=strike, days_to_expiry=days, implied_volatility=mid, right=right, risk_free_rate=RATE
            ).delta
        )
        low, high = (mid, high) if delta < target else (low, mid)
    return (low + high) / 2


def chain(puts=(), calls=(), *, spot=SPOT, expiry=EXPIRY, as_of=NOW) -> cs.ChainQuotes:
    return cs.ChainQuotes("ABC", expiry, spot, cs.QuoteTiming(as_of, as_of), tuple(puts), tuple(calls))


def uptrend(ticker="ABC") -> cs.MarketContext:
    return cs.market_context(ticker, [80 + i * 0.2 for i in range(60)])


def downtrend(ticker="ABC") -> cs.MarketContext:
    return cs.market_context(ticker, [120 - i * 0.2 for i in range(60)])


def qualifying_put_chain(short_bid=1.70, short_ask=1.80, long_bid=0.10, long_ask=0.15, delta=0.165):
    """Short 90 put at the requested delta, long 85 put: executable credit
    1.70 - 0.15 = 1.55, 31% of the $5 width."""
    days = (EXPIRY - TODAY).days
    return chain(
        puts=(
            cs.LegQuote(90.0, short_bid, short_ask, iv_for_delta(SPOT, 90.0, days, delta, PUT), 500),
            cs.LegQuote(85.0, long_bid, long_ask, 0.45, 500),
        )
    )


def evaluate(c=None, *, strategy=cs.BULL_PUT, short=90.0, long=85.0, settings=DEFAULTS, context=None, equity=100_000.0):
    return cs.evaluate_spread(
        ticker="ABC",
        strategy=strategy,
        chain=c or qualifying_put_chain(),
        short_strike=short,
        long_strike=long,
        today=TODAY,
        settings=settings,
        context=context or uptrend(),
        account_equity=equity,
        risk_free_rate=RATE,
        fee_per_contract=FEE,
    )


# ---------------------------------------------------------------- ledger fixtures
#
# Every leg pays a $0.65 commission; the Amount is net of it, as the broker reports.


def opened_put_spread(day="2026-09-01", ticker="SPY", exp="261016", short=500, long=495, n=1, short_px=1.50, long_px=0.50):
    return [
        tx(day, STO, f"-{ticker}{exp}P{short}", -n, short_px, short_px * 100 * n - FEE * n, commission=FEE * n),
        tx(day, BTO, f"-{ticker}{exp}P{long}", n, long_px, -long_px * 100 * n - FEE * n, commission=FEE * n),
    ]


def closed_legs(day, ticker, exp, right, short, long, short_px, long_px, n=1):
    return [
        tx(day, BTC, f"-{ticker}{exp}{right}{short}", n, short_px, -short_px * 100 * n - FEE * n, commission=FEE * n),
        tx(day, STC, f"-{ticker}{exp}{right}{long}", -n, long_px, long_px * 100 * n - FEE * n, commission=FEE * n),
    ]


def positions_from(transactions, **kwargs):
    cycles, _ = build_cycles(transactions)
    legs = {leg.leg_id: leg for cycle in cycles for leg in cycle.legs}
    spreads = [spread for cycle in cycles for spread in cycle.spreads]
    return cs.credit_spread_positions(spreads, legs, **kwargs)


# A: bull put 500/495, credit 1.00, closed for 0.30 on 9/15 (target 0.50).
WIN = opened_put_spread() + closed_legs("2026-09-15", "SPY", "261016", "P", 500, 495, 0.40, 0.10)
# B: bear call 600/605, credit 1.00, closed for 2.20 on 9/20 (loss trigger 2.00).
LOSS = [
    tx("2026-09-01", STO, "-SPY261016C600", -1, 1.20, 120 - FEE, commission=FEE),
    tx("2026-09-01", BTO, "-SPY261016C605", 1, 0.20, -20 - FEE, commission=FEE),
] + closed_legs("2026-09-20", "SPY", "261016", "C", 600, 605, 3.00, 0.80)
# C: bull put on QQQ, credit 1.00, both legs expired worthless on 10/16.
EXPIRES = opened_put_spread("2026-09-02", "QQQ", "261016", 400, 395) + [
    tx("2026-10-19", EXPIRED, "-QQQ261016P400", 1, None, 0.0, as_of="2026-10-16"),
    tx("2026-10-19", EXPIRED, "-QQQ261016P395", 1, None, 0.0, as_of="2026-10-16"),
]


def by_ticker_strategy(positions):
    return {(p.ticker, p.strategy): p for p in positions}


# ---------------------------------------------------------------- tests


class TestSpreadMath(unittest.TestCase):
    def test_bull_put_profit_loss_breakeven(self):
        self.assertAlmostEqual(cs.max_profit_per_contract(1.50), 150.0)
        self.assertAlmostEqual(cs.max_loss_per_contract(5.0, 1.50), 350.0)
        self.assertAlmostEqual(cs.breakeven(cs.BULL_PUT, 500.0, 1.50), 498.50)

    def test_bear_call_breakeven_is_above_the_short_strike(self):
        self.assertAlmostEqual(cs.breakeven(cs.BEAR_CALL, 600.0, 1.25), 601.25)

    def test_other_widths_and_credits(self):
        for width, credit in ((1.0, 0.30), (2.5, 0.80), (10.0, 3.10)):
            with self.subTest(width=width, credit=credit):
                self.assertAlmostEqual(cs.max_loss_per_contract(width, credit) + cs.max_profit_per_contract(credit), width * 100)

    def test_targets_follow_the_spec_examples(self):
        # $1.50 credit, 50% target -> close at $0.75 for $75; 2x loss trigger at $3.00, about $150 lost.
        self.assertAlmostEqual(cs.target_closing_debit(1.50, 50), 0.75)
        self.assertAlmostEqual(cs.loss_trigger_debit(1.50, 2.0), 3.00)
        self.assertAlmostEqual((1.50 - cs.target_closing_debit(1.50, 50)) * 100, 75.0)
        self.assertAlmostEqual((cs.loss_trigger_debit(1.50, 2.0) - 1.50) * 100, 150.0)

    def test_executable_prices_cross_the_spread_and_mid_is_separate(self):
        short, long = cs.LegQuote(90, 1.70, 1.80, 0.3), cs.LegQuote(85, 0.10, 0.15, 0.3)
        self.assertAlmostEqual(cs.executable_open_credit(short, long), 1.55)
        self.assertAlmostEqual(cs.mid_credit(short, long), 1.625)
        self.assertAlmostEqual(cs.executable_close_debit(short, long), 1.70)

    def test_missing_or_crossed_quote_is_none_never_zero(self):
        good = cs.LegQuote(85, 0.10, 0.15, 0.3)
        for bad in (cs.LegQuote(90, None, 1.8, 0.3), cs.LegQuote(90, 1.9, 1.8, 0.3), cs.LegQuote(90, 0.0, 0.0, 0.3)):
            with self.subTest(bad=bad):
                self.assertIsNone(cs.executable_open_credit(bad, good))
                self.assertIsNone(cs.executable_close_debit(bad, good))
        self.assertIsNone(cs.executable_close_debit(None, good))

    def test_sizing_floors_and_needs_an_account_value(self):
        self.assertEqual(cs.suggested_contracts(100_000, 1.0, 346.30), 2)  # $1,000 / $346.30
        self.assertEqual(cs.suggested_contracts(100_000, 1.0, 1_000.0), 1)  # exactly fits
        self.assertEqual(cs.suggested_contracts(10_000, 1.0, 346.30), 0)
        self.assertIsNone(cs.suggested_contracts(None, 1.0, 346.30))

    def test_probability_of_profit_uses_breakeven(self):
        pop = cs.probability_of_profit(strategy=cs.BULL_PUT, spot=100, breakeven_price=88.45, days=35, iv=0.3, risk_free_rate=RATE)
        expected = probability_finish_above(spot=100, level=88.45, days_to_expiry=35, implied_volatility=0.3, risk_free_rate=RATE)
        self.assertAlmostEqual(pop, expected)
        call_pop = cs.probability_of_profit(strategy=cs.BEAR_CALL, spot=100, breakeven_price=111.5, days=35, iv=0.3, risk_free_rate=RATE)
        self.assertAlmostEqual(
            call_pop,
            1 - probability_finish_above(spot=100, level=111.5, days_to_expiry=35, implied_volatility=0.3, risk_free_rate=RATE),
        )
        self.assertIsNone(cs.probability_of_profit(strategy=cs.BULL_PUT, spot=100, breakeven_price=88, days=35, iv=None, risk_free_rate=RATE))


class TestSettings(unittest.TestCase):
    def test_defaults_match_the_spec(self):
        s, warnings = cs.SpreadSettings.from_query({})
        self.assertEqual(warnings, [])
        self.assertEqual(s.underlyings, ("SPY", "QQQ"))
        self.assertEqual((s.entry_dte_min, s.entry_dte_max), (30, 45))
        self.assertEqual((s.short_delta_min, s.short_delta_max), (0.15, 0.18))
        self.assertEqual(s.min_credit_pct_of_width, 30.0)
        self.assertEqual(s.profit_target_pct, 50.0)
        self.assertEqual(s.loss_multiple, 2.0)
        self.assertEqual(s.time_exit_dte, 21)
        self.assertEqual(s.max_risk_pct_of_equity, 1.0)

    def test_values_parse_and_round_trip(self):
        s, warnings = cs.SpreadSettings.from_query(
            {"underlyings": ["iwm, spy"], "delta_min": ["20"], "delta_max": ["25"], "widths": ["10,2.5"],
             "require_trend": ["off"], "min_iv_pct": ["15"], "max_open": ["3"]}
        )
        self.assertEqual(warnings, [])
        self.assertEqual(s.underlyings, ("IWM", "SPY"))
        self.assertEqual((s.short_delta_min, s.short_delta_max), (0.20, 0.25))
        self.assertEqual(s.widths, (2.5, 10.0))
        self.assertFalse(s.require_trend)
        self.assertEqual(s.min_short_iv_pct, 15.0)
        again, _ = cs.SpreadSettings.from_query({k: [",".join(map(str, v)) if isinstance(v, list) else str(v)] for k, v in s.as_dict().items() if v is not None})
        self.assertEqual(again, s)

    def test_invalid_values_keep_the_default_and_warn(self):
        s, warnings = cs.SpreadSettings.from_query(
            {"delta_min": ["abc"], "dte_min": ["50"], "dte_max": ["40"], "widths": ["-5"], "loss_multiple": ["1"]}
        )
        self.assertEqual(s.short_delta_min, 0.15)
        self.assertEqual((s.entry_dte_min, s.entry_dte_max), (30, 45))
        self.assertEqual(s.widths, (5.0,))
        self.assertEqual(s.loss_multiple, 2.0)
        self.assertEqual(len(warnings), 4)

    def test_time_exit_must_be_below_entry_dte(self):
        s, warnings = cs.SpreadSettings.from_query({"dte_min": ["20"], "dte_max": ["30"], "time_exit_dte": ["21"]})
        self.assertEqual(s.time_exit_dte, 19)
        self.assertTrue(any("time_exit_dte" in w for w in warnings))


class TestEntryEvaluation(unittest.TestCase):
    def test_a_spread_that_passes_every_rule_qualifies(self):
        c = evaluate()
        self.assertEqual(c.status, cs.QUALIFIES, [k for k in c.checks if not k.passed])
        self.assertAlmostEqual(c.credit, 1.55)
        self.assertAlmostEqual(c.max_profit, 155.0)
        self.assertAlmostEqual(c.max_loss, 345.0)
        self.assertAlmostEqual(c.breakeven, 88.45)
        self.assertAlmostEqual(c.credit_pct_of_width, 31.0)
        self.assertAlmostEqual(c.return_on_risk_pct, 1.55 / 3.45 * 100)
        self.assertAlmostEqual(c.short_delta, 0.165, places=4)
        self.assertAlmostEqual(c.risk_per_contract, 346.30)
        self.assertEqual(c.contracts, 2)
        self.assertAlmostEqual(c.min_credit, 1.50)

    def test_low_credit_fails_with_the_reason(self):
        c = evaluate(qualifying_put_chain(long_ask=0.25, long_bid=0.20))  # 1.45 = 29%
        self.assertEqual(c.status, cs.FAILS)
        failed = [k for k in c.checks if k.passed is False]
        self.assertEqual([k.key for k in failed], ["credit"])
        self.assertIn("29.0%", failed[0].detail)

    def test_delta_outside_range_fails(self):
        c = evaluate(qualifying_put_chain(delta=0.25))
        self.assertIn("delta", [k.key for k in c.checks if k.passed is False])

    def test_against_the_trend_fails_only_when_required(self):
        self.assertIn("trend", [k.key for k in evaluate(context=downtrend()).checks if k.passed is False])
        relaxed = cs.SpreadSettings(require_trend=False)
        c = evaluate(context=downtrend(), settings=relaxed)
        self.assertEqual(c.status, cs.QUALIFIES)
        self.assertTrue(any("against the 50-day trend" in r for r in cs.candidate_risks(c, NOW)))

    def test_missing_data_is_unverified_not_a_pass(self):
        days = (EXPIRY - TODAY).days
        no_iv = chain(puts=(cs.LegQuote(90.0, 1.70, 1.80, None), cs.LegQuote(85.0, 0.10, 0.15, None)))
        self.assertEqual(evaluate(no_iv).status, cs.UNVERIFIED)
        no_long = chain(puts=(cs.LegQuote(90.0, 1.70, 1.80, iv_for_delta(SPOT, 90, days, 0.165, PUT)),))
        c = evaluate(no_long)
        self.assertEqual(c.status, cs.UNVERIFIED)
        self.assertIsNone(c.credit)
        self.assertIsNone(c.max_loss)
        self.assertIn("85 is not listed", c.checks[0].detail)
        self.assertEqual(evaluate(context=cs.market_context("ABC", [100.0] * 10)).status, cs.UNVERIFIED)

    def test_no_account_value_means_no_size_but_no_fail(self):
        c = evaluate(equity=None)
        self.assertIsNone(c.contracts)
        self.assertNotIn("size", [k.key for k in c.checks])
        self.assertEqual(c.status, cs.QUALIFIES)

    def test_a_small_account_cannot_fit_one_spread(self):
        c = evaluate(equity=20_000.0)  # 1% = $200 < $346.30
        self.assertEqual(c.contracts, 0)
        self.assertIn("size", [k.key for k in c.checks if k.passed is False])

    def test_minimum_iv_filter(self):
        self.assertIn("iv", [k.key for k in evaluate(settings=cs.SpreadSettings(min_short_iv_pct=500)).checks if k.passed is False])

    def test_wrong_strike_order_is_rejected(self):
        with self.assertRaises(ValueError):
            evaluate(short=85.0, long=90.0)
        with self.assertRaises(ValueError):
            evaluate(strategy=cs.BEAR_CALL, short=110.0, long=105.0)

    def test_bear_call(self):
        days = (EXPIRY - TODAY).days
        calls = chain(calls=(
            cs.LegQuote(110.0, 1.70, 1.80, iv_for_delta(SPOT, 110, days, 0.16, CALL)),
            cs.LegQuote(115.0, 0.10, 0.15, 0.4),
        ))
        c = evaluate(calls, strategy=cs.BEAR_CALL, short=110.0, long=115.0, context=downtrend())
        self.assertEqual(c.status, cs.QUALIFIES)
        self.assertAlmostEqual(c.breakeven, 111.55)

    def test_stale_quotes_are_labeled(self):
        timing = cs.QuoteTiming(NOW - timedelta(hours=18), NOW)
        self.assertEqual(timing.status(NOW)[0], "STALE")
        self.assertIn("Stale", timing.payload(NOW)["label"])
        self.assertEqual(cs.QuoteTiming(None, NOW).status(NOW)[0], "UNKNOWN")
        c = evaluate(chain(qualifying_put_chain().puts, as_of=NOW - timedelta(hours=18)))
        self.assertTrue(any("not current" in r for r in cs.candidate_risks(c, NOW)))


class TestScanAndRecommend(unittest.TestCase):
    def scan(self, c, settings=DEFAULTS):
        return cs.scan_chain(chain=c, today=TODAY, settings=settings, context=uptrend(), account_equity=100_000.0,
                             risk_free_rate=RATE, fee_per_contract=FEE)

    def test_only_shorts_inside_the_delta_range_are_candidates(self):
        days = (EXPIRY - TODAY).days
        c = chain(puts=(
            cs.LegQuote(95.0, 3.0, 3.1, iv_for_delta(SPOT, 95, days, 0.30, PUT)),
            cs.LegQuote(90.0, 1.70, 1.80, iv_for_delta(SPOT, 90, days, 0.165, PUT)),
            cs.LegQuote(85.0, 0.10, 0.15, iv_for_delta(SPOT, 85, days, 0.05, PUT)),
        ))
        found, notes = self.scan(c)
        self.assertEqual([(x.short_strike, x.long_strike) for x in found], [(90.0, 85.0)])
        self.assertTrue(any("Bear call" in n and "no short strike" in n for n in notes))

    def test_unlisted_long_strike_is_noted(self):
        found, notes = self.scan(qualifying_put_chain(), cs.SpreadSettings(widths=(3.0,)))
        self.assertEqual(found, [])
        self.assertTrue(any("not listed" in n for n in notes))

    def test_recommendation_picks_the_best_credit_and_never_forces_one(self):
        good = evaluate()
        better = evaluate(qualifying_put_chain(short_bid=1.80, short_ask=1.85))
        failing = evaluate(qualifying_put_chain(long_ask=0.25, long_bid=0.20))
        rec = cs.recommend("ABC", [good, failing, better], open_spread_count=0, settings=DEFAULTS, now=NOW)
        self.assertEqual(rec.status, "RECOMMENDED")
        self.assertIs(rec.pick, better)
        none = cs.recommend("ABC", [failing], open_spread_count=0, settings=DEFAULTS, now=NOW)
        self.assertEqual(none.status, "NO QUALIFYING SPREADS")
        self.assertIsNone(none.pick)
        self.assertIn("Credit / width", none.reasons[0])
        empty = cs.recommend("ABC", [], open_spread_count=0, settings=DEFAULTS, now=NOW, unavailable_reason="No chain.")
        self.assertEqual(empty.reasons, ("No chain.",))

    def test_position_limit_is_stated(self):
        rec = cs.recommend("ABC", [evaluate()], open_spread_count=5, settings=DEFAULTS, now=NOW)
        self.assertEqual(rec.status, "AT POSITION LIMIT")
        self.assertIn("limit is 5", rec.reasons[0])

    def test_scan_expirations_stays_in_the_window(self):
        listed = [TODAY + timedelta(days=d) for d in (7, 29, 30, 33, 37, 38, 40, 45, 46)]
        picked = cs.scan_expirations(listed, TODAY, DEFAULTS)
        self.assertEqual(len(picked), cs.MAX_SCAN_EXPIRATIONS)
        self.assertTrue(all(30 <= (d - TODAY).days <= 45 for d in picked))
        self.assertEqual([(d - TODAY).days for d in picked], [33, 37, 38, 40])


class TestLedgerPositions(unittest.TestCase):
    def test_imported_credit_spreads_with_fees_separate(self):
        positions = by_ticker_strategy(positions_from(WIN + LOSS + EXPIRES))
        win = positions[("SPY", cs.BULL_PUT)]
        self.assertAlmostEqual(win.credit, 1.00)
        self.assertAlmostEqual(win.open_fees, 1.30)
        self.assertAlmostEqual(win.max_profit, 100.0)
        self.assertAlmostEqual(win.max_loss, 400.0)
        self.assertTrue(win.is_closed)
        self.assertAlmostEqual(win.closing_debit, 0.30)
        self.assertAlmostEqual(win.realized_net, 67.40)  # (1.00 - 0.30) x 100 - 4 x $0.65
        self.assertAlmostEqual(win.realized_fees, 2.60)
        self.assertEqual(cs.classify_exit(win, DEFAULTS), cs.EXIT_PROFIT_TARGET)

        loss = positions[("SPY", cs.BEAR_CALL)]
        self.assertAlmostEqual(loss.closing_debit, 2.20)
        self.assertAlmostEqual(loss.realized_net, -122.60)
        self.assertEqual(cs.classify_exit(loss, DEFAULTS), cs.EXIT_LOSS_THRESHOLD)

        expired = positions[("QQQ", cs.BULL_PUT)]
        self.assertAlmostEqual(expired.realized_net, 98.70)
        self.assertEqual(cs.classify_exit(expired, DEFAULTS), cs.EXIT_EXPIRED)

    def test_debit_pairs_are_not_credit_spreads(self):
        hedge = [
            tx("2026-09-01", STO, "-SPY261016P495", -1, 0.50, 50 - FEE, commission=FEE),
            tx("2026-09-01", BTO, "-SPY261016P500", 1, 1.50, -150 - FEE, commission=FEE),
        ]
        self.assertEqual(positions_from(hedge), [])

    def test_partial_close(self):
        txs = opened_put_spread(n=2) + closed_legs("2026-09-15", "SPY", "261016", "P", 500, 495, 0.40, 0.10, n=1)
        (p,) = positions_from(txs)
        self.assertEqual(p.contracts, 2)
        self.assertEqual(p.contracts_open, 1)
        self.assertFalse(p.is_closed)
        self.assertFalse(p.is_broken)
        self.assertAlmostEqual(p.realized_net, 67.40)  # one contract's round trip
        self.assertIsNone(cs.classify_exit(p, DEFAULTS))

    def test_one_leg_closed_is_broken(self):
        txs = opened_put_spread() + [tx("2026-09-15", BTC, "-SPY261016P500", 1, 0.40, -40 - FEE, commission=FEE)]
        (p,) = positions_from(txs)
        self.assertTrue(p.is_broken)
        g = cs.manage_spread(p, today=date(2026, 9, 16), chain=None, settings=DEFAULTS, risk_free_rate=RATE, now=NOW)
        self.assertEqual(g.action_label, "REVIEW: ONE LEG OPEN")

    def test_time_exit_and_other_closes(self):
        opened = opened_put_spread("2026-10-01", "SPY", "261120")
        late = positions_from(opened + closed_legs("2026-11-10", "SPY", "261120", "P", 500, 495, 0.85, 0.05))[0]
        early = positions_from(opened + closed_legs("2026-10-05", "SPY", "261120", "P", 500, 495, 0.85, 0.05))[0]
        self.assertAlmostEqual(late.closing_debit, 0.80)
        self.assertEqual(cs.classify_exit(late, DEFAULTS), cs.EXIT_TIME)
        self.assertEqual(cs.classify_exit(early, DEFAULTS), cs.EXIT_OTHER)

    def test_real_qqq_bull_put_net_collected_and_breakeven(self):
        """The IRA's 2026-10-30 trade, fills as the broker reported them:
        sold 3 QQQ Nov 30 660 puts @ 8.38 (Amount $2,511.95, fees $2.05),
        bought 3 655 puts @ 7.48 (Amount -$2,245.99, fees $1.99)."""
        (p,) = positions_from([
            tx("2026-10-30", STO, "-QQQ261130P660", -3, 8.38, 2511.95, commission=1.95, fees=0.10),
            tx("2026-10-30", BTO, "-QQQ261130P655", 3, 7.48, -2245.99, commission=1.95, fees=0.04),
        ])
        self.assertEqual(p.strategy, cs.BULL_PUT)
        self.assertEqual(p.contracts, 3)
        self.assertAlmostEqual(p.credit, 0.90)  # 8.38 - 7.48
        self.assertAlmostEqual(p.premium_collected, 270.0)
        self.assertAlmostEqual(p.net_premium_collected, 265.96)  # 2,511.95 - 2,245.99
        self.assertAlmostEqual(p.open_fees, 4.04)
        self.assertAlmostEqual(p.max_loss, 1230.0)  # (5 - 0.90) x 100 x 3
        payload = cs.position_payload(p, DEFAULTS)
        self.assertEqual(payload["breakeven"], 659.10)
        self.assertEqual(payload["net_premium_collected"], 265.96)
        self.assertEqual([t["price"] for t in payload["trades"]], [8.38, 7.48])
        self.assertIn("= $659.10", payload["breakeven_tip"])
        self.assertIn("= $265.96", payload["net_collected_tip"])

    def test_closing_fills_are_listed_with_their_price(self):
        (p,) = positions_from(WIN)
        trades = cs.position_payload(p, DEFAULTS)["trades"]
        self.assertEqual([t["action"] for t in trades], ["Sold to open", "Bought to open", "Bought to close", "Sold to close"])
        self.assertEqual([t["price"] for t in trades], [1.50, 0.50, 0.40, 0.10])

    def test_combined_ids_are_account_scoped(self):
        (a,) = positions_from(opened_put_spread(), account_id="ira", account_label="IRA")
        self.assertTrue(a.spread_id.startswith("ira:"))
        self.assertEqual(a.account_label, "IRA")


class TestManagement(unittest.TestCase):
    """Open bull put 500/495 at a $1.00 credit, expiring 2026-10-16."""

    def setUp(self):
        (self.position,) = positions_from(opened_put_spread())
        self.expiry = date(2026, 10, 16)

    def manage(self, short_ask, long_bid, *, days_left=30, spot=520.0, iv=0.20, as_of=NOW, settings=DEFAULTS):
        today = self.expiry - timedelta(days=days_left)
        c = cs.ChainQuotes(
            "SPY", self.expiry, spot, cs.QuoteTiming(as_of, as_of),
            (cs.LegQuote(500.0, max(short_ask - 0.05, 0.0), short_ask, iv), cs.LegQuote(495.0, long_bid, long_bid + 0.05, iv)),
            (),
        )
        return cs.manage_spread(self.position, today=today, chain=c, settings=settings, risk_free_rate=RATE, now=NOW)

    def test_profit_target(self):
        g = self.manage(0.45, 0.05)
        self.assertEqual(g.action_label, "CLOSE: PROFIT TARGET REACHED")
        self.assertAlmostEqual(g.closing_debit, 0.40)
        self.assertAlmostEqual(g.target_debit, 0.50)
        self.assertAlmostEqual(g.unrealized_pl, 60.0)
        self.assertAlmostEqual(g.pct_of_max_profit, 60.0)

    def test_profit_target_beats_time_exit(self):
        self.assertEqual(self.manage(0.45, 0.05, days_left=10).action_label, "CLOSE: PROFIT TARGET REACHED")

    def test_loss_threshold_is_a_prompt_with_the_numbers(self):
        g = self.manage(2.60, 0.50)
        self.assertEqual(g.action_label, "LOSS THRESHOLD REACHED: REVIEW TO CLOSE")
        self.assertAlmostEqual(g.loss_trigger, 2.00)
        self.assertIn("$110.00", g.reason)  # (2.10 - 1.00) x 100
        self.assertIn("$290.00 more", g.reason)  # (5 - 2.10) x 100

    def test_time_exit_losing_shows_loss_and_remaining_risk(self):
        g = self.manage(1.45, 0.05, days_left=15)
        self.assertEqual(g.action_label, "CLOSE: TIME EXIT (21 DTE)")
        self.assertIn("Losing about $40.00", g.reason)
        self.assertIn("$360.00 more at risk", g.reason)

    def test_time_exit_winning(self):
        g = self.manage(0.75, 0.05, days_left=21)
        self.assertEqual(g.action_label, "CLOSE: TIME EXIT (21 DTE)")
        self.assertIn("keeps about $30.00", g.reason)
        self.assertEqual(g.time_exit_date, date(2026, 9, 25))

    def test_breached_short_strike_is_reviewed_not_closed(self):
        g = self.manage(1.55, 0.05, spot=498.0)
        self.assertEqual(g.action_label, "REVIEW: SHORT STRIKE UNDER PRESSURE")
        self.assertLess(g.distance_to_short_pct, 0)
        self.assertTrue(any("crossed the short strike" in f for f in g.flags))

    def test_hold_names_the_targets(self):
        g = self.manage(1.05, 0.05, spot=540.0)
        self.assertEqual(g.action, cs.ACTION_HOLD)
        self.assertIn("$0.50", g.reason)
        self.assertGreater(g.distance_to_short_pct, 0)

    def test_no_quote_is_unverified_and_time_exit_still_applies(self):
        g = cs.manage_spread(self.position, today=self.expiry - timedelta(days=30), chain=None, settings=DEFAULTS,
                             risk_free_rate=RATE, now=NOW, chain_unavailable_reason="Option prices couldn't be loaded.")
        self.assertEqual(g.action, cs.ACTION_UNVERIFIED)
        self.assertIsNone(g.unrealized_pl)
        late = cs.manage_spread(self.position, today=self.expiry - timedelta(days=5), chain=None, settings=DEFAULTS,
                                risk_free_rate=RATE, now=NOW)
        self.assertEqual(late.action_label, "CLOSE: TIME EXIT (21 DTE)")
        self.assertIn("P/L is unknown", late.reason)

    def test_past_expiration_in_the_ledger(self):
        g = cs.manage_spread(self.position, today=self.expiry + timedelta(days=2), chain=None, settings=DEFAULTS,
                             risk_free_rate=RATE, now=NOW)
        self.assertEqual(g.action_label, "CHECK BROKER: PAST EXPIRATION")

    def test_stale_and_unreliable_quotes_are_flagged(self):
        g = self.manage(6.00, 0.50, as_of=NOW - timedelta(days=1))
        self.assertTrue(any("not current" in f for f in g.flags))
        self.assertTrue(any("above the spread width" in f for f in g.flags))


class TestPerformance(unittest.TestCase):
    def setUp(self):
        self.positions = positions_from(WIN + LOSS + EXPIRES)

    def test_metrics(self):
        perf = cs.performance(self.positions, {}, DEFAULTS)
        self.assertEqual(perf["closed_count"], 3)
        self.assertAlmostEqual(perf["premium_collected_opened_in_range"], 300.0)
        self.assertAlmostEqual(perf["realized_net"], 43.50)  # 67.40 - 122.60 + 98.70
        self.assertAlmostEqual(perf["realized_fees"], 6.50)
        self.assertAlmostEqual(perf["realized_gross"], 50.0)
        self.assertEqual(perf["win_rate_pct"], 66.7)
        self.assertAlmostEqual(perf["avg_net_per_closed"], 14.50)
        self.assertAlmostEqual(perf["avg_win"], 83.05)
        self.assertAlmostEqual(perf["avg_loss"], -122.60)
        self.assertAlmostEqual(perf["largest_loss"], -122.60)
        self.assertAlmostEqual(perf["profit_factor"], round(166.10 / 122.60, 2))
        self.assertAlmostEqual(perf["return_on_max_risk_pct"], round(43.5 / 1200 * 100, 1))
        self.assertAlmostEqual(perf["max_drawdown"], 122.60)  # +67.40 peak, then -122.60
        counts = {row["key"]: row["count"] for row in perf["exit_counts"]}
        self.assertEqual((counts["PROFIT_TARGET"], counts["LOSS_THRESHOLD"], counts["EXPIRED"]), (1, 1, 1))

    def test_date_range_uses_each_figures_own_date(self):
        perf = cs.performance(self.positions, {}, DEFAULTS, start=date(2026, 9, 16))
        self.assertEqual(perf["closed_count"], 2)  # the 9/15 win is out
        self.assertEqual(perf["spreads_opened_in_range"], 0)
        self.assertAlmostEqual(perf["realized_net"], -122.60 + 98.70)

    def test_partial_close_counts_toward_realized_but_not_trades(self):
        txs = opened_put_spread(n=2) + closed_legs("2026-09-15", "SPY", "261016", "P", 500, 495, 0.40, 0.10, n=1)
        perf = cs.performance(positions_from(txs), {}, DEFAULTS)
        self.assertAlmostEqual(perf["realized_net"], 67.40)
        self.assertEqual(perf["closed_count"], 0)
        self.assertEqual(perf["unrealized_unpriced_count"], 1)

    def test_no_closed_spreads(self):
        perf = cs.performance([], {}, DEFAULTS)
        self.assertIsNone(perf["win_rate_pct"])
        self.assertIsNone(perf["profit_factor"])
        self.assertEqual(perf["max_drawdown"], 0.0)


# ---------------------------------------------------------------- dashboard integration


def _contract(ticker, strike, right, bid, ask, iv):
    return OptionContractQuote(f"{ticker}{right}{strike}", strike, right, bid, ask, None, None, iv, False, 500, 10)


def _stub_dashboard(transactions, snapshots, expirations, equity=100_000.0):
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.all_cycles, _ = build_cycles(transactions)
    dashboard._price_warnings = ["an existing dashboard warning"]
    dashboard._net_worth = {"available": True, "total_value": equity, "as_of": "2026-10-26T00:00:00"}
    dashboard._option_expirations = lambda tickers: {t: expirations.get(t, []) for t in tickers}
    dashboard._option_chains = lambda pairs: {
        p: (snapshots[p], None) if p in snapshots else (None, f"could not fetch {p[0]}") for p in pairs
    }
    dashboard._option_chain_fetched_at = {}
    dashboard._daily_bars = lambda tickers: {
        t: [PricePoint(TODAY - timedelta(days=60 - i), 80 + i * 0.4) for i in range(60)] for t in tickers
    }
    dashboard._fundamentals = lambda tickers: {t: {"earnings_date": None} for t in tickers}
    return dashboard


class TestDashboardPayload(unittest.TestCase):
    def setUp(self):
        days = (EXPIRY - TODAY).days
        scan_chain = OptionChainSnapshot("ABC", EXPIRY, SPOT, NOW, (
            _contract("ABC", 90.0, "P", 1.70, 1.80, iv_for_delta(SPOT, 90, days, 0.165, PUT)),
            _contract("ABC", 85.0, "P", 0.10, 0.15, 0.45),
        ))
        open_expiry = date(2026, 11, 20)
        open_chain = OptionChainSnapshot("SPY", open_expiry, 520.0, NOW, (
            _contract("SPY", 500.0, "P", 0.40, 0.45, 0.2),
            _contract("SPY", 495.0, "P", 0.05, 0.10, 0.2),
        ))
        self.ledger = opened_put_spread("2026-10-20", "SPY", "261120") + WIN
        self.dashboard = _stub_dashboard(
            self.ledger,
            {("ABC", EXPIRY): scan_chain, ("SPY", open_expiry): open_chain},
            {"ABC": [EXPIRY]},
        )
        self.settings = cs.SpreadSettings(underlyings=("ABC", "NOPE"))
        self.payload = self.dashboard.build_credit_spreads(self.settings, today=TODAY, now=NOW)

    def test_recommendation_is_the_same_object_as_its_table_row(self):
        (abc_rec, nope_rec) = self.payload["recommendations"]
        self.assertEqual(abc_rec["status"], "RECOMMENDED")
        row = next(c for c in self.payload["scan"][0]["candidates"] if c["id"] == abc_rec["pick"]["id"])
        self.assertEqual(row, abc_rec["pick"])
        self.assertEqual(row["credit"], 1.55)
        self.assertIn(f"= ${row['max_loss']:,.2f}", row["tip"])
        self.assertEqual(nope_rec["status"], "NO QUALIFYING SPREADS")
        self.assertIn("NOPE", nope_rec["reasons"][0])

    def test_open_spread_guidance_and_closed_history(self):
        (row,) = self.payload["open_spreads"]
        self.assertEqual(row["action_label"], "CLOSE: PROFIT TARGET REACHED")
        self.assertEqual(row["closing_debit_now"], 0.40)
        self.assertEqual(row["target_debit"], 0.50)
        self.assertEqual(row["unrealized_pl"], 60.0)
        self.assertEqual(row["time_exit_date"], "2026-10-30")
        self.assertIn("= $0.40", row["tip"])
        (closed,) = self.payload["closed_spreads"]
        self.assertEqual(closed["exit"], cs.EXIT_PROFIT_TARGET)
        self.assertEqual(self.payload["performance"]["unrealized"], 60.0)
        self.assertEqual(self.payload["open_spread_count"], 1)

    def test_date_range_filters_closed_spreads_like_the_performance_figures(self):
        # The closed WIN spread closed 2026-09-15; the open SPY spread opened 2026-10-20.
        after = self.dashboard.build_credit_spreads(
            self.settings, filters=Filters(start=date(2026, 10, 1)), today=TODAY, now=NOW
        )
        self.assertEqual(after["closed_spreads"], [])
        self.assertEqual(after["performance"]["closed_count"], 0)
        self.assertEqual(len(after["open_spreads"]), 1)  # open positions ignore the range

        around = self.dashboard.build_credit_spreads(
            self.settings, filters=Filters(start=date(2026, 9, 15), end=date(2026, 9, 15)), today=TODAY, now=NOW
        )
        self.assertEqual([r["close_date"] for r in around["closed_spreads"]], ["2026-09-15"])  # inclusive
        self.assertEqual(around["performance"]["closed_count"], len(around["closed_spreads"]))

        before = self.dashboard.build_credit_spreads(
            self.settings, filters=Filters(end=date(2026, 9, 14)), today=TODAY, now=NOW
        )
        self.assertEqual(before["closed_spreads"], [])

    def test_api_version_matches_the_page(self):
        import re

        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wheel", "static", "spreads.js")
        with open(path, encoding="utf-8") as handle:
            page_version = int(re.search(r"SPREADS_API_VERSION = (\d+);", handle.read()).group(1))
        self.assertEqual(self.payload["api_version"], page_version)

    def test_fetch_warnings_stay_out_of_the_wheel_dashboard(self):
        self.assertEqual(self.dashboard._price_warnings, ["an existing dashboard warning"])

    def test_check_uses_the_same_rules(self):
        check, error = cs.SpreadCheck.from_query({
            "check_ticker": ["abc"], "check_expiry": [EXPIRY.isoformat()], "check_strategy": ["BULL_PUT"],
            "check_short": ["90"], "check_long": ["85"],
        })
        self.assertIsNone(error)
        payload = self.dashboard.build_credit_spreads(self.settings, check=check, today=TODAY, now=NOW)
        scanned = next(c for c in payload["scan"][0]["candidates"] if c["id"] == payload["check"]["candidate"]["id"])
        self.assertEqual(payload["check"]["candidate"], scanned)

        bad, _ = cs.SpreadCheck.from_query({
            "check_ticker": ["ABC"], "check_expiry": [EXPIRY.isoformat()], "check_strategy": ["BULL_PUT"],
            "check_short": ["85"], "check_long": ["90"],
        })
        payload = self.dashboard.build_credit_spreads(self.settings, check=bad, today=TODAY, now=NOW)
        self.assertIn("higher strike", payload["check"]["error"])

    def test_check_request_validation(self):
        self.assertEqual(cs.SpreadCheck.from_query({}), (None, None))
        self.assertIsNotNone(cs.SpreadCheck.from_query({"check_ticker": ["SPY"]})[1])
        self.assertIsNotNone(cs.SpreadCheck.from_query({
            "check_ticker": ["SPY"], "check_expiry": ["2026-12-04"], "check_strategy": ["IRON_CONDOR"],
            "check_short": ["1"], "check_long": ["2"],
        })[1])

    def test_combined_view_lists_every_account_and_sums_equity(self):
        from wheel.accounts import AccountRegistry

        other = _stub_dashboard(LOSS, {}, {}, equity=50_000.0)
        registry = AccountRegistry.__new__(AccountRegistry)
        registry._accounts = {"ira": self.dashboard, "roth": other}
        registry.list_accounts = lambda: [{"id": "ira", "label": "IRA"}, {"id": "roth", "label": "Roth"}]
        payload = registry.build_credit_spreads("combined", self.settings, Filters())
        self.assertEqual(payload["account_equity"]["value"], 150_000.0)
        labels = sorted(row["account_label"] for row in payload["closed_spreads"])
        self.assertEqual(labels, ["IRA", "Roth"])
        self.assertEqual(payload["performance"]["closed_count"], 2)


if __name__ == "__main__":
    unittest.main()
