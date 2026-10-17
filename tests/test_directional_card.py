"""Directional (LC/LP) Trade Log card: wheel/api.py's
``_directional_leg_row`` / ``_build_directional_card`` (the per-leg and
per-position math), and the ``Cycle.kind``-based card-selection wiring in
``Dashboard._build_trade_log`` (a directional cycle gets a card, a wheel
cycle's own fields stay untouched).

No test here makes a real network call -- every chain quote is a
hand-built ``OptionContractQuote``, matching the convention
``tests/test_marketdata.py`` already documents for this module.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_engine import tx  # noqa: E402
from wheel.api import Dashboard, _build_directional_card  # noqa: E402
from wheel.engine import build_cycles  # noqa: E402
from wheel.marketdata import OptionContractQuote  # noqa: E402
from wheel.parser import ASSIGNED, BTO, BUY_STOCK, EXPIRED, STC, STO  # noqa: E402


def _quote(strike: float, right: str, *, iv: float | None = 0.30, symbol: str = "X") -> OptionContractQuote:
    return OptionContractQuote(
        contract_symbol=symbol,
        strike=strike,
        right=right,
        bid=1.0,
        ask=1.2,
        last_price=1.1,
        last_trade_date=None,
        implied_volatility=iv,
        in_the_money=None,
    )


def _one_cycle(transactions):
    cycles, _ = build_cycles(transactions)
    assert len(cycles) == 1, f"expected exactly one cycle, got {len(cycles)}"
    return cycles[0]


def _card(cycle, *, through=date(2026, 1, 1), current_price=180.0, option_mark_prices=None, chain_quotes=None,
          account_total_value=None):
    return _build_directional_card(
        cycle,
        through,
        current_price=current_price,
        option_mark_prices=option_mark_prices or {},
        chain_quotes=chain_quotes or {},
        account_total_value=account_total_value,
    )


class TestLoneDitmLongCall(unittest.TestCase):
    """A single deep-in-the-money long call, the "synthetic stock" case."""

    def setUp(self):
        self.cycle = _one_cycle([tx("2025-12-01", BTO, "AAPL260619C100", 1, 60.0, -6000.0, row_id=1)])
        self.leg = self.cycle.legs[0]
        self.quotes = {("AAPL", self.leg.expiry, 100.0, "C"): _quote(100.0, "C", iv=0.28)}

    def test_classified_directional_not_wheel(self):
        self.assertEqual(self.cycle.kind, "directional")
        self.assertFalse(self.cycle.is_wheel)

    def test_cost_basis_and_expiration_breakeven(self):
        row = _card(self.cycle, chain_quotes=self.quotes)["legs"][0]
        self.assertEqual(row["type_code"], "LC")
        self.assertEqual(row["cost_basis_open"], 6000.0)
        self.assertEqual(row["cost_per_contract"], 6000.0)
        self.assertEqual(row["cost_per_share"], 60.0)
        # Call breakeven at expiry = strike + cost/share = 100 + 60.
        self.assertEqual(row["breakeven_price"], 160.0)
        self.assertAlmostEqual(row["distance_to_breakeven"], 20.0, places=2)  # 180 - 160

    def test_delta_is_high_and_positive_for_a_deep_itm_call(self):
        row = _card(self.cycle, chain_quotes=self.quotes)["legs"][0]
        self.assertGreater(row["delta"], 0.9)
        self.assertAlmostEqual(row["delta_adjusted_shares"], round(row["delta"] * 100, 2))
        self.assertGreater(row["delta_adjusted_dollars"], 0)

    def test_max_loss_equals_capital_committed_for_a_standalone_long(self):
        card = _card(self.cycle, chain_quotes=self.quotes)
        self.assertEqual(card["max_loss_remaining"], card["capital_committed"])
        self.assertEqual(card["max_loss_remaining"], 6000.0)

    def test_intrinsic_and_extrinsic_split_without_a_market_quote(self):
        # No broker Positions mark -> no market value -> no extrinsic split,
        # but intrinsic is still computable from the stock price alone.
        row = _card(self.cycle, chain_quotes=self.quotes)["legs"][0]
        self.assertAlmostEqual(row["intrinsic_value"], 8000.0, places=2)  # (180-100)*100
        self.assertIsNone(row["extrinsic_value"])

    def test_extrinsic_value_when_a_market_quote_is_available(self):
        row = _card(
            self.cycle, chain_quotes=self.quotes, option_mark_prices={self.leg.occ_symbol: 85.0}
        )["legs"][0]
        self.assertEqual(row["market_value"], 8500.0)
        self.assertTrue(row["market_value_is_live_mark"])
        self.assertAlmostEqual(row["intrinsic_value"], 8000.0, places=2)
        self.assertAlmostEqual(row["extrinsic_value"], 500.0, places=2)


class TestLoneDitmLongPutHedge(unittest.TestCase):
    """A single deep-in-the-money long put, the protective-hedge case."""

    def setUp(self):
        self.cycle = _one_cycle([tx("2025-12-01", BTO, "SPY260619P500", 1, 90.0, -9000.0, row_id=1)])
        self.leg = self.cycle.legs[0]
        self.quotes = {("SPY", self.leg.expiry, 500.0, "P"): _quote(500.0, "P", iv=0.22)}

    def test_classified_directional_not_wheel(self):
        self.assertEqual(self.cycle.kind, "directional")

    def test_put_breakeven_is_strike_minus_cost(self):
        row = _card(self.cycle, current_price=420.0, chain_quotes=self.quotes)["legs"][0]
        self.assertEqual(row["type_code"], "LP")
        # Put breakeven at expiry = strike - cost/share = 500 - 90.
        self.assertEqual(row["breakeven_price"], 410.0)

    def test_delta_is_strongly_negative_for_a_deep_itm_put(self):
        row = _card(self.cycle, current_price=420.0, chain_quotes=self.quotes)["legs"][0]
        self.assertLess(row["delta"], -0.8)
        self.assertLess(row["delta_adjusted_shares"], 0)
        self.assertLess(row["delta_adjusted_dollars"], 0)

    def test_negative_delta_never_relabeled_as_positive_stock_exposure(self):
        # A protective put's delta-adjusted shares must stay negative -- it
        # is downside exposure, never reported as owning more stock.
        card = _card(self.cycle, current_price=420.0, chain_quotes=self.quotes)
        self.assertLess(card["net_delta_adjusted_shares"], 0)
        self.assertEqual(card["position_type"], "LP")


class TestMultipleContracts(unittest.TestCase):
    def test_two_contracts_scale_cost_and_exposure_linearly(self):
        cycle = _one_cycle([tx("2025-12-01", BTO, "AAPL260619C100", 2, 60.0, -12000.0, row_id=1)])
        leg = cycle.legs[0]
        quotes = {("AAPL", leg.expiry, 100.0, "C"): _quote(100.0, "C", iv=0.28)}
        row = _card(cycle, chain_quotes=quotes)["legs"][0]
        self.assertEqual(row["contracts_open"], 2)
        self.assertEqual(row["cost_basis_open"], 12000.0)
        self.assertEqual(row["cost_per_contract"], 6000.0)  # per-contract cost unchanged by quantity
        self.assertAlmostEqual(row["delta_adjusted_shares"], round(row["delta"] * 200, 2))


class TestMultipleFillsAtDifferentPrices(unittest.TestCase):
    def test_two_separate_buys_stay_as_two_legs_never_averaged(self):
        transactions = [
            tx("2025-12-01", BTO, "AAPL260619C100", 1, 60.0, -6000.0, row_id=1),
            tx("2025-12-10", BTO, "AAPL260619C100", 1, 65.0, -6500.0, row_id=2),
        ]
        cycle = _one_cycle(transactions)
        self.assertEqual(len(cycle.legs), 2, "each opening fill is its own leg, never merged")
        quotes = {("AAPL", cycle.legs[0].expiry, 100.0, "C"): _quote(100.0, "C", iv=0.28)}
        card = _card(cycle, chain_quotes=quotes)
        self.assertEqual(card["open_leg_count"], 2)
        self.assertEqual(card["position_type"], "LC")  # same type code, still reported as one kind
        self.assertEqual(card["total_cost_basis_open"], 12500.0)  # 6000 + 6500, summed not averaged
        self.assertEqual(sorted(leg["cost_per_share"] for leg in card["legs"]), [60.0, 65.0])
        # Two fills of the same contract still get one whole-position
        # breakeven -- the cost-weighted average, exact since both share
        # the same strike/expiration (see TestMultiLegBreakevenNeverBlended).
        self.assertAlmostEqual(card["position_breakeven"], 162.5, places=2)


class TestPartialClose(unittest.TestCase):
    def test_partial_close_splits_realized_from_still_open(self):
        transactions = [
            tx("2025-12-01", BTO, "AAPL260619C100", 2, 60.0, -12000.0, row_id=1),
            # Sells 1 of the 2 contracts at $70/share; net cash already has
            # the $5 closing fee subtracted (Transaction.amount is fee-net).
            tx("2025-12-15", STC, "AAPL260619C100", 1, 70.0, 6995.0, row_id=2, fees=5.0),
        ]
        cycle = _one_cycle(transactions)
        leg = cycle.legs[0]
        self.assertEqual(leg.remaining_contracts, 1)
        quotes = {("AAPL", leg.expiry, 100.0, "C"): _quote(100.0, "C", iv=0.28)}
        row = _card(cycle, chain_quotes=quotes)["legs"][0]
        # Realized = cash_per_contract(-6000) * 1 closed + 6995 received = 995.
        self.assertAlmostEqual(row["realized_pl"], 995.0, places=2)
        self.assertEqual(row["contracts_closed"], 1)
        self.assertEqual(row["contracts_open"], 1)
        self.assertEqual(row["cost_basis_open"], 6000.0)  # only the still-open contract's cost
        self.assertEqual(row["is_open"], True)


class TestMissingOrStaleDelta(unittest.TestCase):
    def setUp(self):
        self.cycle = _one_cycle([tx("2025-12-01", BTO, "AAPL260619C100", 1, 60.0, -6000.0, row_id=1)])
        self.leg = self.cycle.legs[0]

    def test_quote_present_but_implied_volatility_missing(self):
        quotes = {("AAPL", self.leg.expiry, 100.0, "C"): _quote(100.0, "C", iv=None)}
        row = _card(self.cycle, chain_quotes=quotes)["legs"][0]
        self.assertIsNone(row["delta"])
        self.assertEqual(row["greeks_unavailable_reason"], "implied volatility unavailable")
        # Never a zero or a carried-forward guess -- every Greek stays unset together.
        self.assertIsNone(row["gamma"])
        self.assertIsNone(row["theta_per_day"])
        self.assertIsNone(row["vega"])

    def test_no_quote_at_all_for_this_strike(self):
        row = _card(self.cycle, chain_quotes={})["legs"][0]
        self.assertIsNone(row["delta"])
        self.assertEqual(row["greeks_unavailable_reason"], "implied volatility unavailable")

    def test_no_current_stock_price_leaves_delta_unavailable(self):
        quotes = {("AAPL", self.leg.expiry, 100.0, "C"): _quote(100.0, "C", iv=0.3)}
        row = _card(self.cycle, current_price=None, chain_quotes=quotes)["legs"][0]
        self.assertIsNone(row["delta"])
        self.assertEqual(row["greeks_unavailable_reason"], "no current stock price")

    def test_partial_delta_on_a_multi_leg_position_is_flagged_not_hidden(self):
        transactions = [
            tx("2025-12-01", BTO, "AAPL260619C100", 1, 60.0, -6000.0, row_id=1),
            tx("2025-12-01", BTO, "AAPL260815C120", 1, 40.0, -4000.0, row_id=2),
        ]
        cycle = _one_cycle(transactions)
        legs_by_strike = {leg.strike: leg for leg in cycle.legs}
        # Only the 100-strike leg gets a usable quote; the 120-strike one has none.
        quotes = {("AAPL", legs_by_strike[100.0].expiry, 100.0, "C"): _quote(100.0, "C", iv=0.28)}
        card = _card(cycle, chain_quotes=quotes)
        self.assertFalse(card["net_delta_known_for_all_open_legs"])
        # The known leg's exposure still counts -- it is a real, partial number.
        self.assertIsNotNone(card["net_delta_adjusted_shares"])
        known_leg = next(leg for leg in card["legs"] if leg["strike"] == 100.0)
        self.assertAlmostEqual(card["net_delta_adjusted_shares"], known_leg["delta_adjusted_shares"])


class TestMissingOptionMarketQuote(unittest.TestCase):
    def test_no_broker_mark_falls_back_to_conservative_expires_worthless_estimate(self):
        cycle = _one_cycle([tx("2025-12-01", BTO, "AAPL260619C100", 1, 60.0, -6000.0, row_id=1)])
        row = _card(cycle, option_mark_prices={}, chain_quotes={})["legs"][0]
        self.assertIsNone(row["market_value"])
        self.assertFalse(row["market_value_is_live_mark"])
        self.assertEqual(row["unrealized_pl"], -6000.0)  # the open_premium fallback, never invented


class TestExpiredOption(unittest.TestCase):
    def test_expired_worthless_long_call_has_no_greeks_and_locks_in_the_full_loss(self):
        transactions = [
            tx("2025-01-01", BTO, "AAPL250117C100", 1, 60.0, -6000.0, row_id=1),
            tx("2025-01-17", EXPIRED, "AAPL250117C100", -1, None, 0.0, row_id=2, as_of="2025-01-17"),
        ]
        cycle = _one_cycle(transactions)
        leg = cycle.legs[0]
        self.assertFalse(leg.is_open)
        row = _card(cycle, current_price=50.0, chain_quotes={})["legs"][0]
        self.assertEqual(row["realized_pl"], -6000.0)
        self.assertIsNone(row["unrealized_pl"])  # nothing left open to mark
        self.assertIsNone(row["delta"])
        self.assertEqual(row["greeks_unavailable_reason"], "position is closed")
        self.assertIsNone(row["market_value"])
        self.assertEqual(row["capital_committed"], 0.0)


class TestClosedPosition(unittest.TestCase):
    """A fully closed multi-leg position: the totals a closed card shows
    (everything paid, P&L, return on that, open/close dates) reconcile with
    the per-leg rows, and no open-only figure is left non-zero."""

    def setUp(self):
        transactions = [
            tx("2025-01-02", BTO, "AAPL250620P170", 1, 10.0, -1000.0, row_id=1),
            tx("2025-02-03", BTO, "AAPL250620P125", 1, 4.0, -400.0, row_id=2),
            tx("2025-03-04", STC, "AAPL250620P170", -1, 21.0, 2100.0, row_id=3),
            tx("2025-04-05", STC, "AAPL250620P125", -1, 0.5, 50.0, row_id=4),
        ]
        self.card = _card(_one_cycle(transactions), through=date(2025, 5, 1))

    def test_status_and_dates(self):
        self.assertEqual(self.card["status"], "CLOSED")
        self.assertEqual(self.card["first_open_date"], "2025-01-02")
        self.assertEqual(self.card["last_close_date"], "2025-04-05")
        self.assertEqual([leg["close_date"] for leg in self.card["legs"]], ["2025-03-04", "2025-04-05"])

    def test_return_on_cost_is_total_pl_over_everything_paid(self):
        self.assertEqual(self.card["total_cost_basis_all"], 1400.0)
        self.assertEqual(self.card["total_pl"], 750.0)  # +1100 - 350
        self.assertAlmostEqual(self.card["total_return_on_cost_pct"], round(100.0 * 750.0 / 1400.0, 2))

    def test_nothing_open_is_left_over(self):
        self.assertEqual(self.card["total_cost_basis_open"], 0.0)
        self.assertEqual(self.card["capital_committed"], 0.0)
        self.assertEqual(self.card["max_loss_remaining"], 0.0)
        self.assertIsNone(self.card["total_unrealized_pl"])

    def test_open_position_has_no_close_date(self):
        card = _card(_one_cycle([tx("2025-12-01", BTO, "AAPL260619C100", 1, 60.0, -6000.0, row_id=1)]))
        self.assertIsNone(card["last_close_date"])
        self.assertIsNone(card["legs"][0]["close_date"])


class TestLiquidityFields(unittest.TestCase):
    """bid/ask/spread reuse the same chain quote already fetched for
    implied volatility -- no new network call, no new fetch path."""

    def test_bid_ask_and_spread_from_the_same_chain_quote(self):
        cycle = _one_cycle([tx("2025-12-01", BTO, "AAPL260619C100", 1, 60.0, -6000.0, row_id=1)])
        leg = cycle.legs[0]
        quotes = {("AAPL", leg.expiry, 100.0, "C"): OptionContractQuote(
            contract_symbol="X", strike=100.0, right="C", bid=80.0, ask=82.0, last_price=81.0,
            last_trade_date=None, implied_volatility=0.28, in_the_money=True,
        )}
        row = _card(cycle, chain_quotes=quotes)["legs"][0]
        self.assertEqual(row["bid"], 80.0)
        self.assertEqual(row["ask"], 82.0)
        self.assertAlmostEqual(row["bid_ask_spread_pct"], 100.0 * 2.0 / 82.0, places=1)

    def test_no_quote_leaves_bid_ask_none(self):
        cycle = _one_cycle([tx("2025-12-01", BTO, "AAPL260619C100", 1, 60.0, -6000.0, row_id=1)])
        row = _card(cycle, chain_quotes={})["legs"][0]
        self.assertIsNone(row["bid"])
        self.assertIsNone(row["ask"])
        self.assertIsNone(row["bid_ask_spread_pct"])


class TestExposureToCapitalRatio(unittest.TestCase):
    """Exposure/capital is a magnitude: how much stock-like movement each
    dollar paid in controls, regardless of whether that movement is
    bullish (call) or bearish (put)."""

    def test_is_positive_for_a_long_call(self):
        cycle = _one_cycle([tx("2025-12-01", BTO, "AAPL260619C100", 1, 60.0, -6000.0, row_id=1)])
        leg = cycle.legs[0]
        quotes = {("AAPL", leg.expiry, 100.0, "C"): _quote(100.0, "C", iv=0.28)}
        card = _card(cycle, chain_quotes=quotes)
        self.assertGreater(card["exposure_to_capital_ratio"], 0)

    def test_is_positive_for_a_long_put_even_though_delta_adjusted_dollars_is_negative(self):
        cycle = _one_cycle([tx("2025-12-01", BTO, "SPY260619P500", 1, 90.0, -9000.0, row_id=1)])
        leg = cycle.legs[0]
        quotes = {("SPY", leg.expiry, 500.0, "P"): _quote(500.0, "P", iv=0.22)}
        card = _card(cycle, current_price=420.0, chain_quotes=quotes)
        self.assertLess(card["net_delta_adjusted_dollars"], 0)
        self.assertGreater(card["exposure_to_capital_ratio"], 0)
        self.assertAlmostEqual(
            card["exposure_to_capital_ratio"],
            abs(card["net_delta_adjusted_dollars"]) / card["capital_committed"],
            places=4,
        )

    def test_none_when_delta_is_unavailable(self):
        cycle = _one_cycle([tx("2025-12-01", BTO, "AAPL260619C100", 1, 60.0, -6000.0, row_id=1)])
        card = _card(cycle, chain_quotes={})
        self.assertIsNone(card["net_delta_adjusted_dollars"])
        self.assertIsNone(card["exposure_to_capital_ratio"])


class TestReturnOnCost(unittest.TestCase):
    def test_return_on_cost_equals_unrealized_pl_over_cost_basis(self):
        cycle = _one_cycle([tx("2025-12-01", BTO, "AAPL260619C100", 1, 60.0, -6000.0, row_id=1)])
        leg = cycle.legs[0]
        quotes = {("AAPL", leg.expiry, 100.0, "C"): _quote(100.0, "C", iv=0.28)}
        card = _card(cycle, chain_quotes=quotes, option_mark_prices={leg.occ_symbol: 55.0})
        # Market value 5500, cost basis 6000 -> unrealized -500, -8.33% of cost.
        self.assertAlmostEqual(card["total_unrealized_pl"], -500.0, places=2)
        self.assertAlmostEqual(card["total_unrealized_pl_pct"], -8.33, places=2)
        self.assertAlmostEqual(
            card["total_unrealized_pl_pct"], 100.0 * card["total_unrealized_pl"] / card["total_cost_basis_open"],
            places=2,
        )


class TestMultiLegBreakevenNeverBlended(unittest.TestCase):
    def test_each_open_leg_keeps_its_own_breakeven_when_costs_differ(self):
        transactions = [
            tx("2025-12-01", BTO, "AAPL260619C100", 1, 60.0, -6000.0, row_id=1),
            tx("2025-12-10", BTO, "AAPL260619C100", 1, 65.0, -6500.0, row_id=2),
        ]
        cycle = _one_cycle(transactions)
        quotes = {("AAPL", cycle.legs[0].expiry, 100.0, "C"): _quote(100.0, "C", iv=0.28)}
        card = _card(cycle, chain_quotes=quotes)
        # Per-leg rows (the per-leg contract table) never blend the two
        # fills' own costs together.
        breakevens = sorted(leg["breakeven_price"] for leg in card["legs"])
        self.assertEqual(breakevens, [160.0, 165.0])  # 100+60 and 100+65, never averaged to 162.5
        # The whole-position breakeven IS the cost-weighted figure -- exact,
        # not an approximation, since both legs share the same strike and
        # expiration (just two fills at different prices of the same
        # contract): (6000+6500)/(100+100) = 62.5/share -> 100+62.5.
        self.assertAlmostEqual(card["position_breakeven"], 162.5, places=2)

    def test_position_breakeven_is_none_for_genuinely_different_strikes_or_expirations(self):
        transactions = [
            tx("2025-12-01", BTO, "AAPL260619C100", 1, 60.0, -6000.0, row_id=1),
            tx("2025-12-01", BTO, "AAPL260815C120", 1, 40.0, -4000.0, row_id=2),
        ]
        cycle = _one_cycle(transactions)
        card = _card(cycle, chain_quotes={})
        self.assertIsNone(card["position_breakeven"])

    def test_position_breakeven_is_none_for_a_mixed_call_and_put_position(self):
        transactions = [
            tx("2025-12-01", BTO, "AAPL260619C100", 1, 60.0, -6000.0, row_id=1),
            tx("2025-12-01", BTO, "AAPL260619P90", 1, 50.0, -5000.0, row_id=2),
        ]
        cycle = _one_cycle(transactions)
        card = _card(cycle, chain_quotes={})
        self.assertIsNone(card["position_breakeven"])

    def test_exact_root_across_two_different_strikes_of_the_same_right_and_expiration(self):
        # Two calls, same expiration, different strikes: the combined
        # payoff crosses zero at exactly one point, solved exactly (not a
        # weighted average of the two strikes).
        transactions = [
            tx("2025-12-01", BTO, "AAPL260619C100", 1, 60.0, -6000.0, row_id=1),
            tx("2025-12-01", BTO, "AAPL260619C105", 1, 55.0, -5500.0, row_id=2),
        ]
        cycle = _one_cycle(transactions)
        card = _card(cycle, chain_quotes={})
        # 100*(S-100) + 100*(S-105) = 11500 -> 200S - 20500 = 11500 -> S=160.
        self.assertAlmostEqual(card["position_breakeven"], 160.0, places=2)


class TestTotalsReconcileWithLegs(unittest.TestCase):
    """Validation: every card-level total must equal the sum of the
    corresponding per-leg figures -- never a figure computed a second,
    independent way that could silently drift from the leg table."""

    def _two_leg_card(self):
        transactions = [
            tx("2025-12-01", BTO, "AAPL260619C100", 1, 60.0, -6000.0, row_id=1),
            tx("2025-12-01", BTO, "AAPL260619C105", 1, 55.0, -5500.0, row_id=2),
        ]
        cycle = _one_cycle(transactions)
        quotes = {
            ("AAPL", cycle.legs[0].expiry, 100.0, "C"): _quote(100.0, "C", iv=0.28),
            ("AAPL", cycle.legs[1].expiry, 105.0, "C"): _quote(105.0, "C", iv=0.30),
        }
        marks = {cycle.legs[0].occ_symbol: 70.0, cycle.legs[1].occ_symbol: 62.0}
        return _card(cycle, chain_quotes=quotes, option_mark_prices=marks, account_total_value=500000.0)

    def test_cost_basis_reconciles(self):
        card = self._two_leg_card()
        self.assertAlmostEqual(
            card["total_cost_basis_open"], sum(leg["cost_basis_open"] for leg in card["legs"]), places=2
        )

    def test_market_value_reconciles(self):
        card = self._two_leg_card()
        self.assertAlmostEqual(
            card["total_market_value"], sum(leg["market_value"] for leg in card["legs"]), places=2
        )

    def test_pl_reconciles_and_never_double_counts(self):
        card = self._two_leg_card()
        self.assertAlmostEqual(
            card["total_unrealized_pl"], sum(leg["unrealized_pl"] for leg in card["legs"]), places=2
        )
        self.assertAlmostEqual(
            card["total_realized_pl"], sum(leg["realized_pl"] for leg in card["legs"]), places=2
        )
        self.assertAlmostEqual(card["total_pl"], card["total_realized_pl"] + card["total_unrealized_pl"], places=2)
        self.assertAlmostEqual(card["total_pl"], sum(leg["total_pl"] for leg in card["legs"]), places=2)

    def test_delta_exposure_and_capital_reconcile(self):
        card = self._two_leg_card()
        self.assertAlmostEqual(
            card["net_delta_adjusted_dollars"], sum(leg["delta_adjusted_dollars"] for leg in card["legs"]), places=1
        )
        self.assertAlmostEqual(
            card["capital_committed"], sum(leg["capital_committed"] for leg in card["legs"]), places=2
        )
        self.assertAlmostEqual(
            card["exposure_to_capital_ratio"],
            abs(card["net_delta_adjusted_dollars"]) / card["capital_committed"],
            places=4,
        )

    def test_max_loss_excludes_nothing_closed(self):
        # Both legs here are fully open, so max loss is the full cost basis;
        # a closed leg must never contribute (see TestExpiredOption/TestPartialClose).
        card = self._two_leg_card()
        self.assertEqual(card["max_loss_remaining"], card["total_cost_basis_open"])


class TestWheelVsDirectionalCardSelection(unittest.TestCase):
    """End-to-end through Dashboard._build_trade_log: a real wheel keeps its
    existing card untouched, and only the directional cycle gets the new one.
    Reuses test_metrics.py's proven mixed-book fixture so the wheel side is
    already known to reconcile correctly.
    """

    def _dashboard(self, transactions):
        dashboard = Dashboard.__new__(Dashboard)
        dashboard.transactions = transactions
        dashboard.all_cycles, dashboard.engine = build_cycles(transactions)
        dashboard._company_names = {}
        dashboard.snapshots = []
        dashboard._cycle_metrics_cache = {}
        # No real share counts: every wheel's shares are the ones its own lots trace.
        dashboard._net_worth = {"available": False}
        return dashboard

    def test_wheel_card_is_unchanged_directional_card_is_added(self):
        transactions = [
            tx("2025-01-02", STO, "-MU250117P100", -1, 3.0, 300.0, row_id=1),
            tx("2025-01-17", ASSIGNED, "-MU250117P100", 1, None, 0.0, row_id=2, as_of="2025-01-17"),
            tx("2025-01-20", STO, "-MU250221C110", -1, 2.0, 200.0, row_id=3),
            tx("2025-02-21", ASSIGNED, "-MU250221C110", 1, None, 0.0, row_id=4, as_of="2025-02-21"),
            tx("2025-03-03", BTO, "-NVDA250307C130", 4, 1.0, -400.0, row_id=5),
            tx("2025-03-07", EXPIRED, "-NVDA250307C130", -4, None, 0.0, row_id=6, as_of="2025-03-07"),
        ]
        dashboard = self._dashboard(transactions)
        result = Dashboard._build_trade_log(dashboard, {"MU": 110.0, "NVDA": 120.0})
        by_ticker = {w["underlying"]: w for w in result["wheels"]}

        mu = by_ticker["MU"]
        self.assertEqual(mu["kind"], "wheel")
        self.assertTrue(mu["is_wheel"])
        self.assertIsNone(mu["directional"])
        # The wheel's own fields are exactly what they were before this
        # feature existed -- _trade_log_entry itself was never touched.
        self.assertIsNotNone(mu["annualized_wheel_roc_pct"])
        self.assertIsNotNone(mu["win_rate_pct"])

        nvda = by_ticker["NVDA"]
        self.assertEqual(nvda["kind"], "directional")
        self.assertFalse(nvda["is_wheel"])
        self.assertIsNone(nvda["annualized_wheel_roc_pct"])  # withheld, same as before this feature
        self.assertIsNotNone(nvda["directional"])
        self.assertEqual(nvda["directional"]["position_type"], "LC")
        self.assertEqual(nvda["directional"]["status"], "CLOSED")
        self.assertEqual(nvda["directional"]["total_realized_pl"], -400.0)

    def test_a_cycle_that_only_ever_held_shares_gets_no_directional_card_either(self):
        transactions = [tx("2025-01-02", BUY_STOCK, "-AMD", 10, 100.0, -1000.0, row_id=1)]
        dashboard = self._dashboard(transactions)
        result = Dashboard._build_trade_log(dashboard, {"AMD": 110.0})
        (wheel,) = result["wheels"]
        self.assertEqual(wheel["kind"], "hold")
        self.assertIsNone(wheel["directional"])

    def test_a_covered_call_wheel_with_a_protective_put_gets_both_cards(self):
        """The reported case: TGT is an ordinary covered-call wheel
        (kind == "wheel"), but it also bought a protective put alongside
        the covered call -- that long leg must still get the directional
        card, shown alongside the (untouched) wheel card, not instead of
        it.
        """
        transactions = [
            tx("2025-01-02", STO, "-TGT250117P100", -1, 3.0, 300.0, row_id=1),
            tx("2025-01-17", ASSIGNED, "-TGT250117P100", 1, None, 0.0, row_id=2, as_of="2025-01-17"),
            tx("2025-01-20", STO, "-TGT250221C110", -1, 2.0, 200.0, row_id=3),
            # A protective put bought while the covered call is still open --
            # same cycle (the ticker never went flat), never a CSP/CC itself.
            tx("2025-01-25", BTO, "-TGT250620P95", 1, 4.0, -400.0, row_id=4),
        ]
        dashboard = self._dashboard(transactions)
        result = Dashboard._build_trade_log(dashboard, {"TGT": 108.0})
        (tgt,) = result["wheels"]

        # The wheel card: unchanged, still a real wheel with real ratios.
        self.assertEqual(tgt["kind"], "wheel")
        self.assertTrue(tgt["is_wheel"])
        self.assertEqual(tgt["shares_held"], 100)

        # The directional card: present too, covering only the long put --
        # the short CSP/CC legs must never be fed into it.
        directional = tgt["directional"]
        self.assertIsNotNone(directional)
        self.assertEqual(directional["position_type"], "LP")
        self.assertEqual(len(directional["legs"]), 1)
        self.assertEqual(directional["legs"][0]["strike"], 95.0)
        self.assertEqual(directional["total_cost_basis_open"], 400.0)


if __name__ == "__main__":
    unittest.main()
