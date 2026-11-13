"""The credit spread opportunity scanner: universe discovery, screening, spread
construction, scoring and ranking, the staged scan (through a fake data
provider, no network) and the track record. Chains are hand-built; a strike's
implied volatility is solved so model delta and quoted price agree, the way a
real quote does."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_creditspreads import closed_legs, opened_put_spread, positions_from  # noqa: E402
from wheel import creditspreads as cs  # noqa: E402
from wheel import spreadjournal as journal  # noqa: E402
from wheel import spreadscanner as sc  # noqa: E402
from wheel import spreadscanservice as svc  # noqa: E402
from wheel import spreaduniverse as uni  # noqa: E402
from wheel.marketdata import MarketDataError, PricePoint  # noqa: E402
from wheel.optionsmath import CALL, PUT, black_scholes_price, probability_finish_above  # noqa: E402

RATE = 0.045
FEE = 0.65
TODAY = date(2026, 11, 2)
NOW = datetime(2026, 11, 2, 16, 0, tzinfo=timezone.utc)
EXPIRY = TODAY + timedelta(days=35)
SPOT = 100.0
FRESH = cs.QuoteTiming(NOW, NOW)
DEFAULTS = sc.ScannerSettings()
NO_EXPOSURE = sc.Exposure(None, "", 0.0, {}, {}, {})
QUIET_MARKET = sc.MarketConditions(True, True, 15.0, False, {"Information Technology": True})


def priced_leg(strike, right, *, iv=0.35, half_spread=0.03, open_interest=2000, volume=500, dte=35, spot=SPOT):
    value = black_scholes_price(spot=spot, strike=strike, days_to_expiry=dte, implied_volatility=iv, right=right, risk_free_rate=RATE)
    bid = round(max(value - half_spread, 0.01), 2)
    return cs.LegQuote(strike, bid, round(bid + 2 * half_spread, 2), iv, open_interest, volume)


def put_chain(*, strikes=(80, 82, 84, 85, 86, 88, 90, 92), ticker="ABC", timing=FRESH, spot=SPOT, **leg_options):
    puts = tuple(priced_leg(float(k), PUT, spot=spot, **leg_options) for k in strikes)
    return cs.ChainQuotes(ticker, EXPIRY, spot, timing, puts, ())


def call_chain(*, strikes=(108, 110, 112, 114, 115, 116, 118, 120), ticker="ABC", timing=FRESH, **leg_options):
    calls = tuple(priced_leg(float(k), CALL, **leg_options) for k in strikes)
    return cs.ChainQuotes(ticker, EXPIRY, SPOT, timing, (), calls)


def build(chain):
    return sc.construct_spreads(chain, today=TODAY, risk_free_rate=RATE, fee_per_contract=FEE)


def stock(ticker="ABC", **fields):
    values = dict(
        is_etf=False, name="ABC Corp", sector="Information Technology", price=SPOT, market_cap_b=50.0,
        avg_volume_m=5.0, earnings_date=TODAY + timedelta(days=80), eps_ttm=3.0,
    )
    values.update(fields)
    return sc.Underlying(ticker, **values)


def raw_spread(chain=None, short=90.0, long=85.0):
    spreads, _ = build(chain or put_chain())
    return next(s for s in spreads if s.short.strike == short and s.long.strike == long)


def rising_bars(days=260, start=70.0, step=0.12):
    first = TODAY - timedelta(days=days)
    return [
        PricePoint(first + timedelta(days=i), start + i * step, start + i * step + 0.5, start + i * step - 0.5, 1e6)
        for i in range(days)
    ]


def score(raw, u=None, *, technicals=None, market=QUIET_MARKET, exposure=NO_EXPOSURE, settings=DEFAULTS, now=NOW):
    return sc.score_spread(raw, u or stock(), technicals=technicals, market=market, exposure=exposure, settings=settings, today=TODAY, now=now)


def rank(spreads, underlyings=None, *, technicals=None, exposure=NO_EXPOSURE, settings=DEFAULTS, market=QUIET_MARKET):
    return sc.rank_opportunities(
        spreads, underlyings=underlyings or {"ABC": stock()}, technicals=technicals or {}, market=market,
        exposure=exposure, settings=settings, today=TODAY, now=NOW,
    )


class TestUniverse(unittest.TestCase):
    SP500 = "Symbol,Security,GICS Sector\nAAPL,Apple,Information Technology\nBRK.B,Berkshire,Financials\n,Nameless,Energy\n"
    N100 = json.dumps({"data": {"data": {"rows": [{"symbol": "AAPL", "companyName": "Apple Inc."}, {"symbol": "ASML", "companyName": "ASML"}]}}})
    SCREENER = json.dumps({"data": {"rows": [
        {"symbol": "BIG", "name": "Big", "marketCap": "900.00", "country": "United States", "sector": "Finance"},
        {"symbol": "MID", "name": "Mid", "marketCap": "500.00", "country": "United States", "sector": "Miscellaneous"},
        {"symbol": "SMALL", "name": "Small", "marketCap": "100.00", "country": "United States", "sector": "Energy"},
        {"symbol": "FOR", "name": "Foreign", "marketCap": "950.00", "country": "Israel", "sector": "Energy"},
        {"symbol": "NOCAP", "name": "No cap", "marketCap": "", "country": "United States", "sector": "Energy"},
        {"symbol": "ABC^D", "name": "Preferred", "marketCap": "800.00", "country": "United States", "sector": "Energy"},
    ]}})

    def test_symbols_use_yahoo_spelling_and_skip_odd_share_classes(self):
        self.assertEqual(uni.normalize_symbol("BRK.B"), "BRK-B")
        self.assertEqual(uni.normalize_symbol("brk/b"), "BRK-B")
        self.assertIsNone(uni.normalize_symbol("ABC^D"))
        self.assertIsNone(uni.normalize_symbol(""))

    def test_sp500_csv(self):
        members = uni.parse_sp500_csv(self.SP500)
        self.assertEqual([m.ticker for m in members], ["AAPL", "BRK-B"])
        self.assertEqual(members[1].sector, "Financials")

    def test_nasdaq_screener_takes_largest_us_with_known_cap(self):
        members = uni.parse_nasdaq_screener_json(self.SCREENER, count=2)
        self.assertEqual([m.ticker for m in members], ["BIG", "MID"])
        self.assertEqual(members[0].sector, "Financials")
        self.assertIsNone(members[1].sector, "Nasdaq's Miscellaneous has no real sector")

    def test_bad_payloads_raise_a_universe_error(self):
        for parser in (uni.parse_sp500_csv, uni.parse_nasdaq100_json, uni.parse_nasdaq_screener_json):
            with self.assertRaises(uni.UniverseError):
                parser("<html>not data</html>")

    def test_merge_dedupes_and_keeps_every_source(self):
        merged = uni.merge_members(
            [uni.parse_sp500_csv(self.SP500), uni.parse_nasdaq100_json(self.N100)]
        )
        self.assertEqual([m.ticker for m in merged], ["AAPL", "ASML", "BRK-B"])
        apple = merged[0]
        self.assertEqual(apple.sources, ("sp500", "nasdaq100"))
        self.assertEqual(apple.sector, "Information Technology", "a later source without a sector keeps the first")

    def _source(self, key, members=None, error=None):
        class Source:
            label = key.upper()

            def __init__(self):
                self.key = key

            def fetch(self):
                if error:
                    raise uni.UniverseError(error)
                return members

        return Source()

    def test_failed_source_keeps_its_saved_list_and_warns(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "u.json")
            first = [uni.UniverseMember("AAA", None, None, ("one",))]
            second = [uni.UniverseMember("BBB", None, None, ("two",))]
            uni.get_universe(TODAY, sources=[self._source("one", first), self._source("two", second)], cache_path=path)
            later = TODAY + timedelta(days=uni.UNIVERSE_MAX_AGE_DAYS)
            members, statuses, warnings = uni.get_universe(
                later, sources=[self._source("one", first), self._source("two", error="down")], cache_path=path
            )
            self.assertEqual([m.ticker for m in members], ["AAA", "BBB"])
            self.assertFalse(statuses[1].live)
            self.assertIn("down", warnings[0])

    def test_failed_source_with_no_saved_list_adds_nothing_and_says_so(self):
        with tempfile.TemporaryDirectory() as folder:
            members, statuses, warnings = uni.get_universe(
                TODAY, sources=[self._source("one", error="down")], cache_path=os.path.join(folder, "u.json")
            )
            self.assertEqual(members, [])
            self.assertEqual(statuses[0].count, 0)
            self.assertTrue(warnings)

    def test_fresh_cache_makes_no_download(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "u.json")
            uni.get_universe(TODAY, sources=[self._source("one", [uni.UniverseMember("AAA", None, None, ("one",))])], cache_path=path)
            members, _, warnings = uni.get_universe(TODAY + timedelta(days=1), sources=[self._source("one", error="should not be called")], cache_path=path)
            self.assertEqual([m.ticker for m in members], ["AAA"])
            self.assertEqual(warnings, [])


class TestSettings(unittest.TestCase):
    def test_defaults_match_the_request(self):
        self.assertEqual((DEFAULTS.dte_min, DEFAULTS.dte_max, DEFAULTS.dte_pref_min, DEFAULTS.dte_pref_max), (25, 60, 30, 45))
        self.assertEqual((DEFAULTS.delta_min, DEFAULTS.delta_max), (0.10, 0.25))
        self.assertEqual(DEFAULTS.top_n, 10)

    def test_query_round_trip(self):
        settings, warnings = sc.ScannerSettings.from_query({"delta_max": ["30"], "min_pop": ["70"], "strategy": ["BULL_PUT"], "etfs": ["spy, qqq"]})
        self.assertEqual(warnings, [])
        self.assertAlmostEqual(settings.delta_max, 0.30)
        self.assertEqual(settings.min_pop_pct, 70.0)
        self.assertEqual(settings.strategy, cs.BULL_PUT)
        self.assertEqual(settings.etfs, ("SPY", "QQQ"))
        self.assertEqual(settings.as_dict()["delta_max"], 30.0)

    def test_invalid_values_keep_the_default_and_warn(self):
        settings, warnings = sc.ScannerSettings.from_query({"min_pop": ["abc"], "top_n": ["2.5"], "strategy": ["NOPE"], "etfs": ["$$$"]})
        self.assertEqual(settings.min_pop_pct, DEFAULTS.min_pop_pct)
        self.assertEqual(settings.top_n, DEFAULTS.top_n)
        self.assertEqual(settings.strategy, sc.BOTH)
        self.assertEqual(settings.etfs, DEFAULTS.etfs)
        self.assertEqual(len(warnings), 4)

    def test_backwards_ranges_fall_back(self):
        settings, warnings = sc.ScannerSettings.from_query({"dte_min": ["50"], "dte_max": ["40"]})
        self.assertEqual((settings.dte_min, settings.dte_max), (25, 60))
        self.assertTrue(warnings)

    def test_only_download_settings_change_the_scan_key(self):
        same, _ = sc.ScannerSettings.from_query({"min_pop": ["80"], "strategy": ["BEAR_CALL"]})
        self.assertEqual(same.scan_key, DEFAULTS.scan_key)
        other, _ = sc.ScannerSettings.from_query({"max_stocks": ["10"]})
        self.assertNotEqual(other.scan_key, DEFAULTS.scan_key)


class TestStockScreen(unittest.TestCase):
    def test_a_good_stock_passes(self):
        self.assertIsNone(sc.screen_underlying(stock(), DEFAULTS, TODAY))

    def test_hard_exclusions(self):
        for fields, expected in (
            (dict(price=None), "No current price"),
            (dict(price=10.0), "Price under"),
            (dict(price=900.0), "Price over"),
            (dict(market_cap_b=None), "No market cap"),
            (dict(market_cap_b=0.5), "Market cap under"),
            (dict(avg_volume_m=None), "No average volume"),
            (dict(avg_volume_m=0.1), "Average volume under"),
            (dict(excluded_kind="leveraged ETF"), "Excluded fund type"),
        ):
            self.assertIn(expected, sc.screen_underlying(stock(**fields), DEFAULTS, TODAY))

    def test_unprofitable_and_missing_earnings_are_preferences_not_exclusions(self):
        self.assertIsNone(sc.screen_underlying(stock(eps_ttm=-2.0), DEFAULTS, TODAY))
        self.assertIsNone(sc.screen_underlying(stock(eps_ttm=None, earnings_date=None), DEFAULTS, TODAY))

    def test_etfs_have_their_own_rules(self):
        etf = stock("SPY", is_etf=True, price=735.0, market_cap_b=None, earnings_date=None, eps_ttm=None)
        self.assertIsNone(sc.screen_underlying(etf, DEFAULTS, TODAY), "no price ceiling, market cap or earnings on an ETF")
        self.assertIn("Average volume", sc.screen_underlying(stock("TINY", is_etf=True, avg_volume_m=0.1), DEFAULTS, TODAY))

    def test_earnings_rule_can_be_made_hard(self):
        soon = stock(earnings_date=TODAY + timedelta(days=5))
        self.assertIsNone(sc.screen_underlying(soon, DEFAULTS, TODAY))
        strict, _ = sc.ScannerSettings.from_query({"earnings_policy": [sc.EARNINGS_EXCLUDE]})
        self.assertIn("Earnings", sc.screen_underlying(soon, strict, TODAY))

    def test_most_traded_first_earnings_soon_last_and_capped(self):
        settings, _ = sc.ScannerSettings.from_query({"max_stocks": ["2"]})
        passed = [
            stock("THIN", avg_volume_m=2.0),
            stock("BUSY", avg_volume_m=40.0, earnings_date=TODAY + timedelta(days=10)),
            stock("MID", avg_volume_m=10.0),
            stock("SPY", is_etf=True),
        ]
        self.assertEqual([u.ticker for u in sc.prioritize_stocks(passed, settings, TODAY)], ["MID", "THIN"])

    def test_expirations_prefer_the_preferred_window(self):
        listed = [TODAY + timedelta(days=d) for d in (7, 26, 30, 37, 44, 58, 90)]
        settings, _ = sc.ScannerSettings.from_query({"max_expirations": ["3"]})
        picked = [(d - TODAY).days for d in sc.pick_expirations(listed, TODAY, settings)]
        self.assertEqual(picked, [30, 37, 44])

    def test_expirations_fall_back_inside_the_hard_window_only(self):
        listed = [TODAY + timedelta(days=d) for d in (7, 26, 90)]
        self.assertEqual([(d - TODAY).days for d in sc.pick_expirations(listed, TODAY, DEFAULTS)], [26])
        self.assertEqual(sc.pick_expirations([TODAY + timedelta(days=7)], TODAY, DEFAULTS), [])


class TestUnderlyingFromQuote(unittest.TestCase):
    MEMBER = uni.UniverseMember("ABC", "Member name", "Energy", ("sp500",))

    def epoch(self, day):
        return datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc).timestamp()

    def test_past_earnings_timestamp_is_not_upcoming(self):
        row = {"quoteType": "EQUITY", "earningsTimestamp": self.epoch(TODAY - timedelta(days=3))}
        self.assertIsNone(svc.underlying_from_quote("ABC", row, self.MEMBER, TODAY).earnings_date)

    def test_next_earnings_is_found_and_override_wins(self):
        next_report = TODAY + timedelta(days=20)
        row = {"quoteType": "EQUITY", "earningsTimestampStart": self.epoch(next_report), "earningsTimestamp": self.epoch(TODAY - timedelta(days=3))}
        self.assertEqual(svc.underlying_from_quote("ABC", row, self.MEMBER, TODAY).earnings_date, next_report)
        manual = TODAY + timedelta(days=9)
        self.assertEqual(svc.underlying_from_quote("ABC", row, self.MEMBER, TODAY, manual).earnings_date, manual)

    def test_fund_types(self):
        leveraged = svc.underlying_from_quote("TQQQ", {"quoteType": "ETF", "longName": "ProShares UltraPro QQQ"}, self.MEMBER, TODAY)
        self.assertEqual(leveraged.excluded_kind, "leveraged ETF")
        plain = svc.underlying_from_quote("SPY", {"quoteType": "ETF", "longName": "SPDR S&P 500 ETF Trust"}, self.MEMBER, TODAY)
        self.assertTrue(plain.is_etf)
        self.assertIsNone(plain.excluded_kind)

    def test_missing_fields_stay_missing(self):
        u = svc.underlying_from_quote("ABC", {"quoteType": "EQUITY"}, self.MEMBER, TODAY)
        self.assertIsNone(u.price)
        self.assertIsNone(u.market_cap_b)
        self.assertEqual(u.sector, "Energy")


class TestConstruction(unittest.TestCase):
    def test_bull_put_math_is_exact(self):
        short = cs.LegQuote(90.0, 1.70, 1.80, 0.40, 500, 100)
        long = cs.LegQuote(85.0, 0.60, 0.70, 0.42, 500, 100)
        raw = sc.RawSpread("ABC", cs.BULL_PUT, EXPIRY, 35, SPOT, FRESH, short, long, 1.00, 1.15, 0.2, 0.75, None, FEE)
        self.assertEqual(raw.width, 5.0)
        self.assertAlmostEqual(raw.max_profit, 100.0)
        self.assertAlmostEqual(raw.max_loss, 400.0)
        self.assertAlmostEqual(raw.breakeven, 89.0)
        self.assertAlmostEqual(raw.return_on_risk_pct, 25.0)
        self.assertAlmostEqual(raw.annualized_ror_pct, 25.0 * 365 / 35)
        self.assertAlmostEqual(raw.expected_value, 0.75 * 100 - 0.25 * 400 - 4 * FEE)
        self.assertAlmostEqual(raw.risk_per_contract, 401.30)
        self.assertAlmostEqual(raw.distance_pct, 10.0)
        self.assertAlmostEqual(raw.leg_spread_cost, 0.20)

    def test_bear_call_breakeven_and_pop_use_the_breakeven(self):
        spreads, _ = build(call_chain())
        raw = next(s for s in spreads if s.short.strike == 112.0 and s.long.strike == 114.0)
        self.assertEqual(raw.strategy, cs.BEAR_CALL)
        self.assertAlmostEqual(raw.breakeven, 112.0 + raw.credit)
        expected = 1 - probability_finish_above(spot=SPOT, level=raw.breakeven, days_to_expiry=35, implied_volatility=raw.short.iv, risk_free_rate=RATE)
        self.assertAlmostEqual(raw.pop, expected)

    def test_pop_is_not_just_one_minus_delta(self):
        raw = raw_spread()
        self.assertNotAlmostEqual(raw.pop, 1 - raw.short_delta, places=3)

    def test_credit_is_short_bid_minus_long_ask(self):
        raw = raw_spread()
        self.assertAlmostEqual(raw.credit, raw.short.bid - raw.long.ask)
        self.assertGreater(raw.credit_mid, raw.credit)

    def test_several_widths_for_one_short_strike(self):
        spreads, _ = build(put_chain())
        widths = sorted(s.width for s in spreads if s.short.strike == 90.0)
        self.assertIn(2.0, widths)
        self.assertIn(5.0, widths)
        self.assertIn(10.0, widths)

    def test_width_limits_follow_the_price(self):
        self.assertNotIn(1.0, sc.practical_widths(735.0))
        self.assertIn(50.0, sc.practical_widths(735.0))
        self.assertNotIn(10.0, sc.practical_widths(50.0))

    def test_in_the_money_short_never_builds(self):
        spreads, _ = build(put_chain(strikes=(95, 100, 105, 110)))
        self.assertFalse([s for s in spreads if s.short.strike >= SPOT])

    def _one_put(self, short, long, spot=SPOT):
        return cs.ChainQuotes("ABC", EXPIRY, spot, FRESH, (short, long), ())

    def test_bad_quotes_are_thrown_out_with_a_reason(self):
        good_long = priced_leg(85.0, PUT)
        short_value = black_scholes_price(spot=SPOT, strike=90.0, days_to_expiry=35, implied_volatility=0.35, right=PUT, risk_free_rate=RATE)
        for short, long, reason in (
            (cs.LegQuote(90.0, 0.0, short_value + 0.2, 0.35, 500, 5), good_long, "Short leg has no bid"),
            (cs.LegQuote(90.0, None, None, 0.35, 500, 5), good_long, "Short leg has no bid"),
            (cs.LegQuote(90.0, short_value + 0.2, short_value - 0.2, 0.35, 500, 5), good_long, "Short leg quote is missing or crossed"),
            (cs.LegQuote(90.0, short_value, short_value + 0.05, 0.35, 500, 5), cs.LegQuote(85.0, None, None, 0.35, 500, 5), "Long leg quote is missing or crossed"),
            (cs.LegQuote(90.0, short_value, short_value + 0.05, 0.35, 500, 5), cs.LegQuote(85.0, 9.0, 9.5, 0.35, 500, 5), "No credit after bid and ask"),
        ):
            spreads, rejects = build(self._one_put(short, long))
            self.assertEqual([s for s in spreads if s.short.strike == 90.0], [], reason)
            self.assertIn(reason, rejects)

    def test_no_underlying_price_builds_nothing(self):
        spreads, rejects = build(cs.ChainQuotes("ABC", EXPIRY, None, FRESH, (), ()))
        self.assertEqual(spreads, [])
        self.assertTrue(rejects)

    def test_unbelievable_iv_gives_no_probability(self):
        short = cs.LegQuote(90.0, 1.0, 1.1, 0.0001, 500, 5)
        problem = sc.iv_problem(short, spot=SPOT, right=PUT, dte=35, risk_free_rate=RATE)
        self.assertIn("isn't believable", problem)
        self.assertIn("No implied volatility", sc.iv_problem(cs.LegQuote(90.0, 1.0, 1.1, None), spot=SPOT, right=PUT, dte=35, risk_free_rate=RATE))

    def test_iv_that_does_not_reprice_the_quote_gives_no_probability(self):
        value = black_scholes_price(spot=SPOT, strike=90.0, days_to_expiry=35, implied_volatility=0.35, right=PUT, risk_free_rate=RATE)
        mismatched = cs.LegQuote(90.0, value * 3, value * 3 + 0.1, 0.35, 500, 5)
        self.assertIn("don't match", sc.iv_problem(mismatched, spot=SPOT, right=PUT, dte=35, risk_free_rate=RATE))
        matched = cs.LegQuote(90.0, round(value - 0.03, 2), round(value + 0.03, 2), 0.35, 500, 5)
        self.assertIsNone(sc.iv_problem(matched, spot=SPOT, right=PUT, dte=35, risk_free_rate=RATE))

    def test_unreliable_probability_means_no_expected_value(self):
        value = black_scholes_price(spot=SPOT, strike=90.0, days_to_expiry=35, implied_volatility=0.35, right=PUT, risk_free_rate=RATE)
        short = cs.LegQuote(90.0, value * 3, value * 3 + 0.1, 0.35, 500, 5)
        long = priced_leg(85.0, PUT)
        spreads, _ = build(self._one_put(short, long))
        raw = spreads[0]
        self.assertIsNone(raw.pop)
        self.assertIsNone(raw.expected_value)
        self.assertIsNone(sc.touch_probability(raw, RATE))
        self.assertIn("don't match", raw.pop_problem)

    def test_touch_is_about_double_finishing_past_the_short_strike(self):
        raw = raw_spread()
        past = 1 - probability_finish_above(spot=SPOT, level=raw.short.strike, days_to_expiry=35, implied_volatility=raw.short.iv, risk_free_rate=RATE)
        self.assertAlmostEqual(sc.touch_probability(raw, RATE), 2 * past)


class TestQuoteStatus(unittest.TestCase):
    def status(self, as_of_minutes, fetched_minutes, realtime=False):
        timing = cs.QuoteTiming(NOW - timedelta(minutes=as_of_minutes), NOW - timedelta(minutes=fetched_minutes))
        return sc.quote_status(timing, NOW, realtime=realtime)[0]

    def test_each_state(self):
        self.assertEqual(self.status(5, 1), sc.QUOTE_DELAYED)
        self.assertEqual(self.status(5, 1, realtime=True), sc.QUOTE_LIVE)
        self.assertEqual(self.status(15, 12), sc.QUOTE_CACHED)
        self.assertEqual(self.status(45, 1), sc.QUOTE_STALE)
        self.assertEqual(sc.quote_status(cs.QuoteTiming(None, NOW), NOW)[0], sc.QUOTE_UNAVAILABLE)
        self.assertEqual(sc.quote_status(None, NOW)[0], sc.QUOTE_UNAVAILABLE)

    def test_free_feed_is_never_called_live(self):
        self.assertNotIn("Live", sc.quote_label(sc.QUOTE_DELAYED, 3.0))


class TestScoring(unittest.TestCase):
    def test_score_is_points_minus_penalties_and_bounded(self):
        scored = score(raw_spread())
        earned = sum(c.points for c in scored.components)
        lost = sum(p.points for p in scored.penalties)
        self.assertAlmostEqual(scored.score, max(0.0, min(100.0, earned - lost)))
        self.assertEqual(sum(c.weight for c in sc.SCORE_CATEGORIES), 100.0)
        for component in scored.components:
            self.assertLessEqual(component.points, component.weight + 1e-9)
            self.assertGreaterEqual(component.points, 0.0)

    def test_curves_are_smooth_and_bounded(self):
        for value in (-1e3, 0, 18, 1e3):
            self.assertTrue(0.0 <= sc.logistic(value, 18, 6) <= 1.0)
        self.assertAlmostEqual(sc.logistic(18, 18, 6), 0.5)

    def test_missing_data_earns_nothing(self):
        bare = stock(sector=None)
        raw = raw_spread()
        no_data = sc.MarketConditions()
        scored = score(raw, bare, technicals=None, market=no_data)
        by_key = {c.key: c for c in scored.components}
        self.assertEqual(by_key["technical"].points, 0.0)
        self.assertEqual(by_key["volatility"].points, 0.0)
        self.assertEqual(by_key["market"].points, 0.0)

    def test_unreliable_probability_earns_nothing_and_caps_at_watch(self):
        value = black_scholes_price(spot=SPOT, strike=90.0, days_to_expiry=35, implied_volatility=0.35, right=PUT, risk_free_rate=RATE)
        spreads, _ = build(cs.ChainQuotes("ABC", EXPIRY, SPOT, FRESH, (cs.LegQuote(90.0, value * 3, value * 3 + 0.1, 0.35, 900, 50), priced_leg(85.0, PUT)), ()))
        scored = score(spreads[0])
        self.assertEqual({c.key: c.points for c in scored.components}["pop"], 0.0)
        self.assertEqual(sc.rate(90.0, 45.0, [sc.WATCH]), sc.WATCH)
        self.assertIn(scored.rating, (sc.WATCH, sc.REJECTED))

    def test_high_pop_alone_does_not_make_a_trade_attractive(self):
        far = cs.LegQuote(70.0, 0.05, 0.08, 0.35, 5000, 800)
        farther = cs.LegQuote(65.0, 0.01, 0.02, 0.35, 5000, 800)
        raw = sc.RawSpread("ABC", cs.BULL_PUT, EXPIRY, 35, SPOT, FRESH, far, farther, 0.03, 0.05, 0.02, 0.99, None, FEE)
        scored = score(raw)
        self.assertNotIn(scored.rating, (sc.EXCELLENT, sc.VERY_GOOD), "a 99% trade paying under 1% isn't excellent")
        self.assertTrue(any("return on risk" in reason for reason in scored.rating_cap_reasons))

    def test_high_premium_cannot_buy_past_a_low_probability(self):
        short = cs.LegQuote(98.0, 4.2, 4.3, 0.35, 5000, 800)
        long = cs.LegQuote(93.0, 2.1, 2.2, 0.35, 5000, 800)
        raw = sc.RawSpread("ABC", cs.BULL_PUT, EXPIRY, 35, SPOT, FRESH, short, long, 2.0, 2.1, 0.4, 0.42, None, FEE)
        scored = score(raw)
        self.assertNotIn(scored.rating, (sc.EXCELLENT, sc.VERY_GOOD))
        self.assertTrue(scored.rating_cap_reasons)

    def test_poor_liquidity_lowers_the_score_sharply(self):
        liquid = score(raw_spread(put_chain(open_interest=5000, volume=800, half_spread=0.02)))
        thin = score(raw_spread(put_chain(open_interest=55, volume=None, half_spread=0.25)))
        self.assertGreater(liquid.liquidity, thin.liquidity)
        self.assertTrue(any(p.label == "Poor liquidity" for p in thin.penalties))
        self.assertLess(thin.score, liquid.score - 15)

    def test_missing_open_interest_is_not_rewarded(self):
        known = score(raw_spread(put_chain(open_interest=5000)))
        unknown = score(raw_spread(put_chain(open_interest=None)))
        self.assertLess(unknown.liquidity, known.liquidity)

    def test_earnings_in_window_is_penalized_and_capped_at_watch(self):
        raw = raw_spread()
        clean = score(raw)
        reporting = score(raw, stock(earnings_date=raw.expiry - timedelta(days=5)))
        self.assertTrue(reporting.earnings_in_window)
        self.assertLessEqual(reporting.score, clean.score - sc.PENALTY_EARNINGS_IN_WINDOW)
        self.assertIn(reporting.rating, (sc.WATCH, sc.REJECTED))

    def test_earnings_after_expiration_or_in_the_past_do_not_count(self):
        raw = raw_spread()
        self.assertFalse(score(raw, stock(earnings_date=raw.expiry + timedelta(days=1))).earnings_in_window)
        self.assertFalse(score(raw, stock(earnings_date=TODAY - timedelta(days=1))).earnings_in_window)

    def test_earnings_on_expiration_day_counts(self):
        raw = raw_spread()
        self.assertTrue(score(raw, stock(earnings_date=raw.expiry)).earnings_in_window)

    def test_unknown_earnings_costs_a_little_but_an_etf_has_none(self):
        raw = raw_spread()
        self.assertTrue(any(p.label == "Earnings date unknown" for p in score(raw, stock(earnings_date=None)).penalties))
        etf = stock(is_etf=True, earnings_date=None, eps_ttm=None)
        self.assertFalse(any("Earnings" in p.label for p in score(raw, etf).penalties))

    def test_unprofitable_is_a_small_penalty_never_a_rejection(self):
        raw = raw_spread()
        losing = score(raw, stock(eps_ttm=-1.0))
        self.assertTrue(any(p.label == "Not profitable" for p in losing.penalties))
        self.assertIsNone(sc.hard_filter_failure(raw, stock(eps_ttm=-1.0), DEFAULTS, TODAY))

    def test_stale_quotes_are_held_at_watch(self):
        raw = raw_spread()
        late = NOW + timedelta(hours=8)
        scored = score(raw, now=late)
        self.assertEqual(scored.quote[0], sc.QUOTE_STALE)
        self.assertIn(scored.rating, (sc.WATCH, sc.REJECTED))
        self.assertIn("quotes aren't current", sc.explanation(scored))

    def test_rating_thresholds(self):
        self.assertEqual(sc.rate(80.0, 45.0, []), sc.EXCELLENT)
        self.assertEqual(sc.rate(79.9, 45.0, []), sc.VERY_GOOD)
        self.assertEqual(sc.rate(70.0, 45.0, []), sc.VERY_GOOD)
        self.assertEqual(sc.rate(60.0, 45.0, []), sc.GOOD)
        self.assertEqual(sc.rate(45.0, 45.0, []), sc.WATCH)
        self.assertEqual(sc.rate(44.9, 45.0, []), sc.REJECTED)

    def test_rating_caps_take_the_lowest_and_never_raise(self):
        self.assertEqual(sc.rate(95.0, 45.0, [sc.GOOD, sc.WATCH]), sc.WATCH)
        self.assertEqual(sc.rate(50.0, 45.0, [sc.GOOD]), sc.WATCH)

    def test_market_conditions_favor_the_matching_direction(self):
        raw_put = raw_spread()
        raw_call = next(s for s in build(call_chain())[0] if s.short.strike == 112.0)
        up = sc.MarketConditions(True, True, 15.0, False, {"Energy": True})
        down = sc.MarketConditions(False, False, 35.0, True, {"Energy": False})
        self.assertGreater(sc.market_read(raw_put, up, "Energy")[0], sc.market_read(raw_put, down, "Energy")[0])
        self.assertGreater(sc.market_read(raw_call, down, "Energy")[0], sc.market_read(raw_call, up, "Energy")[0])

    def test_rich_iv_scores_best_and_extreme_iv_is_not_rewarded(self):
        raw = raw_spread()  # IV 35%
        calm = sc.Technicals(realized_vol_pct=25.0)
        self.assertEqual(sc.volatility_read(raw, calm, False)[0], 1.0)
        self.assertLess(sc.volatility_read(raw, calm, True)[0], 1.0, "earnings-inflated premium counts for less")
        self.assertLess(sc.volatility_read(raw, sc.Technicals(realized_vol_pct=8.0), False)[0], 0.3)
        self.assertLess(sc.volatility_read(raw, sc.Technicals(realized_vol_pct=60.0), False)[0], 0.3)


class TestTechnicals(unittest.TestCase):
    def tech(self, **fields):
        values = dict(ma20=100.0, ma50=95.0, ma200=90.0, chart=(("2026-11-01", SPOT),))
        values.update(fields)
        return sc.Technicals(**values)

    def test_short_below_support_is_best_for_a_bull_put(self):
        raw = raw_spread()  # short 90
        below = sc.technical_read(raw, self.tech(support=(sc.Zone(92.0, 95.0, 70.0, 4),)))
        inside = sc.technical_read(raw, self.tech(support=(sc.Zone(88.0, 91.0, 70.0, 4),)))
        above = sc.technical_read(raw, self.tech(support=(sc.Zone(75.0, 80.0, 70.0, 4),)))
        self.assertGreater(below[0], inside[0])
        self.assertGreater(inside[0], above[0])
        self.assertIn("below support", below[1])

    def test_short_above_resistance_is_best_for_a_bear_call(self):
        raw = next(s for s in build(call_chain())[0] if s.short.strike == 112.0)
        credit, text, _ = sc.technical_read(raw, self.tech(ma20=110.0, ma50=115.0, ma200=120.0, resistance=(sc.Zone(104.0, 108.0, 70.0, 3),)))
        self.assertEqual(credit, 1.0)
        self.assertIn("above resistance", text)

    def test_no_history_earns_nothing_and_invents_no_level(self):
        raw = raw_spread()
        credit, text, zone = sc.technical_read(raw, sc.analyze_technicals("ABC", []))
        self.assertEqual(credit, 0.0)
        self.assertIsNone(zone)
        self.assertIn("No daily price history", text)

    def test_no_zone_is_said_plainly(self):
        credit, text, zone = sc.technical_read(raw_spread(), self.tech())
        self.assertIsNone(zone)
        self.assertIn("no clear support", text)
        self.assertLess(credit, 0.5)

    def test_level_touched_once_reads_as_one_price(self):
        self.assertEqual(sc.zone_text(sc.Zone(78.93, 78.93, 40.0, 1)), "$78.93")
        self.assertEqual(sc.zone_text(sc.Zone(78.0, 80.0, 40.0, 2)), "$78.00-$80.00")

    def test_being_under_a_moving_average_never_rejects(self):
        raw = raw_spread()
        self.assertIsNone(sc.hard_filter_failure(raw, stock(), DEFAULTS, TODAY))
        scored = score(raw, technicals=self.tech(ma20=130.0, ma50=130.0, ma200=130.0))
        self.assertNotEqual(scored.rating, sc.REJECTED)

    def test_short_history_notes_the_gap(self):
        tech = sc.analyze_technicals("ABC", rising_bars(days=60))
        self.assertIsNone(tech.ma200)
        self.assertIsNotNone(tech.ma50)
        self.assertIn("60 sessions", tech.limitation)

    def test_market_conditions_from_closes(self):
        rising = [100 + i for i in range(60)]
        falling = list(reversed(rising))
        conditions = sc.market_conditions(rising, falling, [20, 21, 22, 23, 24, 25, 26], {"Energy": rising, "Utilities": [1, 2]})
        self.assertTrue(conditions.spy_up)
        self.assertFalse(conditions.qqq_up)
        self.assertEqual(conditions.vix, 26)
        self.assertTrue(conditions.vix_rising)
        self.assertEqual(dict(conditions.sector_up), {"Energy": True})

    def test_unknown_market_stays_unknown(self):
        conditions = sc.market_conditions([], [], [], {})
        self.assertIsNone(conditions.spy_up)
        self.assertIsNone(conditions.vix)
        self.assertEqual(conditions.payload()["vix"], "Unavailable")


class TestRanking(unittest.TestCase):
    def test_filters_can_be_tightened_without_new_data(self):
        spreads, _ = build(put_chain())
        settings, _ = sc.ScannerSettings.from_query({"min_pop": ["99"]})
        view = rank(spreads, settings=settings)
        self.assertEqual(view.top, ())
        self.assertTrue(any("Chance of profit under 99" in r for r, _ in view.reject_counts))

    def test_each_hard_filter_names_itself(self):
        raw = raw_spread()
        for query, expected in (
            ({"strategy": ["BEAR_CALL"]}, "Other strategy"),
            ({"asset_class": ["ETF"]}, "ETFs only"),
            ({"delta_min": ["45"], "delta_max": ["50"]}, "Short delta outside"),
            ({"max_width": ["0.5"]}, "Width over"),
            ({"min_credit": ["40"]}, "Credit under"),
            ({"min_ror": ["400"]}, "Return on risk under"),
            ({"min_ann_ror": ["4000"]}, "Annualized return under"),
            ({"min_oi": ["100000"]}, "Open interest under"),
            ({"dte_min": ["100"], "dte_max": ["120"]}, "DTE outside"),
        ):
            settings, warnings = sc.ScannerSettings.from_query(query)
            self.assertEqual(warnings, [], query)
            self.assertIn(expected, sc.hard_filter_failure(raw, stock(), settings, TODAY), query)

    def test_missing_open_interest_fails_the_hard_rule(self):
        raw = raw_spread(put_chain(open_interest=None))
        self.assertIn("Open interest", sc.hard_filter_failure(raw, stock(), DEFAULTS, TODAY))

    def test_one_width_is_kept_per_short_strike_and_none_scores_clearly_better(self):
        spreads, _ = build(put_chain())
        settings = sc.ScannerSettings(max_per_ticker=10, top_n=50)
        view = rank(spreads, settings=settings)
        keys = [(s.raw.expiry, s.raw.strategy, s.raw.short.strike) for s in view.top]
        self.assertEqual(len(keys), len(set(keys)))
        for chosen in view.top:
            rivals = [s for s in spreads if s.short.strike == chosen.raw.short.strike and s.expiry == chosen.raw.expiry and s.strategy == chosen.raw.strategy and sc.hard_filter_failure(s, stock(), settings, TODAY) is None]
            for rival in rivals:
                self.assertLessEqual(score(rival, settings=settings).score, chosen.score + sc.WIDTH_SCORE_TIE + 1e-9)

    def test_close_scores_go_to_the_width_risking_less(self):
        narrow = score(raw_spread(short=90.0, long=88.0))
        wide = score(raw_spread(short=90.0, long=80.0))
        wider_better = sc.ScoredSpread(**{**wide.__dict__, "score": 80.0, "rating": sc.GOOD})
        narrow_close = sc.ScoredSpread(**{**narrow.__dict__, "score": 79.0, "rating": sc.GOOD})
        narrow_far = sc.ScoredSpread(**{**narrow.__dict__, "score": 70.0, "rating": sc.GOOD})
        self.assertIs(sc.pick_width([wider_better, narrow_close]), narrow_close)
        self.assertIs(sc.pick_width([narrow_far, wider_better]), wider_better)

    def test_better_rating_outranks_higher_score(self):
        fine = score(raw_spread())
        capped = sc.ScoredSpread(**{**fine.__dict__, "score": 99.0, "rating": sc.WATCH})
        good = sc.ScoredSpread(**{**fine.__dict__, "score": 62.0, "rating": sc.GOOD})
        ordered = sorted([capped, good], key=lambda s: (-sc._rating_rank(s), sc._ranking_key(s)))
        self.assertEqual(ordered[0].rating, sc.GOOD)

    def test_ties_are_deterministic_whatever_the_input_order(self):
        spreads, _ = build(put_chain())
        first = [s.raw.id for s in rank(spreads).top]
        second = [s.raw.id for s in rank(list(reversed(spreads))).top]
        self.assertEqual(first, second)

    def _two_ticker_setup(self):
        spreads = build(put_chain(ticker="AAA"))[0] + build(put_chain(ticker="BBB"))[0] + build(put_chain(ticker="CCC"))[0]
        underlyings = {
            "AAA": stock("AAA", sector="Energy"),
            "BBB": stock("BBB", sector="Energy"),
            "CCC": stock("CCC", sector="Utilities"),
        }
        return spreads, underlyings

    def test_list_is_limited_per_ticker_and_per_sector(self):
        spreads, underlyings = self._two_ticker_setup()
        settings = sc.ScannerSettings(max_per_ticker=1, max_per_sector=1)
        view = rank(spreads, underlyings, settings=settings)
        self.assertEqual(sorted(s.raw.ticker for s in view.top), ["AAA", "CCC"])
        self.assertTrue(any("Energy already has 1" in reason for _, reason in view.also_considered))

    def test_top_n_is_respected_with_ten_by_default(self):
        spreads, underlyings = self._two_ticker_setup()
        view = rank(spreads, underlyings, settings=sc.ScannerSettings(top_n=1))
        self.assertEqual(len(view.top), 1)

    def test_list_warning_when_a_sector_repeats(self):
        spreads, underlyings = self._two_ticker_setup()
        view = rank(spreads, underlyings, settings=sc.ScannerSettings(max_per_sector=5))
        warnings = sc.apply_diversification_warnings(view.top, DEFAULTS)
        self.assertTrue(any("Energy" in text for texts in warnings.values() for text in texts))

    def test_rejected_view_is_never_empty_of_reasons(self):
        spreads, _ = build(put_chain())
        view = rank(spreads, settings=sc.ScannerSettings(min_score=100.0))
        self.assertEqual(view.top, ())
        self.assertTrue(view.reject_counts)
        self.assertTrue(view.rejected_examples)


class TestPortfolio(unittest.TestCase):
    def exposure(self, *, account_value=100000.0, by_ticker=None, by_sector=None, total=0.0, closes=None):
        return sc.Exposure(account_value, "test", total, by_ticker or {}, by_sector or {}, closes or {})

    def test_same_ticker_warns_and_lowers_the_score(self):
        raw = raw_spread()
        clean = score(raw, exposure=self.exposure())
        held = score(raw, exposure=self.exposure(by_ticker={"ABC": 500.0}, total=500.0))
        self.assertLess(held.score, clean.score)
        self.assertTrue(any("already have" in w for w in held.warnings))
        self.assertNotEqual(held.rating, sc.REJECTED)

    def test_each_limit_warns_without_excluding(self):
        raw = raw_spread()
        settings = sc.ScannerSettings(max_trade_pct=0.01, max_total_pct=0.01, max_ticker_pct=0.01, max_sector_pct=0.01)
        scored = score(raw, exposure=self.exposure(), settings=settings)
        labels = [p.label for p in scored.penalties]
        self.assertIn("Over the per-trade limit", labels)
        self.assertIn("Over the ticker limit", labels)
        self.assertIn("Over the Information Technology limit", labels)
        self.assertIn("Over the total limit", labels)
        view = rank([raw], exposure=self.exposure(), settings=sc.ScannerSettings(max_trade_pct=0.01, min_score=0.0))
        self.assertEqual(len(view.top), 1, "limits warn, they never hide a trade")

    def test_no_account_value_means_no_limit_checks(self):
        scored = score(raw_spread(), exposure=self.exposure(account_value=None), settings=sc.ScannerSettings(max_trade_pct=0.01))
        self.assertFalse([p for p in scored.penalties if "limit" in p.label])

    def test_correlated_open_position_warns(self):
        days = [TODAY - timedelta(days=i) for i in range(40, 0, -1)]
        moves = [100 + (i % 5) * 2 + i * 0.1 for i in range(40)]
        same = {d: v for d, v in zip(days, moves)}
        tech = sc.Technicals(closes_by_day=same, chart=(("2026-11-01", SPOT),))
        exposure = self.exposure(by_ticker={"XYZ": 300.0}, total=300.0, closes={"XYZ": dict(same)})
        scored = score(raw_spread(), technicals=tech, exposure=exposure)
        self.assertTrue(any(p.label == "Moves with an open position" for p in scored.penalties))

    def test_correlation_needs_enough_shared_sessions(self):
        days = [TODAY - timedelta(days=i) for i in range(10)]
        series = {d: 100.0 + i for i, d in enumerate(days)}
        self.assertIsNone(sc.return_correlation(series, series))

    def test_exposure_is_measured_in_max_loss_of_open_contracts(self):
        positions = positions_from(opened_put_spread("2026-10-01", "QQQ", "261130", 660, 655, 3, 3.0, 2.1))
        exposure = sc.build_exposure(positions, sector_of={"QQQ": "Index"}, account_value=None, account_source="", closes_by_ticker={})
        position = positions[0]
        expected = (5 - position.credit) * 100 * 3
        self.assertAlmostEqual(exposure.total, expected)
        self.assertAlmostEqual(exposure.by_ticker["QQQ"], expected)
        self.assertAlmostEqual(exposure.by_sector["Index"], expected)


class TestExplanations(unittest.TestCase):
    def test_explanation_uses_only_this_trades_facts(self):
        text = sc.explanation(score(raw_spread()))
        self.assertIn("bull put spread", text)
        reporting = score(raw_spread(), stock(earnings_date=EXPIRY - timedelta(days=3)))
        self.assertIn("earnings fall inside the trade", sc.explanation(reporting))

    def test_technical_sentence_names_distance_and_direction(self):
        credit, text, _ = sc.technical_read(raw_spread(), sc.Technicals(ma50=95.0, chart=(("2026-11-01", SPOT),), support=(sc.Zone(92.0, 95.0, 70.0, 4),)))
        self.assertTrue(text.startswith("Bull put spread: short strike is 10.0% below the price"))
        self.assertIn("above its 50-day average", text)
        self.assertGreater(credit, 0)

    def test_management_matches_the_open_spreads_rules(self):
        raw = raw_spread()
        settings = cs.SpreadSettings()
        plan = sc.management_targets(raw, settings)
        self.assertEqual(plan["target_debit"], round(cs.target_closing_debit(raw.credit, 50.0), 2))
        self.assertEqual(plan["loss_trigger"], round(cs.loss_trigger_debit(raw.credit, 2.0), 2))
        self.assertAlmostEqual(plan["target_profit"], (raw.credit - cs.target_closing_debit(raw.credit, 50.0)) * 100, places=2)
        self.assertAlmostEqual(plan["loss_at_trigger"], raw.credit * 100, places=2)
        self.assertEqual(plan["review_date"], (raw.expiry - timedelta(days=21)).isoformat())

    def test_user_example_one_dollar_credit(self):
        short = cs.LegQuote(90.0, 1.70, 1.80, 0.40, 500, 100)
        long = cs.LegQuote(85.0, 0.60, 0.70, 0.42, 500, 100)
        raw = sc.RawSpread("ABC", cs.BULL_PUT, EXPIRY, 35, SPOT, FRESH, short, long, 1.00, 1.15, 0.2, 0.75, None, FEE)
        plan = sc.management_targets(raw, cs.SpreadSettings())
        self.assertEqual(plan["target_debit"], 0.50)
        self.assertEqual(plan["target_profit"], 50.0)

    def test_payload_has_every_field_and_is_json_ready(self):
        raw = raw_spread()
        tech = sc.analyze_technicals("ABC", rising_bars())
        scored = score(raw, technicals=tech)
        row = sc.opportunity_payload(scored, 1, technicals=tech, settings=DEFAULTS, management=cs.SpreadSettings(), account_value=100000.0, risk_free_rate=RATE)
        json.dumps(row)
        for name in (
            "rank", "ticker", "strategy_label", "spot", "short_strike", "long_strike", "expiry", "dte", "credit", "credit_mid",
            "max_profit", "max_loss", "pop_pct", "ror_pct", "annualized_ror_pct", "short_delta", "score", "rating", "explanation",
            "breakeven", "expected_value", "touch_pct", "pop_delta_pct", "components", "penalties", "reasons", "risks", "management", "chart",
        ):
            self.assertIn(name, row)
        self.assertIsNone(row["pop_broker"], "Yahoo has no broker probability, so none is shown")
        self.assertEqual(row["suggested_contracts"], cs.suggested_contracts(100000.0, DEFAULTS.max_trade_pct, raw.risk_per_contract))
        self.assertTrue(any("Max loss" in risk for risk in row["risks"]))

    def test_displayed_text_uses_no_banned_connectors(self):
        scored = score(raw_spread(), stock(earnings_date=EXPIRY))
        text = " ".join([sc.explanation(scored), scored.technical_text, *sc.risk_reasons(scored), *sc.qualification_reasons(scored)])
        for banned in (" -- ", " — ", "−"):
            self.assertNotIn(banned, text)


class FakeProvider:
    name = "Fake data"
    realtime = False

    def __init__(self):
        self.chain_calls = []
        self.failing_chains = set()
        self.quote_error = False
        self.reprice = {}
        self.listed = {t: [EXPIRY] for t in ("SPY", "ABC", "BIG")}

    def universe(self, today):
        members = [uni.UniverseMember(t, t.title(), "Energy", ("test",)) for t in ("ABC", "BIG", "CHEAP", "NOOPT", "NOQUOTE")]
        return members, [uni.SourceStatus("test", "Test list", 5, True)], []

    def underlyings(self, members, today):
        out = {}
        for ticker, member in members.items():
            if ticker == "NOQUOTE":
                continue
            out[ticker] = sc.Underlying(
                ticker, ticker in ("SPY",) or ticker.startswith("X") or ticker in sc.DEFAULT_ETFS, member.name, member.sector,
                price=5.0 if ticker == "CHEAP" else SPOT, market_cap_b=50.0, avg_volume_m=5.0, earnings_date=today + timedelta(days=80), eps_ttm=2.0,
            )
        return out, []

    def expirations(self, tickers, today):
        return {t: list(self.listed.get(t, [])) for t in tickers}, []

    def chain(self, ticker, expiry):
        self.chain_calls.append((ticker, expiry))
        if (ticker, expiry) in self.failing_chains:
            raise MarketDataError("simulated failure")
        chain = put_chain(ticker=ticker)
        return cs.ChainQuotes(ticker, expiry, chain.spot, cs.QuoteTiming(NOW, NOW), chain.puts, ())

    def bars(self, ticker):
        return rising_bars(), []

    def local_bars(self, ticker):
        return rising_bars()


def run_fake(provider=None, settings=None):
    settings = settings or sc.ScannerSettings(etfs=("SPY",))
    provider = provider or FakeProvider()
    snapshot = svc.run_scan(
        provider, settings, scan_id=1, today=TODAY, now=lambda: NOW, risk_free_rate=RATE, fee_per_contract=FEE
    )
    return snapshot, provider


class TestStagedScan(unittest.TestCase):
    def test_stages_screen_before_any_chain_is_read(self):
        snapshot, provider = run_fake()
        tickers = {t for t, _ in provider.chain_calls}
        self.assertEqual(tickers, {"SPY", "ABC", "BIG"}, "CHEAP fails the screen; NOQUOTE has no quote; NOOPT has no options")
        self.assertEqual(snapshot.screen_rejects["No quote from the data source"], 1)
        self.assertEqual(snapshot.screen_rejects["Price under $20"], 1)
        self.assertEqual(snapshot.screen_rejects["No listed options"], 1)
        self.assertEqual(snapshot.universe_size, 6)
        self.assertEqual(snapshot.underlyings_scanned, 4)
        self.assertTrue(snapshot.spreads)

    def test_max_stocks_limits_chains_and_is_reported(self):
        settings = sc.ScannerSettings(etfs=("SPY",), max_stocks=1)
        snapshot, provider = run_fake(settings=settings)
        self.assertEqual(len({t for t, _ in provider.chain_calls} - {"SPY"}), 1)
        self.assertTrue(any("most actively traded" in reason for reason in snapshot.screen_rejects))

    def test_failed_chain_is_reported_not_replaced(self):
        provider = FakeProvider()
        provider.failing_chains.add(("ABC", EXPIRY))
        snapshot, _ = run_fake(provider)
        self.assertEqual(snapshot.chains_failed, 1)
        self.assertFalse([s for s in snapshot.spreads if s.ticker == "ABC"])
        self.assertTrue(any("couldn't be loaded" in w for w in snapshot.warnings))

    def test_verification_rereads_only_the_top_candidates(self):
        snapshot, provider = run_fake()
        counts = {}
        for pair in provider.chain_calls:
            counts[pair] = counts.get(pair, 0) + 1
        self.assertEqual(sorted(counts.values())[-1], 2, "the top candidates' chains are read a second time")
        self.assertIsNotNone(snapshot.verified_at)
        self.assertIn("Rechecked", snapshot.verify_note)

    def test_a_top_chain_failing_verification_loses_its_spreads(self):
        snapshot, provider = run_fake()
        provider.failing_chains.add(("ABC", EXPIRY))
        verified = svc.verify_top(snapshot, provider, sc.ScannerSettings(etfs=("SPY",)), now=lambda: NOW, risk_free_rate=RATE, fee_per_contract=FEE)
        self.assertFalse([s for s in verified.spreads if s.ticker == "ABC"], "old prices don't survive a failed recheck")
        self.assertIn("failed", verified.verify_note)

    def test_nothing_in_the_universe_still_finishes_cleanly(self):
        provider = FakeProvider()
        provider.universe = lambda today: ([], [], ["Everything is down."])
        provider.underlyings = lambda members, today: ({}, ["no quotes"])
        snapshot, _ = run_fake(provider)
        self.assertEqual(snapshot.spreads, ())
        self.assertIn("Everything is down.", snapshot.warnings)

    def test_rate_limiter_spaces_requests(self):
        clock = [0.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        limiter = svc.RateLimiter(0.5, clock=lambda: clock[0], sleep=sleep)
        limiter.wait()
        limiter.wait()
        limiter.wait()
        self.assertEqual(sleeps, [0.5, 0.5])

    def test_retry_recovers_then_gives_up_and_skips_404(self):
        waits = []
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 2:
                raise MarketDataError("HTTP Error 429")
            return "ok"

        self.assertEqual(svc.with_retry(flaky, sleep=waits.append), "ok")
        self.assertEqual(waits, [1.0])

        def missing():
            calls.append(1)
            raise MarketDataError("HTTP Error 404: Not Found")

        calls.clear()
        with self.assertRaises(MarketDataError):
            svc.with_retry(missing, sleep=waits.append)
        self.assertEqual(len(calls), 1)


def make_service(provider=None):
    return svc.ScanService(provider or FakeProvider(), risk_free_rate=RATE, fee_per_contract=FEE)


def view_of(service, settings=None, positions=(), equity=(100000.0, "test"), **kwargs):
    return service.view(
        settings or sc.ScannerSettings(etfs=("SPY",)), management=cs.SpreadSettings(), positions=list(positions), equity=equity, now=NOW, **kwargs
    )


class TestService(unittest.TestCase):
    def setUp(self):
        self.settings = sc.ScannerSettings(etfs=("SPY",))

    def fresh_snapshot(self, service):
        # The real clock would mark fake quotes stale; the snapshot itself is what's under test.
        service.start("full", self.settings, background=False)
        with service._lock:
            self.assertIsNotNone(service._state.snapshot)

    def test_view_before_any_scan_says_so(self):
        payload = view_of(make_service())
        self.assertFalse(payload["has_results"])
        self.assertEqual(payload["api_version"], sc.SCANNER_API_VERSION)
        json.dumps(payload)

    def test_full_view_is_json_ready_with_top_list_and_reasons(self):
        service = make_service()
        self.fresh_snapshot(service)
        payload = view_of(service, self.settings)
        json.dumps(payload)
        self.assertTrue(payload["has_results"])
        self.assertFalse(payload["rescan_needed"])
        self.assertTrue(payload["opportunities"])
        self.assertTrue(payload["rejected"]["stock_reasons"])
        self.assertEqual(payload["opportunities"][0]["rank"], 1)

    def test_scan_download_setting_change_flags_a_rescan(self):
        service = make_service()
        self.fresh_snapshot(service)
        changed = sc.ScannerSettings(etfs=("SPY",), max_stocks=3)
        self.assertTrue(view_of(service, changed)["rescan_needed"])
        filtered = sc.ScannerSettings(etfs=("SPY",), min_pop_pct=65.0)
        self.assertFalse(view_of(service, filtered)["rescan_needed"])

    def test_filter_change_makes_no_network_calls(self):
        provider = FakeProvider()
        service = make_service(provider)
        self.fresh_snapshot(service)
        before = len(provider.chain_calls)
        view_of(service, sc.ScannerSettings(etfs=("SPY",), min_pop_pct=90.0))
        self.assertEqual(len(provider.chain_calls), before)

    def test_refresh_needs_a_scan_and_rereads_only_top_chains(self):
        provider = FakeProvider()
        service = make_service(provider)
        started, message = service.start("refresh", self.settings, background=False)
        self.assertFalse(started)
        self.assertIn("Run a scan first", message)
        self.fresh_snapshot(service)
        before = len(provider.chain_calls)
        started, _ = service.start("refresh", self.settings, background=False)
        self.assertTrue(started)
        self.assertLessEqual(len(provider.chain_calls) - before, self.settings.top_n * svc.VERIFY_CANDIDATE_FACTOR)

    def test_second_scan_is_refused_too_soon(self):
        service = make_service()
        self.fresh_snapshot(service)
        started, message = service.start("full", self.settings, background=False)
        self.assertFalse(started)
        self.assertIn("seconds", message)

    def test_scan_failure_is_contained_and_keeps_the_last_result(self):
        provider = FakeProvider()
        clock = [0.0]
        service = svc.ScanService(provider, risk_free_rate=RATE, fee_per_contract=FEE, clock=lambda: clock[0])
        self.fresh_snapshot(service)

        def boom(today):
            raise ValueError("universe exploded")

        provider.universe = boom
        clock[0] += svc.FULL_SCAN_COOLDOWN_SECONDS + 1
        started, _ = service.start("full", self.settings, background=False)
        self.assertTrue(started, "the failure surfaces in status, not as an exception to the caller")
        payload = view_of(service, self.settings)
        self.assertIn("universe exploded", payload["scan"]["error"])
        self.assertIsNone(payload["scan"]["running"])
        self.assertTrue(payload["has_results"])

    def test_old_quotes_are_not_recorded_as_recommendations(self):
        service = make_service()
        self.fresh_snapshot(service)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "j.json")
            late = service.view(self.settings, management=cs.SpreadSettings(), positions=[], equity=(None, ""), journal_path=path, now=NOW + timedelta(hours=8))
            self.assertTrue(late["opportunities"])
            self.assertEqual(journal.load(path), [], "last session's prices aren't a recommendation")
            view_of(service, self.settings, journal_path=path)
            first = journal.load(path)
            self.assertTrue(first)
            self.assertEqual(first[0]["recommended_on"], date.today().isoformat())

    def test_account_exposure_changes_ranking_inputs_not_the_scan(self):
        service = make_service()
        self.fresh_snapshot(service)
        positions = positions_from(opened_put_spread("2026-10-01", "ABC", "261207", 95, 90, 1, 3.0, 2.0))
        clean = view_of(service, self.settings)
        held = view_of(service, self.settings, positions=positions)
        self.assertEqual(clean["scan"]["last_scan_at"], held["scan"]["last_scan_at"])
        self.assertGreater(held["exposure"]["total"]["used"], 0)
        self.assertEqual(clean["exposure"]["total"]["used"], 0)


class TestJournal(unittest.TestCase):
    ROW = {
        "id": "QQQ|2026-11-30|BULL_PUT|660|655", "ticker": "QQQ", "strategy": cs.BULL_PUT, "short_strike": 660.0, "long_strike": 655.0,
        "expiry": "2026-11-30", "dte": 30, "score": 75.0, "rating": "Very Good", "pop_pct": 80.0, "expected_value": 2.0, "credit": 0.95, "max_loss": 405.0,
    }

    def spread(self, *closing):
        return positions_from(opened_put_spread("2026-10-01", "QQQ", "261130", 660, 655, 1, 3.0, 2.0) + list(closing))

    def closes_at(self, short_price, long_price):
        return closed_legs("2026-10-15", "QQQ", "261130", "P", 660, 655, short_price, long_price)

    def test_first_recommendation_is_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "j.json")
            journal.record(path, [self.ROW], date(2026, 9, 30))
            journal.record(path, [{**self.ROW, "score": 10.0}], date(2026, 10, 2))
            records = journal.load(path)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["score"], 75.0)
            self.assertEqual(records[0]["recommended_on"], "2026-09-30")

    def test_unreadable_file_is_an_empty_history(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "j.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("not json")
            self.assertEqual(journal.load(path), [])

    def test_trade_after_recommendation_is_matched_with_its_outcome(self):
        record = {**self.ROW, "recommended_on": "2026-09-30"}
        positions = self.spread(*self.closes_at(1.0, 0.5))
        summary = journal.track_record([record], positions, cs.SpreadSettings())
        self.assertEqual(summary["traded"], 1)
        self.assertEqual(summary["closed"], 1)
        trade = summary["trades"][0]
        self.assertAlmostEqual(trade["credit_received"], 1.0)
        self.assertAlmostEqual(trade["closing_price"], 0.5)
        self.assertEqual(trade["days_held"], 14)
        self.assertTrue(trade["reached_target"])
        self.assertFalse(trade["hit_risk_threshold"])
        self.assertEqual(summary["actual_win_rate_pct"], 100.0)
        self.assertEqual(trade["score_at_entry"], 75.0)

    def test_losing_close_hits_the_risk_threshold(self):
        record = {**self.ROW, "recommended_on": "2026-09-30"}
        summary = journal.track_record([record], self.spread(*self.closes_at(5.0, 2.5)), cs.SpreadSettings())
        trade = summary["trades"][0]
        self.assertTrue(trade["hit_risk_threshold"])
        self.assertFalse(trade["reached_target"])
        self.assertEqual(summary["actual_win_rate_pct"], 0.0)

    def test_trade_opened_before_the_recommendation_does_not_count(self):
        record = {**self.ROW, "recommended_on": "2026-10-05"}
        self.assertEqual(journal.track_record([record], self.spread(), cs.SpreadSettings())["traded"], 0)

    def test_never_claims_the_score_predicts_with_little_history(self):
        summary = journal.track_record([], [], cs.SpreadSettings())
        self.assertFalse(summary["enough_history"])
        self.assertIn("don't read the score as predictive", summary["note"])
        self.assertIsNone(summary["actual_win_rate_pct"])


class TestDashboardIsolation(unittest.TestCase):
    """What the scanner must not disturb."""

    def test_tab_chain_helper_still_reads_snapshots(self):
        from wheel.api import _chain_quotes
        from wheel.marketdata import OptionChainSnapshot, OptionContractQuote

        contract = OptionContractQuote("X", 90.0, "P", 1.0, 1.1, None, None, 0.3, False, 10, 4)
        chain = _chain_quotes(OptionChainSnapshot("ABC", EXPIRY, SPOT, NOW, (contract,)), NOW)
        self.assertEqual(chain.puts[0].open_interest, 10)
        self.assertEqual(chain.puts[0].volume, 4)
        self.assertEqual(chain.calls, ())

    def test_scanning_never_uses_the_wheel_chain_cache(self):
        from wheel import api

        before = set(api._OPTION_CHAIN_DEMAND), dict(api._OPTION_CHAIN_CACHE)
        run_fake()
        self.assertEqual((set(api._OPTION_CHAIN_DEMAND), dict(api._OPTION_CHAIN_CACHE)), before)


if __name__ == "__main__":
    unittest.main()
