"""Black-Scholes option pricing and Greeks -- a model-derived *estimate*, not
a broker-quoted value. This project has no real Greeks feed (see
``wheel.marketdata``'s option-chain fetch): spot price, strike, days to
expiry and implied volatility come from live/public market data, but
delta/gamma/theta/vega themselves are computed here from those inputs, under
the standard Black-Scholes assumptions (European exercise, no dividends,
constant volatility and a constant risk-free rate). American-style equity
options can be exercised early and dividends shift the true Greeks a little,
so this is an industry-standard approximation, not an exact value -- the
same caveat every retail broker's own "Greeks" column carries.

Stdlib only (``statistics.NormalDist``), consistent with the rest of the
project's "no third-party packages" rule (see ``wheel.marketdata``'s module
docstring).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from statistics import NormalDist
from typing import Sequence

CALL = "C"
PUT = "P"

_NORMAL = NormalDist()


@dataclass(frozen=True)
class Greeks:
    """Per-underlying-share Greeks (multiply by 100 x contracts for a
    position total). ``theta`` is dollars of time decay per calendar day;
    ``vega`` is dollars per 1 percentage point (0.01) of implied volatility."""

    delta: float
    gamma: float
    theta: float
    vega: float


def bid_ask_mid(bid: float | None, ask: float | None) -> float | None:
    """Midpoint of a usable two-sided quote, or ``None`` for a missing,
    negative, zero-ask or crossed (bid above ask) one. The price both CC TO
    EXIT and CSP TO ENTER assume a new short option fills at."""
    if bid is None or ask is None or bid < 0 or ask <= 0 or ask < bid:
        return None
    return (bid + ask) / 2


def pick_expiration(
    expirations: Sequence[date], today: date, *, target_dte: int, dte_window: tuple[int, int]
) -> date | None:
    """The listed expiration closest to ``target_dte`` days out among those
    inside ``dte_window`` (inclusive); when none is listed inside it, the
    closest to the target overall. A tie goes to the earlier date. Shared by
    CC TO EXIT and CSP TO ENTER, which each pass their own target and window."""
    future = [d for d in expirations if (d - today).days >= 1]
    if not future:
        return None
    low, high = dte_window
    in_window = [d for d in future if low <= (d - today).days <= high]
    pool = in_window or future
    return min(pool, key=lambda d: (abs((d - today).days - target_dte), d))


def intrinsic_value_per_share(*, spot: float, strike: float, right: str) -> float:
    """What the option would be worth if exercised this instant -- never
    negative. The floor every option's market value sits on top of."""
    return max(spot - strike, 0.0) if right == CALL else max(strike - spot, 0.0)


def black_scholes_greeks(
    *,
    spot: float | None,
    strike: float | None,
    days_to_expiry: float | None,
    implied_volatility: float | None,
    right: str,
    risk_free_rate: float = 0.0,
) -> Greeks | None:
    """European Black-Scholes Greeks for one contract-share. Returns
    ``None`` -- never an invented number -- when the inputs can't support
    the model: a missing price/strike/IV, a non-positive spot/strike/IV, or
    no time left (``days_to_expiry <= 0``, where the model's
    ``sigma * sqrt(T)`` denominator collapses to zero).
    """
    if spot is None or strike is None or implied_volatility is None or days_to_expiry is None:
        return None
    if spot <= 0 or strike <= 0 or implied_volatility <= 0 or days_to_expiry <= 0:
        return None

    t = days_to_expiry / 365.0
    sigma = implied_volatility
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    pdf_d1 = _NORMAL.pdf(d1)
    discounted_strike = strike * math.exp(-risk_free_rate * t)

    if right == PUT:
        delta = _NORMAL.cdf(d1) - 1.0
        theta_per_year = (
            -spot * pdf_d1 * sigma / (2.0 * sqrt_t) + risk_free_rate * discounted_strike * _NORMAL.cdf(-d2)
        )
    else:
        delta = _NORMAL.cdf(d1)
        theta_per_year = (
            -spot * pdf_d1 * sigma / (2.0 * sqrt_t) - risk_free_rate * discounted_strike * _NORMAL.cdf(d2)
        )

    gamma = pdf_d1 / (spot * sigma * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t / 100.0
    theta = theta_per_year / 365.0

    return Greeks(delta=delta, gamma=gamma, theta=theta, vega=vega)
