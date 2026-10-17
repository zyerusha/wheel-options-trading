"""Black-Scholes Greeks (wheel/optionsmath.py).

Expected values come from two independent sources, never from re-running
the function under test against itself:

* ``_reference_greeks`` below is a from-scratch re-implementation of the
  same textbook formulas (Hull, *Options, Futures, and Other Derivatives*),
  built on ``math.erf`` directly rather than ``statistics.NormalDist`` or
  anything imported from ``wheel.optionsmath`` -- a coding slip in the
  module under test (a wrong sign, a wrong divisor) would not also appear
  here.
* The delta values for the S=42, K=40, r=10%, sigma=20%, T=0.5y case are
  Hull's own published worked example (call delta 0.7791, put delta
  -0.2209), copied from the textbook, not derived from any code at all.
"""

from __future__ import annotations

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.optionsmath import black_scholes_greeks, intrinsic_value_per_share  # noqa: E402


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _reference_greeks(spot, strike, days, sigma, right, r):
    """Independent re-derivation of the Black-Scholes Greeks, from the raw
    formulas, not from wheel.optionsmath."""
    t = days / 365.0
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    pdf_d1 = _norm_pdf(d1)
    disc_k = strike * math.exp(-r * t)
    if right == "P":
        delta = _norm_cdf(d1) - 1.0
        theta_year = -spot * pdf_d1 * sigma / (2 * sqrt_t) + r * disc_k * _norm_cdf(-d2)
    else:
        delta = _norm_cdf(d1)
        theta_year = -spot * pdf_d1 * sigma / (2 * sqrt_t) - r * disc_k * _norm_cdf(d2)
    gamma = pdf_d1 / (spot * sigma * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t / 100.0
    return delta, gamma, theta_year / 365.0, vega


class TestBlackScholesAgainstIndependentReference(unittest.TestCase):
    CASES = [
        # spot, strike, days, iv, right, r
        (100.0, 100.0, 182.5, 0.20, "C", 0.05),  # ATM call, half a year
        (100.0, 100.0, 182.5, 0.20, "P", 0.05),  # ATM put, same terms
        (150.0, 100.0, 90.0, 0.35, "C", 0.045),  # deep ITM call (DITM)
        (60.0, 100.0, 90.0, 0.35, "P", 0.045),  # deep ITM put (DITM)
        (100.0, 150.0, 30.0, 0.60, "C", 0.045),  # deep OTM call, short-dated
        (100.0, 60.0, 400.0, 0.25, "P", 0.045),  # deep OTM put, long-dated
    ]

    def test_matches_independent_reference_implementation(self):
        for spot, strike, days, iv, right, r in self.CASES:
            with self.subTest(spot=spot, strike=strike, days=days, iv=iv, right=right):
                greeks = black_scholes_greeks(
                    spot=spot, strike=strike, days_to_expiry=days, implied_volatility=iv, right=right,
                    risk_free_rate=r,
                )
                ref_delta, ref_gamma, ref_theta, ref_vega = _reference_greeks(spot, strike, days, iv, right, r)
                self.assertAlmostEqual(greeks.delta, ref_delta, places=9)
                self.assertAlmostEqual(greeks.gamma, ref_gamma, places=9)
                self.assertAlmostEqual(greeks.theta, ref_theta, places=9)
                self.assertAlmostEqual(greeks.vega, ref_vega, places=9)

    def test_hull_textbook_worked_example(self):
        # Hull, "Options, Futures, and Other Derivatives": S=42, K=40, r=10%,
        # sigma=20%, T=0.5 years -> call delta 0.7791, put delta -0.2209
        # (published, not derived from this code).
        call = black_scholes_greeks(
            spot=42.0, strike=40.0, days_to_expiry=182.5, implied_volatility=0.20, right="C",
            risk_free_rate=0.10,
        )
        put = black_scholes_greeks(
            spot=42.0, strike=40.0, days_to_expiry=182.5, implied_volatility=0.20, right="P",
            risk_free_rate=0.10,
        )
        self.assertAlmostEqual(call.delta, 0.7791, places=3)
        self.assertAlmostEqual(put.delta, -0.2209, places=3)

    def test_put_call_parity_delta_identity(self):
        # Textbook identity: delta_call - delta_put == 1 for the same
        # spot/strike/time/vol/rate -- independent of any specific numbers.
        for spot, strike, days, iv, _, r in self.CASES:
            call = black_scholes_greeks(
                spot=spot, strike=strike, days_to_expiry=days, implied_volatility=iv, right="C",
                risk_free_rate=r,
            )
            put = black_scholes_greeks(
                spot=spot, strike=strike, days_to_expiry=days, implied_volatility=iv, right="P",
                risk_free_rate=r,
            )
            self.assertAlmostEqual(call.delta - put.delta, 1.0, places=9)

    def test_call_and_put_share_gamma_and_vega(self):
        # Textbook identity: gamma and vega don't depend on option side.
        call = black_scholes_greeks(
            spot=100.0, strike=95.0, days_to_expiry=60.0, implied_volatility=0.30, right="C",
            risk_free_rate=0.04,
        )
        put = black_scholes_greeks(
            spot=100.0, strike=95.0, days_to_expiry=60.0, implied_volatility=0.30, right="P",
            risk_free_rate=0.04,
        )
        self.assertAlmostEqual(call.gamma, put.gamma, places=9)
        self.assertAlmostEqual(call.vega, put.vega, places=9)

    def test_deep_itm_call_delta_approaches_one(self):
        greeks = black_scholes_greeks(
            spot=200.0, strike=50.0, days_to_expiry=45.0, implied_volatility=0.25, right="C",
            risk_free_rate=0.045,
        )
        self.assertGreater(greeks.delta, 0.99)

    def test_deep_otm_call_delta_approaches_zero(self):
        greeks = black_scholes_greeks(
            spot=50.0, strike=200.0, days_to_expiry=45.0, implied_volatility=0.25, right="C",
            risk_free_rate=0.045,
        )
        self.assertLess(greeks.delta, 0.01)

    def test_deep_itm_put_delta_approaches_negative_one(self):
        greeks = black_scholes_greeks(
            spot=50.0, strike=200.0, days_to_expiry=45.0, implied_volatility=0.25, right="P",
            risk_free_rate=0.045,
        )
        self.assertLess(greeks.delta, -0.99)


class TestInvalidInputsReturnNoneRatherThanInventAValue(unittest.TestCase):
    VALID = dict(spot=100.0, strike=100.0, days_to_expiry=30.0, implied_volatility=0.3, right="C")

    def test_missing_implied_volatility(self):
        kwargs = {**self.VALID, "implied_volatility": None}
        self.assertIsNone(black_scholes_greeks(**kwargs))

    def test_zero_or_negative_implied_volatility(self):
        self.assertIsNone(black_scholes_greeks(**{**self.VALID, "implied_volatility": 0.0}))
        self.assertIsNone(black_scholes_greeks(**{**self.VALID, "implied_volatility": -0.1}))

    def test_expired_contract_has_no_time_value_left_in_the_model(self):
        self.assertIsNone(black_scholes_greeks(**{**self.VALID, "days_to_expiry": 0}))
        self.assertIsNone(black_scholes_greeks(**{**self.VALID, "days_to_expiry": -3}))

    def test_missing_spot_or_strike(self):
        self.assertIsNone(black_scholes_greeks(**{**self.VALID, "spot": None}))
        self.assertIsNone(black_scholes_greeks(**{**self.VALID, "strike": None}))

    def test_non_positive_spot_or_strike(self):
        self.assertIsNone(black_scholes_greeks(**{**self.VALID, "spot": 0.0}))
        self.assertIsNone(black_scholes_greeks(**{**self.VALID, "strike": -5.0}))


class TestIntrinsicValue(unittest.TestCase):
    def test_itm_call(self):
        self.assertAlmostEqual(intrinsic_value_per_share(spot=120.0, strike=100.0, right="C"), 20.0)

    def test_otm_call_floors_at_zero(self):
        self.assertAlmostEqual(intrinsic_value_per_share(spot=80.0, strike=100.0, right="C"), 0.0)

    def test_itm_put(self):
        self.assertAlmostEqual(intrinsic_value_per_share(spot=80.0, strike=100.0, right="P"), 20.0)

    def test_otm_put_floors_at_zero(self):
        self.assertAlmostEqual(intrinsic_value_per_share(spot=120.0, strike=100.0, right="P"), 0.0)


if __name__ == "__main__":
    unittest.main()
