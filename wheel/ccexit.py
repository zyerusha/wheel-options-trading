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
    current_price: float | None = None
    # The calendar date DTE is counted from (the calculation date).
    today: date | None = None
    # True when an input the rules need was unavailable (the price, the
    # shares' cost, a listed expiration, the chain), as opposed to every
    # input loading and no call passing.
    market_data_missing: bool = False

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


def _strike_table(result: CcExit) -> dict | None:
    rows = []
    for candidate in _nearby_candidates(result):
        picked = candidate is result.pick
        if picked:
            reason = None
        elif candidate.qualifies:
            reason = f"Delta further from the {CC_EXIT_TARGET_DELTA:.2f} target"
        else:
            reason = candidate.rejected_reason[:1].upper() + candidate.rejected_reason[1:]
        meets_min = candidate.strike >= (result.preferred_min_exit or 0) - _EPS
        rows.append(
            tc.table_row(
                [
                    _money(candidate.strike),
                    tc.delta(candidate.delta),
                    _money(candidate.mid_premium) if candidate.mid_premium is not None else "Unavailable",
                    _money(candidate.projected_assignment_pnl)
                    if candidate.projected_assignment_pnl is not None
                    else "Unavailable",
                    "PASS" if meets_min else "FAIL",
                ],
                picked=picked,
                qualifies=candidate.qualifies,
                reason=reason,
            )
        )
    return tc.table(
        ["Strike", "Delta", "Midpoint", "P&L if assigned", "Min exit"],
        [True, True, True, True, False],
        rows,
    )


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
