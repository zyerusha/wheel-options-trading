"""Wheel performance metrics.

Three families of number are produced here:

**Realized P/L** -- the sum of broker cash flows on closed option legs plus
realized stock P/L on disposed share lots.  Because Fidelity's ``Amount ($)`` is
already net of commission and fees, no fee adjustment is applied on top; doing so
would double-count.

**Capital** -- collateral is measured on a daily timeline rather than as a single
snapshot, because a wheel's committed capital changes every time a put is rolled
to a different strike or shares are assigned.  Cash-secured puts commit
``strike x 100 x contracts``; assigned shares commit their cost basis; a covered
call commits nothing extra, since the shares already carry the capital -- unless
those shares pre-date the export, when the strike stands in for them.

**Return** -- ``roi_pct`` and ``roi_on_peak_pct`` are quoted against the
collateral committed on day one and against the peak, respectively, using
``net_realized_pl`` (premium plus realized stock P/L), since they exist to show
how sensitive ROI is to denominator choice on a resized position, not to gauge
the wheel's own option income.

The headline **Wheel ROC** (``roi_on_avg_wheel_pct`` and its annualized form,
``annualized_wheel_roc_pct``) is a different, narrower question: how much did
the wheel's *option activity* return on the capital it tied up, over time.  Its
numerator is ``option_realized_pl`` only -- premium credits minus debits paid to
close -- and never includes stock P/L, whether realized or not, gained or lost.
A put assigned at $100 that later trades at $80 does not make the wheel's ROC
negative; the stock is capital the wheel has tied up, not a loss the wheel's
option leg took. The brokerage export already carries the stock's own P/L
(``stock_realized_pl``, and ``net_realized_pl`` for the sum) for anyone who wants
the full investment picture; this tracker's ROC number stays scoped to what the
option strategy itself produced.

``option_realized_pl`` sums every leg in the cycle regardless of strategy --
it always has, because nothing here ever filtered by :data:`WHEEL_STRATEGIES`.
That includes protective puts and the long legs of credit-spread hedges
(``LONG_PUT`` / ``LONG_CALL``), whose own ``realized_pl`` already nets the
purchase against whatever it was later sold, exercised or expired for -- a put
bought for $1,000 and sold for $700 contributes -$300, never -$1,000 or +$700
separately.  ``wheel_core_realized_pl`` (CSP + covered-call legs) and
``hedge_realized_pl`` (everything else -- the model has no notion of "this
long put hedges that short put", so every non-core leg is a hedge) are exposed
as the two addends of ``option_realized_pl`` purely so a caller -- the
dashboard's calculation tooltip, in particular -- can show hedge cost/recovery
as its own line rather than asserting a number with no visible components.

Capital for a long leg (``long_premium``) is the actual debit paid, decaying to
$0 the day it closes -- never the option's notional -- so a protective put's
cost lives in the P/L numerator, not as inflated capital in the denominator.
A short leg paired into a same-day :class:`~wheel.engine.Spread` (short + long,
same underlying/right/expiry, opened together -- see :func:`capital_timeline`
and ``wheel.engine.WheelEngine._detect_spreads``) reports the netted
``|short strike - long strike| x 100`` collateral for its paired portion instead
of the full CSP/covered-call figure; an unpaired short leg, or one in an
ambiguous multi-candidate group, is unaffected and still gets full collateral.

Both variants use the time-weighted average collateral as their denominator,
which is the only one that correctly credits a position for freeing capital
early, and both annualize by scaling to ``DAYS_PER_YEAR / days_active``.

**Dual-track returns** -- ``net_option_yield_pct`` and ``total_position_roi_pct``
(with their annualized forms) sit alongside the pair above, quoted against
*initial* collateral rather than the time-weighted average. Net Option Yield is
``option_realized_pl / initial_collateral``, the same numerator as Wheel ROC on a
different denominator. Total Position ROI adds ``stock_realized_pl``,
``stock_unrealized_pl`` (shares still held, marked to the latest fetched price --
see :mod:`wheel.marketdata`) and ``dividends_received`` (see
:func:`dividends_by_cycle`) on top -- everything except an open long option's
unrealized P/L, which stays ``None`` (``long_leg_unrealized_pl``): no options-quote
feed exists anywhere in this project to mark one to market.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Sequence

from wheel.cashflow import dividend_transactions
from wheel.engine import (
    ACTIVE,
    COVERED_CALL,
    CSP,
    LONG,
    SHORT,
    WHEEL_STRATEGIES,
    Cycle,
    ShareLot,
)
from wheel.parser import Transaction

DAYS_PER_YEAR = 365.0


# --------------------------------------------------------------------------
# Capital timeline
# --------------------------------------------------------------------------


@dataclass
class CapitalPoint:
    """Committed capital on one day, split by what is tying it up."""

    day: date
    put_collateral: float = 0.0  # cash secured against short puts
    stock_basis: float = 0.0  # cost basis of shares held
    long_premium: float = 0.0  # debit tied up in long options
    call_collateral: float = 0.0  # short calls with no tracked shares (proxy)
    spread_collateral: float = 0.0  # netted (short strike - long strike) x 100 for paired legs

    @property
    def total(self) -> float:
        return (
            self.put_collateral
            + self.stock_basis
            + self.long_premium
            + self.call_collateral
            + self.spread_collateral
        )


def _active_spread_contracts(spread, leg_by_id, day: date) -> float:
    """How much of ``spread``'s paired quantity is still protected on ``day``.

    A spread's netting only holds while *both* legs remain open -- if one side
    closes early the other reverts to its own naked formula for whatever
    remains -- so this is capped by both legs' own remaining contracts, not
    just the quantity paired at open time.
    """
    if day < spread.open_date:
        return 0.0
    short_leg = leg_by_id.get(spread.short_leg_id)
    long_leg = leg_by_id.get(spread.long_leg_id)
    if short_leg is None or long_leg is None:
        return 0.0
    return min(
        spread.paired_contracts,
        short_leg.remaining_contracts_on(day),
        long_leg.remaining_contracts_on(day),
    )


def capital_timeline(cycle: Cycle, through: date) -> list[CapitalPoint]:
    """Daily committed capital for a cycle, from its start through ``through``.

    ``long_premium`` (a protective put, or the unpaired portion of a
    credit-spread's long leg) is the actual debit paid per contract while the
    leg is open, never the option's notional -- it drops to $0 the day it
    closes, at which point its cost is already accounted for in
    ``option_realized_pl`` instead.

    A short leg gets full cash-secured-put or covered-call collateral for
    whatever portion of it is *not* paired into a :class:`~wheel.engine.Spread`
    that day. The paired portion instead contributes to ``spread_collateral``
    at the netted ``(short strike - long strike) x 100`` rate -- see
    :class:`~wheel.engine.Spread` and :func:`_active_spread_contracts`.
    Pairing is same-day-open only and never ambiguous (see
    ``WheelEngine._detect_spreads``), so this never guesses which legs belong
    together; a short leg with no same-day long partner still gets full
    collateral, unchanged from before spreads existed.
    """
    end = cycle.end_date or through
    if end < cycle.start_date:
        end = cycle.start_date

    leg_by_id = {leg.leg_id: leg for leg in cycle.legs}

    points: list[CapitalPoint] = []
    day = cycle.start_date
    while day <= end:
        spread_collateral = 0.0
        protected: dict[str, float] = {}
        for spread in cycle.spreads:
            active = _active_spread_contracts(spread, leg_by_id, day)
            if active <= 1e-9:
                continue
            spread_collateral += active * spread.collateral_per_contract
            protected[spread.short_leg_id] = protected.get(spread.short_leg_id, 0.0) + active
            protected[spread.long_leg_id] = protected.get(spread.long_leg_id, 0.0) + active

        put_collateral = call_collateral = long_premium = 0.0
        for leg in cycle.legs:
            naked = max(leg.remaining_contracts_on(day) - protected.get(leg.leg_id, 0.0), 0.0)
            contribution = naked * leg.collateral_per_contract
            if leg.strategy == CSP:
                put_collateral += contribution
            elif leg.strategy == COVERED_CALL and not leg.shares_tracked:
                call_collateral += contribution
            elif leg.side == LONG:
                long_premium += contribution

        points.append(
            CapitalPoint(
                day=day,
                put_collateral=put_collateral,
                call_collateral=call_collateral,
                long_premium=long_premium,
                spread_collateral=spread_collateral,
                stock_basis=sum(lot.capital_on(day) for lot in cycle.share_lots),
            )
        )
        day += timedelta(days=1)
    return points


def _time_weighted_average(points: Sequence[CapitalPoint]) -> float:
    """Mean committed capital over the days the cycle actually held anything.

    Days at zero committed capital are excluded: a cycle that sits flat between
    an expiry and the next entry should not have its denominator diluted.
    """
    engaged = [point.total for point in points if point.total > 1e-9]
    return sum(engaged) / len(engaged) if engaged else 0.0


# --------------------------------------------------------------------------
# Cost basis
# --------------------------------------------------------------------------


def net_adjusted_cost_basis(cycle: Cycle, lot: ShareLot) -> float | None:
    """The wheel's own break-even per share: strike minus every dollar of net
    option cash flow the cycle has produced, allocated to this lot.

    Distinct from ``lot.basis_per_share`` (the raw tax-lot basis -- the bare
    assignment or purchase price, exactly what a 1099-B would show) which
    this function never touches or reads back into: the two numbers answer
    different questions and both stay available side by side.

    The formula is ``strike - net premiums/share + fees/share``, but every
    cash figure already on a leg (``open_cash``, ``LegClose.cash``) is the
    broker's own Amount, already net of commission and fees -- see
    ``wheel/parser.py``. Reconstructing gross premium and then subtracting
    fees back out algebraically collapses to using the already-fee-net total
    directly (``-gross/share + fees/share == -(gross-fees)/share ==
    -net/share``), so that is what this does; adding a fee term on top of the
    already-fee-net total would double the fee's cost, the same trap this
    module's docstring warns about elsewhere.

    Cumulative and whole-cycle: every roll, every covered call sold after
    assignment, folds into the same running total, not just the leg that
    produced this particular lot. When a cycle holds more than one concurrent
    lot (e.g. two partial assignments at different strikes before either
    sells), the cycle's net premium is allocated pro-rata by share count,
    against every share the cycle's lots ever held -- not just what remains
    today -- so the allocation stays stable as shares are later sold off.

    ``None`` when the lot's own basis is unknown (a ``PRE_HISTORY`` lot, see
    ``wheel/engine.py``): there is no strike to net against.
    """
    if lot.basis_per_share is None or lot.shares <= 0:
        return None
    total_shares = sum(other.shares for other in cycle.share_lots)
    if total_shares <= 0:
        return None
    net_cash_flow = sum(leg.open_cash + sum(close.cash for close in leg.closes) for leg in cycle.legs)
    allocated = net_cash_flow * (lot.shares / total_shares)
    return lot.basis_per_share - allocated / lot.shares


# --------------------------------------------------------------------------
# Dividends
# --------------------------------------------------------------------------


def dividends_by_cycle(cycles: Sequence[Cycle], transactions: Sequence[Transaction]) -> dict[str, float]:
    """Attribute every dividend transaction to the one cycle that was open
    when it posted, keyed by ``cycle_id`` -- feeds Total Position ROI's
    ``dividends`` argument.

    A dividend row carries the underlying's own ticker (never an option
    symbol), so it is first grouped by ``underlying``, then matched against
    that ticker's cycles by ``[start_date, end_date or still-open]``. On the
    rare day one cycle closes and another for the same ticker opens, the
    earlier (closing) cycle claims it: cycles are walked in their existing,
    chronological order and the first whole window that contains the date
    wins, and a later cycle for the same ticker can never start before the
    earlier one's own end_date.
    """
    by_underlying: dict[str, list[Cycle]] = {}
    for cycle in cycles:
        by_underlying.setdefault(cycle.underlying, []).append(cycle)

    result: dict[str, float] = {}
    for dividend in dividend_transactions(transactions):
        for cycle in by_underlying.get(dividend.underlying, []):
            end = cycle.end_date
            if cycle.start_date <= dividend.event_date and (end is None or dividend.event_date <= end):
                result[cycle.cycle_id] = result.get(cycle.cycle_id, 0.0) + dividend.amount
                break
    return result


# --------------------------------------------------------------------------
# Cycle metrics
# --------------------------------------------------------------------------


@dataclass
class CycleMetrics:
    cycle_id: str
    underlying: str
    status: str
    start_date: date
    end_date: date | None
    days_active: int

    premium_received: float = 0.0  # credits taken in on short opens
    premium_paid: float = 0.0  # debits paid to close shorts (negative)
    option_realized_pl: float = 0.0  # = wheel_core_realized_pl + hedge_realized_pl
    wheel_core_realized_pl: float = 0.0  # CSP + covered-call legs only
    hedge_realized_pl: float = 0.0  # protective puts + credit-spread legs (LONG_PUT/LONG_CALL)
    option_open_premium: float = 0.0  # credit held on still-open legs
    stock_realized_pl: float = 0.0
    stock_basis_unknown_shares: float = 0.0
    fees: float = 0.0
    net_realized_pl: float = 0.0

    initial_collateral: float = 0.0
    peak_collateral: float = 0.0
    avg_collateral: float = 0.0
    current_collateral: float = 0.0
    put_collateral_now: float = 0.0
    stock_basis_now: float = 0.0
    call_collateral_now: float = 0.0
    capital_estimated: bool = False

    roi_pct: float | None = None  # net P/L (incl. stock) vs initial collateral
    roi_on_peak_pct: float | None = None  # net P/L (incl. stock) vs peak collateral
    roi_on_avg_wheel_pct: float | None = None  # option P/L only, vs time-weighted avg collateral
    annualized_wheel_roc_pct: float | None = None  # the headline Wheel ROC: option P/L only

    # Dual-track pair, reported side by side with the figures above rather
    # than replacing them -- see the module docstring's "Dual-track returns"
    # section. Both are quoted against initial_collateral, not the
    # time-weighted average roi_on_avg_wheel_pct/annualized_wheel_roc_pct use.
    dividends_received: float = 0.0
    stock_unrealized_pl: float | None = None  # None: no shares held, or no price available
    # Mark-to-market for an *open* long put/call is out of scope -- no options
    # quote feed exists anywhere in this project. Always None; present so a
    # caller can display "not available" rather than inferring absence.
    long_leg_unrealized_pl: None = None
    net_option_yield_pct: float | None = None  # option_realized_pl / initial_collateral
    annualized_net_option_yield_pct: float | None = None
    total_position_roi_pct: float | None = None  # everything (incl. unrealized stock, dividends) / initial_collateral
    annualized_total_position_roi_pct: float | None = None

    legs_total: int = 0
    legs_open: int = 0
    rolls: int = 0
    assignments: int = 0
    wins: int = 0
    losses: int = 0
    avg_days_in_trade: float | None = None
    synthetic_cash: float = 0.0  # assignment share flows not present in the export
    warnings: list[str] = field(default_factory=list)

    @property
    def win_rate_pct(self) -> float | None:
        """Winning legs over winning-plus-losing legs -- a secondary, diagnostic
        figure, never the primary performance number (that's the annualized
        Wheel ROC). ``wins`` and ``losses`` already come only from *closed*
        legs (open legs are excluded), and a leg with ``realized_pl == 0``
        counts toward neither, so it drops out of this ratio's denominator
        entirely rather than counting against it. ``None`` -- not ``0.0`` --
        when nothing has been decided yet, since 0% would misreport "no data"
        as "all losses".
        """
        decided = self.wins + self.losses
        return 100.0 * self.wins / decided if decided else None


def _safe_pct(numerator: float, denominator: float) -> float | None:
    return 100.0 * numerator / denominator if denominator > 1e-9 else None


def cycle_metrics(
    cycle: Cycle,
    through: date,
    *,
    current_price: float | None = None,
    dividends: float = 0.0,
) -> CycleMetrics:
    """Compute every headline number for one cycle.

    ``current_price`` and ``dividends`` are optional and default to values
    that leave the dual-track fields at their safe "unknown"/zero state --
    every existing call site keeps working unchanged. A caller wanting Total
    Position ROI to reflect open shares and dividends passes the ticker's
    latest close (see ``wheel.marketdata``) and this cycle's own dividend
    total (see ``wheel.cashflow.dividend_transactions``).
    """
    points = capital_timeline(cycle, through)
    end = cycle.end_date or through
    days_active = max((end - cycle.start_date).days, 0) or 1

    short_legs = [leg for leg in cycle.legs if leg.side == SHORT]
    closed_legs = [leg for leg in cycle.legs if not leg.is_open]

    premium_received = sum(leg.gross_premium for leg in short_legs)
    premium_paid = sum(
        close.cash for leg in short_legs for close in leg.closes if close.cash < 0
    )
    # Hedges (protective puts, credit-spread legs) are whatever isn't a plain
    # CSP or covered call -- see the module docstring. Summing the two halves
    # must equal summing every leg directly; nothing here filters legs out.
    wheel_core_realized = sum(leg.realized_pl for leg in cycle.legs if leg.strategy in WHEEL_STRATEGIES)
    hedge_realized = sum(leg.realized_pl for leg in cycle.legs if leg.strategy not in WHEEL_STRATEGIES)
    option_realized = wheel_core_realized + hedge_realized
    option_open_premium = sum(leg.open_premium for leg in cycle.legs if leg.is_open)

    stock_realized = 0.0
    unknown_shares = 0.0
    for lot in cycle.share_lots:
        for disposal in lot.disposals:
            if disposal["basis_known"]:
                stock_realized += disposal["realized"]
            else:
                unknown_shares += disposal["shares"]

    net_realized = option_realized + stock_realized

    initial = points[0].total if points else 0.0
    peak = max((point.total for point in points), default=0.0)
    average = _time_weighted_average(points)
    current = points[-1] if points else CapitalPoint(cycle.start_date)

    wins = sum(1 for leg in closed_legs if leg.realized_pl > 0)
    losses = sum(1 for leg in closed_legs if leg.realized_pl < 0)
    held = [leg.days_held for leg in closed_legs if leg.days_held is not None]

    annualized_wheel_roc = None
    if average > 1e-9:
        annualized_wheel_roc = 100.0 * (option_realized / average) * (DAYS_PER_YEAR / days_active)

    # Stock Unrealized P&L: mark every share still held to `current_price`.
    # None (not 0.0) when nothing is held or no price was supplied -- both are
    # "unknown", never "no gain" -- see the module-level "None means unknown"
    # convention _safe_pct/win_rate_pct already use.
    shares_held = sum(lot.remaining for lot in cycle.share_lots if lot.remaining > 1e-9)
    stock_unrealized: float | None = None
    if shares_held > 1e-9 and current_price is not None:
        stock_unrealized = sum(
            (current_price - lot.basis_per_share) * lot.remaining
            for lot in cycle.share_lots
            if lot.remaining > 1e-9 and lot.basis_per_share is not None
        )

    net_option_yield_pct = _safe_pct(option_realized, initial)
    annualized_net_option_yield = (
        net_option_yield_pct * (DAYS_PER_YEAR / days_active) if net_option_yield_pct is not None else None
    )

    total_position_pl = option_realized + stock_realized + (stock_unrealized or 0.0) + dividends
    total_position_roi_pct = _safe_pct(total_position_pl, initial)
    annualized_total_position_roi = (
        total_position_roi_pct * (DAYS_PER_YEAR / days_active) if total_position_roi_pct is not None else None
    )

    return CycleMetrics(
        cycle_id=cycle.cycle_id,
        underlying=cycle.underlying,
        status=cycle.status,
        start_date=cycle.start_date,
        end_date=cycle.end_date,
        days_active=days_active,
        premium_received=premium_received,
        premium_paid=premium_paid,
        option_realized_pl=option_realized,
        wheel_core_realized_pl=wheel_core_realized,
        hedge_realized_pl=hedge_realized,
        option_open_premium=option_open_premium,
        stock_realized_pl=stock_realized,
        stock_basis_unknown_shares=unknown_shares,
        fees=sum(leg.total_fees for leg in cycle.legs),
        net_realized_pl=net_realized,
        initial_collateral=initial,
        peak_collateral=peak,
        avg_collateral=average,
        current_collateral=current.total if cycle.is_open else 0.0,
        put_collateral_now=current.put_collateral if cycle.is_open else 0.0,
        stock_basis_now=current.stock_basis if cycle.is_open else 0.0,
        call_collateral_now=current.call_collateral if cycle.is_open else 0.0,
        capital_estimated=cycle.capital_estimated,
        roi_pct=_safe_pct(net_realized, initial),
        roi_on_peak_pct=_safe_pct(net_realized, peak),
        roi_on_avg_wheel_pct=_safe_pct(option_realized, average),
        annualized_wheel_roc_pct=annualized_wheel_roc,
        dividends_received=dividends,
        stock_unrealized_pl=stock_unrealized,
        net_option_yield_pct=net_option_yield_pct,
        annualized_net_option_yield_pct=annualized_net_option_yield,
        total_position_roi_pct=total_position_roi_pct,
        annualized_total_position_roi_pct=annualized_total_position_roi,
        legs_total=len(cycle.legs),
        legs_open=sum(1 for leg in cycle.legs if leg.is_open),
        rolls=len(cycle.rolls),
        assignments=len(cycle.assignments),
        wins=wins,
        losses=losses,
        avg_days_in_trade=sum(held) / len(held) if held else None,
        synthetic_cash=sum(assignment.cash for assignment in cycle.assignments),
        warnings=list(cycle.warnings),
    )


# --------------------------------------------------------------------------
# Portfolio metrics
# --------------------------------------------------------------------------


@dataclass
class PortfolioMetrics:
    cycles: int = 0
    active_cycles: int = 0
    tickers: int = 0
    first_date: date | None = None
    last_date: date | None = None
    days_span: int = 0

    premium_received: float = 0.0
    premium_paid: float = 0.0
    option_realized_pl: float = 0.0  # = wheel_core_realized_pl + hedge_realized_pl
    wheel_core_realized_pl: float = 0.0  # CSP + covered-call legs only
    hedge_realized_pl: float = 0.0  # protective puts + credit-spread legs (LONG_PUT/LONG_CALL)
    stock_realized_pl: float = 0.0
    net_realized_pl: float = 0.0
    open_premium: float = 0.0
    fees: float = 0.0

    capital_deployed_now: float = 0.0
    peak_capital: float = 0.0
    avg_capital: float = 0.0
    annualized_wheel_roc_pct: float | None = None  # the headline Wheel ROC: option P/L only
    roi_on_avg_wheel_pct: float | None = None

    # Dual-track pair, summed from cycle absolutes -- never averaged from each
    # cycle's own percentage -- and quoted against total_initial_collateral
    # (the sum of every cycle's own initial_collateral), a different
    # denominator concept from avg_capital above. See CycleMetrics for why
    # each field exists.
    total_initial_collateral: float = 0.0
    dividends_received: float = 0.0
    stock_unrealized_pl: float = 0.0
    net_option_yield_pct: float | None = None
    annualized_net_option_yield_pct: float | None = None
    total_position_roi_pct: float | None = None
    annualized_total_position_roi_pct: float | None = None

    total_legs: int = 0
    open_legs: int = 0
    rolls: int = 0
    assignments: int = 0
    wins: int = 0
    losses: int = 0
    avg_days_in_trade: float | None = None

    @property
    def win_rate_pct(self) -> float | None:
        """Winning legs over winning-plus-losing legs -- a secondary, diagnostic
        figure, never the primary performance number (that's the annualized
        Wheel ROC). ``wins`` and ``losses`` already come only from *closed*
        legs (open legs are excluded), and a leg with ``realized_pl == 0``
        counts toward neither, so it drops out of this ratio's denominator
        entirely rather than counting against it. ``None`` -- not ``0.0`` --
        when nothing has been decided yet, since 0% would misreport "no data"
        as "all losses".
        """
        decided = self.wins + self.losses
        return 100.0 * self.wins / decided if decided else None


def portfolio_capital_series(
    cycles: Iterable[Cycle], through: date, since: date | None = None
) -> list[CapitalPoint]:
    """Aggregate committed capital across all cycles, day by day.

    Days on which no cycle was live are emitted explicitly at zero rather than
    left out.  A caller plotting the result draws a straight line between
    consecutive points, so an absent stretch becomes a ramp asserting capital
    that was never committed -- filtering to one ticker can leave months of it.
    Filling only the interior keeps both endpoints, and every metric is
    unaffected: the average already skips zero days and the peak is a maximum.

    ``since``, if given, crops the returned series to that day onward -- but
    only *after* the full history has been reconstructed from ``cycles``.  A
    position opened well before ``since`` and still open when it arrives is
    therefore already showing its true committed capital on day one, rather
    than appearing to spring from nothing because ``cycles`` itself was built
    from a transaction slice that started at ``since``.
    """
    buckets: dict[date, list[float]] = {}
    for cycle in cycles:
        for point in capital_timeline(cycle, through):
            slot = buckets.setdefault(point.day, [0.0, 0.0, 0.0, 0.0, 0.0])
            slot[0] += point.put_collateral
            slot[1] += point.stock_basis
            slot[2] += point.long_premium
            slot[3] += point.call_collateral
            slot[4] += point.spread_collateral

    if not buckets:
        return []

    empty = [0.0, 0.0, 0.0, 0.0, 0.0]
    series: list[CapitalPoint] = []
    day, last = min(buckets), max(buckets)
    while day <= last:
        put, stock, long_premium, call, spread = buckets.get(day, empty)
        series.append(
            # Keyword arguments on purpose: the dataclass orders these fields
            # put/stock/long/call/spread while the JSON payload uses
            # put/stock/call/long, and a positional call here would silently
            # swap fields.
            CapitalPoint(
                day=day,
                put_collateral=put,
                stock_basis=stock,
                long_premium=long_premium,
                call_collateral=call,
                spread_collateral=spread,
            )
        )
        day += timedelta(days=1)

    if since is not None:
        series = [point for point in series if point.day >= since]
    return series


def portfolio_metrics(
    cycles: Sequence[Cycle],
    through: date,
    *,
    capital_cycles: Sequence[Cycle] | None = None,
    since: date | None = None,
    current_prices: dict[str, float] | None = None,
    dividends_by_cycle: dict[str, float] | None = None,
) -> PortfolioMetrics:
    """Roll cycle-level numbers up to the account level.

    ``cycles`` drives every P&L figure -- premium, realized P/L, wins, rolls --
    so it should already be scoped to whatever window "activity in this period"
    is meant to cover.  ``capital_cycles`` drives the committed-capital figures
    (average, peak, current, and the ROC denominator) and defaults to ``cycles``
    for callers that don't need the distinction.  Pass the same cycles rebuilt
    *without* a start-date cutoff here, plus that cutoff as ``since``, and a
    position opened before the window still contributes its true capital for
    every day the window covers -- see :func:`portfolio_capital_series`.

    The annualized figure is computed against the portfolio's own time-weighted
    average capital rather than by averaging per-cycle percentages, which would
    weight a one-day $1,400 trade the same as a two-month $60,000 one.

    ``current_prices`` (ticker -> latest close) and ``dividends_by_cycle``
    (cycle_id -> dividends received) feed the same-named ``cycle_metrics``
    keyword arguments for Total Position ROI; both default to empty, which
    leaves the dual-track fields at their safe defaults.
    """
    prices = current_prices or {}
    dividends = dividends_by_cycle or {}
    per_cycle = [
        cycle_metrics(
            cycle,
            through,
            current_price=prices.get(cycle.underlying),
            dividends=dividends.get(cycle.cycle_id, 0.0),
        )
        for cycle in cycles
    ]
    series = portfolio_capital_series(
        capital_cycles if capital_cycles is not None else cycles, through, since
    )

    result = PortfolioMetrics(
        cycles=len(per_cycle),
        active_cycles=sum(1 for metric in per_cycle if metric.status == ACTIVE),
        tickers=len({metric.underlying for metric in per_cycle}),
    )
    if not per_cycle:
        return result

    result.first_date = min(metric.start_date for metric in per_cycle)
    result.last_date = through
    result.days_span = max((result.last_date - result.first_date).days, 1)

    for metric in per_cycle:
        result.premium_received += metric.premium_received
        result.premium_paid += metric.premium_paid
        result.option_realized_pl += metric.option_realized_pl
        result.wheel_core_realized_pl += metric.wheel_core_realized_pl
        result.hedge_realized_pl += metric.hedge_realized_pl
        result.stock_realized_pl += metric.stock_realized_pl
        result.net_realized_pl += metric.net_realized_pl
        result.open_premium += metric.option_open_premium
        result.fees += metric.fees
        result.total_legs += metric.legs_total
        result.open_legs += metric.legs_open
        result.rolls += metric.rolls
        result.assignments += metric.assignments
        result.wins += metric.wins
        result.losses += metric.losses
        result.total_initial_collateral += metric.initial_collateral
        result.dividends_received += metric.dividends_received
        result.stock_unrealized_pl += metric.stock_unrealized_pl or 0.0

    result.capital_deployed_now = series[-1].total if series else 0.0
    result.peak_capital = max((point.total for point in series), default=0.0)
    result.avg_capital = _time_weighted_average(series)

    if result.avg_capital > 1e-9:
        result.roi_on_avg_wheel_pct = 100.0 * result.option_realized_pl / result.avg_capital
        result.annualized_wheel_roc_pct = result.roi_on_avg_wheel_pct * (
            DAYS_PER_YEAR / result.days_span
        )

    if result.total_initial_collateral > 1e-9:
        result.net_option_yield_pct = 100.0 * result.option_realized_pl / result.total_initial_collateral
        result.annualized_net_option_yield_pct = result.net_option_yield_pct * (
            DAYS_PER_YEAR / result.days_span
        )
        total_position_pl = (
            result.option_realized_pl
            + result.stock_realized_pl
            + result.stock_unrealized_pl
            + result.dividends_received
        )
        result.total_position_roi_pct = 100.0 * total_position_pl / result.total_initial_collateral
        result.annualized_total_position_roi_pct = result.total_position_roi_pct * (
            DAYS_PER_YEAR / result.days_span
        )

    weighted = [
        (metric.avg_days_in_trade, metric.wins + metric.losses)
        for metric in per_cycle
        if metric.avg_days_in_trade is not None
    ]
    total_weight = sum(weight for _, weight in weighted)
    if total_weight:
        result.avg_days_in_trade = sum(days * weight for days, weight in weighted) / total_weight

    return result


def ticker_summary(
    cycles: Sequence[Cycle],
    through: date,
    *,
    capital_cycles: Sequence[Cycle] | None = None,
    since: date | None = None,
    current_prices: dict[str, float] | None = None,
    dividends_by_cycle: dict[str, float] | None = None,
) -> list[dict]:
    """Per-underlying rollup used by the P/L and premium charts.

    Same split as :func:`portfolio_metrics`: P&L figures come from ``cycles``
    (the requested window), capital figures come from ``capital_cycles`` (that
    ticker's full history, cropped to ``since``) so a position opened before the
    window still shows its real committed capital rather than appearing to
    start from zero.

    Iterates the *union* of tickers in both sets, not just ``cycles``: a wheel
    that is fully dormant during the window -- no rolls, no assignments, nothing
    -- has no P&L cycle at all once ``cycles`` is date-filtered, but it can still
    be holding real capital the whole time.  Dropping it from this table would
    make it vanish from the P&L and ROC charts precisely when it did nothing,
    which is the opposite of what a capital-utilization view is for.
    """
    prices = current_prices or {}
    dividends = dividends_by_cycle or {}

    grouped: dict[str, list[Cycle]] = {}
    for cycle in cycles:
        grouped.setdefault(cycle.underlying, []).append(cycle)

    capital_grouped: dict[str, list[Cycle]] = {}
    for cycle in (capital_cycles if capital_cycles is not None else cycles):
        capital_grouped.setdefault(cycle.underlying, []).append(cycle)

    rows: list[dict] = []
    for underlying in set(grouped) | set(capital_grouped):
        group = grouped.get(underlying, [])
        cap_group = capital_grouped.get(underlying, group)
        metrics = [
            cycle_metrics(
                cycle,
                through,
                current_price=prices.get(underlying),
                dividends=dividends.get(cycle.cycle_id, 0.0),
            )
            for cycle in group
        ]
        series = portfolio_capital_series(cap_group, through, since)
        average = _time_weighted_average(series)
        net = sum(metric.net_realized_pl for metric in metrics)
        option_net = sum(metric.option_realized_pl for metric in metrics)
        total_initial = sum(metric.initial_collateral for metric in metrics)
        dividends_total = sum(metric.dividends_received for metric in metrics)
        stock_unrealized_total = sum(metric.stock_unrealized_pl or 0.0 for metric in metrics)
        stock_realized_total = sum(metric.stock_realized_pl for metric in metrics)
        total_position_pl = option_net + stock_realized_total + stock_unrealized_total + dividends_total
        total_position_roi_pct = _safe_pct(total_position_pl, total_initial)
        if metrics:
            span = max((through - min(metric.start_date for metric in metrics)).days, 1)
        elif series:
            # No P&L activity in the window at all -- span the capital series
            # itself, so a dormant-but-funded position still annualizes sanely
            # instead of falling back to a 1-day span.
            span = max((through - series[0].day).days, 1)
        else:
            span = 1
        rows.append(
            {
                "underlying": underlying,
                "cycles": len(group),
                "active": sum(1 for cycle in cap_group if cycle.status == ACTIVE),
                "capital_estimated": any(cycle.capital_estimated for cycle in cap_group),
                "premium_received": sum(metric.premium_received for metric in metrics),
                "premium_paid": sum(metric.premium_paid for metric in metrics),
                "option_realized_pl": option_net,
                "wheel_core_realized_pl": sum(metric.wheel_core_realized_pl for metric in metrics),
                "hedge_realized_pl": sum(metric.hedge_realized_pl for metric in metrics),
                "stock_realized_pl": sum(metric.stock_realized_pl for metric in metrics),
                "net_realized_pl": net,
                "open_premium": sum(metric.option_open_premium for metric in metrics),
                "fees": sum(metric.fees for metric in metrics),
                "legs": sum(metric.legs_total for metric in metrics),
                "open_legs": sum(metric.legs_open for metric in metrics),
                "rolls": sum(metric.rolls for metric in metrics),
                "assignments": sum(metric.assignments for metric in metrics),
                "wins": sum(metric.wins for metric in metrics),
                "losses": sum(metric.losses for metric in metrics),
                "avg_capital": average,
                "peak_capital": max((point.total for point in series), default=0.0),
                "capital_now": series[-1].total if series else 0.0,
                "days_span": span,  # denominator of the 365/span annualizing factor below
                "roi_on_avg_wheel_pct": _safe_pct(option_net, average),
                "annualized_wheel_roc_pct": (
                    _safe_pct(option_net, average) * (DAYS_PER_YEAR / span)
                    if average > 1e-9
                    else None
                ),
                "total_initial_collateral": total_initial,
                "dividends_received": dividends_total,
                "stock_unrealized_pl": stock_unrealized_total,
                "net_option_yield_pct": _safe_pct(option_net, total_initial),
                "annualized_net_option_yield_pct": (
                    _safe_pct(option_net, total_initial) * (DAYS_PER_YEAR / span)
                    if total_initial > 1e-9
                    else None
                ),
                "total_position_roi_pct": total_position_roi_pct,
                "annualized_total_position_roi_pct": (
                    total_position_roi_pct * (DAYS_PER_YEAR / span) if total_initial > 1e-9 else None
                ),
            }
        )
    rows.sort(key=lambda row: row["net_realized_pl"], reverse=True)
    return rows


def realized_pl_series(cycles: Sequence[Cycle]) -> list[dict]:
    """Cumulative option premium P/L and stock P/L, by event date.

    ``option_pl`` is the net option cash flow -- credits received minus debits
    paid to close, dated to when each leg closes -- and never reflects the
    underlying's price. ``stock_pl`` is realized gain/loss from selling
    shares: a separate source of profit or loss driven by price movement, not
    premium. ``total_pl`` is their sum, the full wheel result.
    """
    daily: dict[date, list[float]] = {}

    for cycle in cycles:
        for leg in cycle.legs:
            for close in leg.closes:
                slot = daily.setdefault(close.date, [0.0, 0.0])
                slot[0] += leg.cash_per_contract * close.contracts + close.cash
        for lot in cycle.share_lots:
            for disposal in lot.disposals:
                if disposal["basis_known"]:
                    daily.setdefault(disposal["date"], [0.0, 0.0])[1] += disposal["realized"]

    rows: list[dict] = []
    cumulative_option = cumulative_stock = 0.0
    for day in sorted(daily):
        option_pl, stock_pl = daily[day]
        cumulative_option += option_pl
        cumulative_stock += stock_pl
        rows.append(
            {
                "date": day.isoformat(),
                "option_pl": option_pl,
                "stock_pl": stock_pl,
                "total_pl": option_pl + stock_pl,
                "cum_option_pl": cumulative_option,
                "cum_stock_pl": cumulative_stock,
                "cum_total_pl": cumulative_option + cumulative_stock,
            }
        )
    return rows


def leg_rows(cycle: Cycle) -> list[dict]:
    """Flatten a cycle's legs for the detail table and timeline chart."""
    rows: list[dict] = []
    for leg in cycle.legs:
        paired_contracts = sum(leg.paired_contracts.values())
        naked_contracts = leg.contracts - paired_contracts
        rows.append(
            {
                "leg_id": leg.leg_id,
                "cycle_id": leg.cycle_id,
                "symbol": leg.occ_symbol,
                "underlying": leg.underlying,
                "right": leg.right,
                "strike": leg.strike,
                "expiry": leg.expiry.isoformat() if leg.expiry else None,
                "side": leg.side,
                "strategy": leg.strategy,
                "shares_tracked": leg.shares_tracked,
                "open_date": leg.open_date.isoformat(),
                "open_action": leg.open_action,
                "contracts": leg.contracts,
                "open_price": leg.open_price,
                "open_cash": leg.open_cash,
                "close_date": leg.close_date.isoformat() if leg.close_date else None,
                "remaining": leg.remaining_contracts,
                "outcome": leg.outcome,
                "realized_pl": leg.realized_pl,
                "open_premium": leg.open_premium,
                "fees": leg.total_fees,
                "days_held": leg.days_held,
                # Naked-only: contracts paired into a Spread report $0 extra
                # here, since their (smaller, netted) collateral is already
                # counted once at the Spread's own level -- see
                # wheel.metrics.capital_timeline and payload["spreads"].
                "collateral": leg.collateral_per_contract * naked_contracts,
                "paired_contracts": paired_contracts,
                "naked_contracts": naked_contracts,
                "spread_ids": list(leg.paired_contracts.keys()),
                "opened_by_roll": leg.open_roll_id,
                "is_wheel_leg": leg.strategy in WHEEL_STRATEGIES,
                "closes": [
                    {
                        "date": close.date.isoformat(),
                        "action": close.action,
                        "contracts": close.contracts,
                        "price": close.price,
                        "cash": close.cash,
                        "roll_id": close.roll_id,
                    }
                    for close in leg.closes
                ],
            }
        )
    rows.sort(key=lambda row: (row["open_date"], row["symbol"]))
    return rows
