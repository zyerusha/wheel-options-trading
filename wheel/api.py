"""JSON payload assembly for the dashboard.

Filtering rebuilds the cycles from the filtered transaction slice rather than
post-filtering finished cycles.  That costs a few milliseconds and buys the
guarantee the dataviz brief asks for: every stat, chart and table on the page is
derived from the same slice, so the numbers always agree with each other.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from typing import Any, Sequence

from wheel import benchmark as bm
from wheel import cashflow as cf
from wheel import marketdata
from wheel.engine import Cycle, WheelEngine, build_cycles
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
)
from wheel.parser import (
    OPTION_ACTIONS,
    TRADE_ACTIONS,
    MergeReport,
    ParseReport,
    Transaction,
    parse_exports,
)
from wheel.positions import discover_position_snapshots, latest_snapshot_per_account, load_snapshots

EXPORT_DIRS = (".", "data")


def _find_history_header(path: str) -> str | None:
    """The 'Run Date...' header line of a broker export, if this file has one."""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            head = handle.read(4096)
    except OSError:
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
    return {
        "date": _iso(point.day),
        "put": put,
        "stock": stock,
        "call": call,
        "long": long_premium,
        "spread": spread,
        "total": round(put + stock + call + long_premium + spread, 2),
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
    ):
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

    # ---- market data ----

    def _current_prices(self) -> dict[str, float | None]:
        """Latest close for every ticker this dashboard holds open shares in.

        Computed once per Dashboard instance, not once per build() -- see the
        comment in __init__. A ticker whose fetch fails yields ``None`` for
        that ticker only (wheel.marketdata never raises), which flows through
        to that cycle's stock_unrealized_pl as "unavailable," not a crash.

        Fetched in parallel, not one ticker at a time: each is an independent
        network round trip (its own URL, its own cache file under
        ``data/prices/``), so nothing about them requires serializing, and a
        cold cache with a few dozen tickers turned a single-digit-second page
        load into a multi-second one when fetched sequentially. ``pool.map``
        keeps `tickers`' order, so building `prices`/`warnings` from the
        zipped results needs no lock -- every dict/list write still happens
        on this thread, only the network wait itself overlaps.
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
        if tickers:
            with ThreadPoolExecutor(max_workers=min(8, len(tickers))) as pool:
                for ticker, (points, ticker_warnings) in zip(tickers, pool.map(marketdata.get_price_series, tickers)):
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

        latest_by_account = latest_snapshot_per_account(self.snapshots)
        primary_account = max(latest_by_account.values(), key=lambda snapshot: snapshot.as_of).account_number
        account_snapshots = sorted(
            (s for s in self.snapshots if s.account_number == primary_account),
            key=lambda snapshot: snapshot.as_of,
        )

        if len(account_snapshots) < 2:
            return {
                "available": False,
                "warnings": [
                    "at least two Portfolio Positions snapshots, taken on different "
                    "dates, are needed to compute a return -- only one is available so far"
                ],
            }

        events, warnings = bm.external_cashflows(self.transactions)
        first_snapshot = account_snapshots[0]
        as_of = account_snapshots[-1].as_of.date()

        # The tracked transaction history rarely reaches back to when the account
        # was first funded, so the account's value at the earliest available
        # snapshot stands in for an opening investment made on that date. Without
        # it, XIRR would see only cash moved after that point and ignore whatever
        # balance was already in place -- understating invested capital, or with
        # no external transfers on record at all, making the return uncomputable.
        opening_day = first_snapshot.as_of.date()
        opening_event = bm.CashFlowEvent(
            date=opening_day,
            amount=first_snapshot.total_value,
            label="Opening balance (first available snapshot)",
            source=first_snapshot.source,
            kind="OPENING_BALANCE",
        )
        all_events = [opening_event] + [event for event in events if event.date > opening_day]

        price_points, market_warnings = marketdata.get_price_series("SPY")
        warnings.extend(market_warnings)

        def price_lookup(day: date):
            return marketdata.price_on_or_before(price_points, day)

        valuation_dates = [snapshot.as_of.date() for snapshot in account_snapshots]
        benchmark_values = bm.simulate_benchmark_series(all_events, valuation_dates, price_lookup)

        actual_terminal_value = account_snapshots[-1].total_value
        benchmark_terminal_value = benchmark_values.get(as_of)

        result = bm.compare_to_benchmark(all_events, actual_terminal_value, benchmark_terminal_value, as_of)

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
            "series": [
                {
                    "as_of": snapshot.as_of.date().isoformat(),
                    "actual_value": _money(snapshot.total_value),
                    "benchmark_value": _money(benchmark_values.get(snapshot.as_of.date())),
                }
                for snapshot in account_snapshots
            ],
        }

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

        # Stock Unrealized P&L (current_prices) and Total Position ROI's
        # dividend term (dividends) both apply to `cycles` -- the same
        # ticker/date/status-filtered set every other P&L figure here uses --
        # not `capital_cycles`, so a filtered-out ticker's dividends and
        # unrealized gains don't leak into the figures on screen.
        current_prices = self._current_prices()
        dividends = dividends_by_cycle(cycles, transactions)

        portfolio = portfolio_metrics(
            cycles,
            through,
            capital_cycles=capital_cycles,
            since=since,
            current_prices=current_prices,
            dividends_by_cycle=dividends,
        )
        capital = portfolio_capital_series(capital_cycles, through, since)

        # Cash flow is dated to each row's own event_date, not to the cycle it
        # eventually belongs to, so it is built straight from the (ticker/date)
        # filtered transaction slice -- the same one `cycles` came from -- rather
        # than from `cycles` itself. Collateral for the yield-% denominator reuses
        # `capital`, the same series the "Capital deployed" chart already shows,
        # so the two agree with each other.
        cash_flow_rows = cf.monthly_cashflow_series(
            transactions, [(point.day, point.total) for point in capital], through, since
        )
        cash_flow = {
            "months": cash_flow_rows,
            "trailing": cf.trailing_metrics(cash_flow_rows, through),
        }

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
                "statuses": ["ACTIVE", "CLOSED", "ASSIGNED"],
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
            "portfolio": {
                **{
                    key: (_money(value) if isinstance(value, float) else value)
                    for key, value in asdict(portfolio).items()
                },
                "first_date": _iso(portfolio.first_date),
                "last_date": _iso(portfolio.last_date),
                "win_rate_pct": portfolio.win_rate_pct,
            },
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
            "cash_flow": cash_flow,
            "reconciliation": _reconciliation(
                transactions, built_cycles, self.reports, engine.unmatched_cash
            ),
            "net_worth": self._net_worth,
            "benchmark": self._benchmark,
        }
