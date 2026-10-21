"""Historical resistance for CC TO EXIT: the shared swing-point engine
(wheel/pricezones.py) on the resistance side, ccexit.resistance_context's
strike-vs-zone relationship, its payload and card, and that it is context
only -- the delta-based recommendation never changes because of it."""

from __future__ import annotations

import math
import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel import ccexit, pricezones  # noqa: E402
from wheel.api import _cc_exit_payload  # noqa: E402
from wheel.ccexit import CallQuote, ShareBlock, calculate_cc_to_exit, resistance_context, tooltip_card  # noqa: E402
from wheel.marketdata import PricePoint  # noqa: E402
from wheel.optionsmath import black_scholes_greeks  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HALF_RANGE = 0.012  # every synthetic bar's high/low is close +-1.2%
TODAY = date(2026, 10, 17)
EXPIRY = date(2026, 11, 20)
DTE = (EXPIRY - TODAY).days


def bars_through(anchors, *, start=date(2025, 9, 1), volume=1_000_000.0):
    """Daily bars whose close moves linearly between ``(session, close)`` anchors."""
    closes: list[float] = []
    for (i0, c0), (i1, c1) in zip(anchors, anchors[1:]):
        closes += [c0 + (c1 - c0) * (i - i0) / (i1 - i0) for i in range(i0, i1)]
    closes.append(anchors[-1][1])
    bars, day = [], start
    for close in closes:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        bars.append(PricePoint(day, close, high=close * (1 + HALF_RANGE), low=close * (1 - HALF_RANGE), volume=volume))
        day += timedelta(days=1)
    return bars


def high_of(close: float) -> float:
    return close * (1 + HALF_RANGE)


# Three rallies stall near $150 (closes 150, 149, 151) with deep pullbacks
# between, then the stock drifts to $140: one overhead resistance zone.
CEILING = [(0, 130), (30, 150), (50, 132), (80, 149), (100, 131), (130, 151), (150, 133), (180, 138), (260, 140)]
PRICE = 140.0


def ceiling_analysis():
    return pricezones.analyze(bars_through(CEILING), pricezones.RESISTANCE)


class TestResistanceZones(unittest.TestCase):
    def test_repeated_swing_highs_are_found_and_grouped_into_one_zone(self):
        analysis = ceiling_analysis()
        self.assertEqual(analysis.side, pricezones.RESISTANCE)
        top = analysis.zones[0]
        self.assertEqual(top.touches, 3)
        self.assertAlmostEqual(top.low, high_of(149))
        self.assertAlmostEqual(top.high, high_of(151))
        self.assertEqual(top.touch_levels, (high_of(150), high_of(149), high_of(151)))  # oldest first
        self.assertEqual(top.breaks, 0)
        self.assertGreaterEqual(top.score, ccexit.MIN_RESISTANCE_SCORE)

    def test_distant_highs_form_separate_zones(self):
        # A stall near $150, then a later one near $175: two zones, never one wide one.
        bars = bars_through(
            [(0, 130), (30, 150), (50, 132), (80, 149), (100, 131), (140, 175), (160, 155), (190, 174), (210, 156), (260, 160)]
        )
        zones = pricezones.analyze(bars, pricezones.RESISTANCE).zones
        tops = sorted(round(zone.high) for zone in zones)
        self.assertEqual(len(zones), 2)
        self.assertEqual(tops, [round(high_of(150)), round(high_of(175))])

    def test_a_steady_rise_has_no_swing_highs(self):
        analysis = pricezones.analyze(bars_through([(0, 100), (260, 160)]), pricezones.RESISTANCE)
        self.assertEqual(analysis.zones, ())

    def test_the_last_sessions_cannot_be_swing_highs_yet(self):
        # A spike in the final few sessions has no right side yet: no look-ahead.
        bars = bars_through([(0, 120), (255, 125), (258, 140), (260, 139)])
        analysis = pricezones.analyze(bars, pricezones.RESISTANCE)
        self.assertFalse(any(zone.high > high_of(130) for zone in analysis.zones))

    def test_closing_through_the_zone_halves_the_score(self):
        held = ceiling_analysis().zones[0]
        broke = pricezones.analyze(
            bars_through(CEILING[:-2] + [(200, 165), (230, 150), (260, 152)]), pricezones.RESISTANCE
        )
        zone = next(z for z in broke.zones if abs(z.high - held.high) < 0.01)
        self.assertEqual(zone.breaks, 1)
        self.assertLess(zone.score, held.score)

    def test_score_is_the_documented_weighted_formula(self):
        analysis = ceiling_analysis()
        zone = analysis.zones[0]
        weights = pricezones.ZONE_WEIGHTS
        expected = (
            100.0
            * sum(weights[name] * credit for name, credit in zone.components.items())
            / sum(weights[name] for name in zone.components)
            * pricezones.BREAK_PENALTY_FACTOR**zone.breaks
            * analysis.history_confidence
        )
        self.assertAlmostEqual(zone.score, expected, places=9)
        self.assertEqual(ceiling_analysis().zones[0].score, zone.score)  # same input, same score
        self.assertAlmostEqual(
            zone.components["recency"], 0.5 ** (zone.sessions_since_last_touch / pricezones.RECENCY_HALF_LIFE_BARS)
        )

    def test_too_little_history_says_so(self):
        analysis = pricezones.analyze(bars_through([(0, 100), (20, 110)]), pricezones.RESISTANCE)
        self.assertEqual(analysis.zones, ())
        self.assertIn("needed to find resistance", analysis.limitations[-1])


class TestStrikeVsResistance(unittest.TestCase):
    def setUp(self):
        self.analysis = ceiling_analysis()
        self.zone = self.analysis.zones[0]

    def test_strike_just_above_the_zone(self):
        strike = 155.0
        context = resistance_context(self.analysis, strike, PRICE)
        self.assertIs(context.zone, self.zone)
        self.assertEqual(context.relationship, ccexit.ABOVE_ZONE)
        self.assertAlmostEqual(context.distance_pct, 100 * (strike - self.zone.high) / strike)
        self.assertTrue(context.near)
        self.assertEqual(
            context.reason,
            f"Recommended $155.00 is just above the ${self.zone.low:,.2f} to ${self.zone.high:,.2f} resistance zone.",
        )
        self.assertIs(context.below, self.zone)
        self.assertIsNone(context.at)

    def test_strike_inside_the_zone(self):
        context = resistance_context(self.analysis, 151.0, PRICE)
        self.assertEqual((context.relationship, context.distance_pct), (ccexit.INSIDE_ZONE, 0.0))
        self.assertIn("Recommended $151.00 falls inside the", context.reason)
        self.assertIs(context.at, self.zone)

    def test_strike_below_the_zone(self):
        context = resistance_context(self.analysis, 145.0, PRICE)
        self.assertEqual(context.relationship, ccexit.BELOW_ZONE)
        self.assertAlmostEqual(context.distance_pct, 100 * (self.zone.low - 145.0) / 145.0)
        self.assertIn("Recommended $145.00 is", context.reason)
        self.assertIn("below the", context.reason)
        self.assertIs(context.above, self.zone)

    def test_far_from_the_strike_is_not_just_above(self):
        context = resistance_context(self.analysis, 185.0, PRICE)
        self.assertEqual(context.relationship, ccexit.ABOVE_ZONE)
        self.assertFalse(context.near)
        self.assertIn("is above the", context.reason)

    def test_no_meaningful_resistance(self):
        far = resistance_context(self.analysis, 260.0, PRICE)  # zone is > 25% under the strike
        self.assertIsNone(far.zone)
        self.assertEqual(far.reason, ccexit.NO_RESISTANCE_NEAR)
        self.assertEqual(far.reason, "No significant historical resistance identified near the recommended strike.")
        overrun = resistance_context(self.analysis, 175.0, 170.0)  # price already above the zone
        self.assertIsNone(overrun.zone)
        flat = pricezones.analyze(bars_through([(0, 100), (260, 160)]), pricezones.RESISTANCE)
        self.assertIn("No swing highs", resistance_context(flat, 165.0, 160.0).reason)
        self.assertIn("Not enough daily price history", resistance_context(
            pricezones.analyze(bars_through([(0, 100), (20, 110)]), pricezones.RESISTANCE), 115.0, 110.0
        ).reason)
        self.assertIsNone(resistance_context(None, 150.0, PRICE).zone)

    def test_weak_zones_do_not_count(self):
        weak = pricezones.ZoneAnalysis(
            pricezones.RESISTANCE, 252, 2.0, 3.0, {},
            (pricezones.PriceZone(pricezones.RESISTANCE, 150, 152, (150, 152), (TODAY, TODAY), 5, 6.0, 0, (), None, None, {}, 39.9),),
            1.0, (),
        )
        self.assertIsNone(resistance_context(weak, 151.0, PRICE).zone)


def _call(strike: float, delta: float, *, spot: float = PRICE) -> CallQuote:
    """A quote whose implied volatility makes the model delta land on ``delta``."""
    low, high = 0.01, 5.0
    for _ in range(200):
        mid = (low + high) / 2
        got = black_scholes_greeks(spot=spot, strike=strike, days_to_expiry=DTE, implied_volatility=mid, right="C", risk_free_rate=0.045).delta
        # An out-of-the-money call's delta rises with volatility; an in-the-money one's falls.
        low, high = (low, mid) if (got > delta) == (strike > spot) else (mid, high)
        if abs(got - delta) < 1e-6:
            break
    return CallQuote(strike, 2.0, 2.2, mid)


def _run(resistance):
    return calculate_cc_to_exit(
        share_blocks=[ShareBlock(100, 130.0)],
        covered_shares=0,
        current_price=PRICE,
        expiry=EXPIRY,
        today=TODAY,
        calls=(_call(145, 0.40), _call(150, 0.30), _call(155, 0.22), _call(160, 0.15)),
        risk_free_rate=0.045,
        resistance=resistance,
    )


class TestRecommendationIsUnchanged(unittest.TestCase):
    def test_resistance_never_moves_or_rejects_the_pick(self):
        without, with_zone = _run(None), _run(ceiling_analysis())
        self.assertEqual(with_zone.strike, without.strike)
        self.assertEqual(with_zone.status, without.status)
        self.assertEqual(
            [(c.strike, c.rejected_reason) for c in with_zone.candidates],
            [(c.strike, c.rejected_reason) for c in without.candidates],
        )
        # The 0.30-delta pick ($150) sits just under the $150.79 zone, and it
        # is still the pick: resistance is reported, never acted on.
        self.assertEqual(with_zone.strike, 150.0)
        self.assertEqual(with_zone.resistance.relationship, ccexit.BELOW_ZONE)
        self.assertTrue(with_zone.resistance.near)

    def test_no_pick_has_no_resistance_context(self):
        result = calculate_cc_to_exit(
            share_blocks=[ShareBlock(100, 130.0)], covered_shares=0, current_price=PRICE, expiry=EXPIRY,
            today=TODAY, calls=None, resistance=ceiling_analysis(),
        )
        self.assertIsNone(result.resistance)


class TestStrikeGuidance(unittest.TestCase):
    """Per-strike advice against the one reported zone ($150.79 to $152.81,
    zone width = the analysis's ATR-based width)."""

    def setUp(self):
        self.analysis = ceiling_analysis()
        self.zone = self.analysis.zones[0]
        self.context = resistance_context(self.analysis, 150.0, PRICE)

    def guide(self, strike, qualifying=(145.0, 150.0, 155.0, 160.0, 175.0)):
        return ccexit.strike_guidance(self.context, strike, qualifying)

    def test_below_suggests_a_higher_strike_only_when_one_qualifies(self):
        below = self.guide(145.0)
        self.assertEqual((below.position, below.label, below.title), ("below", "Below", "Below resistance"))
        self.assertIn("consider a higher qualifying strike", below.text)
        self.assertIn("usually pays less premium", below.text)  # the trade-off, not a promise
        top = self.guide(145.0, qualifying=(140.0, 145.0))
        self.assertEqual(top.position, ccexit.GUIDE_BELOW)
        self.assertNotIn("consider", top.text)
        self.assertIn("no higher strike qualifies", top.text)

    def test_inside_suggests_a_strike_above_the_zone_only_when_one_qualifies(self):
        inside = self.guide(151.0)
        self.assertEqual(inside.label, "Inside")
        self.assertIn("consider a qualifying strike above the zone", inside.text)
        # 152.5 qualifies but is still inside the zone: nothing above it qualifies.
        none_above = self.guide(151.0, qualifying=(150.0, 151.0, 152.5))
        self.assertIn("no qualifying strike is above the zone", none_above.text)

    def test_just_above_versus_well_above_uses_one_zone_width(self):
        width = self.analysis.zone_width_pct
        just_strike = self.zone.high / (1 - width / 100) - 0.01  # just inside one width
        well_strike = self.zone.high / (1 - width / 100) + 0.01  # just past it
        self.assertEqual(self.guide(just_strike).position, ccexit.GUIDE_JUST_ABOVE)
        self.assertEqual(self.guide(well_strike).position, ccexit.GUIDE_WELL_ABOVE)
        self.assertIn("room to move through resistance", self.guide(155.0).text)
        self.assertIn("lower premium is worth it", self.guide(175.0).text)

    def test_no_resistance_is_neutral(self):
        flat = pricezones.analyze(bars_through([(0, 100), (260, 160)]), pricezones.RESISTANCE)
        neutral = ccexit.strike_guidance(resistance_context(flat, 165.0, 160.0), 165.0, (165.0, 170.0))
        self.assertEqual((neutral.position, neutral.label), (ccexit.GUIDE_NONE, "None found"))
        self.assertEqual(neutral.text, "Decide using delta, profitability and the minimum exit.")

    def test_sentences_are_short_and_never_repeat_zone_prices(self):
        for strike in (145.0, 151.0, 155.0, 175.0):
            text = self.guide(strike).text
            self.assertNotIn("$", text)
            self.assertEqual(text.count("."), 1)  # one sentence

    def test_table_rows_carry_the_backend_guidance(self):
        result = _run(ceiling_analysis())
        self.assertEqual(result.strike, 150.0)  # the pick does not move
        table = tooltip_card(result)["sections"][0]["table"]
        self.assertEqual(table["columns"][-2:], ["Resistance", "Result"])
        by_strike = {row["cells"][0]: row for row in table["rows"]}
        picked = by_strike["$150.00"]
        self.assertTrue(picked["picked"])
        self.assertEqual((picked["cells"][-2], picked["reason_label"]), ("Below", "Below resistance"))
        self.assertIn("consider a higher qualifying strike", picked["reason"])
        payload = _cc_exit_payload(result)["cc_to_exit_detail"]["resistance"]["strike_guidance"]
        self.assertEqual(
            {item["strike"]: item["label"] for item in payload},
            # Zone width is 3.6%: $155 sits 1.4% over the top, $160 4.5%.
            {145.0: "Below", 150.0: "Below", 155.0: "Just above", 160.0: "Well above"},
        )

    def test_rejected_strikes_stay_rejected_with_no_advice(self):
        result = calculate_cc_to_exit(
            share_blocks=[ShareBlock(100, 130.0)], covered_shares=0, current_price=PRICE, expiry=EXPIRY,
            today=TODAY, calls=(_call(125, 0.70), _call(150, 0.30)), risk_free_rate=0.045,
            resistance=ceiling_analysis(),
        )
        rejected = next(c for c in result.candidates if c.strike == 125)
        self.assertIn("minimum exit", rejected.rejected_reason)
        self.assertIsNone(ccexit.candidate_guidance(result, rejected))
        row = next(r for r in tooltip_card(result)["sections"][0]["table"]["rows"] if r["cells"][0] == "$125.00")
        self.assertEqual((row["cells"][-2], row["result"], row["reason_label"]), ("-", "Rejected", None))


class TestPresentation(unittest.TestCase):
    def test_payload_and_card_show_the_same_one_result(self):
        result = _run(ceiling_analysis())
        detail = _cc_exit_payload(result, "XYZ")["cc_to_exit_detail"]["resistance"]
        card = tooltip_card(result, "XYZ")
        zone = result.resistance.zone
        self.assertTrue(detail["found"])
        self.assertEqual((detail["zone"]["low"], detail["zone"]["high"]), (round(zone.low, 2), round(zone.high, 2)))
        self.assertEqual(detail["relationship"], "below")
        self.assertEqual(detail["reason"], result.resistance.reason)
        context = card["context"]
        self.assertEqual(context["title"], "Historical resistance")
        self.assertEqual(context["value"], f"${zone.low:,.2f} to ${zone.high:,.2f}")
        self.assertEqual(context["badge"], f"{result.resistance.strength_label} · {zone.score:.0f}/100")
        self.assertEqual(context["text"], detail["reason"])
        stats = {stat["label"]: stat["value"] for stat in context["stats"]}
        self.assertEqual(stats["Relationship"], "Below resistance")
        self.assertEqual(stats["Recommended strike"], "$150.00")
        notes = " ".join(context["details"]["notes"])
        self.assertIn("does not guarantee that the stock will stop there", notes)
        self.assertIn("never changes which strike is recommended", notes)

    def test_card_says_when_there_is_none(self):
        flat = pricezones.analyze(bars_through([(0, 100), (260, 160)]), pricezones.RESISTANCE)
        context = tooltip_card(_run(flat))["context"]
        self.assertEqual(context["value"], "None found")
        self.assertIsNone(context["badge"])

    def test_a_single_swing_high_zone_reads_as_one_price(self):
        one = pricezones.PriceZone(pricezones.RESISTANCE, 66.8, 66.8, (66.8,), (TODAY,), 5, 6.0, 0, (), None, None, {}, 60.0)
        analysis = pricezones.ZoneAnalysis(pricezones.RESISTANCE, 252, 2.0, 3.0, {}, (one,), 1.0, ())
        context = resistance_context(analysis, 62.5, 60.0)
        self.assertEqual(context.reason, "Recommended $62.50 is below the $66.80 resistance zone.")

    def test_strength_label(self):
        context = resistance_context(ceiling_analysis(), 151.0, PRICE)
        expected = "Strong" if context.zone.score >= ccexit.STRONG_RESISTANCE_SCORE else "Moderate"
        self.assertEqual(context.strength_label, expected)

    def test_frontend_only_renders_the_card(self):
        # Every CC TO EXIT surface shows the one cc_to_exit_card; the frontend
        # has no resistance math of its own.
        with open(os.path.join(ROOT, "wheel", "static", "app.js"), encoding="utf-8") as handle:
            js = handle.read()
        self.assertIn("setTipCard(cell, row.cc_to_exit_card);", js)
        self.assertIn("card: entry.cc_to_exit_card,", js)
        self.assertNotIn(".resistance", js)
        self.assertIn("if (card.context)", js)
        # The guidance column is plain backend text: no classification here.
        for backend_only in ("just_above", "well_above", "zone_width", "strike_guidance"):
            self.assertNotIn(backend_only, js)
        self.assertIn("row.reason_label", js)


if __name__ == "__main__":
    unittest.main()
