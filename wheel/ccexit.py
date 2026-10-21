"""CC TO EXIT: the strike for the next covered call written against the
shares no open call covers yet. The one implementation every table, card
and tooltip reads (via the Trade Log wheel's ``cc_to_exit`` fields in
``wheel/api.py``); nothing else computes it.

Pure: no network, no clock. The caller supplies the option chain for one
expiration (``pick_expiration``) and today's date.

Selection, in order:

1. Shares covered = 100 x open short covered-call contracts. Uncovered =
   shares held - covered. None uncovered -> ALL SHARES COVERED.
2. Which shares are uncovered follows the engine's FIFO lot order: open
   calls cover the oldest shares first, so the uncovered ones are the
   newest. If a call is assigned the broker sells FIFO too, which is what
   makes this the right pairing.
3. The next call(s) cover ``contracts x 100`` of those shares. Their
   weighted-average cost is the lot cost; the preferred minimum exit is
   lot cost x 1.02, rounded up to the next $0.50.
4. Every call strike on the chain is priced at the mid of bid/ask, less the
   per-contract fee (``FeeModel``), with a Black-Scholes delta from its
   implied volatility (``wheel.optionsmath``; Yahoo carries no Greeks).
5. Rejected: strikes below the minimum exit, and strikes whose total P&L if
   assigned (stock gain/loss + net premium - the fee on selling the shares
   + option cash those same shares already earned) is not positive.
6. Of the rest, the one with delta closest to 0.30 wins (lower strike on a
   tie). None left -> NO QUALIFYING CC with the reason. Never a fallback to
   the stock price.
7. Historical resistance (``resistance_context``) is then related to the
   strike already chosen: context for the reader, never an input to the
   choice. It can't reject, move or re-rank a strike.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Sequence

from wheel import optionsmath, pricezones
from wheel import tooltipcard as tc
from wheel.optionsmath import CALL, bid_ask_mid, black_scholes_greeks

OPTION_MULTIPLIER = 100

CC_EXIT_TARGET_DELTA = 0.30
CC_EXIT_PREFERRED_DELTA_RANGE = (0.25, 0.35)
# Minimum exit: at least this much above what the covered shares cost.
CC_EXIT_MIN_GAIN_PCT = 2.0
# Expiration: the listed date closest to the target inside the window, or,
# when nothing is listed inside it, the date closest to the target overall.
CC_EXIT_TARGET_DTE = 35
CC_EXIT_DTE_WINDOW = (30, 45)
# Fidelity's published per-contract options commission: the fee used only
# when an account has never sold a call to measure its own from.
OPTION_FEE_PER_CONTRACT = 0.65
# Fee on a stock sale as a fraction of proceeds, used only when an account
# has never sold stock: $20.60 per $1M, the rate every 2026 assignment sale
# in this user's ledger was charged (the SEC's Section 31 fee).
SALE_FEE_RATE_FALLBACK = 20.60 / 1_000_000
SALE_FEE_FALLBACK_SOURCE = "the 2026 SEC rate; no stock sales in this account to measure"

STATUS_RECOMMENDED = "recommended"
STATUS_ALL_COVERED = "all_covered"
STATUS_NO_QUALIFYING = "no_qualifying"
STATUS_NO_SHARES = "no_shares"

# What every table shows in place of a strike for the non-numeric statuses.
STATUS_LABELS = {
    STATUS_ALL_COVERED: "ALL SHARES COVERED",
    STATUS_NO_QUALIFYING: "NO QUALIFYING CC",
}

_EPS = 1e-9

# ---- historical resistance (context only) ----------------------------------

# A zone has to score this much to count as meaningful: the same bar CSP TO
# ENTER sets for support (cspentry.MIN_SUPPORT_SCORE), so "meaningful" means
# the same on both sides.
MIN_RESISTANCE_SCORE = 40.0
STRONG_RESISTANCE_SCORE = 70.0  # "Strong" at or above, "Moderate" below
# Zones further than this from the strike (% of the strike) say nothing
# useful about it; mirrors CSP TO ENTER's 25% support reach.
MAX_RESISTANCE_DISTANCE_PCT = 25.0
# Usefulness = strength x exp(-(distance / 15%)^2), CSP TO ENTER's support
# relevance curve: a strong zone 3% away beats a weak one touching the strike.
RESISTANCE_DISTANCE_SCALE_PCT = 15.0

ABOVE_ZONE, INSIDE_ZONE, BELOW_ZONE = "above", "inside", "below"  # the strike vs the zone
RELATIONSHIP_LABELS = {
    ABOVE_ZONE: "Above resistance",
    INSIDE_ZONE: "Inside resistance",
    BELOW_ZONE: "Below resistance",
}
NO_RESISTANCE_NEAR = "No significant historical resistance identified near the recommended strike."
RESISTANCE_EXPLAINER = (
    "Historical resistance is based on prior swing highs and repeated price reactions. It can help show "
    "where the stock has previously struggled to move higher, but it does not guarantee that the stock "
    "will stop there."
)


def round_up_to_half_dollar(value: float) -> float:
    """Up to the next $0.50. Rounding up keeps a minimum a minimum. The
    ``round(..., 4)`` absorbs float noise so an exact $0.50 multiple
    (255.0, 306.0) is not bumped a step higher."""
    return math.ceil(round(value / 0.5, 4)) * 0.5


@dataclass(frozen=True)
class ShareBlock:
    """A run of shares held at one cost, in FIFO order. ``prior_credit_per_share``:
    option cash these shares already earned (``metrics.lot_option_credit``)."""

    shares: float
    cost_per_share: float | None
    prior_credit_per_share: float = 0.0


@dataclass(frozen=True)
class CallQuote:
    strike: float
    bid: float | None
    ask: float | None
    implied_volatility: float | None


@dataclass(frozen=True)
class FeeModel:
    """What a covered call costs in fees, and what selling the shares costs
    if it's assigned. ``*_source``: plain words for the tooltip saying where
    each number came from."""

    option_fee_per_contract: float = OPTION_FEE_PER_CONTRACT
    option_fee_source: str = "Fidelity's $0.65 commission"
    # Fee on the stock sale at assignment, as a fraction of the sale
    # proceeds (Fidelity passes on the SEC's per-dollar fee on sales).
    sale_fee_rate: float = 0.0
    sale_fee_source: str = "not counted"


@dataclass(frozen=True)
class ExitChain:
    """The market inputs for one ticker: the chosen expiration's call quotes,
    or ``calls=None`` with the reason they couldn't be loaded."""

    today: date
    expiry: date | None
    calls: tuple[CallQuote, ...] | None
    unavailable_reason: str | None = None
    # Yahoo's own market time on the chain response, and when this app
    # fetched it; both shown so every display can say how fresh the prices are.
    quotes_as_of: datetime | None = None
    fetched_at: datetime | None = None
    # The ticker's resistance zones from its daily history
    # (``pricezones.analyze(bars, RESISTANCE)``); context only.
    resistance: pricezones.ZoneAnalysis | None = None


@dataclass(frozen=True)
class ResistanceContext:
    """Historical resistance around the recommended strike. ``zone`` is the
    most useful meaningful zone (``None`` with ``reason`` saying why there
    is none); ``below`` / ``at`` / ``above`` are the nearest meaningful zone
    under the strike, around it, and over it."""

    strike: float
    zone: pricezones.PriceZone | None
    relationship: str | None  # ABOVE_ZONE / INSIDE_ZONE / BELOW_ZONE: the strike vs ``zone``
    # Strike to the zone's nearest edge, % of the strike; 0 inside it.
    distance_pct: float | None
    near: bool  # within one zone width of the zone ("just above/below")
    below: pricezones.PriceZone | None
    at: pricezones.PriceZone | None
    above: pricezones.PriceZone | None
    meaningful_zones: int  # zones that scored >= MIN_RESISTANCE_SCORE within reach
    zone_width_pct: float | None
    sessions_analyzed: int
    limitations: tuple[str, ...]
    reason: str  # the one-sentence relationship, or why there is no zone

    @property
    def strength_label(self) -> str | None:
        if self.zone is None:
            return None
        return "Strong" if self.zone.score >= STRONG_RESISTANCE_SCORE else "Moderate"


@dataclass(frozen=True)
class CandidateCall:
    strike: float
    delta: float | None
    mid_premium: float | None  # per share
    net_premium: float | None  # all contracts, after fees
    stock_pnl: float
    sale_fee: float  # fee on selling the shares at this strike if assigned
    projected_assignment_pnl: float | None
    rejected_reason: str | None

    @property
    def qualifies(self) -> bool:
        return self.rejected_reason is None


@dataclass(frozen=True)
class CcExit:
    status: str
    reason: str
    shares_held: float
    covered_shares: float
    uncovered_shares: float
    contracts: int = 0
    lot_cost: float | None = None
    prior_credit: float = 0.0
    raw_min_exit: float | None = None
    preferred_min_exit: float | None = None
    expiry: date | None = None
    days_to_expiry: int | None = None
    pick: CandidateCall | None = None
    candidates: tuple[CandidateCall, ...] = field(default_factory=tuple)
    quotes_as_of: datetime | None = None
    fetched_at: datetime | None = None
    calculated_at: datetime | None = None
    fees: FeeModel = FeeModel()
    current_price: float | None = None
    # The calendar date DTE is counted from (the calculation date).
    today: date | None = None
    # True when an input the rules need was unavailable (the price, the
    # shares' cost, a listed expiration, the chain), as opposed to every
    # input loading and no call passing.
    market_data_missing: bool = False
    # Historical resistance around the pick (context only); None without a pick.
    resistance: ResistanceContext | None = None

    @property
    def strike(self) -> float | None:
        return self.pick.strike if self.pick else None

    @property
    def expiry_in_window(self) -> bool | None:
        """Whether the expiration sits inside the 30-45 DTE window. ``False``:
        none was listed there, so the date closest to 35 overall was used."""
        if self.days_to_expiry is None:
            return None
        low, high = CC_EXIT_DTE_WINDOW
        return low <= self.days_to_expiry <= high


def pick_expiration(expirations: Sequence[date], today: date) -> date | None:
    """CC TO EXIT's own expiration pick (its target and window). CSP TO
    ENTER picks separately (``cspentry.pick_expiration``)."""
    return optionsmath.pick_expiration(
        expirations, today, target_dte=CC_EXIT_TARGET_DTE, dte_window=CC_EXIT_DTE_WINDOW
    )


def _take_fifo(blocks: Sequence[ShareBlock], shares: float) -> tuple[list[ShareBlock], list[ShareBlock]]:
    """(the first ``shares`` of ``blocks``, everything after them)."""
    taken: list[ShareBlock] = []
    rest: list[ShareBlock] = []
    left = shares
    for block in blocks:
        if left <= _EPS:
            rest.append(block)
            continue
        take = min(block.shares, left)
        taken.append(ShareBlock(take, block.cost_per_share, block.prior_credit_per_share))
        if block.shares - take > _EPS:
            rest.append(ShareBlock(block.shares - take, block.cost_per_share, block.prior_credit_per_share))
        left -= take
    return taken, rest


def _evaluate(
    quote: CallQuote,
    *,
    contracts: int,
    shares: float,
    lot_cost: float,
    prior_credit: float,
    preferred_min_exit: float,
    spot: float,
    days_to_expiry: int,
    risk_free_rate: float,
    fees: FeeModel,
) -> CandidateCall:
    stock_pnl = (quote.strike - lot_cost) * shares
    sale_fee = quote.strike * shares * fees.sale_fee_rate
    mid = bid_ask_mid(quote.bid, quote.ask)
    greeks = black_scholes_greeks(
        spot=spot,
        strike=quote.strike,
        days_to_expiry=days_to_expiry,
        implied_volatility=quote.implied_volatility,
        right=CALL,
        risk_free_rate=risk_free_rate,
    )
    delta = greeks.delta if greeks else None
    net_premium = (mid * OPTION_MULTIPLIER - fees.option_fee_per_contract) * contracts if mid is not None else None
    projected = stock_pnl + net_premium - sale_fee + prior_credit if net_premium is not None else None

    reason = None
    if quote.strike < preferred_min_exit - _EPS:
        reason = f"below the ${preferred_min_exit:.2f} minimum exit"
    elif mid is None:
        reason = "no bid/ask quote"
    elif projected <= 0:
        reason = f"would lose ${-projected:,.2f} if assigned"
    elif delta is None:
        reason = "no implied volatility, so no delta"
    return CandidateCall(quote.strike, delta, mid, net_premium, stock_pnl, sale_fee, projected, reason)


def _no_candidate_reason(candidates: Sequence[CandidateCall], preferred_min_exit: float) -> str:
    """Names the check that actually rejected the strikes, so the reason
    matches the filter: all below the minimum exit, or the commonest
    rejections among the strikes at or above it."""
    if not candidates:
        return "The option chain lists no call strikes."
    eligible = [c for c in candidates if c.strike >= preferred_min_exit - _EPS]
    if not eligible:
        return f"Every listed call strike is below the ${preferred_min_exit:.2f} minimum exit."
    counts: dict[str, int] = {}
    for candidate in eligible:
        # Group the per-strike dollar figure out of "would lose $x if assigned".
        why = candidate.rejected_reason
        key = "would lose money if assigned" if why.startswith("would lose") else why
        counts[key] = counts.get(key, 0) + 1
    detail = "; ".join(f"{n} {why}" for why, n in sorted(counts.items(), key=lambda item: (-item[1], item[0])))
    return f"No call at or above the ${preferred_min_exit:.2f} minimum exit passes every check ({detail})."


def _strike_vs_zone(strike: float, zone: pricezones.PriceZone) -> tuple[str, float]:
    """(where the strike sits relative to ``zone``, its distance from the
    zone's nearest edge as % of the strike; 0 inside)."""
    if strike > zone.high + _EPS:
        return ABOVE_ZONE, 100.0 * (strike - zone.high) / strike
    if strike < zone.low - _EPS:
        return BELOW_ZONE, 100.0 * (zone.low - strike) / strike
    return INSIDE_ZONE, 0.0


def _resistance_sentence(strike: float, zone: pricezones.PriceZone, relationship: str, near: bool) -> str:
    where = f"the {tc.price_range(zone.low, zone.high)} resistance zone"
    if relationship == INSIDE_ZONE:
        return f"Recommended {_money(strike)} falls inside {where}."
    side = "above" if relationship == ABOVE_ZONE else "below"
    return f"Recommended {_money(strike)} is {'just ' if near else ''}{side} {where}."


def resistance_context(
    analysis: pricezones.ZoneAnalysis | None, strike: float, current_price: float | None
) -> ResistanceContext:
    """Historical resistance around ``strike``, for context only.

    Meaningful zones score at least ``MIN_RESISTANCE_SCORE``, still sit
    overhead (their top is above the current price; a zone the price is
    already above has broken) and lie within ``MAX_RESISTANCE_DISTANCE_PCT``
    of the strike. Of the nearest one under the strike, the one around it
    and the nearest one over it, the most useful is the best on strength x
    distance decay (CSP TO ENTER's support relevance), a tie going to the
    zone around the strike, then the nearer one."""
    empty = {
        "strike": strike, "zone": None, "relationship": None, "distance_pct": None, "near": False,
        "below": None, "at": None, "above": None, "meaningful_zones": 0,
        "zone_width_pct": analysis.zone_width_pct if analysis else None,
        "sessions_analyzed": analysis.sessions_analyzed if analysis else 0,
        "limitations": analysis.limitations if analysis else (),
    }
    if analysis is None:
        return ResistanceContext(**empty, reason="No daily price history was available to look for resistance.")
    if analysis.sessions_analyzed < pricezones.MIN_BARS:
        return ResistanceContext(**empty, reason="Not enough daily price history to find resistance.")
    if not analysis.zones:
        return ResistanceContext(
            **empty, reason=f"No swing highs with a real pullback in the last {analysis.sessions_analyzed} sessions."
        )

    placed = []
    for zone in analysis.zones:
        if zone.score < MIN_RESISTANCE_SCORE:
            continue
        if current_price is not None and zone.high <= current_price:
            continue
        relationship, distance = _strike_vs_zone(strike, zone)
        if distance <= MAX_RESISTANCE_DISTANCE_PCT + _EPS:
            placed.append((zone, relationship, distance))
    if not placed:
        return ResistanceContext(**empty, reason=NO_RESISTANCE_NEAR)

    def nearest(relationship: str) -> tuple | None:
        options = [item for item in placed if item[1] == relationship]
        # Nearest first; among zones the strike sits inside, the strongest.
        return min(options, key=lambda item: (round(item[2], 6), -item[0].score), default=None)

    under, around, over = nearest(ABOVE_ZONE), nearest(INSIDE_ZONE), nearest(BELOW_ZONE)

    def usefulness(item: tuple) -> tuple:
        zone, relationship, distance = item
        relevance = zone.score * math.exp(-((distance / RESISTANCE_DISTANCE_SCALE_PCT) ** 2))
        return (round(relevance, 6), relationship == INSIDE_ZONE, -round(distance, 6))

    zone, relationship, distance = max((item for item in (under, around, over) if item), key=usefulness)
    near = analysis.zone_width_pct is not None and distance <= analysis.zone_width_pct + _EPS
    return ResistanceContext(
        **{
            **empty,
            "zone": zone,
            "relationship": relationship,
            "distance_pct": distance,
            "near": near,
            "below": under[0] if under else None,
            "at": around[0] if around else None,
            "above": over[0] if over else None,
            "meaningful_zones": len(placed),
        },
        reason=_resistance_sentence(strike, zone, relationship, near),
    )


# ---- per-strike resistance guidance (advice only) ---------------------------

GUIDE_BELOW, GUIDE_INSIDE, GUIDE_JUST_ABOVE, GUIDE_WELL_ABOVE, GUIDE_NONE = (
    "below", "inside", "just_above", "well_above", "none",
)
# Short column text, then the bold lead-in of the advice line.
GUIDANCE_LABELS = {
    GUIDE_BELOW: ("Below", "Below resistance"),
    GUIDE_INSIDE: ("Inside", "Inside resistance"),
    GUIDE_JUST_ABOVE: ("Just above", "Just above resistance"),
    GUIDE_WELL_ABOVE: ("Well above", "Well above resistance"),
    GUIDE_NONE: ("None found", "No resistance found"),
}


StrikeGuidance = pricezones.StrikeGuidance


def strike_guidance(
    context: ResistanceContext | None, strike: float, qualifying_strikes: Sequence[float]
) -> StrikeGuidance | None:
    """Advice for one *qualifying* strike against the resistance zone CC TO
    EXIT already reported (``context.zone``); ``None`` without a context.
    Same classification as the context: above / inside / below the zone,
    "just" above when within one zone width of its top (the ATR-based width
    the zones are built with), "well" above past that. A higher strike is
    only suggested when one actually qualifies (``qualifying_strikes``).
    Advice only: it never rejects, moves or re-ranks a strike."""
    if context is None:
        return None
    zone = context.zone

    def make(position: str, text: str) -> StrikeGuidance:
        label, title = GUIDANCE_LABELS[position]
        return StrikeGuidance(strike, position, label, title, text)

    if zone is None:
        return make(GUIDE_NONE, "Decide using delta, profitability and the minimum exit.")
    relationship, distance = _strike_vs_zone(strike, zone)
    if relationship == BELOW_ZONE:
        if any(other > strike + _EPS for other in qualifying_strikes):
            return make(
                GUIDE_BELOW,
                "Caps your upside before the stock reaches resistance; consider a higher qualifying strike, "
                "which usually pays less premium.",
            )
        return make(GUIDE_BELOW, "Caps your upside before resistance, but no higher strike qualifies.")
    if relationship == INSIDE_ZONE:
        if any(other > zone.high + _EPS for other in qualifying_strikes):
            return make(
                GUIDE_INSIDE,
                "Sits within resistance; consider a qualifying strike above the zone, which usually pays less premium.",
            )
        return make(GUIDE_INSIDE, "Sits within resistance, and no qualifying strike is above the zone.")
    if context.zone_width_pct is not None and distance <= context.zone_width_pct + _EPS:
        return make(GUIDE_JUST_ABOVE, "Gives the stock room to move through resistance before your shares are called away.")
    return make(GUIDE_WELL_ABOVE, "More upside room, but check whether the usually lower premium is worth it.")


def calculate_cc_to_exit(
    *,
    share_blocks: Sequence[ShareBlock],
    covered_shares: float,
    current_price: float | None,
    expiry: date | None,
    today: date,
    calls: Sequence[CallQuote] | None,
    chain_unavailable_reason: str | None = None,
    risk_free_rate: float = 0.0,
    quotes_as_of: datetime | None = None,
    fetched_at: datetime | None = None,
    calculated_at: datetime | None = None,
    fees: FeeModel = FeeModel(),
    resistance: pricezones.ZoneAnalysis | None = None,
) -> CcExit:
    """``share_blocks``: every share held, oldest first. ``calls``: the call
    side of the chain for ``expiry``; ``None`` when it couldn't be loaded
    (``chain_unavailable_reason`` says why). ``quotes_as_of`` / ``fetched_at``
    / ``calculated_at`` are carried onto the result unchanged, so every
    display of it can show the same snapshot and calculation time."""
    stamps = {
        "quotes_as_of": quotes_as_of,
        "fetched_at": fetched_at,
        "calculated_at": calculated_at,
        "fees": fees,
        "current_price": current_price,
        "today": today,
    }
    shares_held = sum(block.shares for block in share_blocks)
    if shares_held <= _EPS:
        return CcExit(STATUS_NO_SHARES, "No shares held.", 0.0, covered_shares, 0.0, **stamps)
    uncovered_shares = shares_held - covered_shares
    base = {
        "shares_held": shares_held,
        "covered_shares": covered_shares,
        "uncovered_shares": max(uncovered_shares, 0.0),
        **stamps,
    }
    if uncovered_shares <= _EPS:
        return CcExit(STATUS_ALL_COVERED, "Every share already has an open call against it.", **base)

    contracts = int((uncovered_shares + 1e-6) // OPTION_MULTIPLIER)
    if contracts < 1:
        return CcExit(
            STATUS_NO_QUALIFYING,
            f"Only {uncovered_shares:g} uncovered shares; one call needs 100.",
            **base,
        )

    _, uncovered_blocks = _take_fifo(share_blocks, covered_shares)
    next_blocks, _ = _take_fifo(uncovered_blocks, contracts * OPTION_MULTIPLIER)
    next_shares = sum(block.shares for block in next_blocks)
    if any(block.cost_per_share is None for block in next_blocks):
        return CcExit(
            STATUS_NO_QUALIFYING,
            "What you paid for the uncovered shares isn't on file, so no strike can be checked for a loss.",
            contracts=contracts,
            market_data_missing=True,
            **base,
        )
    lot_cost = sum(block.shares * block.cost_per_share for block in next_blocks) / next_shares
    prior_credit = sum(block.shares * block.prior_credit_per_share for block in next_blocks)
    raw_min_exit = lot_cost * (1 + CC_EXIT_MIN_GAIN_PCT / 100)
    preferred_min_exit = round_up_to_half_dollar(raw_min_exit)
    sized = {
        **base,
        "contracts": contracts,
        "lot_cost": lot_cost,
        "prior_credit": prior_credit,
        "raw_min_exit": raw_min_exit,
        "preferred_min_exit": preferred_min_exit,
    }

    if current_price is None:
        return CcExit(
            STATUS_NO_QUALIFYING,
            "No current stock price, so delta can't be worked out.",
            market_data_missing=True,
            **sized,
        )
    if expiry is None:
        return CcExit(
            STATUS_NO_QUALIFYING,
            chain_unavailable_reason or "No listed option expiration to check.",
            market_data_missing=True,
            **sized,
        )
    days_to_expiry = (expiry - today).days
    sized = {**sized, "expiry": expiry, "days_to_expiry": days_to_expiry}
    if calls is None:
        return CcExit(
            STATUS_NO_QUALIFYING,
            chain_unavailable_reason or "Option prices couldn't be loaded.",
            market_data_missing=True,
            **sized,
        )

    candidates = tuple(
        _evaluate(
            quote,
            contracts=contracts,
            shares=next_shares,
            lot_cost=lot_cost,
            prior_credit=prior_credit,
            preferred_min_exit=preferred_min_exit,
            spot=current_price,
            days_to_expiry=days_to_expiry,
            risk_free_rate=risk_free_rate,
            fees=fees,
        )
        for quote in sorted(calls, key=lambda q: q.strike)
    )
    qualifying = [c for c in candidates if c.qualifies]
    if not qualifying:
        return CcExit(
            STATUS_NO_QUALIFYING,
            _no_candidate_reason(candidates, preferred_min_exit),
            candidates=candidates,
            **sized,
        )
    # Distance compared at 4 decimals so float noise can't break a genuine
    # tie; a tie goes to the lower strike (more premium, same delta gap).
    pick = min(qualifying, key=lambda c: (round(abs(c.delta - CC_EXIT_TARGET_DELTA), 4), c.strike))
    return CcExit(
        STATUS_RECOMMENDED,
        "Qualifying strike closest to the target delta.",
        pick=pick,
        candidates=candidates,
        # Worked out only after the pick is final: context, never a selection input.
        resistance=resistance_context(resistance, pick.strike, current_price),
        **sized,
    )


def _shares(value: float) -> str:
    return tc.shares(value)


def _money(value: float) -> str:
    return tc.money(value)


def _nearby_candidates(result: CcExit) -> list[CandidateCall]:
    """The strikes worth showing: the last one rejected for the minimum
    exit, then up to three from the minimum upward, always including the pick."""
    below = [c for c in result.candidates if c.strike < (result.preferred_min_exit or 0) - _EPS]
    above = [c for c in result.candidates if c not in below]
    shown = below[-1:] + above[:3]
    if result.pick and result.pick not in shown:
        shown.append(result.pick)
    return shown


# ---- tooltip card ----------------------------------------------------------

STRATEGY = "CC TO EXIT"
FIFO_NOTE = (
    "Open calls cover your oldest shares first (FIFO, also Fidelity's default for stocks), so the "
    "next call covers the newest uncovered shares, priced at what those shares cost."
)


def card_status(result: CcExit) -> str:
    if result.status == STATUS_RECOMMENDED:
        return tc.STATUS_QUALIFIES
    if result.status == STATUS_ALL_COVERED:
        return tc.STATUS_INFO
    return tc.STATUS_UNVERIFIED if result.market_data_missing else tc.STATUS_DOES_NOT_QUALIFY


def _net_per_contract(result: CcExit, candidate: CandidateCall) -> float | None:
    """One contract's premium after its fee: the per-contract slice of
    ``net_premium`` (which covers every contract of the call)."""
    if candidate.net_premium is None or result.contracts < 1:
        return None
    return candidate.net_premium / result.contracts


def _contracts_text(result: CcExit) -> str:
    covered = _shares(result.contracts * OPTION_MULTIPLIER)
    return f"{result.contracts} contract{'s' if result.contracts != 1 else ''}, {covered} shares"


def _why(result: CcExit) -> list[str]:
    pick = result.pick
    low, high = CC_EXIT_PREFERRED_DELTA_RANGE
    in_band = low <= pick.delta <= high
    delta_line = (
        f"Delta {pick.delta:.2f} is the qualifying strike closest to the {CC_EXIT_TARGET_DELTA:.2f} target"
        + (
            f", inside the preferred {low:.2f} to {high:.2f} range."
            if in_band
            else f"; no qualifying strike lands inside the preferred {low:.2f} to {high:.2f} range."
        )
    )
    return [
        f"{_money(pick.strike)} is at or above the {_money(result.preferred_min_exit)} minimum exit.",
        delta_line,
        f"P&L if assigned is {_money(pick.projected_assignment_pnl)}, above zero as required.",
    ]


def candidate_guidance(result: CcExit, candidate: CandidateCall) -> StrikeGuidance | None:
    """Resistance advice for one listed strike: qualifying strikes only (a
    rejected one keeps its rejection reason), against every qualifying
    strike on the chain, not just the ones the table shows."""
    if not candidate.qualifies:
        return None
    qualifying = [c.strike for c in result.candidates if c.qualifies]
    return strike_guidance(result.resistance, candidate.strike, qualifying)


def _strike_table(result: CcExit) -> dict | None:
    # The Resistance column appears whenever resistance was looked at (a
    # strike was recommended); its advice takes the row's one-sentence line.
    with_guidance = result.resistance is not None
    rows = []
    for candidate in _nearby_candidates(result):
        picked = candidate is result.pick
        guidance = candidate_guidance(result, candidate) if with_guidance else None
        reason_label = None
        if guidance is not None:
            reason_label, reason = guidance.title, guidance.text
        elif picked:
            reason = None
        elif candidate.qualifies:
            reason = f"Delta further from the {CC_EXIT_TARGET_DELTA:.2f} target"
        else:
            reason = candidate.rejected_reason[:1].upper() + candidate.rejected_reason[1:]
        meets_min = candidate.strike >= (result.preferred_min_exit or 0) - _EPS
        cells = [
            _money(candidate.strike),
            tc.delta(candidate.delta),
            _money(candidate.mid_premium) if candidate.mid_premium is not None else "Unavailable",
            _money(candidate.projected_assignment_pnl) if candidate.projected_assignment_pnl is not None else "Unavailable",
            "PASS" if meets_min else "FAIL",
        ]
        if with_guidance:
            cells.append(guidance.label if guidance else "-")
        rows.append(
            tc.table_row(
                cells, picked=picked, qualifies=candidate.qualifies, reason=reason, reason_label=reason_label
            )
        )
    columns = ["Strike", "Delta", "Midpoint", "P&L if assigned", "Min exit"]
    numeric = [True, True, True, True, False]
    if with_guidance:
        columns.append("Resistance")
        numeric.append(False)
    return tc.table(columns, numeric, rows)


def _allocation_section(result: CcExit) -> dict | None:
    rows = [
        ("Shares held", _shares(result.shares_held)),
        ("Covered by open calls", _shares(result.covered_shares)),
        ("Uncovered", _shares(result.uncovered_shares)),
    ]
    if result.contracts >= 1:
        rows.append(("This call covers", f"the newest {_contracts_text(result)}"))
    if result.lot_cost is not None:
        rows.append(("Cost of those shares", f"{_money(result.lot_cost)} a share"))
    return tc.section("Share allocation", rows=rows, notes=[FIFO_NOTE])


def _pnl_section(result: CcExit) -> dict | None:
    pick = result.pick
    if pick is None:
        return None
    covered = _shares(result.contracts * OPTION_MULTIPLIER)
    fees = result.fees
    rows = [
        (
            "Stock P&L at the strike",
            f"({_money(pick.strike)} - {_money(result.lot_cost)}) × {covered} = {_money(pick.stock_pnl)}",
        ),
        ("New call premium", f"{_money(pick.net_premium)} after fees, all contracts"),
        ("Sale fee if assigned", f"- {_money(pick.sale_fee)}"),
        ("Earlier premium on these shares", _money(result.prior_credit)),
        ("P&L if assigned", _money(pick.projected_assignment_pnl)),
    ]
    notes = [
        f"Premium is the bid/ask midpoint less ${fees.option_fee_per_contract:.2f} per contract "
        f"({fees.option_fee_source}).",
        f"Sale fee = strike × shares × ${fees.sale_fee_rate * 1_000_000:.2f} per $1M sold ({fees.sale_fee_source}); "
        "per-trade rounding and per-share charges aren't modeled.",
        "Earlier premium is only the option cash these same shares already earned.",
    ]
    return tc.section("P&L if assigned, step by step", rows=rows, notes=notes)


def _calculation_section(result: CcExit) -> dict | None:
    rows: list[tuple[str, str]] = []
    if result.raw_min_exit is not None:
        rows.append(
            (
                "Minimum exit",
                f"{_money(result.lot_cost)} × {1 + CC_EXIT_MIN_GAIN_PCT / 100:.2f} = {_money(result.raw_min_exit)}, "
                f"rounded up to {_money(result.preferred_min_exit)}",
            )
        )
    if result.expiry is not None and result.days_to_expiry is not None:
        low_dte, high_dte = CC_EXIT_DTE_WINDOW
        rule = (
            f"closest to {CC_EXIT_TARGET_DTE} inside {low_dte} to {high_dte}"
            if result.expiry_in_window
            else f"none listed {low_dte} to {high_dte} days out, so the closest to {CC_EXIT_TARGET_DTE}"
        )
        rows.append(
            ("DTE", f"{result.days_to_expiry} calendar days, {tc.day(result.today)} to {tc.day(result.expiry)} ({rule})")
        )
    if result.pick is not None:
        low, high = CC_EXIT_PREFERRED_DELTA_RANGE
        rows.append(("Delta rule", f"closest to {CC_EXIT_TARGET_DELTA:.2f}; preferred {low:.2f} to {high:.2f}"))
        per_contract = _net_per_contract(result, result.pick)
        rows.append(
            (
                "Net premium / contract",
                f"{_money(result.pick.mid_premium)} × 100 - ${result.fees.option_fee_per_contract:.2f} = "
                f"{_money(per_contract)}",
            )
        )
    rows += [
        ("Prices as of", tc.when(result.quotes_as_of)),
        ("Fetched", tc.when(result.fetched_at)),
        ("Calculated", tc.when(result.calculated_at)),
    ]
    return tc.section("Calculation details", rows=rows)


def _zone_text(zone: pricezones.PriceZone | None) -> str:
    if zone is None:
        return "None"
    return f"{tc.price_range(zone.low, zone.high)} ({zone.score:.0f}/100)"


def _resistance_block(context: ResistanceContext | None) -> dict | None:
    """The "Historical resistance" block shown right after "Why this
    strike?": the zone, its strength, and where the strike sits against it."""
    if context is None:
        return None
    notes = [
        RESISTANCE_EXPLAINER,
        f"Based on the last {context.sessions_analyzed} daily sessions. A swing high is the highest daily high "
        f"within {pricezones.SWING_PIVOT_BARS} sessions either side, followed within "
        f"{pricezones.REACTION_WINDOW_BARS} sessions by a pullback of at least "
        f"max({pricezones.SWING_MIN_REACTION_PCT:g}%, {pricezones.SWING_MIN_REACTION_ATR:g} ATR). The score ranks "
        "past evidence from 0 to 100; it is not a probability.",
        "Shown for context only: it never changes which strike is recommended.",
        *context.limitations,
    ]
    zone = context.zone
    if zone is None:
        return {
            "title": "Historical resistance",
            "value": "None found",
            "badge": None,
            "text": context.reason,
            "stats": [],
            "details": tc.section("About historical resistance", notes=notes),
        }
    last_day = zone.touch_days[-1] if zone.touch_days else None
    rows = [
        ("Why this zone", (
            f"The best mix of strength and closeness to the strike among {context.meaningful_zones} zone"
            f"{'s' if context.meaningful_zones != 1 else ''} scoring {MIN_RESISTANCE_SCORE:g}+ within "
            f"{MAX_RESISTANCE_DISTANCE_PCT:g}% of it"
        )),
        ("Strike to zone", "inside it" if context.relationship == INSIDE_ZONE else f"{context.distance_pct:.1f}%"),
        ("Swing highs", f"{zone.touches}: " + ", ".join(tc.day(d) for d in zone.touch_days)),
        ("Last touch", f"{tc.day(last_day)}, {zone.sessions_since_last_touch} sessions ago"),
        ("Average pullback", f"{zone.avg_reaction_pct:.1f}% (lowest low within {pricezones.REACTION_WINDOW_BARS} sessions)"),
        ("Closed above since", f"{zone.breaks}x (each halves the score)" if zone.breaks else "Never"),
        ("Moving averages near", ", ".join(zone.moving_averages_near) or "None"),
        ("Nearest under the strike", _zone_text(context.below)),
        ("Around the strike", _zone_text(context.at)),
        ("Nearest over the strike", _zone_text(context.above)),
    ]
    return {
        "title": "Historical resistance",
        "value": tc.price_range(zone.low, zone.high),
        "badge": f"{context.strength_label} · {zone.score:.0f}/100",
        "text": context.reason,
        "stats": [
            tc.item("Recommended strike", _money(context.strike)),
            tc.item("Nearest resistance", tc.price_range(zone.low, zone.high)),
            tc.item("Relationship", RELATIONSHIP_LABELS[context.relationship]),
        ],
        "details": tc.section("About this resistance", rows=rows, notes=notes),
    }


def tooltip_card(result: CcExit, ticker: str | None = None) -> dict | None:
    """The CC TO EXIT tooltip every table and card shows, built from this
    one result (layout: ``wheel/tooltipcard.py``). ``None`` with no shares:
    there is nothing to explain."""
    if result.status == STATUS_NO_SHARES:
        return None
    status = card_status(result)
    common = {
        "strategy": STRATEGY,
        "ticker": ticker,
        "status": status,
        "status_label": STATUS_LABELS.get(STATUS_ALL_COVERED) if status == tc.STATUS_INFO else None,
        "subtitle_text": tc.subtitle(result.current_price, result.expiry, result.days_to_expiry, result.today),
        "quoted_text": tc.quoted(result.quotes_as_of, result.fetched_at),
        "footer": "Premium uses the bid/ask midpoint, an estimate, not a guaranteed fill.",
    }
    strikes = tc.section(
        "Strikes checked",
        table=_strike_table(result) if result.candidates else None,
        open_=status != tc.STATUS_QUALIFIES,
    )
    sections = [strikes, _allocation_section(result), _pnl_section(result), _calculation_section(result)]
    if result.status != STATUS_RECOMMENDED:
        return tc.card(**common, summary=result.reason, sections=sections)

    pick = result.pick
    low, high = CC_EXIT_PREFERRED_DELTA_RANGE
    covered = _shares(result.contracts * OPTION_MULTIPLIER)
    return tc.card(
        **common,
        headline=tc.item(
            "Recommended call", _money(pick.strike), f"Qualifying strike closest to {CC_EXIT_TARGET_DELTA:.2f} delta"
        ),
        headline_side=tc.item(
            "Delta", tc.delta(pick.delta), f"Target {CC_EXIT_TARGET_DELTA:.2f} · preferred {low:.2f} to {high:.2f}"
        ),
        metrics=[
            tc.item(
                "Net premium / contract",
                _money(_net_per_contract(result, pick)),
                f"Midpoint less the ${result.fees.option_fee_per_contract:.2f} fee",
            ),
            tc.item("P&L if assigned", _money(pick.projected_assignment_pnl), _contracts_text(result)),
            tc.item("Minimum exit strike", _money(result.preferred_min_exit), f"Cost + {CC_EXIT_MIN_GAIN_PCT:g}%, rounded up"),
            tc.item("Stock P&L at the strike", _money(pick.stock_pnl), "Before any premium"),
        ],
        why=_why(result),
        context=_resistance_block(result.resistance),
        risk={
            "title": "If assigned",
            "text": (
                f"Your {covered} newest uncovered shares are sold at {_money(pick.strike)}. The "
                f"{_money(result.preferred_min_exit)} minimum exit is a floor on the strike, not advice that "
                "selling there is the best move."
            ),
            "stats": [
                tc.item("Shares held", _shares(result.shares_held)),
                tc.item("Covered by open calls", _shares(result.covered_shares)),
                tc.item("Uncovered", _shares(result.uncovered_shares)),
                tc.item("This call covers", covered),
            ],
        },
        sections=sections,
    )
