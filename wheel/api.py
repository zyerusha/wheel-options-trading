"""JSON payload assembly for the dashboard.

Filtering rebuilds the cycles from the filtered transaction slice rather than
post-filtering finished cycles.  That costs a few milliseconds and buys the
guarantee the dataviz brief asks for: every stat, chart and table on the page is
derived from the same slice, so the numbers always agree with each other.
"""

from __future__ import annotations

import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from typing import Any, Sequence

from wheel import benchmark as bm
from wheel import cashflow as cf
from wheel import marketdata
from wheel.engine import COVERED_CALL, CSP, LONG, Cycle, WheelEngine, build_cycles
from wheel.fileio import peek_text
from wheel.insights import portfolio_insights, wheel_insights
from wheel.metrics import (
    capital_timeline,
    cycle_metrics,
    dividends_by_cycle,
    leg_rows,
    net_adjusted_cost_basis,
    portfolio_capital_series,
    portfolio_metrics,
    realized_pl_series,
    ticker_summary,
    weekly_ppd_series,
    wheel_cash_flow_events,
    wheel_state_breakdown,
    wheel_terminal_value,
)
from wheel.parser import (
    ASSIGNED,
    BTC,
    BTO,
    BUY_STOCK,
    EXPIRED,
    OPTION_ACTIONS,
    OPTION_MULTIPLIER,
    SELL_STOCK,
    STC,
    STO,
    TRADE_ACTIONS,
    MergeReport,
    ParseReport,
    Transaction,
    company_name_from_description,
    parse_exports,
)
from wheel.positions import discover_position_snapshots, latest_snapshot, latest_snapshot_per_account, load_snapshots

EXPORT_DIRS = (".", "data")


def _find_history_header(path: str) -> str | None:
    """The 'Run Date...' header line of a broker export, if this file has one."""
    head = peek_text(path)
    if head is None:
        return None
    return next((line for line in head.splitlines() if line.lower().startswith("run date")), None)


def _is_multi_account_header(header: str) -> bool:
    columns = {col.strip().strip('"').lower() for col in header.split(",")}
    return {"account", "account number"}.issubset(columns)


def looks_like_multi_account_export(path: str) -> bool:
    """Fidelity's combined, "Accounts_History.csv"-style download: every
    linked account's transactions in one file, distinguished by 'Account'/
    'Account Number' columns the single-account 'History_for_Account_*.csv'
    export never has.

    Not supported yet (see ``docs/DESIGN.md``, "Account folders"): its other
    column names don't match this parser's expected header either (``Amount``
    vs. ``Amount ($)``, ``Price`` vs. ``Price ($)``, etc.), so importing it as
    an ordinary export would silently zero out every dollar amount rather
    than fail loudly -- and even with the names fixed, there's still no
    per-row account column plumbed through to split trades by, which would
    merge different real accounts' cycles together. Kept out of
    :func:`looks_like_export` (and therefore out of discovery, uploads, and
    the dataset picker) until that's built.
    """
    header = _find_history_header(path)
    return header is not None and _is_multi_account_header(header)


def looks_like_export(path: str) -> bool:
    """Cheap header peek: does this CSV look like a *supported* broker history export?

    Keeps generated output and unrelated CSVs out of discovery and out of the
    dashboard's file picker, without paying for a full parse. A file that
    otherwise looks like an export but carries 'Account'/'Account Number'
    columns is Fidelity's multi-account download, not the single-account
    dialect this parser handles -- see :func:`looks_like_multi_account_export`.
    """
    header = _find_history_header(path)
    if header is None:
        return False
    return not _is_multi_account_header(header)


def discover_exports(directories: Sequence[str] = EXPORT_DIRS) -> list[str]:
    """Every broker export we can find, so no account number is hard-coded."""
    found: list[str] = []
    for directory in directories:
        if not os.path.isdir(directory):
            continue
        for entry in sorted(os.listdir(directory)):
            path = os.path.join(directory, entry)
            if entry.lower().endswith(".csv") and looks_like_export(path):
                found.append(path)
    return found


def discover_multi_account_exports(directories: Sequence[str] = EXPORT_DIRS) -> list[str]:
    """Every not-yet-supported multi-account export found, so callers can
    tell the user it was seen and skipped, rather than it vanishing silently
    the way ``discover_exports`` (correctly) leaves it out.
    """
    found: list[str] = []
    for directory in directories:
        if not os.path.isdir(directory):
            continue
        for entry in sorted(os.listdir(directory)):
            path = os.path.join(directory, entry)
            if entry.lower().endswith(".csv") and looks_like_multi_account_export(path):
                found.append(path)
    return found


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------


@dataclass
class Filters:
    tickers: list[str] = field(default_factory=list)
    start: date | None = None
    end: date | None = None
    statuses: list[str] = field(default_factory=list)

    @classmethod
    def from_query(cls, query: dict[str, list[str]]) -> "Filters":
        def first(key: str) -> str | None:
            values = query.get(key)
            return values[0].strip() if values and values[0].strip() else None

        def csv_list(key: str) -> list[str]:
            raw = first(key)
            return [part.strip().upper() for part in raw.split(",") if part.strip()] if raw else []

        def parse_day(key: str) -> date | None:
            raw = first(key)
            if not raw:
                return None
            try:
                return datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                return None

        return cls(
            tickers=csv_list("tickers"),
            start=parse_day("start"),
            end=parse_day("end"),
            statuses=csv_list("status"),
        )

    def matches_transaction(self, transaction: Transaction) -> bool:
        if self.tickers and transaction.underlying not in self.tickers:
            return False
        if self.start and transaction.event_date < self.start:
            return False
        if self.end and transaction.event_date > self.end:
            return False
        return True


# --------------------------------------------------------------------------
# Serialization helpers
# --------------------------------------------------------------------------


def _money(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _capital_point(point) -> dict[str, Any]:
    """Serialize one day of committed capital.

    ``total`` is summed from the already-rounded components rather than rounded
    independently, so the figure in the tooltip and table always equals the parts
    beside it -- rounding each separately lets them disagree by a cent or two, and
    the chart builds its stack height from the components.
    """
    put = _money(point.put_collateral)
    stock = _money(point.stock_basis)
    call = _money(point.call_collateral)
    long_premium = _money(point.long_premium)
    spread = _money(point.spread_collateral)
    idle_stock = _money(point.idle_stock_basis)
    return {
        "date": _iso(point.day),
        "put": put,
        "stock": stock,
        "call": call,
        "long": long_premium,
        "spread": spread,
        "total": round(put + stock + call + long_premium + spread, 2),
        # Slice of `stock` with no covered call currently written against it
        # -- see CapitalPoint.working_capital. `total - idle_stock` is the
        # capital actually backing an open put or covered call that day.
        "idle_stock": idle_stock,
    }


def _cycle_payload(
    cycle: Cycle,
    through: date,
    *,
    current_price: float | None = None,
    dividends: float = 0.0,
) -> dict[str, Any]:
    metrics = cycle_metrics(cycle, through, current_price=current_price, dividends=dividends)
    payload = {key: value for key, value in asdict(metrics).items()}
    payload["start_date"] = _iso(metrics.start_date)
    payload["end_date"] = _iso(metrics.end_date)
    payload["win_rate_pct"] = metrics.win_rate_pct
    payload["last_activity"] = _iso(cycle.last_activity)
    # CycleMetrics already exposes integer `rolls` / `assignments` counts, so the
    # detail lists take distinct names rather than shadowing them.
    payload["legs"] = leg_rows(cycle)
    payload["roll_events"] = [
        {
            "roll_id": roll.roll_id,
            "date": _iso(roll.date),
            "right": roll.right,
            "direction": roll.direction,
            "net_credit": _money(roll.net_credit),
            "closed": [
                {**item, "expiry": _iso(item["expiry"]), "cash": _money(item["cash"])} for item in roll.closed
            ],
            "opened": [
                {**item, "expiry": _iso(item["expiry"]), "cash": _money(item["cash"])} for item in roll.opened
            ],
        }
        for roll in cycle.rolls
    ]
    payload["assignment_events"] = [
        {
            "date": _iso(item.date),
            "symbol": item.occ_symbol,
            "right": item.right,
            "strike": item.strike,
            "contracts": item.contracts,
            "shares": item.shares,
            "direction": item.direction,
            "cash": _money(item.cash),
            "synthetic": item.synthetic,
            "note": item.note,
        }
        for item in cycle.assignments
    ]
    payload["share_lots"] = [
        {
            "lot_id": lot.lot_id,
            "acquired": _iso(lot.acquired),
            "shares": lot.shares,
            "basis_per_share": lot.basis_per_share,  # tax basis: raw assignment/purchase price
            "net_adjusted_cost_basis": _money(net_adjusted_cost_basis(cycle, lot)),
            "remaining": lot.remaining,
            "source": lot.source,
            "synthetic": lot.synthetic,
            "basis_known": lot.basis_known,
            "cost": _money(lot.cost),
        }
        for lot in cycle.share_lots
    ]
    payload["spreads"] = [
        {
            "spread_id": spread.spread_id,
            "right": spread.right,
            "expiry": _iso(spread.expiry),
            "open_date": _iso(spread.open_date),
            "short_leg_id": spread.short_leg_id,
            "long_leg_id": spread.long_leg_id,
            "paired_contracts": spread.paired_contracts,
            "short_strike": spread.short_strike,
            "long_strike": spread.long_strike,
            "collateral": _money(spread.collateral_per_contract * spread.paired_contracts),
            "net_credit": _money(spread.net_credit),
            "capital_estimated": spread.capital_estimated,
        }
        for spread in cycle.spreads
    ]
    payload["capital"] = [_capital_point(point) for point in capital_timeline(cycle, through)]
    return payload


# --------------------------------------------------------------------------
# Trade Log -- one wheel's transactions, end to end.
#
# Always built from the account's *full* history (``all_cycles``), never the
# filtered slice: a wheel is a historical unit, so a date/ticker/status filter
# must not slice it in half here. Two attribution paths, chosen per underlying
# (see ``Dashboard._build_trade_log``):
#   * common -- filter raw ``Transaction``s by underlying + inclusive date span,
#     safe only when a ticker's cycles are strictly separated in time;
#   * engine-exact -- derive rows from the engine's own ``cycle_id``-tagged
#     legs/closes/assignments, used when two same-ticker cycles touch or overlap
#     (a legal same-day close/reopen). Its one cost: legs carry commission and
#     fees combined, so those rows show a single Fees figure.
# --------------------------------------------------------------------------

_OPEN_TYPE = {
    (STO, "P"): "Sell Put",
    (STO, "C"): "Sell Call",
    (BTO, "P"): "Buy Put",
    (BTO, "C"): "Buy Call",
}
_CLOSE_TYPE = {
    (BTC, "P"): "Buy Put",
    (BTC, "C"): "Buy Call",
    (STC, "P"): "Sell Put",
    (STC, "C"): "Sell Call",
    (EXPIRED, "P"): "Put Expired",
    (EXPIRED, "C"): "Call Expired",
    (ASSIGNED, "P"): "Put Assigned",
    (ASSIGNED, "C"): "Call Assigned",
}


_CLOSING_ACTIONS = frozenset({BTC, STC, EXPIRED, ASSIGNED})

# Open-hedge banner: how many days before a long protective leg's expiry the
# advice flips from "keep writing premium against it" to "wind it down."
HEDGE_WIND_DOWN_DAYS = 60  # the user's "two months"
HEDGE_EXPIRING_DAYS = 7


def _close_return_pct(open_price: float | None, close_price: float | None, side: str) -> float | None:
    """Realized return on the contract this closing fill shut, as a % of the
    premium at open. Short: ``(open - close) / open`` -- a put sold at 0.50 and
    bought back at 0.25 kept 50%. Long: ``(close - open) / open`` -- a call
    bought at 1.00 and sold at 0.20 lost 80%. Expiry/assignment closes at 0.
    """
    if not open_price:
        return None
    settle = 0.0 if close_price is None else close_price
    gain = (settle - open_price) if side == LONG else (open_price - settle)
    return 100.0 * gain / open_price


def _trade_log_txn_type(transaction: Transaction, dividend_row_ids: set[int]) -> str:
    if transaction.row_id in dividend_row_ids:
        return "Dividend"
    if transaction.action == BUY_STOCK:
        return "Buy Shares"
    if transaction.action == SELL_STOCK:
        return "Sell Shares"
    right = transaction.right or ""
    if transaction.action in (STO, BTO):
        return _OPEN_TYPE.get((transaction.action, right), "Other")
    if transaction.action in (BTC, STC, EXPIRED, ASSIGNED):
        return _CLOSE_TYPE.get((transaction.action, right), "Other")
    return "Other"


def _trade_log_raw_row(
    transaction: Transaction,
    type_: str,
    close_return_pct: float | None = None,
    *,
    is_settled: bool = False,
    is_open_long: bool = False,
) -> dict[str, Any]:
    quantity = abs(transaction.contracts)
    is_csp_open = (
        transaction.action == STO
        and transaction.right == "P"
        and transaction.strike is not None
    )
    csp = transaction.strike * OPTION_MULTIPLIER * quantity if is_csp_open else None
    return {
        "type": type_,
        "date": _iso(transaction.event_date),
        "expiration": _iso(transaction.expiry),
        "strike": transaction.strike,
        "quantity": quantity,
        # Signed: long/bought positive, short/sold negative. `contracts` already
        # carries that sign from the parser; 0 (a dividend) becomes None so the
        # column shows a dash rather than "+0".
        "signed_quantity": transaction.contracts or None,
        "price": transaction.price,
        "initial_csp_collateral": _money(csp),
        "fees": _money(transaction.fees),
        "commission": _money(transaction.commission),
        "net_cash_flow": _money(transaction.amount),
        # Only a closing fill has one; filled in by the caller, which knows the
        # opening price of the contract this row shut.
        "close_return_pct": close_return_pct,
        # This row belongs to a position that is no longer active -- a fully
        # closed leg (its open *and* its closes), a sale, an expiry. The
        # frontend greys the whole row. The caller resolves it against the
        # engine's leg state; see `_trade_log_entry`.
        "is_settled": is_settled,
        # A bought (long) option leg with no matching close yet -- an open
        # protective put / directional long. The frontend keeps this row
        # highlighted for as long as it stays unpaired.
        "is_open_long": is_open_long,
        "synthetic": False,
        "_sort": (transaction.event_date, 1, transaction.row_id),
    }


def _trade_log_engine_rows(cycle: Cycle) -> list[dict[str, Any]]:
    """Rows for one cycle taken from the engine's own structures -- exact
    ``cycle_id`` attribution, no date-boundary ambiguity. Commission and fees
    are combined on a leg, so those rows report a single Fees figure.
    """
    rows: list[dict[str, Any]] = []
    for leg in cycle.legs:
        is_csp_open = leg.open_action == STO and leg.right == "P"
        csp = leg.strike * OPTION_MULTIPLIER * leg.contracts if is_csp_open else None
        rows.append(
            {
                "type": _OPEN_TYPE.get((leg.open_action, leg.right), "Other"),
                "date": _iso(leg.open_date),
                "expiration": _iso(leg.expiry),
                "strike": leg.strike,
                "quantity": leg.contracts,
                "signed_quantity": -leg.contracts if leg.open_action == STO else leg.contracts,
                "price": leg.open_price,
                "initial_csp_collateral": _money(csp),
                "fees": _money(leg.open_fees),
                "commission": None,
                "net_cash_flow": _money(leg.open_cash),
                "close_return_pct": None,
                "is_settled": not leg.is_open,
                "is_open_long": leg.is_open and leg.side == LONG,
                "synthetic": False,
                "_sort": (leg.open_date, 0, 0),
            }
        )
        for close in leg.closes:
            rows.append(
                {
                    "type": _CLOSE_TYPE.get((close.action, leg.right), "Other"),
                    "date": _iso(close.date),
                    "expiration": _iso(leg.expiry),
                    "strike": leg.strike,
                    "quantity": close.contracts,
                    # Closing a short is a buy (+), closing a long is a sell (-).
                    "signed_quantity": close.contracts if leg.open_action == STO else -close.contracts,
                    "price": close.price,
                    "initial_csp_collateral": None,
                    "fees": _money(close.fees),
                    "commission": None,
                    "net_cash_flow": _money(close.cash),
                    "close_return_pct": _close_return_pct(leg.open_price, close.price, leg.side),
                    "is_settled": not leg.is_open,
                    "is_open_long": False,
                    "synthetic": False,
                    "_sort": (close.date, 1, 0),
                }
            )
    return rows


def _trade_log_assignment_row(assignment, *, is_settled: bool) -> dict[str, Any]:
    acquire = assignment.direction == "ACQUIRE"
    return {
        "type": "Shares Assigned" if acquire else "Shares Called Away",
        "date": _iso(assignment.date),
        "expiration": None,
        "strike": assignment.strike,
        "quantity": assignment.shares,
        "signed_quantity": assignment.shares if acquire else -assignment.shares,
        "price": assignment.strike,
        "initial_csp_collateral": None,
        "fees": 0.0,
        "commission": 0.0,
        "net_cash_flow": _money(assignment.cash),
        "close_return_pct": None,
        "is_settled": is_settled,
        "is_open_long": False,
        "synthetic": assignment.synthetic,
        "_sort": (assignment.date, 2, 0),
    }


def _trade_log_entry(
    cycle: Cycle,
    transactions: Sequence[Transaction],
    through: date,
    *,
    name: str | None,
    dividend_row_ids: set[int],
    dividends: float,
    engine_exact: bool,
    current_price: float | None = None,
) -> dict[str, Any]:
    metrics = cycle_metrics(cycle, through, current_price=current_price, dividends=dividends)

    # A cycle holding no shares right now: any acquire-then-flat share row and
    # any bare stock purchase are settled positions.
    share_flat = sum(lot.remaining for lot in cycle.share_lots) <= 1e-9

    def _assignment_settled(assignment) -> bool:
        return assignment.direction == "DISPOSE" or share_flat

    if engine_exact:
        rows = _trade_log_engine_rows(cycle)
        rows += [
            _trade_log_assignment_row(a, is_settled=_assignment_settled(a)) for a in cycle.assignments
        ]
        attribution_note = (
            "Same-day close/reopen on this ticker: rows are attributed via the "
            "engine, and Fees combines commission + fees."
        )
    else:
        end = cycle.end_date or through
        # Contract-weighted opening price + side per (symbol, close-day), taken
        # from the engine's own FIFO matching, so a raw closing fill can report
        # its return against the premium it actually opened at -- even after a
        # roll or a scaled entry at several prices.
        close_ref: dict[tuple[str, date], list[tuple[float, float, str]]] = {}
        # Whether the leg a raw fill belongs to is fully closed -- so its open
        # row is greyed together with its closes once the position is done.
        open_settled: dict[tuple[str, date], bool] = {}
        close_settled: dict[tuple[str, date], bool] = {}
        open_long_keys: set[tuple[str, date]] = {
            (leg.occ_symbol, leg.open_date)
            for leg in cycle.legs
            if leg.is_open and leg.side == LONG
        }
        for leg in cycle.legs:
            open_settled[(leg.occ_symbol, leg.open_date)] = not leg.is_open
            for close in leg.closes:
                close_ref.setdefault((leg.occ_symbol, close.date), []).append(
                    (close.contracts, leg.open_price, leg.side)
                )
                close_settled[(leg.occ_symbol, close.date)] = not leg.is_open

        def _raw_close_pct(t: Transaction) -> float | None:
            if t.action not in _CLOSING_ACTIONS:
                return None
            items = [i for i in close_ref.get((t.occ_symbol, t.event_date), []) if i[1]]
            total = sum(qty for qty, _op, _sd in items)
            if total <= 0:
                return None
            wavg_open = sum(qty * op for qty, op, _sd in items) / total
            return _close_return_pct(wavg_open, t.price, items[0][2])

        def _raw_settled(t: Transaction) -> bool:
            if t.row_id in dividend_row_ids:
                return True  # a dividend is cash received, complete on arrival
            if t.action in (STO, BTO):
                return open_settled.get((t.occ_symbol, t.event_date), False)
            if t.action in _CLOSING_ACTIONS:
                return close_settled.get((t.occ_symbol, t.event_date), True)
            if t.action == SELL_STOCK:
                return True
            if t.action == BUY_STOCK:
                return share_flat
            return False

        rows = [
            _trade_log_raw_row(
                t,
                _trade_log_txn_type(t, dividend_row_ids),
                _raw_close_pct(t),
                is_settled=_raw_settled(t),
                is_open_long=(
                    t.action == BTO and (t.occ_symbol, t.event_date) in open_long_keys
                ),
            )
            for t in transactions
            if t.underlying == cycle.underlying
            and cycle.start_date <= t.event_date <= end
            and (t.is_option or t.action in (BUY_STOCK, SELL_STOCK) or t.row_id in dividend_row_ids)
        ]
        # A broker export that carries the equity leg already yields a real
        # Buy/Sell Shares row above; only the synthesized legs need adding.
        rows += [
            _trade_log_assignment_row(a, is_settled=_assignment_settled(a))
            for a in cycle.assignments
            if a.synthetic
        ]
        attribution_note = None

    rows.sort(key=lambda row: row.pop("_sort"))
    running = 0.0
    for row in rows:
        running += row.get("net_cash_flow") or 0.0
        row["running_cash_flow"] = _money(running)

    held_lots = [lot for lot in cycle.share_lots if lot.remaining > 1e-9]
    known = [lot for lot in held_lots if lot.basis_known and lot.basis_per_share is not None]
    shares_held = sum(lot.remaining for lot in held_lots)
    cost_basis = (
        sum(lot.basis_per_share * lot.remaining for lot in known) / sum(lot.remaining for lot in known)
        if known
        else None
    )
    break_even_lots = [
        (lot.remaining, net_adjusted_cost_basis(cycle, lot))
        for lot in held_lots
    ]
    break_even_lots = [(qty, value) for qty, value in break_even_lots if value is not None]
    break_even = (
        sum(qty * value for qty, value in break_even_lots) / sum(qty for qty, value in break_even_lots)
        if break_even_lots
        else None
    )
    total_fees_commissions = sum(
        (row.get("fees") or 0.0) + (row.get("commission") or 0.0) for row in rows
    )

    # P&L per day a position was actually held: total realized P&L of every
    # completed open->close leg (roll segments included) over the sum of their
    # holding days. Open legs have no finished pair yet.
    closed_legs = [leg for leg in cycle.legs if not leg.is_open]
    leg_days = [max(1, leg.days_held or 0) for leg in closed_legs]
    closed_leg_pl = sum(leg.realized_pl for leg in closed_legs)
    total_days_held = sum(leg_days)
    pl_per_day_held = closed_leg_pl / total_days_held if total_days_held else None

    # A lone directional/long-only cycle keeps its real P&L below but is not
    # running the wheel, so the wheel-framed ratios are withheld -- see
    # Cycle.is_wheel. (annualized_wheel_roc_pct / roi_on_avg_wheel_pct /
    # win_rate_pct already come back None from cycle_metrics; these two are
    # derived here, so they're nulled here.)
    if not cycle.is_wheel:
        pl_per_day_held = None

    # Where the campaign really stands right now, vs the misleading realized-only
    # figure. Open option legs are valued at expiry (`option_open_premium`: a
    # long put's whole debit is a loss, a short call's whole credit a gain) --
    # their remaining time value is not marked, so a held protective put makes
    # this conservative. `stock_unrealized_pl` marks held shares to the latest
    # close (None when there is no price / no shares).
    open_option_pl = metrics.option_open_premium
    non_stock_pl = metrics.option_realized_pl + metrics.stock_realized_pl + dividends + open_option_pl
    stock_unrealized = metrics.stock_unrealized_pl
    mark_to_market_pl = (
        None
        if shares_held > 1e-9 and stock_unrealized is None
        else non_stock_pl + (stock_unrealized or 0.0)
    )
    # The stock price at which the whole campaign nets to $0: raw cost of the
    # shares still held, less every other dollar the campaign has banked or paid.
    # <= 0 means the premium/profit already banked exceeds the share cost -- there
    # is no "price to reach", so it is reported as None (an insight covers it).
    break_even_price = (
        cost_basis - non_stock_pl / shares_held
        if shares_held > 1e-9 and cost_basis is not None
        else None
    )
    if break_even_price is not None and break_even_price <= 0:
        break_even_price = None
    dollars_to_break_even = (
        shares_held * (break_even_price - current_price)
        if break_even_price is not None and current_price is not None
        else None
    )

    # A waterfall from gross premium sold down to where the wheel stands now.
    # Each `step` floats on the previous `running`; the two `anchor` rows are
    # subtotals pinned at 0. The steps provably sum: premium + short-close cash
    # + hedge P&L = option realized P&L; + stock realized + dividends + open
    # options + shares unrealized = mark-to-market P&L.
    pl_bridge: list[dict[str, Any]] = []
    _run = 0.0

    def _step(label: str, delta: float) -> None:
        nonlocal _run
        _run += delta
        pl_bridge.append({"label": label, "delta": _money(delta), "running": _money(_run), "kind": "step"})

    def _anchor(label: str, kind: str) -> None:
        pl_bridge.append({"label": label, "delta": None, "running": _money(_run), "kind": kind})

    _step("Premium sold", metrics.premium_received)
    _step("Bought back shorts", metrics.wheel_core_realized_pl - metrics.premium_received)
    if metrics.hedge_realized_pl:
        _step("Hedge P&L", metrics.hedge_realized_pl)
    _anchor("Option P&L", "subtotal")
    if metrics.stock_realized_pl:
        _step("Shares sold/away", metrics.stock_realized_pl)
    if dividends:
        _step("Dividends", dividends)
    if open_option_pl:
        _step("Open options", open_option_pl)
    shares_priced = not (shares_held > 1e-9 and stock_unrealized is None)
    if stock_unrealized:
        _step("Held shares P&L", stock_unrealized)
    _anchor("P&L now" if shares_priced else "P&L now (no price)", "total")

    return {
        "cycle_id": cycle.cycle_id,
        "underlying": cycle.underlying,
        "name": name,
        "status": cycle.status,
        "is_open": cycle.is_open,
        "is_wheel": cycle.is_wheel,
        "start_date": _iso(cycle.start_date),
        "end_date": _iso(cycle.end_date),
        "cost_basis_per_share": _money(cost_basis),
        "break_even_per_share": _money(break_even),
        "shares_held": round(shares_held, 4),
        "open_contracts": round(sum(leg.remaining_contracts for leg in cycle.legs if leg.is_open), 4),
        "gross_premium_received": _money(metrics.premium_received),
        "option_realized_pl": _money(metrics.option_realized_pl),
        "stock_realized_pl": _money(metrics.stock_realized_pl),
        "net_realized_pl": _money(metrics.net_realized_pl),
        "stock_unrealized_pl": _money(stock_unrealized),
        "open_option_pl": _money(open_option_pl),
        "mark_to_market_pl": _money(mark_to_market_pl),
        "current_price": _money(current_price),
        "break_even_price": _money(break_even_price),
        "dollars_to_break_even": _money(dollars_to_break_even),
        "pl_bridge": pl_bridge,
        "insights": wheel_insights(
            cycle,
            metrics,
            current_price=current_price,
            cost_basis=cost_basis,
            dividends=dividends,
            break_even_price=break_even_price,
            mark_to_market_pl=mark_to_market_pl,
        ),
        "dividends": _money(dividends),
        "total_fees_commissions": _money(total_fees_commissions),
        "capital_committed_now": _money(metrics.current_collateral),
        "capital_estimated": cycle.capital_estimated,
        "days_active": metrics.days_active,
        "profit_per_day": _money(metrics.profit_per_day),
        "pl_per_day_held": _money(pl_per_day_held),
        "closed_leg_pl": _money(closed_leg_pl),
        "closed_leg_count": len(closed_legs),
        "total_days_held": total_days_held,
        "win_rate_pct": metrics.win_rate_pct,
        "wins": metrics.wins,
        "losses": metrics.losses,
        "avg_days_in_trade": metrics.avg_days_in_trade,
        "avg_collateral": _money(metrics.avg_collateral),
        "roi_on_avg_wheel_pct": metrics.roi_on_avg_wheel_pct,
        "annualized_wheel_roc_pct": metrics.annualized_wheel_roc_pct,
        # Weekly PPD track for this one wheel. Empty for a non-wheel cycle --
        # profit_per_day is withheld there, so a running PPD makes no sense.
        "ppd_series": (
            weekly_ppd_series(realized_pl_series([cycle]), cycle.start_date, through)
            if cycle.is_wheel
            else []
        ),
        "attribution_note": attribution_note,
        "transactions": rows,
    }


def _hedge_pl_phrase(value: float | None) -> str:
    if value is None:
        return "at an unknown mark"
    if value > 1:
        return f"up +${value:,.0f}"
    if value < -1:
        return f"down -${abs(value):,.0f}"
    return "roughly flat"


def _open_hedge_entry(
    cycle: Cycle,
    leg,
    through: date,
    *,
    name: str | None,
    current_price: float | None,
    dividends: float,
) -> dict[str, Any]:
    """One open, unpaired long option leg -- a protective put/call still on the
    books -- with the timing and position figures the banner needs to say "keep
    writing premium against it" vs "wind it down."

    The decision is driven by ``days_to_expiry`` (the phase) and by where the
    whole wheel actually stands: ``wheel_pl_now`` is the cycle's mark-to-market
    P&L -- realized option + realized stock + dividends + open-option value at
    expiry + unrealized stock. ``premium_written_since`` (net realized P&L of
    every CSP/covered-call leg in the cycle that *closed* on or after this hedge
    opened) is reported as a plain fact, not as "the hedge is paid for": that
    premium may have turned into shares now underwater. There is no options
    quote feed here, so the hedge's own market value can't be shown -- only its
    ``intrinsic_now`` floor.
    """
    days_to_expiry = (leg.expiry - through).days
    days_open = max((through - leg.open_date).days, 0)
    cost = abs(leg.open_cash)  # debit paid, as a positive number
    contracts = leg.remaining_contracts
    is_put = leg.right == "P"
    kind = "put" if is_put else "call"

    if days_to_expiry <= HEDGE_EXPIRING_DAYS:
        phase = "expiring"
    elif days_to_expiry <= HEDGE_WIND_DOWN_DAYS:
        phase = "wind_down"
    else:
        phase = "runway"

    intrinsic = None
    if current_price is not None and leg.strike is not None:
        per_share = max(0.0, leg.strike - current_price) if is_put else max(0.0, current_price - leg.strike)
        intrinsic = per_share * OPTION_MULTIPLIER * contracts

    metrics = cycle_metrics(cycle, through, current_price=current_price, dividends=dividends)
    shares_held = sum(lot.remaining for lot in cycle.share_lots if lot.remaining > 1e-9)
    non_stock_pl = (
        metrics.option_realized_pl + metrics.stock_realized_pl + dividends + metrics.option_open_premium
    )
    stock_unrealized = metrics.stock_unrealized_pl
    wheel_pl_now = (
        None
        if shares_held > 1e-9 and stock_unrealized is None
        else non_stock_pl + (stock_unrealized or 0.0)
    )

    premium_written_since = None
    if cycle.is_wheel:
        premium_written_since = sum(
            other.realized_pl
            for other in cycle.legs
            if other.strategy in (CSP, COVERED_CALL)
            and other.close_date is not None
            and other.close_date >= leg.open_date
        )

    months = days_to_expiry / 30.4
    span = f"{months:.1f} months" if days_to_expiry >= 45 else f"{days_to_expiry} days"
    label = f"long ${leg.strike:g} {kind}"
    pl_phrase = _hedge_pl_phrase(wheel_pl_now)
    losing = wheel_pl_now is not None and wheel_pl_now < -1
    intrinsic_phrase = f" (${intrinsic:,.0f} today)" if intrinsic is not None else ""

    if not cycle.is_wheel:
        headline = f"DIRECTIONAL · {days_to_expiry}d"
        if phase == "runway":
            message = (
                f"Directional {kind} with {span} to run and no wheel selling premium behind "
                f"it -- theta is working against its ${cost:,.0f} cost every day. Decide whether "
                f"you still want the exposure or should cut it."
            )
        else:
            message = (
                f"Directional {kind}, {days_to_expiry} days left. Close it for whatever time "
                f"value remains, or hold it for the move -- it finances nothing."
            )
    elif phase == "runway":
        headline = f"RUNWAY · {days_to_expiry}d"
        if losing:
            message = (
                f"{span} of downside protection left, and the wheel is {pl_phrase} right now"
                f"{' with shares below cost' if shares_held > 1e-9 else ''}. This "
                f"${leg.strike:g} {kind} is the leg that pays if {cycle.underlying} keeps "
                f"falling -- hold it, and keep writing puts to carry its ${cost:,.0f} cost. "
                f"Do not close it while the wheel is underwater."
            )
        else:
            message = (
                f"{span} of protection left; the wheel is {pl_phrase}. There is still runway "
                f"to write puts against this hedge -- plan to sell it around two months out to "
                f"salvage its time value rather than letting it decay."
            )
    elif phase == "wind_down":
        headline = f"WIND DOWN · {days_to_expiry}d"
        message = (
            f"{days_to_expiry} days left -- inside two months. Sell the hedge now to recover its "
            f"remaining time value{intrinsic_phrase}, then stop adding puts against it."
        )
        if losing:
            message += (
                f" The wheel is {pl_phrase}; if you still want protection, roll to a later "
                f"expiry instead of holding this one into its decay."
            )
    else:  # expiring
        headline = f"EXPIRING · {days_to_expiry}d"
        message = (
            f"{days_to_expiry} days left -- time value is nearly gone{intrinsic_phrase}. Close "
            f"it or let it lapse; it will not finance more premium."
        )

    return {
        "cycle_id": cycle.cycle_id,
        "underlying": cycle.underlying,
        "name": name,
        "is_wheel": cycle.is_wheel,
        "right": leg.right,
        "strike": leg.strike,
        "label": label,
        "expiry": _iso(leg.expiry),
        "opened": _iso(leg.open_date),
        "contracts": contracts,
        "shares_held": round(shares_held, 4),
        "days_open": days_open,
        "days_to_expiry": days_to_expiry,
        "cost": _money(cost),
        "wheel_pl_now": _money(wheel_pl_now),
        "premium_written_since": _money(premium_written_since),
        "current_price": _money(current_price),
        "intrinsic_now": _money(intrinsic),
        "phase": phase,
        "headline": headline,
        "message": message,
    }


def _reconciliation(
    transactions: Sequence[Transaction],
    cycles: Sequence[Cycle],
    reports: Sequence[ParseReport],
    unmatched_cash: float,
) -> dict[str, Any]:
    """Prove the model did not gain or lose a cent against the broker file.

    Summing every opening and closing cash figure the engine holds must equal the
    sum of the ``Amount ($)`` column, because the engine only ever redistributes
    those figures across lots -- it never derives cash from price x quantity.

    ``unmatched_cash`` is the one legitimate gap: a date filter can include a
    buy-back whose sell-to-open sits outside the window, leaving cash with no leg
    to attach to.  It is added back before the balance check so a filtered view
    does not look like a bookkeeping error.
    """
    # Only rows the engine actually consumes belong in the identity. Dividends,
    # collateral marks and other non-trade rows are real cash but are not part of
    # any option leg, so counting them would guarantee a false mismatch.
    option_rows = [t for t in transactions if t.is_option and t.action in OPTION_ACTIONS]
    equity_rows = [t for t in transactions if not t.is_option]
    other_rows = [t for t in transactions if t.is_option and t.action not in OPTION_ACTIONS]

    file_total = sum(transaction.amount for transaction in option_rows)
    model_total = sum(
        leg.open_cash + sum(close.cash for close in leg.closes)
        for cycle in cycles
        for leg in cycle.legs
    )
    # Equity cash is real broker cash but lives on share lots, not option legs, so
    # it is reported alongside rather than folded into the option-leg identity.
    equity_cash = sum(transaction.amount for transaction in equity_rows)
    synthetic = sum(
        assignment.cash for cycle in cycles for assignment in cycle.assignments if assignment.synthetic
    )
    delta = file_total - model_total - unmatched_cash
    return {
        "file_cash_total": _money(file_total),
        "model_cash_total": _money(model_total),
        "unmatched_cash": _money(unmatched_cash),
        "equity_cash_total": _money(equity_cash),
        "equity_rows": len(equity_rows),
        "non_trade_rows": len(other_rows),
        "delta": round(delta, 6),
        "balanced": abs(delta) < 0.005,
        "rows_checked": sum(report.reconciled for report in reports),
        "row_failures": [
            failure for report in reports for failure in report.reconcile_failures
        ],
        "reconcile_rate_pct": round(
            100.0
            * sum(report.reconciled for report in reports)
            / max(
                1,
                sum(report.reconciled + len(report.reconcile_failures) for report in reports),
            ),
            4,
        ),
        "synthetic_assignment_cash": _money(synthetic),
        "note": (
            "Option cash comes verbatim from the broker's Amount column."
            + (
                f" {len(equity_rows)} equity rows supply real share fills."
                if equity_rows
                else " Assignment share movements are synthesized at the strike, "
                "because this export contains no equity rows."
            )
        ),
    }


# --------------------------------------------------------------------------
# Dataset
# --------------------------------------------------------------------------


class Dashboard:
    """Parses one or more exports once, then answers filtered queries.

    Several exports are combined into a single timeline with duplicate trades
    counted once, which is what lets a year-split set of downloads produce one
    seamless history instead of positions that appear to open or close out of
    nowhere at each file boundary.
    """

    def __init__(
        self,
        csv_path: str | Sequence[str] | None = None,
        position_paths: str | Sequence[str] | None = None,
        account_number: str | None = None,
        opening_balance: tuple[date, float] | None = None,
    ):
        # A manual stand-in for a real, earlier Positions snapshot -- see
        # data/accounts.json's "opening_balances" (wheel/accounts.py's module
        # docstring) and _build_benchmark() below, the only place this is
        # used. Never touches self.snapshots or anything derived from it --
        # Total value, Capital deployed, and every other current-state figure
        # still come only from real, itemized Positions data.
        self.opening_balance = opening_balance
        if csv_path is None:
            csv_path = discover_exports()
        paths = [csv_path] if isinstance(csv_path, str) else list(csv_path)

        if position_paths is None:
            position_paths = discover_position_snapshots()
        else:
            position_paths = [position_paths] if isinstance(position_paths, str) else list(position_paths)

        if not paths and not position_paths:
            raise ValueError("no broker export or position snapshot found; pass one explicitly")

        self.csv_paths = paths
        self.csv_path = paths[0] if paths else None  # kept for single-file callers
        if paths:
            self.transactions, self.reports, self.merge = parse_exports(paths)
            self.report = self.reports[0]
        else:
            # A positions-only account (no transaction history yet) is legitimate --
            # net worth still works, wheel-cycle figures are just all zero.
            self.transactions, self.reports, self.merge = [], [], MergeReport()
            self.report = ParseReport()
        self.all_cycles, self.engine = build_cycles(self.transactions)

        self.position_paths = position_paths
        self.snapshots, self.position_warnings = (
            load_snapshots(position_paths) if position_paths else ([], [])
        )
        if account_number and self.snapshots:
            # A single Positions export can list more than one real account
            # (e.g. a Fidelity "all accounts" download) even though this
            # Dashboard is scoped to one account -- see wheel.accounts, both
            # for a folder whose account is named in data/accounts.json and
            # for an auto-discovered, positions-only account that has no
            # folder at all. Once the caller has told us which account this
            # is, any other account's rows found in the same file(s) must
            # never be blended in or silently shown in its place.
            other_numbers = sorted(
                {snapshot.account_number for snapshot in self.snapshots if snapshot.account_number != account_number}
            )
            self.snapshots = [s for s in self.snapshots if s.account_number == account_number]
            if other_numbers:
                self.position_warnings = self.position_warnings + [
                    "ignored Positions rows for account(s) "
                    + ", ".join(other_numbers)
                    + f" -- this dashboard is scoped to account {account_number}"
                ]
        self._net_worth = self._build_net_worth()
        self._benchmark = self._build_benchmark()
        # Lazily populated on the first build() call and reused after that --
        # get_price_series() does disk I/O and a freshness check even when it
        # skips the network fetch, and build() runs once per filter change
        # from the frontend, so re-fetching every ticker on every call would
        # multiply that cost by however many times the user adjusts a filter.
        self._price_cache: dict[str, float | None] | None = None
        self._price_warnings: list[str] = []
        # Lazily built on the first build() call too -- it needs current_prices,
        # which needs the same network fetch _price_cache above is guarding
        # against repeating, so it can't be computed any earlier than that.
        self._wheel_return: dict[str, Any] | None = None

        # Best-effort issuer name per ticker, for the Trade Log summary only.
        # Option descriptions are the most standardized and by far the most
        # numerous, so only they feed the vote -- dividend/ledger prose stays out.
        name_votes: dict[str, Counter] = {}
        for transaction in self.transactions:
            if not transaction.is_option:
                continue
            name = company_name_from_description(transaction.description, transaction.underlying)
            if name:
                name_votes.setdefault(transaction.underlying, Counter())[name] += 1
        self._company_names = {
            ticker: votes.most_common(1)[0][0] for ticker, votes in name_votes.items()
        }
        # Filter-independent (built from all_cycles), so cached after first build().
        self._trade_log: dict[str, Any] | None = None
        self._open_hedges: list[dict[str, Any]] | None = None

    # ---- market data ----

    def _current_prices(self) -> dict[str, float | None]:
        """Latest close for every ticker this dashboard holds open shares in.

        Computed once per Dashboard instance, not once per build() -- see the
        comment in __init__. A ticker whose fetch fails yields ``None`` for
        that ticker only (wheel.marketdata never raises), which flows through
        to that cycle's stock_unrealized_pl as "unavailable," not a crash.

        Two passes so the common case pays nothing for threads: first resolve
        every ticker that a fresh cache or the in-process memo can answer
        without network (``local_only=True``), then fan the genuine misses --
        typically only the first page load after a trading session closes --
        out across a thread pool, since each is an independent network round
        trip (its own URL, its own cache file under ``data/prices/``). A cold
        pull of a few dozen tickers one at a time turned a single-digit-second
        page load into a multi-second one; spinning the pool up when there is
        nothing to fetch was itself costing ~1.5s per Combined build.
        """
        if self._price_cache is not None:
            return self._price_cache

        tickers = sorted(
            {
                cycle.underlying
                for cycle in self.all_cycles
                if any(lot.remaining > 1e-9 for lot in cycle.share_lots)
            }
        )
        prices: dict[str, float | None] = {}
        warnings: list[str] = []
        misses: list[str] = []
        for ticker in tickers:
            local = marketdata.get_price_series(ticker, local_only=True)
            if local is None:
                misses.append(ticker)
                continue
            points, ticker_warnings = local
            warnings.extend(ticker_warnings)
            prices[ticker] = points[-1].close if points else None

        if misses:
            with ThreadPoolExecutor(max_workers=min(8, len(misses))) as pool:
                for ticker, (points, ticker_warnings) in zip(misses, pool.map(marketdata.get_price_series, misses)):
                    warnings.extend(ticker_warnings)
                    prices[ticker] = points[-1].close if points else None

        self._price_cache = prices
        self._price_warnings = warnings
        return prices

    # ---- metadata ----

    @property
    def first_date(self) -> date | None:
        return min((t.event_date for t in self.transactions), default=None)

    @property
    def last_date(self) -> date | None:
        return max((t.event_date for t in self.transactions), default=None)

    def available_tickers(self) -> list[str]:
        """Tickers the engine actually trades.

        Built from trade rows only -- a ledger also carries dividends and
        collateral marks, whose symbol column holds money-market codes and CUSIPs
        that would otherwise show up as filterable tickers.
        """
        return sorted(
            {
                transaction.underlying
                for transaction in self.transactions
                if transaction.action in TRADE_ACTIONS and transaction.underlying
            }
        )

    # ---- net worth & benchmark ----
    #
    # Both are computed once in __init__, independent of Filters -- a position
    # snapshot is a fact about a moment in time, not a slice of the transaction
    # window, so it is copied verbatim into every build() result regardless of
    # the ticker/date/status filters in play.

    def _build_net_worth(self) -> dict[str, Any]:
        if not self.snapshots:
            return {
                "available": False,
                "warnings": list(self.position_warnings)
                + ["no Portfolio Positions snapshot found for this account"],
                "snapshot_files": [],
            }

        latest_by_account = latest_snapshot_per_account(self.snapshots)
        warnings = list(self.position_warnings)
        if len(latest_by_account) > 1:
            warnings.append(
                "multiple account numbers found in one dataset ("
                + ", ".join(sorted(latest_by_account))
                + "); showing the most recently seen -- put each account in its own "
                "folder under data/ to keep them separate"
            )
        latest = max(latest_by_account.values(), key=lambda snapshot: snapshot.as_of)
        as_of_day = latest.as_of.date()

        wheel_tickers = {cycle.underlying for cycle in self.all_cycles}
        capital_series = (
            portfolio_capital_series(self.all_cycles, as_of_day) if self.all_cycles else []
        )
        wheel_capital_deployed = capital_series[-1].total if capital_series else 0.0
        if capital_series and capital_series[-1].day < as_of_day:
            warnings.append(
                "wheel capital deployed is carried forward from the last transaction "
                f"activity on {capital_series[-1].day}, prior to the snapshot date {as_of_day}"
            )

        account_history = sorted(
            (s for s in self.snapshots if s.account_number == latest.account_number),
            key=lambda snapshot: snapshot.as_of,
        )

        return {
            "available": True,
            "warnings": warnings,
            "snapshot_files": [
                {"name": s.source, "as_of": s.as_of.isoformat(), "as_of_source": s.as_of_source}
                for s in self.snapshots
            ],
            "account_number": latest.account_number,
            "account_name": latest.account_name,
            "as_of": latest.as_of.isoformat(),
            "total_value": _money(latest.total_value),
            "cash_total": _money(latest.cash_total),
            "equity_value": _money(latest.equity_value),
            "option_value": _money(latest.option_value),
            "cost_basis_known_total": _money(latest.cost_basis_known_total),
            "cost_basis_unknown_rows": latest.cost_basis_unknown_rows,
            "wheel_capital_deployed": _money(wheel_capital_deployed),
            "positions": [
                {
                    "symbol": row.symbol,
                    "description": row.description,
                    "kind": row.kind,
                    "quantity": row.quantity,
                    "current_value": _money(row.current_value),
                    "cost_basis_total": _money(row.cost_basis_total),
                    "account_type": row.account_type,
                    "in_wheel_history": bool(row.underlying) and row.underlying in wheel_tickers,
                }
                for row in latest.rows
            ],
            "timeline": [
                {
                    "as_of": snapshot.as_of.date().isoformat(),
                    "total_value": _money(snapshot.total_value),
                    "cash_total": _money(snapshot.cash_total),
                }
                for snapshot in account_history
            ],
        }

    def _build_benchmark(self) -> dict[str, Any]:
        if not self.snapshots:
            return {
                "available": False,
                "warnings": ["no Portfolio Positions snapshot found for this account"],
            }

        primary_account = latest_snapshot(self.snapshots).account_number
        account_snapshots = sorted(
            (s for s in self.snapshots if s.account_number == primary_account),
            key=lambda snapshot: snapshot.as_of,
        )

        # The opening point this comparison measures from: a real, earlier
        # snapshot if two or more are on file, or `self.opening_balance` --
        # data/accounts.json's "opening_balances" -- standing in for one that
        # isn't. The configured value also wins over the earliest real
        # snapshot when its own date comes first, stretching the comparison
        # window further back than the Positions export history alone would
        # allow (a later real "opening" is strictly worse than an earlier
        # configured one).
        earliest_real = account_snapshots[0].as_of.date()
        if self.opening_balance and self.opening_balance[0] < earliest_real:
            opening_day, opening_value = self.opening_balance
            opening_label = "Opening balance (configured in accounts.json)"
            opening_source = "accounts.json"
        elif len(account_snapshots) >= 2:
            opening_day = earliest_real
            opening_value = account_snapshots[0].total_value
            opening_label = "Opening balance (first available snapshot)"
            opening_source = account_snapshots[0].source
        else:
            return {
                "available": False,
                "warnings": [
                    "at least two Portfolio Positions snapshots, taken on different dates, are "
                    "needed to compute a return -- only one is available so far (or add an "
                    "'opening_balances' entry for this account to data/accounts.json to supply "
                    "an earlier starting point manually)"
                ],
            }

        events, warnings = bm.external_cashflows(self.transactions)
        as_of = account_snapshots[-1].as_of.date()

        # The tracked transaction history rarely reaches back to when the account
        # was first funded, so the account's value at the opening day (real or
        # configured) stands in for an investment made on that date. Without
        # it, XIRR would see only cash moved after that point and ignore whatever
        # balance was already in place -- understating invested capital, or with
        # no external transfers on record at all, making the return uncomputable.
        opening_event = bm.CashFlowEvent(
            date=opening_day,
            amount=opening_value,
            label=opening_label,
            source=opening_source,
            kind="OPENING_BALANCE",
        )
        all_events = [opening_event] + [event for event in events if event.date > opening_day]

        price_points, market_warnings = marketdata.get_price_series("SPY")
        warnings.extend(market_warnings)

        def price_lookup(day: date):
            return marketdata.price_on_or_before(price_points, day)

        valuation_dates = sorted({opening_day, *(snapshot.as_of.date() for snapshot in account_snapshots)})
        benchmark_values = bm.simulate_benchmark_series(all_events, valuation_dates, price_lookup)

        actual_terminal_value = account_snapshots[-1].total_value
        benchmark_terminal_value = benchmark_values.get(as_of)

        result = bm.compare_to_benchmark(all_events, actual_terminal_value, benchmark_terminal_value, as_of)

        series = [
            {
                "as_of": snapshot.as_of.date().isoformat(),
                "actual_value": _money(snapshot.total_value),
                "benchmark_value": _money(benchmark_values.get(snapshot.as_of.date())),
            }
            for snapshot in account_snapshots
        ]
        if opening_day < earliest_real:
            # A configured opening point that reaches earlier than any real
            # snapshot -- give the growth-over-time chart a starting point to
            # draw from too, not just the return math above.
            series.insert(
                0,
                {
                    "as_of": opening_day.isoformat(),
                    "actual_value": _money(opening_value),
                    "benchmark_value": _money(benchmark_values.get(opening_day)),
                },
            )

        return {
            "available": True,
            "warnings": warnings,
            "as_of": as_of.isoformat(),
            "cash_flow_events": [
                {
                    "date": event.date.isoformat(),
                    "amount": _money(event.amount),
                    "label": event.label,
                    "kind": event.kind,
                }
                for event in all_events
            ],
            "actual": {
                "terminal_value": _money(result.actual_terminal_value),
                "xirr_pct": _money(result.actual_xirr_pct),
            },
            "benchmark": {
                "name": "SPY",
                "terminal_value": _money(result.benchmark_terminal_value),
                "xirr_pct": _money(result.benchmark_xirr_pct),
            },
            "value_added": _money(result.value_added),
            "series": series,
        }

    def _build_wheel_return(self, current_prices: dict[str, float]) -> dict[str, Any]:
        """Money-weighted (XIRR) return isolated to just the wheel -- see
        ``wheel.metrics.wheel_cash_flow_events``/``wheel_terminal_value`` for
        what counts and why (option legs and assignment-sourced share lots
        only; every other holding in the account -- buy-and-hold ETFs,
        non-wheel stock, dividends -- is excluded, unlike ``_build_benchmark``'s
        whole-account figure, which only sees a single terminal total_value
        with no notion of "which dollars are wheel dollars").

        Also replays the wheel's own cash-flow timing into SPY -- the same
        technique ``_build_benchmark`` uses for the whole account -- so "did
        the wheel beat buy-and-hold SPY" has a direct answer isolated from
        whatever else (buy-and-hold ETFs, other stock) sits in the same
        account. A whole-account XIRR ahead of or behind SPY doesn't answer
        that question by itself: it's diluted by every non-wheel dollar in
        the account too.

        Uses ``self.all_cycles`` -- every cycle ever built from this
        account's full history -- independent of whatever ticker/date
        filters are active on this particular ``build()`` call, the same
        "fact about a moment in time, not a slice of the transaction window"
        reasoning ``_build_net_worth``/``_build_benchmark`` already follow.
        """
        events = wheel_cash_flow_events(self.all_cycles)
        if len(events) < 2:
            return {
                "available": False,
                "warnings": [
                    "not enough wheel activity yet to compute a return -- need at least one "
                    "full open/close (or assignment) on record"
                ],
            }

        through = self.last_date or date.today()
        terminal_value = wheel_terminal_value(self.all_cycles, through, current_prices)
        cash_flow_events = [
            bm.CashFlowEvent(date=event_date, amount=amount, label=label, source="wheel", kind="WHEEL")
            for event_date, amount, label in events
        ]

        price_points, warnings = marketdata.get_price_series("SPY")

        def price_lookup(day: date):
            return marketdata.price_on_or_before(price_points, day)

        benchmark_terminal_value = bm.simulate_benchmark(cash_flow_events, through, price_lookup)
        result = bm.compare_to_benchmark(cash_flow_events, terminal_value, benchmark_terminal_value, through)

        if result.actual_xirr_pct is None:
            return {
                "available": False,
                "warnings": [
                    "wheel cash flows don't yet have enough sign variation (money in AND "
                    "money out) to solve for a rate -- typically means every tracked "
                    "position is still open"
                ],
            }

        payload: dict[str, Any] = {
            "available": True,
            "warnings": warnings,
            "as_of": through.isoformat(),
            "terminal_value": _money(result.actual_terminal_value),
            "xirr_pct": _money(result.actual_xirr_pct),
            "cash_flow_events": [
                {"date": event.date.isoformat(), "amount": _money(event.amount), "label": event.label}
                for event in sorted(cash_flow_events, key=lambda event: event.date)
            ],
            "benchmark": {
                "name": "SPY",
                "terminal_value": _money(result.benchmark_terminal_value),
                "xirr_pct": _money(result.benchmark_xirr_pct),
            },
            "value_added": _money(result.value_added),
        }
        return payload

    def _build_trade_log(self, current_prices: dict[str, float | None]) -> dict[str, Any]:
        """One entry per wheel (``all_cycles``), each with its whole-history
        transaction ledger -- deliberately independent of any ticker/date/status
        filter, the same "a wheel is a historical unit" reasoning
        ``_build_wheel_return`` / ``_build_net_worth`` follow.

        Transaction attribution is by underlying + inclusive date span, which is
        only safe while a ticker's cycles are strictly separated in time. The
        engine can legally close one cycle and open the next on the same day, so
        any same-ticker pair that touches (or, defensively, overlaps) switches to
        engine-exact attribution instead -- see ``_trade_log_entry``.
        """
        through = self.last_date or date.today()
        dividend_row_ids = {t.row_id for t in cf.dividend_transactions(self.transactions)}
        dividends = dividends_by_cycle(self.all_cycles, self.transactions)

        by_underlying: dict[str, list[Cycle]] = {}
        for cycle in self.all_cycles:
            by_underlying.setdefault(cycle.underlying, []).append(cycle)

        engine_exact: set[str] = set()
        warnings: list[str] = []
        for group in by_underlying.values():
            ordered = sorted(group, key=lambda cycle: cycle.start_date)
            for prev, nxt in zip(ordered, ordered[1:]):
                prev_end = prev.end_date or through
                if nxt.start_date <= prev_end:
                    engine_exact.update((prev.cycle_id, nxt.cycle_id))
                    if prev.end_date is not None and nxt.start_date < prev.end_date:
                        warnings.append(
                            f"{prev.underlying}: cycles {prev.cycle_id} and {nxt.cycle_id} "
                            "overlap in time -- Trade Log attributed them via the engine"
                        )

        wheels = [
            _trade_log_entry(
                cycle,
                self.transactions,
                through,
                name=self._company_names.get(cycle.underlying),
                dividend_row_ids=dividend_row_ids,
                dividends=dividends.get(cycle.cycle_id, 0.0),
                engine_exact=cycle.cycle_id in engine_exact,
                current_price=current_prices.get(cycle.underlying),
            )
            for cycle in sorted(self.all_cycles, key=lambda cycle: (cycle.start_date, cycle.underlying))
        ]

        # Each wheel's currently-committed capital as a share of the account:
        # of total account value when a Positions snapshot is on hand, otherwise
        # of the capital committed across every wheel right now.
        net_worth = getattr(self, "_net_worth", None) or {}
        if net_worth.get("available") and net_worth.get("total_value"):
            denom, denom_label = net_worth["total_value"], "account value"
        else:
            denom = sum(w["capital_committed_now"] or 0.0 for w in wheels)
            denom_label = "capital in wheels"
        for wheel in wheels:
            cap = wheel["capital_committed_now"] or 0.0
            wheel["capital_committed_pct"] = round(100.0 * cap / denom, 1) if denom and cap else None
            wheel["capital_committed_pct_of"] = denom_label

        return {"wheels": wheels, "warnings": warnings}

    def _build_open_hedges(self, current_prices: dict[str, float | None]) -> list[dict[str, Any]]:
        """Every open, unpaired long option leg across ``all_cycles`` -- a
        protective put/call (or a lone directional long) still on the books.

        Filter-independent, like the Trade Log: a hedge needs managing whatever
        date window is on screen. A leg paired into a same-day ``Spread`` is
        excluded -- its risk is already defined, there is nothing to "wind down."
        Sorted soonest-expiry first, so the most urgent row leads the banner.
        """
        through = self.last_date or date.today()
        dividends = dividends_by_cycle(self.all_cycles, self.transactions)
        hedges: list[dict[str, Any]] = []
        for cycle in self.all_cycles:
            spread_leg_ids: set[str] = set()
            for spread in cycle.spreads:
                spread_leg_ids.add(spread.short_leg_id)
                spread_leg_ids.add(spread.long_leg_id)
            for leg in cycle.legs:
                if not leg.is_open or leg.side != LONG or leg.expiry is None:
                    continue
                if leg.leg_id in spread_leg_ids:
                    continue
                hedges.append(
                    _open_hedge_entry(
                        cycle,
                        leg,
                        through,
                        name=self._company_names.get(cycle.underlying),
                        current_price=current_prices.get(cycle.underlying),
                        dividends=dividends.get(cycle.cycle_id, 0.0),
                    )
                )
        hedges.sort(key=lambda h: h["days_to_expiry"])
        return hedges

    # ---- query ----

    def build(self, filters: Filters | None = None) -> dict[str, Any]:
        filters = filters or Filters()
        transactions = [t for t in self.transactions if filters.matches_transaction(t)]

        if transactions:
            cycles, engine = build_cycles(transactions)
            through = filters.end or max(t.event_date for t in transactions)
        else:
            cycles, engine = [], WheelEngine([])
            through = filters.end or self.last_date or date.today()

        # Reconciliation is a claim about ingest integrity -- "every dollar in the
        # file landed somewhere in the model" -- so it is measured before the
        # status filter, which merely hides already-built cycles from the view.
        built_cycles = cycles
        if filters.statuses:
            cycles = [cycle for cycle in cycles if cycle.status in filters.statuses]

        # Capital is a state, not an event count: a position opened before the
        # window still has real money committed once the window begins.
        # Reconstructing it only from start-filtered transactions would show it
        # appearing from nothing partway through, understating capital until the
        # next in-window trade happens to touch that position -- the VOO/VEON/etc
        # style long-lived wheel is exactly the case this breaks. So capital is
        # rebuilt from the full history for the selected tickers/status/end date
        # (a start filter is a display crop, not history amputation) and only
        # cropped to `filters.start` afterwards, for the average and the chart.
        since = filters.start
        capital_transactions = [
            t for t in self.transactions if replace(filters, start=None).matches_transaction(t)
        ]
        capital_cycles = build_cycles(capital_transactions)[0] if capital_transactions else []
        if filters.statuses:
            capital_cycles = [cycle for cycle in capital_cycles if cycle.status in filters.statuses]

        # The capital snapshot reaches at least as far as the latest Positions
        # export, even past `through` -- `through` is pinned to the last
        # *transaction*, so a day with a fresh account download but no trade
        # (the common case: most days nothing happens) would otherwise have no
        # capital_series point for that date at all, and the frontend's
        # "not deployed" overlay (which matches a Positions snapshot date
        # against an exact capital_series day -- see notDeployedByDate in
        # app.js) would silently fail to show anything for it.
        #
        # Gated on `through >= self.last_date`, not on `filters.end is None`:
        # every non-"all history" preset (ytd, last 30 days, ...) sets
        # `filters.end` explicitly to `meta.data_last_date` (app.js's
        # applyPreset()), so gating on "no explicit end" would mean this
        # almost never fires under the app's own default view. What actually
        # distinguishes "stop exactly here" from "this happens to be as far
        # as the data goes" is whether `through` reaches the account's true
        # latest transaction (`self.last_date`, unfiltered by ticker/date) --
        # a deliberately historical window (a past calendar year, a ticker
        # filter whose last trade predates other tickers' activity) sits
        # strictly before it and is left alone.
        #
        # Kept as a separate variable (not a `through` reassignment) so P&L
        # figures -- days_span and everything annualized against it -- stay
        # anchored to real trading activity, not inflated by trade-free days
        # that exist only because an export happened to be downloaded.
        capital_through = through
        if self.snapshots and through >= (self.last_date or through):
            latest_snapshot_date = max(s.as_of.date() for s in self.snapshots)
            if latest_snapshot_date > capital_through:
                capital_through = latest_snapshot_date

        # Stock Unrealized P&L (current_prices) and Total Position ROI's
        # dividend term (dividends) both apply to `cycles` -- the same
        # ticker/date/status-filtered set every other P&L figure here uses --
        # not `capital_cycles`, so a filtered-out ticker's dividends and
        # unrealized gains don't leak into the figures on screen.
        current_prices = self._current_prices()
        dividends = dividends_by_cycle(cycles, transactions)
        if self._wheel_return is None:
            self._wheel_return = self._build_wheel_return(current_prices)
        if self._trade_log is None:
            self._trade_log = self._build_trade_log(current_prices)
        if self._open_hedges is None:
            self._open_hedges = self._build_open_hedges(current_prices)

        portfolio = portfolio_metrics(
            cycles,
            through,
            capital_cycles=capital_cycles,
            since=since,
            current_prices=current_prices,
            dividends_by_cycle=dividends,
            capital_through=capital_through,
        )
        capital = portfolio_capital_series(capital_cycles, capital_through, since)
        # "Right now," not "since": uses capital_cycles directly (uncropped by
        # `since`), the same state-scoped set capital_deployed_now/avg_capital/
        # peak_capital already use -- a single current-moment snapshot has no
        # display-crop analog the way a time series does.
        wheel_state = wheel_state_breakdown(capital_cycles, capital_through)

        # Cash flow is dated to each row's own event_date, not to the cycle it
        # eventually belongs to, so it is built straight from the (ticker/date)
        # filtered transaction slice -- the same one `cycles` came from -- rather
        # than from `cycles` itself. Collateral for the yield-% denominator reuses
        # `capital`, the same series the "Capital deployed" chart already shows,
        # so the two agree with each other.
        capital_points = [(point.day, point.total) for point in capital]
        cash_flow_rows = cf.monthly_cashflow_series(transactions, capital_points, through, since)
        cash_flow = {
            "months": cash_flow_rows,
            "weeks": cf.weekly_cashflow_series(transactions, through, since),
            "trailing": cf.range_summary(cash_flow_rows, capital_points, through),
        }

        portfolio_payload = {
            **{
                key: (_money(value) if isinstance(value, float) else value)
                for key, value in asdict(portfolio).items()
            },
            "first_date": _iso(portfolio.first_date),
            "last_date": _iso(portfolio.last_date),
            "win_rate_pct": portfolio.win_rate_pct,
        }
        dashboard_insights = portfolio_insights(
            portfolio_payload,
            (self._trade_log or {}).get("wheels", []),
            self._open_hedges or [],
            wheel_return=self._wheel_return,
            benchmark=self._benchmark,
            wheel_state=wheel_state,
        )

        return {
            "meta": {
                "source": " + ".join(os.path.basename(path) for path in self.csv_paths),
                "sources": [
                    {
                        **source,
                        "first_date": _iso(source["first_date"]),
                        "last_date": _iso(source["last_date"]),
                    }
                    for source in self.merge.sources
                ],
                "combined": self.merge.combined,
                "rows_parsed": self.merge.rows_parsed,
                "rows_kept": self.merge.rows_kept,
                "duplicates_removed": self.merge.duplicates_removed,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "through": _iso(through),
                "data_first_date": _iso(self.first_date),
                "data_last_date": _iso(self.last_date),
                "available_tickers": self.available_tickers(),
                "statuses": ["ACTIVE", "NO_ACTIVITY", "CLOSED"],
                "transactions_in_slice": len(transactions),
                "transactions_total": len(self.transactions),
                "columns_swapped": any(report.columns_swapped for report in self.reports),
                "parse_warnings": [
                    f"{os.path.basename(path)}: {warning}"
                    for path, report in zip(self.csv_paths, self.reports)
                    for warning in report.warnings
                ],
                "engine_warnings": engine.warnings,
                "unmatched_closes": engine.unmatched_closes,
                "market_data_warnings": self._price_warnings,
                "filters": {
                    "tickers": filters.tickers,
                    "start": _iso(filters.start),
                    "end": _iso(filters.end),
                    "statuses": filters.statuses,
                },
            },
            "portfolio": portfolio_payload,
            "insights": dashboard_insights,
            "cycles": [
                _cycle_payload(
                    cycle,
                    through,
                    current_price=current_prices.get(cycle.underlying),
                    dividends=dividends.get(cycle.cycle_id, 0.0),
                )
                for cycle in cycles
            ],
            "tickers": [
                {key: (_money(value) if isinstance(value, float) else value) for key, value in row.items()}
                for row in ticker_summary(
                    cycles,
                    through,
                    capital_cycles=capital_cycles,
                    since=since,
                    current_prices=current_prices,
                    dividends_by_cycle=dividends,
                )
            ],
            "capital_series": [
                _capital_point(point)
                for point in capital
            ],
            "pnl_series": realized_pl_series(cycles),
            # Wheel-only realized P/L series, and the weekly PPD track built from
            # it. PPD's numerator is wheel-only (matches portfolio.profit_per_day
            # after the is_wheel split); its denominator spans every cycle, so
            # the last cum_ppd point equals the Performance tile exactly.
            "pnl_series_wheel": realized_pl_series([c for c in cycles if c.is_wheel]),
            "ppd_series": weekly_ppd_series(
                realized_pl_series([c for c in cycles if c.is_wheel]),
                min((cycle.start_date for cycle in cycles), default=through),
                through,
            ),
            "cash_flow": cash_flow,
            "wheel_state": wheel_state,
            "reconciliation": _reconciliation(
                transactions, built_cycles, self.reports, engine.unmatched_cash
            ),
            "net_worth": self._net_worth,
            "benchmark": self._benchmark,
            "wheel_return": self._wheel_return,
            "trade_log": self._trade_log,
            "open_hedges": self._open_hedges,
        }
