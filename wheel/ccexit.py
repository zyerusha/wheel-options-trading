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
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Sequence

from wheel import optionsmath
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
) -> CcExit:
    """``share_blocks``: every share held, oldest first. ``calls``: the call
    side of the chain for ``expiry``; ``None`` when it couldn't be loaded
    (``chain_unavailable_reason`` says why). ``quotes_as_of`` / ``fetched_at``
    / ``calculated_at`` are carried onto the result unchanged, so every
    display of it can show the same snapshot and calculation time."""
    stamps = {"quotes_as_of": quotes_as_of, "fetched_at": fetched_at, "calculated_at": calculated_at, "fees": fees}
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
        return CcExit(STATUS_NO_QUALIFYING, "No current stock price, so delta can't be worked out.", **sized)
    if expiry is None:
        return CcExit(
            STATUS_NO_QUALIFYING,
            chain_unavailable_reason or "No listed option expiration to check.",
            **sized,
        )
    days_to_expiry = (expiry - today).days
    sized = {**sized, "expiry": expiry, "days_to_expiry": days_to_expiry}
    if calls is None:
        return CcExit(STATUS_NO_QUALIFYING, chain_unavailable_reason or "Option prices couldn't be loaded.", **sized)

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
        **sized,
    )


def _shares(value: float) -> str:
    return f"{value:,.0f}" if abs(value - round(value)) < 1e-6 else f"{value:,.3f}"


def _money(value: float) -> str:
    return f"-${-value:,.2f}" if value < 0 else f"${value:,.2f}"


def _when(moment: datetime) -> str:
    """Local wall-clock time, e.g. "Oct 17, 2026 3:59 PM"."""
    return moment.astimezone().strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")


def _candidate_line(candidate: CandidateCall, *, picked: bool) -> str:
    delta = f"delta {candidate.delta:.2f}" if candidate.delta is not None else "delta n/a"
    if candidate.rejected_reason:
        return f"  {_money(candidate.strike)}: {delta}, rejected, {candidate.rejected_reason}"
    marker = " (pick)" if picked else ""
    return (
        f"  {_money(candidate.strike)}: {delta}, mid ${candidate.mid_premium:.2f}, "
        f"P&L if assigned {_money(candidate.projected_assignment_pnl)}{marker}"
    )


def _nearby_candidates(result: CcExit) -> list[CandidateCall]:
    """The strikes worth showing: the last one rejected for the minimum
    exit, then up to three from the minimum upward, always including the pick."""
    below = [c for c in result.candidates if c.strike < (result.preferred_min_exit or 0) - _EPS]
    above = [c for c in result.candidates if c not in below]
    shown = below[-1:] + above[:3]
    if result.pick and result.pick not in shown:
        shown.append(result.pick)
    return shown


def explain(result: CcExit) -> str:
    """The tooltip every CC TO EXIT cell shows, built from the live numbers."""
    if result.status == STATUS_NO_SHARES:
        return "CC TO EXIT\nNo shares held, so there is no covered call to write."

    lines = ["CC TO EXIT"]
    lines.append(f"Shares held: {_shares(result.shares_held)}")
    lines.append(f"Covered by open calls: {_shares(result.covered_shares)}")
    lines.append(f"Uncovered: {_shares(result.uncovered_shares)} (newest shares; open calls cover the oldest first)")
    if result.status == STATUS_ALL_COVERED:
        lines += ["", "ALL SHARES COVERED: no shares are free for another call."]
        return "\n".join(lines)

    if result.lot_cost is not None:
        lines.append(
            f"Next call covers {_shares(result.contracts * OPTION_MULTIPLIER)} shares "
            f"({result.contracts} contract{'s' if result.contracts != 1 else ''}), cost {_money(result.lot_cost)}/share"
        )
        lines.append(
            f"Minimum exit = {_money(result.lot_cost)} × {1 + CC_EXIT_MIN_GAIN_PCT / 100:.2f} = "
            f"{_money(result.raw_min_exit)}, rounded up to {_money(result.preferred_min_exit)}"
        )
    if result.expiry is not None:
        lines.append(f"Expiration: {result.expiry.strftime('%b %d, %Y')} ({result.days_to_expiry} days)")
        if result.expiry_in_window is False:
            low_dte, high_dte = CC_EXIT_DTE_WINDOW
            lines.append(f"  None listed {low_dte}-{high_dte} days out, so the closest to {CC_EXIT_TARGET_DTE} is used.")
    if result.quotes_as_of is not None or result.fetched_at is not None:
        lines.append(f"Option prices as of {_when(result.quotes_as_of or result.fetched_at)}")
    low, high = CC_EXIT_PREFERRED_DELTA_RANGE
    lines.append(f"Target delta: {CC_EXIT_TARGET_DELTA:.2f} (preferred {low:.2f} to {high:.2f})")

    if result.candidates:
        lines.append("")
        lines += [_candidate_line(c, picked=c is result.pick) for c in _nearby_candidates(result)]

    lines.append("")
    if result.status == STATUS_NO_QUALIFYING:
        lines.append(f"NO QUALIFYING CC: {result.reason}")
        return "\n".join(lines)

    pick = result.pick
    lines.append(f"CC TO EXIT = {_money(pick.strike)}, the qualifying strike closest to {CC_EXIT_TARGET_DELTA:.2f} delta.")
    if not low <= pick.delta <= high:
        lines.append(f"Its delta is outside {low:.2f} to {high:.2f}: no qualifying strike lands inside it.")
    shares_text = _shares(result.contracts * OPTION_MULTIPLIER)
    lines.append(
        f"Stock P&L at the strike = ({_money(pick.strike)} - {_money(result.lot_cost)}) × {shares_text} = "
        f"{_money(pick.stock_pnl)}"
    )
    lines.append(
        f"P&L if assigned = stock {_money(pick.stock_pnl)} + premium {_money(pick.net_premium)} "
        f"- sale fee {_money(pick.sale_fee)} + earlier premium on these shares {_money(result.prior_credit)} "
        f"= {_money(pick.projected_assignment_pnl)}"
    )
    fees = result.fees
    lines.append(
        f"Premium is the bid/ask mid less ${fees.option_fee_per_contract:.2f} per contract ({fees.option_fee_source})."
    )
    lines.append(
        f"Sale fee ≈ {_money(pick.strike)} × {shares_text} shares × "
        f"${fees.sale_fee_rate * 1_000_000:.2f} per $1M sold ({fees.sale_fee_source}); "
        "per-trade rounding and per-share charges aren't modeled."
    )
    lines.append("The minimum exit is a floor on the strike, not advice that selling there is the best move.")
    return "\n".join(lines)
