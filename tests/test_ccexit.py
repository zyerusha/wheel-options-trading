"""CC TO EXIT: wheel/ccexit.py's ``calculate_cc_to_exit`` / ``pick_expiration``
/ ``tooltip_card``. Pure, no network: every chain is hand-built. A strike's
implied volatility is solved here so the model delta lands on the value a
test wants, since delta itself is always derived (wheel.optionsmath), never
supplied."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel import ccexit  # noqa: E402
from wheel.ccexit import CallQuote, FeeModel, ShareBlock, calculate_cc_to_exit, pick_expiration, tooltip_card  # noqa: E402
from wheel.tooltipcard import card_text  # noqa: E402
from wheel.optionsmath import black_scholes_greeks  # noqa: E402


def explain(result):
    """The CC TO EXIT tooltip as the reader sees it, flattened to text."""
    return card_text(tooltip_card(result))


TODAY = date(2026, 10, 17)
EXPIRY = date(2026, 11, 20)  # 34 days out
DTE = (EXPIRY - TODAY).days
SPOT = 130.58
RATE = 0.045


def _iv_for_delta(strike: float, delta: float, spot: float = SPOT) -> float:
    """Bisection on implied volatility: a call's delta rises monotonically
    with IV when it's out of the money."""
    low, high = 0.01, 5.0
    for _ in range(200):
        mid = (low + high) / 2
        got = black_scholes_greeks(
            spot=spot, strike=strike, days_to_expiry=DTE, implied_volatility=mid, right="C", risk_free_rate=RATE
        ).delta
        if got < delta:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def _call(strike: float, delta: float, *, bid: float = 2.0, ask: float = 2.2, spot: float = SPOT) -> CallQuote:
    return CallQuote(strike, bid, ask, _iv_for_delta(strike, delta, spot))


# The IRA's JXN today: 300 old shares at the broker's $58.12, 100 just
# assigned at $135, 3 open calls covering 300.
JXN_BLOCKS = [ShareBlock(300, 58.12), ShareBlock(100, 135.0)]


def _run(
    blocks=JXN_BLOCKS, covered=300, calls=(), *, price=SPOT, expiry=EXPIRY, chain_reason=None, chain=True, fees=FeeModel()
):
    return calculate_cc_to_exit(
        fees=fees,
        share_blocks=blocks,
        covered_shares=covered,
        current_price=price,
        expiry=expiry,
        today=TODAY,
        calls=tuple(calls) if chain else None,
        chain_unavailable_reason=chain_reason,
        risk_free_rate=RATE,
    )


class TestJxnScenario(unittest.TestCase):
    """Test 1: 400 shares, 3 open calls, 100 uncovered at $135."""

    def setUp(self):
        self.result = _run(calls=[_call(135, 0.40), _call(140, 0.27), _call(145, 0.17)])

    def test_uncovered_shares_are_the_newest_lot(self):
        self.assertEqual(self.result.shares_held, 400)
        self.assertEqual(self.result.covered_shares, 300)
        self.assertEqual(self.result.uncovered_shares, 100)
        self.assertEqual(self.result.contracts, 1)
        # Not the $77.34 blended cost of all 400 shares.
        self.assertAlmostEqual(self.result.lot_cost, 135.0)

    def test_preferred_minimum_exit_is_138(self):
        self.assertAlmostEqual(self.result.raw_min_exit, 137.70, places=6)
        self.assertEqual(self.result.preferred_min_exit, 138.0)

    def test_recommendation_is_never_below_the_minimum_exit(self):
        self.assertEqual(self.result.status, ccexit.STATUS_RECOMMENDED)
        self.assertGreaterEqual(self.result.strike, 138.0)
        self.assertEqual(self.result.strike, 140.0)

    def test_never_falls_back_to_the_stock_price(self):
        self.assertNotEqual(self.result.strike, 131.0)


class TestDeltaSelection(unittest.TestCase):
    def test_030_delta_strike_wins_when_it_qualifies(self):
        # Test 2: $140 is 0.30 delta and above the $138 minimum.
        result = _run(calls=[_call(135, 0.40), _call(140, 0.30), _call(145, 0.20)])
        self.assertEqual(result.strike, 140.0)
        self.assertAlmostEqual(result.pick.delta, 0.30, places=3)

    def test_030_delta_below_the_minimum_moves_up_to_the_next_qualifying_strike(self):
        # Test 3: $135 is 0.30 delta but under $138, so $140 at 0.22 wins.
        result = _run(calls=[_call(135, 0.30), _call(140, 0.22), _call(145, 0.15)])
        self.assertEqual(result.strike, 140.0)
        self.assertAlmostEqual(result.pick.delta, 0.22, places=3)
        rejected = {c.strike: c.rejected_reason for c in result.candidates}
        self.assertIn("minimum exit", rejected[135.0])

    def test_closest_to_030_among_qualifying_strikes_when_price_is_far_above_cost(self):
        # Cost $100 (minimum $102) with the stock at $130: every strike from
        # $105 qualifies, so the pick is simply the one nearest 0.30 delta.
        blocks = [ShareBlock(100, 100.0)]
        calls = [_call(k, d, spot=130.0) for k, d in [(105, 0.95), (125, 0.65), (135, 0.33), (140, 0.24)]]
        result = _run(blocks, covered=0, calls=calls, price=130.0)
        self.assertEqual(result.strike, 135.0)

    def test_outside_the_preferred_range_is_explained(self):
        result = _run(calls=[_call(135, 0.30), _call(140, 0.22)])
        self.assertIn("no qualifying strike lands inside the preferred 0.25 to 0.35 range", explain(result))

    def test_tie_goes_to_the_lower_strike(self):
        result = _run(calls=[_call(140, 0.33), _call(145, 0.27)])
        self.assertEqual(result.strike, 140.0)


class TestProfitability(unittest.TestCase):
    def test_a_loss_on_assignment_is_rejected_even_above_the_minimum(self):
        # Test 5: these shares already lost $800 buying back an earlier call.
        # $140: stock +$500, premium about +$209, prior -$800 -> a loss.
        blocks = [ShareBlock(100, 135.0, prior_credit_per_share=-8.0)]
        result = _run(blocks, covered=0, calls=[_call(130, 0.55), _call(140, 0.30), _call(145, 0.20)])
        by_strike = {c.strike: c for c in result.candidates}
        self.assertIn("minimum exit", by_strike[130.0].rejected_reason)  # below cost too
        self.assertIn("would lose", by_strike[140.0].rejected_reason)
        self.assertLess(by_strike[140.0].projected_assignment_pnl, 0)
        self.assertEqual(result.strike, 145.0)
        self.assertGreater(result.pick.projected_assignment_pnl, 0)

    def test_assignment_pnl_is_stock_plus_net_premium_plus_prior_credit(self):
        blocks = [ShareBlock(100, 135.0, prior_credit_per_share=3.39)]
        result = _run(blocks, covered=0, calls=[_call(140, 0.30, bid=3.70, ask=3.80)])
        pick = result.pick
        self.assertAlmostEqual(pick.mid_premium, 3.75)
        self.assertAlmostEqual(pick.net_premium, 375.0 - 0.65)
        self.assertAlmostEqual(pick.stock_pnl, 500.0)
        self.assertAlmostEqual(pick.projected_assignment_pnl, 500.0 + 374.35 + 339.0, places=6)

    def test_net_premium_scales_with_contracts(self):
        blocks = [ShareBlock(250, 50.0)]
        result = _run(blocks, covered=0, calls=[_call(55, 0.30, spot=52.0, bid=1.0, ask=1.0)], price=52.0)
        self.assertEqual(result.contracts, 2)
        self.assertAlmostEqual(result.pick.net_premium, 2 * (100.0 - 0.65))
        self.assertAlmostEqual(result.pick.stock_pnl, 5.0 * 200)


class TestProfitabilityInvariant(unittest.TestCase):
    """Whatever the inputs, a recommended strike is at or above the minimum
    exit AND its P&L if assigned (stock + premium after the per-contract fee
    - the fee on selling the shares + these shares' earlier option cash) is
    positive and adds up exactly.
    Earlier option cash can be negative, which is the case the $0.50-rounded
    minimum exit alone cannot catch."""

    def test_every_recommendation_is_profitable(self):
        import random

        rng = random.Random(20261017)
        recommended = 0
        for _ in range(3000):
            cost = rng.uniform(5, 500)
            spot = cost * rng.uniform(0.6, 1.5)
            shares = rng.choice([100, 200, 300, 450])
            covered = rng.choice([0, 100]) if shares > 100 else 0
            prior = rng.uniform(-0.25, 0.10) * cost  # per share, often a loss
            fees = FeeModel(rng.uniform(0.0, 1.0), "test", rng.uniform(0.0, 5e-5), "test")
            blocks = [ShareBlock(shares, cost, prior)]
            calls = []
            for strike in sorted({round(spot * f, 1) for f in (0.9, 1.0, 1.02, 1.05, 1.1, 1.2, 1.35)}):
                bid = max(rng.uniform(-0.2, 0.15) * spot, 0.0)
                calls.append(CallQuote(strike, bid, bid + rng.uniform(0.0, 0.5), rng.uniform(0.15, 1.2)))
            result = calculate_cc_to_exit(
                share_blocks=blocks,
                covered_shares=covered,
                current_price=spot,
                expiry=EXPIRY,
                today=TODAY,
                calls=calls,
                risk_free_rate=RATE,
                fees=fees,
            )
            if result.status != ccexit.STATUS_RECOMMENDED:
                continue
            recommended += 1
            pick = result.pick
            next_shares = result.contracts * 100
            self.assertGreaterEqual(pick.strike, result.preferred_min_exit)
            self.assertGreater(pick.projected_assignment_pnl, 0)
            self.assertAlmostEqual(
                pick.projected_assignment_pnl,
                (pick.strike - cost) * next_shares
                + (pick.mid_premium * 100 - fees.option_fee_per_contract) * result.contracts
                - pick.strike * next_shares * fees.sale_fee_rate
                + prior * next_shares,
                places=6,
            )
            # No rejected strike is ever the pick.
            self.assertIsNone(pick.rejected_reason)
        self.assertGreater(recommended, 500)  # the sweep really exercised the path


class TestFees(unittest.TestCase):
    MEASURED = FeeModel(0.667, "your average over 189 call contracts sold", 20.60e-6, "your latest sale")

    def test_premium_uses_the_measured_per_contract_fee(self):
        blocks = [ShareBlock(100, 135.0)]
        result = _run(blocks, covered=0, calls=[_call(140, 0.30, bid=3.70, ask=3.80)], fees=self.MEASURED)
        self.assertAlmostEqual(result.pick.net_premium, 375.0 - 0.667)

    def test_sale_fee_is_charged_on_the_assignment_proceeds(self):
        blocks = [ShareBlock(100, 135.0)]
        result = _run(blocks, covered=0, calls=[_call(140, 0.30, bid=3.70, ask=3.80)], fees=self.MEASURED)
        pick = result.pick
        self.assertAlmostEqual(pick.sale_fee, 140.0 * 100 * 20.60e-6)  # $0.29
        self.assertAlmostEqual(pick.projected_assignment_pnl, 500.0 + (375.0 - 0.667) - pick.sale_fee)

    def test_a_strike_only_the_sale_fee_pushes_into_a_loss_is_rejected(self):
        # $140 call, $0.01 mid: stock +$500 + premium $0.333 - earlier loss
        # $500.10 = +$0.233 before the sale fee, -$0.055 after it.
        blocks = [ShareBlock(100, 135.0, prior_credit_per_share=-5.001)]
        call = _call(140, 0.30, bid=0.01, ask=0.01)
        without = _run(blocks, covered=0, calls=[call], fees=FeeModel(0.667, "x", 0.0, "x"))
        with_fee = _run(blocks, covered=0, calls=[call], fees=self.MEASURED)
        self.assertEqual(without.strike, 140.0)
        self.assertEqual(with_fee.status, ccexit.STATUS_NO_QUALIFYING)
        self.assertIn("would lose", with_fee.candidates[0].rejected_reason)

    def test_tooltip_names_both_fees_and_where_they_came_from(self):
        text = explain(_run(calls=[_call(140, 0.30)], fees=self.MEASURED))
        self.assertIn("less $0.67 per contract (your average over 189 call contracts sold)", text)
        self.assertIn("$20.60 per $1M sold (your latest sale)", text)
        self.assertIn("Sale fee if assigned: - $0.29", text)


class TestStatuses(unittest.TestCase):
    def test_all_shares_covered(self):
        # Test 4: 400 shares, 4 open calls.
        result = _run(covered=400, calls=[_call(140, 0.30)])
        self.assertEqual(result.status, ccexit.STATUS_ALL_COVERED)
        self.assertIsNone(result.strike)
        self.assertEqual(ccexit.STATUS_LABELS[result.status], "ALL SHARES COVERED")

    def test_no_qualifying_strike(self):
        # Test 8: every listed strike is under the $138 minimum.
        result = _run(calls=[_call(125, 0.65), _call(130, 0.50), _call(135, 0.35)])
        self.assertEqual(result.status, ccexit.STATUS_NO_QUALIFYING)
        self.assertIsNone(result.strike)
        self.assertEqual(ccexit.STATUS_LABELS[result.status], "NO QUALIFYING CC")
        card = tooltip_card(result)
        self.assertEqual(card["status_label"], "DOES NOT QUALIFY")
        self.assertEqual(card["summary"], result.reason)

    def test_no_shares(self):
        result = _run([], covered=0)
        self.assertEqual(result.status, ccexit.STATUS_NO_SHARES)
        self.assertNotIn(result.status, ccexit.STATUS_LABELS)

    def test_fewer_than_100_uncovered(self):
        result = _run([ShareBlock(150, 50.0)], covered=100)
        self.assertEqual(result.status, ccexit.STATUS_NO_QUALIFYING)
        self.assertIn("one call needs 100", result.reason)

    def test_unknown_cost_of_the_uncovered_shares(self):
        result = _run([ShareBlock(100, 50.0), ShareBlock(100, None)], covered=100, calls=[_call(140, 0.30)])
        self.assertEqual(result.status, ccexit.STATUS_NO_QUALIFYING)
        self.assertIn("isn't on file", result.reason)

    def test_unknown_cost_on_shares_already_covered_does_not_matter(self):
        result = _run([ShareBlock(300, None), ShareBlock(100, 135.0)], calls=[_call(140, 0.30)])
        self.assertEqual(result.strike, 140.0)

    def test_chain_unavailable_says_why(self):
        result = _run(chain=False, chain_reason="Option prices for JXN 2026-11-20 couldn't be loaded.")
        self.assertEqual(result.status, ccexit.STATUS_NO_QUALIFYING)
        self.assertIn("couldn't be loaded", result.reason)
        self.assertEqual(result.preferred_min_exit, 138.0)

    def test_no_current_price(self):
        result = _run(calls=[_call(140, 0.30)], price=None)
        self.assertEqual(result.status, ccexit.STATUS_NO_QUALIFYING)
        self.assertIn("No current stock price", result.reason)

    def test_missing_quote_or_iv_is_rejected_not_guessed(self):
        calls = [
            CallQuote(140.0, None, None, 0.3),
            CallQuote(145.0, 1.0, 1.2, None),
        ]
        result = _run(calls=calls)
        self.assertEqual(result.status, ccexit.STATUS_NO_QUALIFYING)
        reasons = {c.strike: c.rejected_reason for c in result.candidates}
        self.assertIn("bid/ask", reasons[140.0])
        self.assertIn("delta", reasons[145.0])


class TestFifoCoverage(unittest.TestCase):
    def test_open_calls_cover_the_oldest_shares_first(self):
        # 100 @ $50 (oldest), 100 @ $60, 100 @ $70; one open call -> the
        # next call covers the $60 lot, not the newest or the average.
        blocks = [ShareBlock(100, 50.0), ShareBlock(100, 60.0), ShareBlock(100, 70.0)]
        result = _run(blocks, covered=100, calls=[_call(65, 0.30, spot=62.0)], price=62.0)
        self.assertEqual(result.contracts, 2)
        self.assertAlmostEqual(result.lot_cost, 65.0)  # (60 + 70) / 2 across the 200 uncovered

    def test_a_partially_covered_lot_is_split(self):
        blocks = [ShareBlock(150, 50.0), ShareBlock(100, 80.0)]
        result = _run(blocks, covered=100, calls=[_call(85, 0.30, spot=80.0)], price=80.0)
        # 150 uncovered: 50 @ $50 + 100 @ $80; one contract takes the oldest 100.
        self.assertEqual(result.contracts, 1)
        self.assertAlmostEqual(result.lot_cost, (50 * 50 + 50 * 80) / 100)


class TestPickExpiration(unittest.TestCase):
    def _dates(self, *days):
        return [TODAY + timedelta(days=d) for d in days]

    def test_closest_to_35_inside_the_window(self):
        self.assertEqual(pick_expiration(self._dates(7, 28, 34, 41, 63), TODAY), TODAY + timedelta(days=34))

    def test_window_beats_a_closer_date_outside_it(self):
        self.assertEqual(pick_expiration(self._dates(29, 45), TODAY), TODAY + timedelta(days=45))

    def test_nearest_to_35_when_nothing_is_in_the_window(self):
        self.assertEqual(pick_expiration(self._dates(7, 14, 63), TODAY), TODAY + timedelta(days=14))

    def test_none_without_a_future_date(self):
        self.assertIsNone(pick_expiration(self._dates(-3, 0), TODAY))


class TestExplanation(unittest.TestCase):
    def test_shows_the_live_math(self):
        text = explain(_run(calls=[_call(135, 0.30), _call(140, 0.22), _call(145, 0.15)]))
        for expected in (
            "Shares held: 400",
            "Covered by open calls: 300",
            "Uncovered: 100",
            "Cost of those shares: $135.00 a share",
            "$135.00 × 1.02 = $137.70, rounded up to $138.00",
            "Target 0.30",
            "$135.00 | 0.30 | $",
            "| FAIL | Rejected\n  Below the $138.00 minimum exit",
            "Recommended call\n$140.00",
        ):
            self.assertIn(expected, text)

    def test_follows_the_ui_punctuation_rules(self):
        text = explain(_run(calls=[_call(135, 0.30), _call(140, 0.22)]))
        for banned in ("—", "--", "−"):
            self.assertNotIn(banned, text)


class TestLotLevelAssignmentPnl(unittest.TestCase):
    """The one lot-level formula: (strike - raw cost) x shares + earlier
    option cash on those shares + new call net premium - sale fee. Earlier
    premium is counted once: as ``prior_credit``, never also netted into
    the cost the stock P&L and minimum exit are measured from."""

    def test_earlier_premium_is_counted_once_against_the_raw_cost(self):
        blocks = [ShareBlock(100, 135.0, prior_credit_per_share=5.0)]
        result = _run(blocks, covered=0, calls=[_call(140, 0.30, bid=3.70, ask=3.80)])
        pick = result.pick
        # Raw cost, not 135 - 5: the minimum exit and stock P&L ignore the credit.
        self.assertAlmostEqual(result.lot_cost, 135.0)
        self.assertEqual(result.preferred_min_exit, 138.0)
        self.assertAlmostEqual(pick.stock_pnl, 500.0)
        self.assertAlmostEqual(result.prior_credit, 500.0)
        self.assertAlmostEqual(pick.projected_assignment_pnl, 500.0 + 500.0 + (375.0 - 0.65), places=6)

    def test_prior_credit_follows_the_fifo_shares_the_call_covers(self):
        # 200 covered (the two oldest blocks); the next call covers the 100
        # newest, so only that block's $2/share counts.
        blocks = [
            ShareBlock(100, 50.0, prior_credit_per_share=9.0),
            ShareBlock(100, 60.0, prior_credit_per_share=7.0),
            ShareBlock(150, 70.0, prior_credit_per_share=2.0),
        ]
        result = _run(blocks, covered=200, calls=[_call(75, 0.30, spot=72.0)], price=72.0)
        self.assertEqual((result.uncovered_shares, result.contracts), (150, 1))
        self.assertAlmostEqual(result.lot_cost, 70.0)
        self.assertAlmostEqual(result.prior_credit, 200.0)

    def test_a_call_spanning_two_blocks_weights_cost_and_credit_by_shares(self):
        blocks = [ShareBlock(150, 40.0, prior_credit_per_share=1.0), ShareBlock(150, 50.0, prior_credit_per_share=3.0)]
        result = _run(blocks, covered=100, calls=[_call(55, 0.30, spot=52.0)], price=52.0)
        # Uncovered: 50 @ $40 and 150 @ $50 -> two contracts cover all 200.
        self.assertEqual(result.contracts, 2)
        self.assertAlmostEqual(result.lot_cost, (50 * 40 + 150 * 50) / 200)
        self.assertAlmostEqual(result.prior_credit, 50 * 1.0 + 150 * 3.0)

    def test_fractional_shares_size_whole_contracts_only(self):
        result = _run([ShareBlock(151.806, 50.0)], covered=0, calls=[_call(55, 0.30, spot=52.0)], price=52.0)
        self.assertAlmostEqual(result.uncovered_shares, 151.806)
        self.assertEqual(result.contracts, 1)
        self.assertAlmostEqual(result.pick.stock_pnl, 5.0 * 100)


class TestRejectionReasonsMatchTheFilter(unittest.TestCase):
    def test_every_strike_below_the_minimum_exit(self):
        result = _run([ShareBlock(100, 135.0)], covered=0, calls=[_call(130, 0.55), _call(135, 0.40)])
        self.assertEqual(result.status, ccexit.STATUS_NO_QUALIFYING)
        self.assertEqual(result.reason, "Every listed call strike is below the $138.00 minimum exit.")

    def test_losses_above_the_minimum_are_named_as_losses(self):
        blocks = [ShareBlock(100, 135.0, prior_credit_per_share=-20.0)]
        result = _run(blocks, covered=0, calls=[_call(140, 0.30), _call(145, 0.20)])
        self.assertEqual(result.status, ccexit.STATUS_NO_QUALIFYING)
        self.assertIn("2 would lose money if assigned", result.reason)

    def test_mixed_rejections_are_counted_separately(self):
        no_iv = CallQuote(165, 2.0, 2.2, None)  # profitable, so the delta check is what fails
        no_quote = CallQuote(150, None, None, 0.4)
        blocks = [ShareBlock(100, 135.0, prior_credit_per_share=-20.0)]
        result = _run(blocks, covered=0, calls=[_call(140, 0.30), no_iv, no_quote])
        self.assertIn("1 would lose money if assigned", result.reason)
        self.assertIn("1 no bid/ask quote", result.reason)
        self.assertIn("1 no implied volatility, so no delta", result.reason)

    def test_no_strikes_on_the_chain(self):
        result = _run([ShareBlock(100, 135.0)], covered=0, calls=[])
        self.assertEqual(result.reason, "The option chain lists no call strikes.")


class TestTooltipCard(unittest.TestCase):
    def test_statuses(self):
        self.assertEqual(tooltip_card(_run(calls=[_call(140, 0.30)]))["status_label"], "QUALIFIES")
        covered = tooltip_card(_run(covered=400, calls=[_call(140, 0.30)]))
        self.assertEqual((covered["status"], covered["status_label"]), ("info", "ALL SHARES COVERED"))
        self.assertIsNone(tooltip_card(_run([], covered=0)))
        # Missing inputs can't be checked: UNVERIFIED, not a rule failure.
        self.assertEqual(tooltip_card(_run(calls=[_call(140, 0.30)], price=None))["status_label"], "UNVERIFIED")
        self.assertEqual(tooltip_card(_run(chain=False, chain_reason="down"))["status_label"], "UNVERIFIED")
        unknown_cost = _run([ShareBlock(100, 50.0), ShareBlock(100, None)], covered=100, calls=[_call(140, 0.30)])
        self.assertEqual(tooltip_card(unknown_cost)["status_label"], "UNVERIFIED")
        # Fewer than 100 uncovered shares is a rule outcome, not missing data.
        self.assertEqual(tooltip_card(_run([ShareBlock(150, 50.0)], covered=100))["status_label"], "DOES NOT QUALIFY")

    def test_key_numbers_match_the_result(self):
        fees = FeeModel(0.667, "measured", 20.60e-6, "latest sale")
        result = _run([ShareBlock(200, 135.0)], covered=0, calls=[_call(140, 0.30, bid=3.70, ask=3.80)], fees=fees)
        card = tooltip_card(result, "JXN")
        metrics = {m["label"]: m["value"] for m in card["metrics"]}
        # Per contract = mid x 100 - fee = 375 - 0.667; the call is 2 contracts.
        self.assertAlmostEqual(result.pick.net_premium, 2 * (3.75 * 100 - 0.667))
        self.assertEqual(metrics["Net premium / contract"], "$374.33")
        self.assertEqual(metrics["P&L if assigned"], f"${result.pick.projected_assignment_pnl:,.2f}")
        self.assertEqual(metrics["Minimum exit strike"], "$138.00")
        self.assertEqual(card["headline"]["value"], "$140.00")
        self.assertEqual(card["ticker"], "JXN")
        self.assertLessEqual(len(card["why"]), 3)
        self.assertIn(f"{DTE} DTE from Oct 17", card["subtitle"])

    def test_risk_section_says_the_minimum_is_a_floor_not_advice(self):
        text = explain(_run(calls=[_call(140, 0.30)]))
        self.assertIn("is a floor on the strike, not advice that selling there is the best move", text)
        self.assertIn("newest uncovered shares are sold at $140.00", text)


class TestExpirationWindowIsVisible(unittest.TestCase):
    def test_outside_the_window_is_flagged(self):
        far = TODAY + timedelta(days=60)
        result = _run([ShareBlock(100, 135.0)], covered=0, calls=[], expiry=far)
        self.assertIs(result.expiry_in_window, False)
        self.assertIn("none listed 30 to 45 days out", explain(result))

    def test_inside_the_window(self):
        self.assertIs(_run(calls=[_call(140, 0.30)]).expiry_in_window, True)


class TestPayloadKeepsTheThreePnlFiguresApart(unittest.TestCase):
    def test_detail_fields(self):
        from wheel.api import _cc_exit_payload

        blocks = [ShareBlock(100, 135.0, prior_credit_per_share=3.39)]
        fees = FeeModel(0.667, "measured", 20.60e-6, "latest sale")
        detail = _cc_exit_payload(_run(blocks, covered=0, calls=[_call(140, 0.30, bid=3.70, ask=3.80)], fees=fees))[
            "cc_to_exit_detail"
        ]
        self.assertEqual(detail["stock_pnl_if_assigned"], 500.0)
        self.assertEqual(detail["prior_option_credit"], 339.0)
        self.assertEqual(detail["sale_fee_if_assigned"], round(140 * 100 * 20.60e-6, 2))
        self.assertEqual(
            detail["projected_assignment_pnl"], round(500.0 + 339.0 + (375.0 - 0.667) - 140 * 100 * 20.60e-6, 2)
        )
        self.assertEqual((detail["option_fee_per_contract"], detail["sale_fee_rate_per_million"]), (0.667, 20.6))
        self.assertIs(detail["expiry_in_window"], True)


class TestMeasuredFeeModel(unittest.TestCase):
    def _fees(self, transactions):
        from wheel.api import Dashboard

        dashboard = Dashboard.__new__(Dashboard)
        dashboard.transactions = transactions
        return Dashboard._cc_exit_fee_model(dashboard)

    def test_latest_sale_rate_and_average_call_fee(self):
        from tests.test_engine import tx
        from wheel.parser import SELL_STOCK, STO

        fees = self._fees(
            [
                tx("2026-01-05", STO, "-XYZ260220C55", -2, 1.00, 198.66, commission=1.30, fees=0.04),
                tx("2026-03-02", SELL_STOCK, "XYZ", -100, 50.0, 4999.0, fees=0.20),
                tx("2026-06-01", SELL_STOCK, "XYZ", -100, 60.0, 5999.0, fees=0.12),
            ]
        )
        self.assertAlmostEqual(fees.option_fee_per_contract, 1.34 / 2)
        self.assertAlmostEqual(fees.sale_fee_rate, 0.12 / 6000.0)
        self.assertIn("Jun 1, 2026", fees.sale_fee_source)

    def test_a_zero_fee_sale_is_stated_not_silent(self):
        from tests.test_engine import tx
        from wheel.parser import SELL_STOCK

        fees = self._fees([tx("2026-06-01", SELL_STOCK, "XYZ", -100, 60.0, 6000.0)])
        self.assertEqual(fees.sale_fee_rate, 0.0)
        self.assertIn("showed no fee, so none is counted", fees.sale_fee_source)

    def test_no_history_uses_the_named_fallbacks(self):
        fees = self._fees([])
        self.assertEqual(fees.option_fee_per_contract, ccexit.OPTION_FEE_PER_CONTRACT)
        self.assertEqual(fees.sale_fee_rate, ccexit.SALE_FEE_RATE_FALLBACK)
        self.assertEqual(fees.sale_fee_source, ccexit.SALE_FEE_FALLBACK_SOURCE)


if __name__ == "__main__":
    unittest.main()
