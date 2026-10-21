"""CSP TO ENTER: wheel/cspentry.py (support detection, strike selection,
tooltip), its single per-ticker payload in wheel/api.py, the Combined-view
merge in wheel/accounts.py, and the frontend's display-only use of it."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel import cspentry as ce  # noqa: E402
from wheel import marketdata  # noqa: E402
from wheel.tooltipcard import card_text  # noqa: E402
from wheel.accounts import _combine_csp_to_enter, _combine_wheel_targets  # noqa: E402
from wheel.api import Dashboard, _csp_to_enter_payload, wheel_target_gap_pct  # noqa: E402
from wheel.marketdata import OptionChainSnapshot, OptionContractQuote, PricePoint  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODAY = date(2026, 10, 17)
EXPIRY_15D = TODAY + timedelta(days=15)
# Half the daily high-low range of every synthetic bar (so ATR is ~2.4%).
HALF_RANGE = 0.012


def bars_through(anchors, *, start=date(2025, 9, 1), volume=1_000_000.0, volume_at=None):
    """Daily bars whose close moves linearly between ``(session, close)``
    anchors; high/low are close +-1.2%. ``volume_at``: {session: volume}."""
    closes: list[float] = []
    for (i0, c0), (i1, c1) in zip(anchors, anchors[1:]):
        closes += [c0 + (c1 - c0) * (i - i0) / (i1 - i0) for i in range(i0, i1)]
    closes.append(anchors[-1][1])
    bars, day = [], start
    for index, close in enumerate(closes):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        bars.append(
            PricePoint(
                day,
                close,
                high=close * (1 + HALF_RANGE),
                low=close * (1 - HALF_RANGE),
                volume=(volume_at or {}).get(index, volume),
            )
        )
        day += timedelta(days=1)
    return bars


def low_at(level: float) -> float:
    """The close whose bar low lands exactly on ``level``."""
    return level / (1 - HALF_RANGE)


# A $150 stock that sold off to $143, $142, $141 and $140 over the past year
# and rallied back toward $150 every time.
SUPPORT_140_143 = [
    (0, 150), (30, 148), (50, low_at(143)), (70, 152), (110, low_at(142)), (130, 151),
    (170, low_at(141)), (190, 150), (225, low_at(140)), (245, 149), (262, 150),
]


def zone(low, high, score, *, width_pct=3.0):
    """A hand-built support analysis with one zone, for strike-selection tests."""
    z = ce.SupportZone(
        low=low,
        high=high,
        touch_lows=(low, high),
        touch_days=(date(2026, 6, 1), date(2026, 9, 1)),
        sessions_since_last_touch=30,
        avg_bounce_pct=8.0,
        breaks=0,
        moving_averages_near=("50-day MA",),
        round_level=None,
        volume_ratio=None,
        components={},
        score=score,
    )
    return ce.SupportAnalysis(252, 2.0, width_pct, {}, (z,), 1.0, ())


def recommend(analysis, puts, *, price=150.0, expiry=EXPIRY_15D):
    return ce.recommend_put(current_price=price, support=analysis, expiry=expiry, today=TODAY, puts=puts)


def by_strike(entry):
    return {c.strike: c for c in entry.candidates}


class TestOldFormulaRemoved(unittest.TestCase):
    """Test 1: no 93%-of-price CSP TO ENTER survives anywhere."""

    def test_no_fixed_percentage_csp_formula_in_source(self):
        patterns = re.compile(r"CSP_ENTRY_OTM_PCT|preferred_csp_entry|cspEntryTarget|cspTargetPct|csp-target-pct|\* ?0\.93\b")
        offenders = []
        for folder in ("wheel", os.path.join("wheel", "static")):
            for name in os.listdir(os.path.join(ROOT, folder)):
                if not name.endswith((".py", ".js", ".html", ".css")):
                    continue
                with open(os.path.join(ROOT, folder, name), encoding="utf-8") as handle:
                    for number, line in enumerate(handle, 1):
                        if patterns.search(line):
                            offenders.append(f"{folder}/{name}:{number}: {line.strip()}")
        self.assertEqual(offenders, [])

    def test_no_support_means_no_strike_not_a_percentage_fallback(self):
        flat = [PricePoint(TODAY - timedelta(days=i), 150.0, 150.5, 149.5, 1e6) for i in range(300, 0, -1)]
        entry = ce.calculate_csp_to_enter(
            current_price=150.0,
            bars=flat,
            expiry=EXPIRY_15D,
            today=TODAY,
            puts=(ce.PutQuote(139.5, 2.0, 2.1, 0.5),),
        )
        self.assertEqual(entry.status, ce.STATUS_NO_QUALIFYING)
        self.assertIsNone(entry.strike)
        self.assertEqual(ce.STATUS_LABELS[entry.status], "NO QUALIFYING CSP")


class TestStrikeSelection(unittest.TestCase):
    def test_strong_support_and_target_delta_picks_the_support_strike(self):
        """Test 2: $150 stock, $140-$143 support (86/100), $140 put ~0.25 delta, ~35% annualized."""
        entry = recommend(
            zone(140, 143, 86),
            (ce.PutQuote(145, 3.10, 3.30, 0.55), ce.PutQuote(140, 1.95, 2.05, 0.55), ce.PutQuote(135, 1.05, 1.15, 0.55)),
        )
        self.assertEqual(entry.status, ce.STATUS_RECOMMENDED)
        self.assertEqual(entry.strike, 140)
        pick = entry.pick
        self.assertAlmostEqual(pick.delta, 0.25, delta=0.01)
        self.assertEqual(pick.zone_position, ce.INSIDE)
        # Annualized return = (mid - fee/100) / strike x 365 / DTE.
        self.assertAlmostEqual(pick.net_premium, 2.00 - 0.0065, places=6)
        self.assertAlmostEqual(pick.annualized_return_pct, 100 * 1.9935 / 140 * 365 / 15, places=6)
        self.assertAlmostEqual(pick.effective_purchase_price, 140 - 1.9935, places=6)
        self.assertAlmostEqual(pick.downside_to_strike_pct, 100 * 10 / 150, places=6)
        # $135 is under the 30% minimum; $145 qualifies but ranks lower.
        candidates = by_strike(entry)
        self.assertIn("under the 30% minimum", candidates[135].rejected_reason)
        self.assertTrue(candidates[145].qualifies)
        self.assertLess(candidates[145].score, pick.score)

    def test_target_delta_strike_far_above_support_is_rejected(self):
        """Test 3: the ~0.25-delta put sits 9% above a $128-$131 zone; it doesn't win on delta."""
        entry = recommend(
            zone(128, 131, 80),
            (ce.PutQuote(145, 4.00, 4.20, 0.60), ce.PutQuote(130, 1.65, 1.75, 0.90)),
        )
        candidates = by_strike(entry)
        self.assertFalse(candidates[145].qualifies)
        self.assertIn("support zone", candidates[145].rejected_reason)
        self.assertEqual(entry.strike, 130)

    def test_support_strike_below_minimum_return_falls_to_next_qualifier(self):
        """Test 4: the in-zone $140 put pays ~20% annualized; it's rejected and $145 is evaluated."""
        entry = recommend(
            zone(140, 143, 86),
            (ce.PutQuote(145, 2.70, 2.80, 0.45), ce.PutQuote(140, 1.12, 1.22, 0.55)),
        )
        candidates = by_strike(entry)
        self.assertIn("under the 30% minimum", candidates[140].rejected_reason)
        self.assertEqual(entry.strike, 145)
        self.assertEqual(entry.pick.zone_position, ce.ABOVE)
        # The reason must say it is above the zone, not imply it is at support.
        self.assertIn("above the $140.00 to $143.00 support zone", entry.reason)

    def test_higher_delta_strike_just_above_support_can_beat_a_low_delta_in_zone_strike(self):
        """Spec example 23: $140 (in zone) is ~0.12 delta, $145 (just above) ~0.27."""
        entry = recommend(
            zone(140, 143, 86),
            (ce.PutQuote(145, 2.55, 2.65, 0.30), ce.PutQuote(140, 1.75, 1.85, 0.30)),
        )
        candidates = by_strike(entry)
        self.assertTrue(candidates[140].qualifies and candidates[145].qualifies)
        self.assertAlmostEqual(candidates[140].delta, 0.12, delta=0.01)
        self.assertAlmostEqual(candidates[145].delta, 0.27, delta=0.015)
        self.assertEqual(entry.strike, 145)
        self.assertIn("above", entry.reason)

    def test_no_qualifying_candidate_returns_no_qualifying_csp(self):
        """Test 5: nothing clears the hard filters -> no strike, a reason, never a fallback."""
        entry = recommend(
            zone(140, 143, 86),
            (
                ce.PutQuote(145, 0.80, 0.90, 0.30),  # under 30%
                ce.PutQuote(140, 0.0, 0.40, 0.30),  # no bid
                ce.PutQuote(135, 0.50, 2.50, 0.30),  # spread wider than half the mid
                ce.PutQuote(150, 5.0, 5.2, 0.30),  # at the price: never a candidate
            ),
        )
        self.assertEqual(entry.status, ce.STATUS_NO_QUALIFYING)
        self.assertIsNone(entry.strike)
        self.assertNotIn(150, by_strike(entry))
        self.assertIn("passes every check", entry.reason)
        payload = _csp_to_enter_payload(entry)
        self.assertIsNone(payload["recommended_strike"])
        self.assertEqual(payload["label"], "NO QUALIFYING CSP")
        self.assertEqual(payload["card"]["status_label"], "DOES NOT QUALIFY")
        self.assertEqual(payload["card"]["summary"], entry.reason)

    def test_missing_chain_or_price_is_no_qualifying_with_reason(self):
        no_chain = ce.recommend_put(
            current_price=150.0, support=zone(140, 143, 86), expiry=EXPIRY_15D, today=TODAY, puts=None,
            chain_unavailable_reason="Option prices couldn't be loaded.",
        )
        self.assertEqual((no_chain.status, no_chain.reason), (ce.STATUS_NO_QUALIFYING, "Option prices couldn't be loaded."))
        no_price = ce.calculate_csp_to_enter(current_price=None, bars=[], expiry=None, today=TODAY, puts=None)
        self.assertEqual(no_price.status, ce.STATUS_NO_QUALIFYING)
        self.assertIsNone(no_price.strike)

    def test_weak_or_remote_support_is_not_selected(self):
        weak = recommend(zone(140, 143, 30), (ce.PutQuote(140, 1.95, 2.05, 0.55),))
        self.assertEqual(weak.status, ce.STATUS_NO_QUALIFYING)
        self.assertIn("scores at least 40", weak.reason)
        remote = recommend(zone(100, 104, 95), (ce.PutQuote(100, 1.95, 2.05, 0.9),))
        self.assertEqual(remote.status, ce.STATUS_NO_QUALIFYING)


class TestSupportDetection(unittest.TestCase):
    def test_nearby_swing_lows_cluster_into_one_scored_zone(self):
        analysis = ce.analyze_support(bars_through(SUPPORT_140_143))
        top = analysis.zones[0]
        self.assertEqual(top.touches, 4)
        self.assertAlmostEqual(top.low, 140, places=6)
        self.assertAlmostEqual(top.high, 143, places=6)
        self.assertTrue(0 <= top.score <= 100)
        self.assertEqual(top.breaks, 0)
        self.assertEqual(set(top.components), set(ce.SUPPORT_WEIGHTS))

    def test_end_to_end_picks_the_strike_inside_detected_support(self):
        entry = ce.calculate_csp_to_enter(
            current_price=150.0,
            bars=bars_through(SUPPORT_140_143),
            expiry=EXPIRY_15D,
            today=TODAY,
            puts=(ce.PutQuote(145, 3.10, 3.30, 0.55), ce.PutQuote(140, 1.95, 2.05, 0.55), ce.PutQuote(135, 1.05, 1.15, 0.55)),
        )
        self.assertEqual(entry.strike, 140)
        self.assertEqual(entry.pick.zone_position, ce.INSIDE)

    def test_more_touches_and_recent_touches_score_higher(self):
        one_touch = ce.analyze_support(bars_through([(0, 150), (225, low_at(140)), (245, 149), (262, 150)])).zones[0]
        four = ce.analyze_support(bars_through(SUPPORT_140_143)).zones[0]
        self.assertGreater(four.components["touches"], one_touch.components["touches"])
        old = ce.analyze_support(bars_through([(0, 150), (30, low_at(140)), (50, 150), (262, 152)])).zones[0]
        recent = ce.analyze_support(bars_through([(0, 150), (230, low_at(140)), (250, 150), (262, 152)])).zones[0]
        self.assertGreater(recent.components["recency"], old.components["recency"])
        self.assertGreater(old.components["recency"], 0.0)  # old support fades, never vanishes

    def test_moving_average_confluence_only_counts_when_near_the_zone(self):
        analysis = ce.analyze_support(bars_through(SUPPORT_140_143))
        top = analysis.zones[0]
        for name in top.moving_averages_near:
            self.assertLessEqual(analysis.moving_averages[name], top.high * (1 + analysis.zone_width_pct / 100))
        far = ce.analyze_support(bars_through([(0, 100), (60, 130), (80, low_at(125)), (100, 160), (262, 200)])).zones
        self.assertTrue(all(z.moving_averages_near == () for z in far if z.high < 130))

    def test_volume_missing_renormalizes_instead_of_failing(self):
        no_volume = [
            PricePoint(b.day, b.close, b.high, b.low, None) for b in bars_through(SUPPORT_140_143)
        ]
        analysis = ce.analyze_support(no_volume)
        self.assertNotIn("volume", analysis.zones[0].components)
        self.assertTrue(any("volume" in note for note in analysis.limitations))

    def test_round_number_alone_creates_no_zone(self):
        # Price drifts straight from 160 to 150 through $150 (a round number)
        # with no swing low: there is no support zone to credit it to.
        analysis = ce.analyze_support(bars_through([(0, 160), (262, 150)]))
        self.assertEqual(analysis.zones, ())


class TestBrokenSupportAndDowntrends(unittest.TestCase):
    """Test 6."""

    def test_staircase_downtrend_leaves_no_support_below_the_price(self):
        stairs = bars_through([
            (0, 160), (40, low_at(150)), (60, 158), (90, low_at(140)), (110, 147),
            (140, low_at(130)), (160, 137), (200, low_at(118)), (215, 124), (262, 104),
        ])
        entry = ce.calculate_csp_to_enter(
            current_price=104.0, bars=stairs, expiry=EXPIRY_15D, today=TODAY, puts=(ce.PutQuote(100, 2.0, 2.1, 0.6),)
        )
        self.assertEqual(entry.status, ce.STATUS_NO_QUALIFYING)
        self.assertIsNone(entry.strike)
        self.assertIn("under every support zone", entry.reason)

    def test_support_that_was_closed_through_is_penalized(self):
        held = ce.analyze_support(
            bars_through([(0, 150), (60, low_at(140)), (80, 150), (150, low_at(140)), (170, 150), (262, 152)])
        ).zones[0]
        broken = ce.analyze_support(
            bars_through([
                (0, 150), (60, low_at(140)), (80, 150), (150, low_at(140)), (170, 150),
                (190, 128), (205, 129), (230, 150), (262, 152),
            ])
        )
        zone_140 = next(z for z in broken.zones if abs(z.low - 140) < 0.5)
        self.assertGreaterEqual(zone_140.breaks, 1)
        self.assertLess(zone_140.score, held.score)


class TestInsufficientHistory(unittest.TestCase):
    """Test 7."""

    def test_short_history_degrades_with_a_stated_limitation(self):
        short = bars_through([(0, 150), (30, low_at(140)), (45, 150), (70, 151)])
        analysis = ce.analyze_support(short)
        self.assertLess(analysis.history_confidence, 1.0)
        self.assertGreaterEqual(analysis.history_confidence, ce.HISTORY_CONFIDENCE_FLOOR)
        self.assertTrue(any(note.startswith("Limited history") for note in analysis.limitations))
        self.assertEqual(len(analysis.zones), 1)
        entry = ce.calculate_csp_to_enter(current_price=150.0, bars=short, expiry=EXPIRY_15D, today=TODAY, puts=())
        self.assertIn("Limited history", card_text(ce.tooltip_card(entry)))

    def test_too_little_history_is_no_qualifying_not_an_error(self):
        tiny = bars_through([(0, 150), (20, 148)])
        entry = ce.calculate_csp_to_enter(
            current_price=150.0, bars=tiny, expiry=EXPIRY_15D, today=TODAY, puts=(ce.PutQuote(140, 2.0, 2.1, 0.5),)
        )
        self.assertEqual(entry.status, ce.STATUS_NO_QUALIFYING)
        self.assertIn("Not enough daily price history", entry.reason)

    def test_close_only_bars_are_skipped_not_read_as_lows(self):
        close_only = [PricePoint(b.day, b.close) for b in bars_through(SUPPORT_140_143)]
        analysis = ce.analyze_support(close_only)
        self.assertEqual(analysis.zones, ())
        self.assertTrue(any("no daily high/low" in note for note in analysis.limitations))


def _dashboard_with_market(prices, bars, puts_by_ticker):
    """A Dashboard whose market-data seams return canned data (no network)."""
    dashboard = Dashboard.__new__(Dashboard)
    dashboard._price_warnings = []
    dashboard._daily_bars = lambda tickers: {t: bars for t in tickers}
    dashboard._option_expirations = lambda tickers: {t: [TODAY + timedelta(days=35)] for t in tickers}

    def chains(pairs):
        out = {}
        for ticker, expiry in pairs:
            contracts = tuple(
                OptionContractQuote(f"{ticker}P{q.strike}", q.strike, "P", q.bid, q.ask, None, None, q.implied_volatility, False)
                for q in puts_by_ticker.get(ticker, ())
            )
            out[(ticker, expiry)] = (OptionChainSnapshot(ticker, expiry, prices[ticker], None, contracts), None)
        return out

    dashboard._option_chains = chains
    return dashboard


class TestOneCanonicalValuePerTicker(unittest.TestCase):
    """Test 8: every surface reads the same per-ticker entry."""

    def setUp(self):
        # At 35 DTE a ~0.25-delta put needs ~2.9% of strike to clear 30%.
        self.puts = (ce.PutQuote(140, 4.10, 4.30, 0.60), ce.PutQuote(135, 2.4, 2.6, 0.60))
        self.dashboard = _dashboard_with_market({"ABC": 150.0}, bars_through(SUPPORT_140_143), {"ABC": self.puts})
        self.entries = Dashboard._build_csp_to_enter(self.dashboard, {"ABC": 150.0, "NOPRICE": None})

    def test_one_entry_per_ticker_with_status_for_every_ticker(self):
        self.assertEqual(set(self.entries), {"ABC", "NOPRICE"})
        self.assertEqual(self.entries["ABC"]["recommended_strike"], 140.0)
        self.assertEqual(self.entries["NOPRICE"]["label"], "NO QUALIFYING CSP")

    def test_wheel_targets_and_combined_views_read_the_same_entry(self):
        wheel = {
            "cycle_id": "ABC-1", "underlying": "ABC", "is_wheel": True, "is_open": True,
            "wheel_phase": "csp", "current_price": 150.0,
        }
        (target,) = Dashboard._build_wheel_targets_banner(self.dashboard, [wheel], self.entries)
        expected_gap = round(100 * (140.0 - 150.0) / 150.0, 2)
        self.assertEqual(target["gap_pct"], expected_gap)

        # A second account computed a different value at another instant: the
        # combined view keeps one, and re-reads the gap against it.
        other = {"ABC": {**self.entries["ABC"], "recommended_strike": 135.0}}
        combined = _combine_csp_to_enter([self.entries, other])
        self.assertIs(combined["ABC"], self.entries["ABC"])
        rows = _combine_wheel_targets(
            {"a": {"wheel_targets": [target]}, "b": {"wheel_targets": [{**target, "gap_pct": -10.0}]}}, combined
        )
        self.assertEqual({r["gap_pct"] for r in rows}, {expected_gap})
        self.assertEqual(wheel_target_gap_pct(wheel, combined["ABC"], 150.0), expected_gap)

    def test_every_frontend_surface_uses_the_shared_lookup(self):
        with open(os.path.join(ROOT, "wheel", "static", "app.js"), encoding="utf-8") as handle:
            js = handle.read()
        # The payload map is read in exactly one place...
        self.assertEqual(len(re.findall(r"state\.data\.csp_to_enter\b", js)), 1)
        # ...and every CSP TO ENTER column / cell renders through it.
        self.assertEqual(js.count("cspToEnterCell(row.underlying)"), 3)  # targets, open positions, candidates
        self.assertIn("const cspEntry = cspToEnterFor(entry.underlying);", js)  # Trade Log card
        self.assertEqual(len(re.findall(r"label: 'CSP TO ENTER'", js)), 3)
        self.assertEqual(len(re.findall(r"key: 'csp_to_enter'", js)), 3)


class TestCandidateSizingUsesTheStrike(unittest.TestCase):
    def test_payload_collateral_is_strike_times_100(self):
        entry = recommend(zone(140, 143, 86), (ce.PutQuote(140, 1.95, 2.05, 0.55),))
        self.assertEqual(_csp_to_enter_payload(entry)["collateral_per_contract"], 14_000.0)
        none = recommend(zone(140, 143, 86), (ce.PutQuote(140, 0.5, 0.6, 0.55),))
        self.assertIsNone(_csp_to_enter_payload(none)["collateral_per_contract"])

    def test_candidates_table_sizes_from_the_canonical_collateral(self):
        with open(os.path.join(ROOT, "wheel", "static", "app.js"), encoding="utf-8") as handle:
            js = handle.read()
        start = js.index("function renderCspCandidates(")
        body = js[start : js.index("\nfunction ", start + 1)]
        self.assertIn("const collateral = entry ? entry.collateral_per_contract : null;", body)
        self.assertIn("contracts: collateral ? Math.floor(available / collateral) : null,", body)
        self.assertNotIn("ATM", body)
        self.assertNotIn("'Qty ≈ Available Cash ÷ (last price × 100)'", body)

    def test_gap_to_target_header_matches_the_backend_formula(self):
        with open(os.path.join(ROOT, "wheel", "static", "app.js"), encoding="utf-8") as handle:
            js = handle.read()
        self.assertIn("'Gap % = (Target - Last Price) ÷ Last Price',", js)
        self.assertNotIn("CC rows only: Gap %", js)
        wheel = {"wheel_phase": "cc", "profit_target": 110.0}
        self.assertEqual(wheel_target_gap_pct(wheel, None, 100.0), 10.0)  # (target - last) / last


class TestTooltipMatchesCanonicalResult(unittest.TestCase):
    """Test 9: the tooltip is generated from, and agrees with, the same result."""

    def test_card_shows_the_payload_numbers(self):
        entry = recommend(
            zone(140, 143, 86),
            (ce.PutQuote(145, 3.10, 3.30, 0.55), ce.PutQuote(140, 1.95, 2.05, 0.55)),
        )
        payload = _csp_to_enter_payload(entry, "ABC")
        card = payload["card"]
        self.assertEqual((card["strategy"], card["ticker"], card["status_label"]), ("CSP TO ENTER", "ABC", "QUALIFIES"))
        self.assertEqual(card["headline"]["value"], f"${payload['recommended_strike']:,.2f}")
        self.assertEqual(card["headline_side"]["value"], f"{payload['delta']:.2f}")
        metrics = {m["label"]: m["value"] for m in card["metrics"]}
        self.assertEqual(metrics["Net premium / contract"], f"${payload['net_premium_per_contract']:,.2f}")
        self.assertEqual(metrics["Effective cost / share"], f"${payload['effective_purchase_price']:,.2f}")
        self.assertEqual(metrics["Annualized screening return"], f"{payload['annualized_roi_pct']:.1f}%")
        self.assertEqual(metrics["Cash collateral / contract"], f"${payload['collateral_per_contract']:,.2f}")
        text = card_text(card)
        self.assertIn("Support zone: $140.00 to $143.00", text)
        self.assertIn("Support score: 86/100", text)
        self.assertIn("not a probability", text)
        for banned in ("−", " -- ", " — ", " – "):
            self.assertNotIn(banned, text)

    def test_card_keeps_to_three_reasons_and_four_key_numbers(self):
        card = ce.tooltip_card(
            recommend(zone(140, 143, 86), (ce.PutQuote(145, 3.10, 3.30, 0.55), ce.PutQuote(140, 1.95, 2.05, 0.55)))
        )
        self.assertEqual(len(card["why"]), 3)
        self.assertEqual(len(card["metrics"]), 4)
        self.assertIn("above the 30% minimum", card["why"][0])
        self.assertIn("preferred 0.20 to 0.30", card["why"][1])

    def test_strike_table_marks_the_pick_and_says_why_others_lost(self):
        entry = recommend(
            zone(140, 143, 86),
            (ce.PutQuote(140, 1.95, 2.05, 0.55), ce.PutQuote(135, 0.50, 2.50, 0.30)),
        )
        table = ce.tooltip_card(entry)["sections"][0]["table"]
        by_strike = {row["cells"][0]: row for row in table["rows"]}
        self.assertTrue(by_strike["$140.00"]["picked"])
        self.assertEqual(by_strike["$140.00"]["result"], "Selected")
        # A qualifying strike's line is its support advice.
        self.assertEqual(by_strike["$140.00"]["reason_label"], "Inside support zone")
        self.assertFalse(by_strike["$135.00"]["picked"])
        self.assertEqual(by_strike["$135.00"]["result"], "Rejected")
        self.assertEqual(by_strike["$135.00"]["reason"], "Bid/ask spread wider than 50% of the mid")
        self.assertEqual(table["columns"][5], "Spread")
        self.assertEqual(by_strike["$135.00"]["cells"][5], "FAIL")
        self.assertEqual(by_strike["$135.00"]["cells"][-1], "Rejected")

    def test_missing_market_data_reads_unverified_not_rejected(self):
        no_chain = ce.recommend_put(
            current_price=150.0, support=zone(140, 143, 86), expiry=EXPIRY_15D, today=TODAY, puts=None,
        )
        self.assertEqual(ce.tooltip_card(no_chain)["status_label"], "UNVERIFIED")
        failed = recommend(zone(140, 143, 86), (ce.PutQuote(140, 0.50, 0.60, 0.55),))
        self.assertEqual(ce.tooltip_card(failed)["status_label"], "DOES NOT QUALIFY")
        no_price = ce.calculate_csp_to_enter(current_price=None, bars=[], expiry=None, today=TODAY, puts=None)
        self.assertEqual(ce.tooltip_card(no_price)["status_label"], "UNVERIFIED")


class TestWorkedExample(unittest.TestCase):
    """$331.21 stock, $310 put at a $10.00 mid, $0.65 fee, Nov 20 expiry."""

    def _entry(self, today):
        return ce.recommend_put(
            current_price=331.21, support=zone(311.43, 326.48, 92), expiry=date(2026, 11, 20), today=today,
            puts=(ce.PutQuote(310, 9.90, 10.10, 0.42),),
        )

    def test_economics(self):
        pick = self._entry(date(2026, 10, 16)).pick
        self.assertAlmostEqual(pick.net_premium, 9.9935, places=10)
        self.assertAlmostEqual(pick.net_premium * 100, 999.35, places=8)
        self.assertAlmostEqual(pick.effective_purchase_price, 300.0065, places=10)
        self.assertAlmostEqual(pick.downside_to_strike_pct, 100 * (331.21 - 310) / 331.21, places=10)
        self.assertAlmostEqual(pick.effective_discount_pct, 100 * (331.21 - 300.0065) / 331.21, places=10)
        card = ce.tooltip_card(self._entry(date(2026, 10, 16)))
        metrics = {m["label"]: m["value"] for m in card["metrics"]}
        self.assertEqual(metrics["Net premium / contract"], "$999.35")
        self.assertEqual(metrics["Effective cost / share"], "$300.01")
        self.assertEqual(metrics["Cash collateral / contract"], "$31,000.00")
        self.assertIn("Price to strike: 6.4% lower", card_text(card))

    def test_dte_and_return_use_the_same_as_of_date(self):
        for today, dte, annualized in ((date(2026, 10, 16), 35, "33.6%"), (date(2026, 10, 17), 34, "34.6%")):
            card = ce.tooltip_card(self._entry(today))
            self.assertIn(f"{dte} DTE from {today.strftime('%b')} {today.day}", card["subtitle"])
            self.assertEqual(card["metrics"][2]["value"], annualized)
            self.assertIn(f"× 365 ÷ {dte} = {annualized}", card_text(card))


class TestSpreadCheck(unittest.TestCase):
    def _candidate(self, bid, ask):
        entry = recommend(zone(140, 143, 86), (ce.PutQuote(140, bid, ask, 0.55),))
        return entry.candidates[0]

    def test_states(self):
        self.assertEqual(self._candidate(1.95, 2.05).spread_check, "PASS")
        self.assertEqual(self._candidate(1.20, 2.80).spread_check, "FAIL")  # 80% of the mid
        self.assertEqual(self._candidate(0.0, 0.40).spread_check, "FAIL")  # no bid
        self.assertEqual(self._candidate(None, None).spread_check, "UNAVAILABLE")

    def test_boundary_is_inclusive(self):
        # (2.5 - 1.5) / 2.0 = exactly 50% of the mid: allowed, as the filter allows it.
        candidate = self._candidate(1.5, 2.5)
        self.assertAlmostEqual(candidate.spread_pct_of_mid, 50.0)
        self.assertEqual(candidate.spread_check, "PASS")
        self.assertNotIn("spread", candidate.rejected_reason or "")

    def test_frontend_shows_the_card_verbatim(self):
        with open(os.path.join(ROOT, "wheel", "static", "app.js"), encoding="utf-8") as handle:
            js = handle.read()
        self.assertIn("if (entry) setTipCard(cell, entry.card);", js)
        self.assertIn("card: cspEntry ? cspEntry.card : null,", js)


class TestDailyRangesInPriceCache(unittest.TestCase):
    def test_chart_parse_keeps_high_low_volume(self):
        import json
        from datetime import datetime, timezone

        ts = int(datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc).timestamp())
        text = json.dumps({"chart": {"result": [{
            "timestamp": [ts],
            "indicators": {"quote": [{"close": [100.0], "high": [101.5], "low": [98.25], "volume": [12345]}]},
        }]}})
        (point,) = marketdata.parse_yahoo_chart(text)
        self.assertEqual((point.high, point.low, point.volume), (101.5, 98.25, 12345.0))

    def test_cache_round_trip_and_legacy_two_column_rows(self):
        points = [PricePoint(date(2026, 1, 2), 100.0, 101.0, 99.0, 5e5), PricePoint(date(2026, 1, 5), 101.0, None, 100.0, None)]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "X.csv")
            marketdata.save_cache(points, path)
            self.assertEqual(marketdata.load_cache(path), points)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("2026-01-02,100.0\n")
            self.assertEqual(marketdata.load_cache(path), [PricePoint(date(2026, 1, 2), 100.0)])

    def test_require_ranges_refetches_a_close_only_cache_once(self):
        import json
        from datetime import datetime, timezone

        calls = []
        day = date.today()
        ts = int(datetime(day.year, day.month, day.day, 14, 30, tzinfo=timezone.utc).timestamp())

        def fetch():
            calls.append(1)
            return json.dumps({"chart": {"result": [{
                "timestamp": [ts],
                "indicators": {"quote": [{"close": [10.0], "high": [10.5], "low": [9.5], "volume": [100]}]},
            }]}})

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "X.csv")
            marketdata.save_cache([PricePoint(day, 10.0)], path)
            points, _ = marketdata.get_price_series("X", fetch=fetch, cache_path=path)
            self.assertEqual(calls, [])  # fresh close-only cache is fine for close-only callers
            points, _ = marketdata.get_price_series("X", fetch=fetch, cache_path=path, require_ranges=True)
            self.assertEqual(calls, [1])
            self.assertTrue(points[-1].has_range)
            marketdata.get_price_series("X", fetch=fetch, cache_path=path, require_ranges=True)
            self.assertEqual(calls, [1])


class TestExpirationIsCspsOwn(unittest.TestCase):
    """CSP TO ENTER picks its expiration with its own target and window,
    never CC TO EXIT's, and says when it had to leave the window."""

    def test_pick_ignores_cc_to_exit_settings(self):
        from unittest import mock

        from wheel import ccexit

        listed = [TODAY + timedelta(days=35), TODAY + timedelta(days=42)]
        with mock.patch.object(ccexit, "CC_EXIT_TARGET_DTE", 42):
            self.assertEqual(ccexit.pick_expiration(listed, TODAY), TODAY + timedelta(days=42))
            self.assertEqual(ce.pick_expiration(listed, TODAY), TODAY + timedelta(days=35))

    def test_window_fallback_is_flagged_in_payload_and_tooltip(self):
        listed = [TODAY + timedelta(days=20), TODAY + timedelta(days=60)]
        expiry = ce.pick_expiration(listed, TODAY)
        self.assertEqual(expiry, TODAY + timedelta(days=20))  # 15 from the target vs 25
        entry = recommend(zone(140, 143, 86), (ce.PutQuote(140, 1.95, 2.05, 0.55),), expiry=expiry)
        payload = _csp_to_enter_payload(entry)
        self.assertIs(payload["expiry_in_window"], False)
        self.assertIn("none listed 30 to 45 days out", card_text(payload["card"]))
        inside = _csp_to_enter_payload(recommend(zone(140, 143, 86), (), expiry=TODAY + timedelta(days=35)))
        self.assertIs(inside["expiry_in_window"], True)

    def test_dashboard_uses_the_csp_pick_and_one_chain_per_date(self):
        from unittest import mock

        from wheel import ccexit

        puts = (ce.PutQuote(140, 4.10, 4.30, 0.60),)
        dashboard = _dashboard_with_market({"ABC": 150.0}, bars_through(SUPPORT_140_143), {"ABC": puts})
        dashboard._option_expirations = lambda tickers: {
            t: [TODAY + timedelta(days=35), TODAY + timedelta(days=42)] for t in tickers
        }
        asked = []
        chains = dashboard._option_chains
        dashboard._option_chains = lambda pairs: asked.append(set(pairs)) or chains(pairs)
        with mock.patch.object(ccexit, "CC_EXIT_TARGET_DTE", 42):
            entries = Dashboard._build_csp_to_enter(dashboard, {"ABC": 150.0})
        self.assertEqual(entries["ABC"]["expiry"], (TODAY + timedelta(days=35)).isoformat())
        self.assertEqual(asked, [{("ABC", TODAY + timedelta(days=35))}])


class TestEntryEconomicsArePresentedSeparately(unittest.TestCase):
    """Net premium, collateral, effective price, delta limits, DTE and the
    annualized screening figure stay distinct fields, and the tooltip never
    reads as a promise."""

    def setUp(self):
        self.entry = recommend(
            zone(140, 143, 86),
            (ce.PutQuote(145, 3.10, 3.30, 0.55), ce.PutQuote(140, 1.95, 2.05, 0.55)),
        )
        self.payload = _csp_to_enter_payload(self.entry)

    def test_payload_fields(self):
        p = self.payload
        net = 2.00 - 0.0065
        self.assertEqual(p["recommended_strike"], 140.0)
        self.assertAlmostEqual(p["net_premium"], net, places=4)
        self.assertEqual(p["net_premium_per_contract"], round(net * 100, 2))
        self.assertEqual(p["collateral_per_contract"], 14000.0)
        self.assertEqual(p["effective_purchase_price"], round(140 - net, 2))
        self.assertAlmostEqual(p["effective_discount_pct"], round(100 * (150 - (140 - net)) / 150, 2), places=6)
        self.assertAlmostEqual(p["downside_to_strike_pct"], round(100 * 10 / 150, 2), places=6)
        self.assertEqual(p["delta_target"], 0.25)
        self.assertEqual(p["delta_allowed_range"], [0.10, 0.40])
        self.assertEqual(p["days_to_expiry"], 15)

    def test_tooltip_labels_the_screening_metric_and_the_downside(self):
        text = card_text(self.payload["card"])
        self.assertIn("allowed 0.10 to 0.40", text)
        self.assertIn("screening figure for comparing puts, not a forecast", text)
        self.assertIn("does not limit losses", text)
        self.assertIn("You must buy 100 shares per contract", text)
        self.assertIn("They do not guarantee that the stock will stop falling", text)
        self.assertIn("an estimate, not a guaranteed fill", text)
        # Distance to the strike is never called protection or a maximum loss.
        for misleading in ("protection", "maximum loss", "max loss", "downside"):
            self.assertNotIn(misleading, text.lower())

    def test_snapshot_times_are_carried_onto_the_payload(self):
        from datetime import datetime, timezone

        quoted = datetime(2026, 10, 16, 20, 0, tzinfo=timezone.utc)
        fetched = datetime(2026, 10, 17, 14, 0, tzinfo=timezone.utc)
        ran = datetime(2026, 10, 17, 14, 0, 5, tzinfo=timezone.utc)
        entry = ce.recommend_put(
            current_price=150.0, support=zone(140, 143, 86), expiry=EXPIRY_15D, today=TODAY,
            puts=(ce.PutQuote(140, 1.95, 2.05, 0.55),), quotes_as_of=quoted, fetched_at=fetched, calculated_at=ran,
        )
        payload = _csp_to_enter_payload(entry)
        self.assertEqual(
            (payload["quotes_as_of"], payload["chain_fetched_at"], payload["calculated_at"]),
            (quoted.isoformat(), fetched.isoformat(), ran.isoformat()),
        )
        self.assertTrue(payload["card"]["quoted"].startswith("Option prices as of"))


class TestOptionLiquidityFilters(unittest.TestCase):
    """The existing quote filters: a bid, a usable mid, a spread no wider
    than half the mid. Open interest and volume are shown, never filtered."""

    def _reason(self, quote):
        return by_strike(recommend(zone(140, 143, 86), (quote,)))[quote.strike].rejected_reason

    def test_missing_or_zero_bid_is_rejected(self):
        self.assertIn("no bid", self._reason(ce.PutQuote(140, None, 2.05, 0.55)))
        self.assertIn("no bid", self._reason(ce.PutQuote(140, 0.0, 2.05, 0.55)))

    def test_invalid_mid_is_rejected(self):
        self.assertIn("no bid", self._reason(ce.PutQuote(140, 2.10, 1.90, 0.55)))  # crossed quote
        self.assertIn("no bid", self._reason(ce.PutQuote(140, 0.0, 0.0, 0.55)))  # zero ask

    def test_spread_wider_than_half_the_mid_is_rejected(self):
        self.assertIn("spread wider than 50%", self._reason(ce.PutQuote(140, 1.20, 2.80, 0.55)))

    def test_unreported_open_interest_and_volume_never_reject(self):
        reported = recommend(zone(140, 143, 86), (ce.PutQuote(140, 1.95, 2.05, 0.55, 1234, 56),))
        unreported = recommend(zone(140, 143, 86), (ce.PutQuote(140, 1.95, 2.05, 0.55),))
        self.assertEqual(reported.strike, 140)
        self.assertEqual(unreported.strike, 140)
        self.assertEqual(_csp_to_enter_payload(reported)["open_interest"], 1234)
        self.assertIn("Open interest: 1,234\nVolume today: 56", card_text(ce.tooltip_card(reported)))
        self.assertIsNone(_csp_to_enter_payload(unreported)["open_interest"])
        self.assertIn(
            "Open interest: Not reported\nVolume today: Not reported", card_text(ce.tooltip_card(unreported))
        )


class TestSupportEvidenceInPayload(unittest.TestCase):
    def test_zone_evidence_is_exposed(self):
        payload = _csp_to_enter_payload(recommend(zone(140, 143, 86), (ce.PutQuote(140, 1.95, 2.05, 0.55),)))
        evidence = payload["support_evidence"]
        self.assertEqual(evidence["touches"], 2)
        self.assertEqual(evidence["touch_days"], ["2026-06-01", "2026-09-01"])
        self.assertEqual(evidence["breaks"], 0)
        self.assertEqual(payload["support_distance_pct"], round(100 * 7 / 150, 2))

    def test_no_history_has_no_zone_and_says_why(self):
        entry = ce.calculate_csp_to_enter(current_price=150.0, bars=[], expiry=EXPIRY_15D, today=TODAY, puts=())
        payload = _csp_to_enter_payload(entry)
        self.assertEqual(payload["label"], "NO QUALIFYING CSP")
        self.assertIsNone(payload["support_evidence"])
        self.assertIsNone(payload["support_score"])
        self.assertIn("Not enough daily price history", payload["reason"])

    def test_close_through_count_shows_in_evidence(self):
        broken = ce.SupportAnalysis(
            252, 2.0, 3.0, {},
            (replace_zone(zone(140, 143, 86).zones[0], breaks=1, score=43.0),), 1.0, (),
        )
        payload = _csp_to_enter_payload(recommend(broken, (ce.PutQuote(140, 1.95, 2.05, 0.55),)))
        self.assertEqual(payload["support_evidence"]["breaks"], 1)
        self.assertIn("Closed through since: 1x (each halves the score)", card_text(payload["card"]))


def replace_zone(z, **changes):
    from dataclasses import replace

    return replace(z, **changes)


class TestCombinedEntryUsesTheNewestSnapshot(unittest.TestCase):
    def test_newer_calculation_wins_and_ties_keep_the_first(self):
        old = {"ABC": {"recommended_strike": 135.0, "calculated_at": "2026-10-17T14:00:00+00:00"}}
        new = {"ABC": {"recommended_strike": 140.0, "calculated_at": "2026-10-17T14:00:20+00:00"}}
        self.assertEqual(_combine_csp_to_enter([old, new])["ABC"]["recommended_strike"], 140.0)
        self.assertEqual(_combine_csp_to_enter([new, old])["ABC"]["recommended_strike"], 140.0)
        same = {"ABC": {**old["ABC"], "recommended_strike": 150.0}}
        self.assertEqual(_combine_csp_to_enter([old, same])["ABC"]["recommended_strike"], 135.0)

    def test_complete_data_beats_a_newer_entry_that_lacked_it(self):
        priced = {"ABC": {"recommended_strike": 140.0, "calculated_at": "1", "market_data_complete": True}}
        unpriced = {
            "ABC": {"recommended_strike": None, "label": "NO QUALIFYING CSP", "calculated_at": "2",
                    "market_data_complete": False},
        }
        for order in ([priced, unpriced], [unpriced, priced]):
            self.assertIs(_combine_csp_to_enter(order)["ABC"], priced["ABC"])

    def test_when_every_entry_lacked_data_the_newest_shows_and_no_strike_is_invented(self):
        older = {"ABC": {"recommended_strike": None, "calculated_at": "1", "market_data_complete": False}}
        newer = {"ABC": {"recommended_strike": None, "calculated_at": "2", "market_data_complete": False}}
        chosen = _combine_csp_to_enter([older, newer])["ABC"]
        self.assertIs(chosen, newer["ABC"])
        self.assertIsNone(chosen["recommended_strike"])

    def test_payload_flags_which_results_lacked_market_data(self):
        def complete(entry):
            return _csp_to_enter_payload(entry)["market_data_complete"]

        self.assertFalse(complete(ce.calculate_csp_to_enter(current_price=None, bars=[], expiry=None, today=TODAY, puts=None)))
        self.assertFalse(complete(ce.calculate_csp_to_enter(current_price=150.0, bars=[], expiry=None, today=TODAY, puts=None)))
        self.assertFalse(complete(recommend(zone(140, 143, 86), None)))  # chain didn't load
        self.assertFalse(complete(recommend(zone(140, 143, 86), (), expiry=None)))  # no listed expiration
        # Every input loaded: a rules rejection and a recommendation are both complete.
        self.assertTrue(complete(recommend(zone(140, 143, 30), (ce.PutQuote(140, 1.95, 2.05, 0.55),))))
        self.assertTrue(complete(recommend(zone(140, 143, 86), (ce.PutQuote(140, 1.95, 2.05, 0.55),))))

    def test_overlapping_and_separate_tickers_each_appear_once(self):
        a = {"ABC": {"calculated_at": "1"}, "XYZ": {"calculated_at": "1"}}
        b = {"ABC": {"calculated_at": "2"}, "QQQ": {"calculated_at": "1"}}
        combined = _combine_csp_to_enter([a, b, None])
        self.assertEqual(set(combined), {"ABC", "XYZ", "QQQ"})
        self.assertIs(combined["ABC"], b["ABC"])




class TestSupportGuidance(unittest.TestCase):
    """Per-strike support advice against the chosen zone ($140 to $143, a
    3% zone width = $4.50 on a $150 stock). Advice only."""

    def setUp(self):
        self.entry = recommend(zone(140, 143, 86), (ce.PutQuote(140, 1.95, 2.05, 0.55),))
        self.assertEqual(self.entry.strike, 140)

    def guide(self, strike, qualifying=(130.0, 134.0, 137.0, 140.0, 141.0, 145.0, 148.0)):
        return ce.strike_guidance(self.entry, strike, qualifying)

    def test_each_position(self):
        cases = {
            148.0: ("above", "Above"),  # 3.3% over the zone top: past one width
            145.0: ("near", "Near"),  # 1.3% over the top
            141.0: ("inside", "Inside"),
            137.0: ("just_below", "Just below"),  # 2.0% under the bottom
            134.0: ("well_below", "Well below"),  # 4.0% under the bottom
        }
        for strike, (position, label) in cases.items():
            got = self.guide(strike)
            self.assertEqual((got.position, got.label), (position, label), strike)

    def test_one_zone_width_is_the_boundary(self):
        # Width 3% of $150 = $4.50: $147.50 is near, a cent more is above;
        # $135.50 is just below, a cent less is well below.
        self.assertEqual(self.guide(147.50).position, ce.GUIDE_NEAR)
        self.assertEqual(self.guide(147.51).position, ce.GUIDE_ABOVE)
        self.assertEqual(self.guide(135.50).position, ce.GUIDE_JUST_BELOW)
        self.assertEqual(self.guide(135.49).position, ce.GUIDE_WELL_BELOW)

    def test_a_lower_strike_is_only_suggested_when_one_qualifies(self):
        self.assertIn("a lower qualifying strike gives more downside room", self.guide(148.0).text)
        self.assertIn("usually for less premium", self.guide(148.0).text)
        self.assertIn("no lower strike qualifies", self.guide(148.0, qualifying=(148.0,)).text)
        self.assertIn("a qualifying strike below the zone", self.guide(141.0).text)
        self.assertNotIn("qualifying strike", self.guide(141.0, qualifying=(141.0, 142.0)).text)

    def test_inside_never_promises_the_level_holds(self):
        text = self.guide(141.0).text
        self.assertIn("a break below it could lead to assignment", text)
        for promise in ("safe", "guarantee", "will hold", "certain"):
            self.assertNotIn(promise, text.lower())

    def test_price_already_inside_the_zone_says_so(self):
        entry = recommend(zone(140, 143, 86), (ce.PutQuote(140, 1.95, 2.05, 0.55),), price=142.0)
        self.assertEqual(entry.zone_distance_pct, 0.0)
        got = ce.strike_guidance(entry, 141.0, (141.0,))
        self.assertEqual(got.position, ce.GUIDE_INSIDE)
        self.assertIn("already trading in this support area", got.text)

    def test_no_or_broken_support_is_neutral(self):
        # The only zone is above the price: that support has broken, so it is
        # never presented as a floor under the strike.
        broken = recommend(zone(160, 165, 86), (ce.PutQuote(140, 1.95, 2.05, 0.55),))
        self.assertIsNone(broken.zone)
        self.assertIn("already broken", broken.reason)
        got = ce.strike_guidance(broken, 140.0, (140.0,))
        self.assertEqual((got.position, got.label), (ce.GUIDE_NONE, "None found"))
        self.assertEqual(got.text, "Use the strike metrics without relying on historical support.")

    def test_sentences_are_short_and_never_repeat_prices_or_evidence(self):
        for strike in (148.0, 145.0, 141.0, 137.0, 134.0):
            text = self.guide(strike).text
            self.assertNotIn("$", text)
            self.assertNotIn("/100", text)
            self.assertEqual(text.count("."), 1)

    def test_jxn_example(self):
        # $131.21 stock, $123.97 to $126.14 zone: $125 is inside it, $120 is
        # 3.0% of the price under its bottom -- just below with a zone width
        # of 3.5%, well below with 2.5%.
        jxn = ce.SupportZone(123.97, 126.14, (123.97, 126.14), (date(2026, 6, 1), date(2026, 9, 1)), 20, 7.0, 0, (), None, None, {}, 70.0)
        for width, below in ((3.5, ce.GUIDE_JUST_BELOW), (2.5, ce.GUIDE_WELL_BELOW)):
            entry = ce.recommend_put(
                current_price=131.21, support=ce.SupportAnalysis(252, 2.0, width, {}, (jxn,), 1.0, ()),
                expiry=date(2026, 11, 20), today=TODAY,
                puts=(ce.PutQuote(120, 4.15, 4.25, 0.42), ce.PutQuote(125, 5.5, 5.6, 0.40)), risk_free_rate=0.045,
            )
            strikes = by_strike(entry)
            self.assertTrue(strikes[120].qualifies and strikes[125].qualifies)
            self.assertEqual(ce.candidate_guidance(entry, strikes[125]).position, ce.GUIDE_INSIDE)
            self.assertEqual(ce.candidate_guidance(entry, strikes[120]).position, below)

    def test_guidance_never_changes_the_pick_or_the_ranking(self):
        puts = (ce.PutQuote(145, 3.10, 3.30, 0.55), ce.PutQuote(140, 1.95, 2.05, 0.55), ce.PutQuote(135, 0.50, 2.50, 0.30))
        entry = recommend(zone(140, 143, 86), puts)
        best = max((c for c in entry.candidates if c.qualifies), key=lambda c: c.score)
        self.assertIs(entry.pick, best)
        table = ce.tooltip_card(entry)["sections"][0]["table"]
        selected = [row for row in table["rows"] if row["picked"]]
        self.assertEqual([row["cells"][0] for row in selected], [f"${entry.strike:,.2f}"])

    def test_table_shows_downside_room_and_support_from_the_backend(self):
        puts = (ce.PutQuote(145, 3.10, 3.30, 0.55), ce.PutQuote(140, 1.95, 2.05, 0.55), ce.PutQuote(135, 0.50, 2.50, 0.30))
        entry = recommend(zone(140, 143, 86), puts)
        section = ce.tooltip_card(entry)["sections"][0]
        table = section["table"]
        self.assertEqual(table["columns"], ["Strike", "Delta", "Net / contract", "Ann. return", "To strike", "Spread", "Support", "Result"])
        rows = {row["cells"][0]: row for row in table["rows"]}
        self.assertEqual(rows["$140.00"]["cells"][4], "6.7%")  # (150 - 140) / 150
        self.assertEqual(rows["$140.00"]["cells"][6], "Inside")
        self.assertEqual(rows["$140.00"]["reason_label"], "Inside support zone")
        # A rejected strike stays rejected and gets no support advice.
        self.assertEqual((rows["$135.00"]["cells"][6], rows["$135.00"]["result"]), ("-", "Rejected"))
        self.assertIn("spread", rows["$135.00"]["reason"].lower())
        self.assertIn("whether you would be comfortable owning 100 shares there", " ".join(section["notes"]))
        payload = _csp_to_enter_payload(entry)["support_guidance"]
        self.assertEqual(
            {item["strike"]: item["label"] for item in payload},
            {c.strike: ce.candidate_guidance(entry, c).label for c in entry.candidates if c.qualifies},
        )

    def test_frontend_does_not_classify(self):
        with open(os.path.join(ROOT, "wheel", "static", "app.js"), encoding="utf-8") as handle:
            js = handle.read()
        for backend_only in ("just_below", "well_below", "support_guidance", "zone_width"):
            self.assertNotIn(backend_only, js)

if __name__ == "__main__":
    unittest.main()
