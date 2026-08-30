"""External cash-flow classification, XIRR, and the S&P 500 benchmark replay.

Three independent, dependency-free pieces:

**Classification** decides which non-trade ledger rows are money genuinely
entering or leaving the account (a wire, a check, a rollover) versus money that
just moved *within* it (a dividend, a fee, a corporate-action rename). Only the
former belongs in a return calculation -- counting a dividend as a "contribution"
would make the account look like it needed less of its own performance to reach
its ending value than it actually did.

**XIRR** is the money-weighted return: the single annualized rate that makes the
present value of every dated cash flow net to zero. It is the only fair return
figure once money enters or leaves at different times, which point-to-point
value comparison ignores entirely.

**The benchmark replay** answers "what if these same dollars, on these same
dates, had bought SPY instead?" by buying/selling a synthetic SPY position on
each cash-flow date. Feeding the real account's own contribution timing into the
benchmark -- rather than comparing a lump-sum SPY return to the account's XIRR --
is what isolates "did the strategy beat the index" from "the user happened to add
money before a rally," which would otherwise bias the comparison in either
direction.

This module has no knowledge of *why* the caller's cash-flow list contains what
it contains -- callers are free to include a synthetic "opening balance" flow at
the first known valuation date (the account almost always holds value before the
earliest transaction history available), which is what makes an otherwise
flow-free account's return computable at all. See ``wheel/api.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Callable, Sequence

from wheel.parser import OTHER, Transaction

EXTERNAL_IN = "EXTERNAL_CASH_IN"
EXTERNAL_OUT = "EXTERNAL_CASH_OUT"
INTERNAL = "INTERNAL"
UNCLASSIFIED = "UNCLASSIFIED"

# Ordered most-specific-first, the same idiom as wheel.parser._ACTION_PATTERNS.
# "DISTRIBUTION NAME/SYMBOL CHANGE" must precede any looser "distribution" rule:
# it is a corporate-action rename (see docs/DESIGN.md "Corporate actions"), not
# cash leaving or entering the account.
_CASHFLOW_PATTERNS: Sequence[tuple[str, str]] = (
    (r"DISTRIBUTION\s+NAME/SYMBOL\s+CHANGE", INTERNAL),
    (r"IN\s+LIEU\s+OF.*PAYOUT", INTERNAL),
    (r"DIVIDEND\s+RECEIVED", INTERNAL),
    (r"REINVESTMENT", INTERNAL),
    (r"FEE\s+CHARGED", INTERNAL),
    (r"FOREIGN\s+TAX\s+PAID", INTERNAL),
    (r"INTEREST\s+(FULLY\s+)?PAID", INTERNAL),
    (r"DECREASE\s+COLLATERAL|INCREASE\s+COLLATERAL", INTERNAL),
    (r"TRANSFER\s+OF\s+ASSETS.*(RECEIVED|DEPOSIT)", EXTERNAL_IN),
    (r"TRANSFER\s+OF\s+ASSETS.*(SENT|DELIVERED|WITHDRAWN)", EXTERNAL_OUT),
    (r"(CASH|ELECTRONIC|WIRE|CHECK).*(RECEIVED|DEPOSIT)", EXTERNAL_IN),
    (r"(CASH|ELECTRONIC|WIRE|CHECK).*(SENT|WITHDRAWAL|DISBURSEMENT)", EXTERNAL_OUT),
    (r"DIRECT\s+DEPOSIT", EXTERNAL_IN),
    (r"CONTRIBUTION", EXTERNAL_IN),
    (r"ROLLOVER", EXTERNAL_IN),
)


def classify_cashflow(action_raw: str) -> str:
    """Map a non-trade ledger row's free text to a cash-flow category."""
    text = (action_raw or "").upper()
    for pattern, category in _CASHFLOW_PATTERNS:
        if re.search(pattern, text):
            return category
    return UNCLASSIFIED


@dataclass(frozen=True)
class CashFlowEvent:
    """One dated dollar amount moving into or out of the account.

    ``amount`` follows the same sign convention as ``Transaction.amount``:
    positive means cash was added to the account, negative means cash left it.
    """

    date: date
    amount: float
    label: str
    source: str
    kind: str = EXTERNAL_IN


def external_cashflows(transactions: Sequence[Transaction]) -> tuple[list[CashFlowEvent], list[str]]:
    """Deposits/withdrawals implied by non-trade ledger rows.

    Only ``OTHER``-action rows are candidates -- every trade action (options,
    BUY_STOCK/SELL_STOCK) moves cash between the market and cash-on-hand, never
    in or out of the account, so trade rows are never cash-flow events.
    ``UNCLASSIFIED`` rows are excluded from the returned events and instead
    produce one warning per distinct action text (deduped), never a silent guess.
    """
    events: list[CashFlowEvent] = []
    warned: set[str] = set()
    warnings: list[str] = []

    for transaction in transactions:
        if transaction.action != OTHER:
            continue
        category = classify_cashflow(transaction.action_raw)
        if category == INTERNAL:
            continue
        if category == UNCLASSIFIED:
            if transaction.action_raw not in warned:
                warned.add(transaction.action_raw)
                warnings.append(f"unclassified ledger row, excluded from cash-flow totals: {transaction.action_raw!r}")
            continue
        events.append(
            CashFlowEvent(
                date=transaction.event_date,
                amount=transaction.amount,
                label=transaction.action_raw,
                source=transaction.source,
                kind=category,
            )
        )

    events.sort(key=lambda event: event.date)
    return events, warnings


# --------------------------------------------------------------------------
# XIRR
# --------------------------------------------------------------------------


def _xnpv(rate: float, flows: Sequence[tuple[date, float]], t0: date) -> float:
    return sum(amount / (1.0 + rate) ** ((when - t0).days / 365.0) for when, amount in flows)


def _xnpv_derivative(rate: float, flows: Sequence[tuple[date, float]], t0: date) -> float:
    total = 0.0
    for when, amount in flows:
        t = (when - t0).days / 365.0
        if t == 0:
            continue
        total += -t * amount / (1.0 + rate) ** (t + 1.0)
    return total


def _bisect_xirr(
    flows: Sequence[tuple[date, float]], t0: date, tol: float, max_iterations: int
) -> float | None:
    """Bracket a root of xnpv(rate) over (-0.9999, 10.0) by scanning, then bisect."""
    lo, hi = -0.9999, 10.0
    steps = 400
    prev_r, prev_f = lo, _xnpv(lo, flows, t0)
    if abs(prev_f) < tol:
        return prev_r * 100.0

    bracket: tuple[float, float] | None = None
    for i in range(1, steps + 1):
        r = lo + (hi - lo) * i / steps
        f = _xnpv(r, flows, t0)
        if abs(f) < tol:
            return r * 100.0
        if (f > 0) != (prev_f > 0):
            bracket = (prev_r, r)
            break
        prev_r, prev_f = r, f

    if bracket is None:
        return None

    lo, hi = bracket
    f_lo = _xnpv(lo, flows, t0)
    mid = lo
    for _ in range(max_iterations):
        mid = (lo + hi) / 2.0
        f_mid = _xnpv(mid, flows, t0)
        if abs(f_mid) < tol:
            return mid * 100.0
        if (f_mid > 0) == (f_lo > 0):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
    return mid * 100.0


def xirr(
    cash_flows: Sequence[tuple[date, float]],
    *,
    guess: float = 0.1,
    tol: float = 1e-7,
    max_iterations: int = 100,
) -> float | None:
    """Annualized money-weighted return, as a percent.

    Standard sign convention: negative amounts leave the investor's pocket (a
    contribution), positive amounts return to it (a withdrawal, or a final flow
    standing in for "as if liquidated" on that date). Returns ``None`` -- never
    raises -- when fewer than two flows are given, or every flow shares one sign
    (no rate can reconcile a series with no sign change).

    Newton's method runs first; if it fails to converge, or steps outside the
    ``rate > -1`` domain, a bisection fallback searches the same domain for a
    sign change and narrows it, which is slower but cannot diverge.
    """
    flows = sorted(cash_flows, key=lambda flow: flow[0])
    if len(flows) < 2:
        return None
    amounts = [amount for _, amount in flows]
    if all(amount >= 0 for amount in amounts) or all(amount <= 0 for amount in amounts):
        return None

    t0 = flows[0][0]
    scale = max(abs(amount) for amount in amounts) or 1.0
    rate = guess

    for _ in range(max_iterations):
        npv = _xnpv(rate, flows, t0)
        if abs(npv) < tol * scale:
            return rate * 100.0
        derivative = _xnpv_derivative(rate, flows, t0)
        if derivative == 0:
            break
        new_rate = rate - npv / derivative
        if new_rate <= -0.9999:
            new_rate = (rate - 0.9999) / 2.0
        rate = new_rate

    if abs(_xnpv(rate, flows, t0)) < tol * scale:
        return rate * 100.0

    return _bisect_xirr(flows, t0, tol=tol * scale, max_iterations=200)


# --------------------------------------------------------------------------
# Benchmark replay
# --------------------------------------------------------------------------


def simulate_benchmark_series(
    cash_flows: Sequence[CashFlowEvent],
    valuation_dates: Sequence[date],
    price_lookup: Callable[[date], object],
) -> dict[date, float]:
    """Replay ``cash_flows`` into a synthetic SPY position, valued at each date.

    Each event buys (a positive amount) or sells (a negative amount)
    ``amount / price_lookup(event.date).close`` shares -- no fees, no slippage.
    Feeding the account's *own* contribution timing into the benchmark, rather
    than comparing a lump-sum benchmark return to the account's XIRR, is what
    makes the comparison fair: money-weighted return depends on when money
    moved, so identical timing on both sides isolates "did the strategy beat
    buy-and-hold SPY" from "money happened to arrive before a rally."

    A valuation date earlier than the cached price series, or an event date the
    lookup can't price, is simply omitted from the affected computation rather
    than defaulted to zero -- callers must treat a missing valuation date as
    unavailable, not as a zero balance.
    """
    events = sorted(cash_flows, key=lambda event: event.date)
    result: dict[date, float] = {}

    for valuation_date in sorted(set(valuation_dates)):
        final_price = price_lookup(valuation_date)
        if final_price is None:
            continue
        shares = 0.0
        for event in events:
            if event.date > valuation_date:
                break
            price = price_lookup(event.date)
            if price is None:
                continue
            shares += event.amount / price.close
        result[valuation_date] = shares * final_price.close

    return result


def simulate_benchmark(
    cash_flows: Sequence[CashFlowEvent], as_of: date, price_lookup: Callable[[date], object]
) -> float | None:
    """Convenience wrapper: the benchmark's mark-to-market value on one date."""
    return simulate_benchmark_series(cash_flows, [as_of], price_lookup).get(as_of)


@dataclass(frozen=True)
class BenchmarkResult:
    as_of: date
    actual_terminal_value: float
    benchmark_terminal_value: float | None
    actual_xirr_pct: float | None
    benchmark_xirr_pct: float | None
    value_added: float | None
    cash_flow_events: list[CashFlowEvent]


def compare_to_benchmark(
    cash_flows: Sequence[CashFlowEvent],
    actual_terminal_value: float,
    benchmark_terminal_value: float | None,
    as_of: date,
) -> BenchmarkResult:
    """Pure function, no I/O: builds two XIRR flow lists that differ only in
    their terminal "as if liquidated on ``as_of``" flow, so the actual account
    and the benchmark are compared on identical contribution/withdrawal timing.
    """
    investor_flows = [(event.date, -event.amount) for event in cash_flows]

    actual_flows = investor_flows + [(as_of, actual_terminal_value)]
    actual_rate = xirr(actual_flows)

    benchmark_rate = None
    value_added = None
    if benchmark_terminal_value is not None:
        benchmark_flows = investor_flows + [(as_of, benchmark_terminal_value)]
        benchmark_rate = xirr(benchmark_flows)
        value_added = actual_terminal_value - benchmark_terminal_value

    return BenchmarkResult(
        as_of=as_of,
        actual_terminal_value=actual_terminal_value,
        benchmark_terminal_value=benchmark_terminal_value,
        actual_xirr_pct=actual_rate,
        benchmark_xirr_pct=benchmark_rate,
        value_added=value_added,
        cash_flow_events=list(cash_flows),
    )
