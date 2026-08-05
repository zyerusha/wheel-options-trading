"""JSON payload assembly for the dashboard.

Filtering rebuilds the cycles from the filtered transaction slice rather than
post-filtering finished cycles.  That costs a few milliseconds and buys the
guarantee the dataviz brief asks for: every stat, chart and table on the page is
derived from the same slice, so the numbers always agree with each other.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from typing import Any, Sequence

from wheel.engine import Cycle, WheelEngine, build_cycles
from wheel.metrics import (
    capital_timeline,
    cycle_metrics,
    leg_rows,
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

EXPORT_DIRS = (".", "data")


def looks_like_export(path: str) -> bool:
    """Cheap header peek: does this CSV look like a broker history export?

    Keeps generated output and unrelated CSVs out of discovery and out of the
    dashboard's file picker, without paying for a full parse.
    """
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    return any(line.lower().startswith("run date") for line in head.splitlines())


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
    return {
        "date": _iso(point.day),
        "put": put,
        "stock": stock,
        "call": call,
        "long": long_premium,
        "total": round(put + stock + call + long_premium, 2),
    }


def _cycle_payload(cycle: Cycle, through: date) -> dict[str, Any]:
    metrics = cycle_metrics(cycle, through)
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
            "basis_per_share": lot.basis_per_share,
            "remaining": lot.remaining,
            "source": lot.source,
            "synthetic": lot.synthetic,
            "basis_known": lot.basis_known,
            "cost": _money(lot.cost),
        }
        for lot in cycle.share_lots
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

    def __init__(self, csv_path: str | Sequence[str] | None = None):
        if csv_path is None:
            csv_path = discover_exports()
        paths = [csv_path] if isinstance(csv_path, str) else list(csv_path)
        if not paths:
            raise ValueError("no broker export found; pass one explicitly")

        self.csv_paths = paths
        self.csv_path = paths[0]  # kept for single-file callers
        self.transactions, self.reports, self.merge = parse_exports(paths)
        self.report = self.reports[0]
        self.all_cycles, self.engine = build_cycles(self.transactions)

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

        portfolio = portfolio_metrics(cycles, through, capital_cycles=capital_cycles, since=since)
        capital = portfolio_capital_series(capital_cycles, through, since)

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
            "cycles": [_cycle_payload(cycle, through) for cycle in cycles],
            "tickers": [
                {key: (_money(value) if isinstance(value, float) else value) for key, value in row.items()}
                for row in ticker_summary(cycles, through, capital_cycles=capital_cycles, since=since)
            ],
            "capital_series": [
                _capital_point(point)
                for point in capital
            ],
            "pnl_series": realized_pl_series(cycles),
            "reconciliation": _reconciliation(
                transactions, built_cycles, self.reports, engine.unmatched_cash
            ),
        }
