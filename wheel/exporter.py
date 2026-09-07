"""Turn dashboard payload lists into plain CSV for record-keeping and spreadsheets.

Numbers go out raw -- no ``$`` / ``%`` / thousands separators -- so a spreadsheet
reads them as numbers. ``columns`` is an ordered ``[(payload_key, header), ...]``
list; a missing key becomes an empty cell rather than an error, so the same
column set survives a payload that grew or shrank.
"""

from __future__ import annotations

import csv
import io
from typing import Any, Iterable, Sequence


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def rows_to_csv(
    rows: Iterable[dict[str, Any]],
    columns: Sequence[tuple[str, str]],
) -> str:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow([header for _, header in columns])
    for row in rows:
        writer.writerow([_cell(row.get(key)) for key, _ in columns])
    return buf.getvalue()


CYCLE_COLUMNS: list[tuple[str, str]] = [
    ("cycle_id", "Wheel"),
    ("underlying", "Ticker"),
    ("status", "Status"),
    ("is_wheel", "Is wheel"),
    ("start_date", "Start"),
    ("end_date", "End"),
    ("days_active", "Days active"),
    ("premium_received", "Premium received"),
    ("premium_paid", "Premium paid"),
    ("option_realized_pl", "Option realized P/L"),
    ("stock_realized_pl", "Stock realized P/L"),
    ("net_realized_pl", "Net realized P/L"),
    ("dividends_received", "Dividends"),
    ("fees", "Fees"),
    ("avg_collateral", "Avg collateral"),
    ("roi_on_avg_wheel_pct", "Wheel ROC %"),
    ("annualized_wheel_roc_pct", "Annualized Wheel ROC %"),
    ("net_option_yield_pct", "Net option yield %"),
    ("total_position_roi_pct", "Total position ROI %"),
    ("rolls", "Rolls"),
    ("assignments", "Assignments"),
    ("wins", "Wins"),
    ("losses", "Losses"),
]

TICKER_COLUMNS: list[tuple[str, str]] = [
    ("underlying", "Ticker"),
    ("cycles", "Cycles"),
    ("is_wheel", "Is wheel"),
    ("premium_received", "Premium received"),
    ("premium_paid", "Premium paid"),
    ("option_realized_pl", "Option realized P/L"),
    ("stock_realized_pl", "Stock realized P/L"),
    ("net_realized_pl", "Net realized P/L"),
    ("open_premium", "Open premium"),
    ("dividends_received", "Dividends"),
    ("fees", "Fees"),
    ("avg_capital", "Avg capital"),
    ("profit_per_day", "Profit per day"),
    ("roi_on_avg_wheel_pct", "Wheel ROC %"),
    ("annualized_wheel_roc_pct", "Annualized Wheel ROC %"),
    ("total_position_roi_pct", "Total position ROI %"),
    ("rolls", "Rolls"),
    ("assignments", "Assignments"),
    ("wins", "Wins"),
    ("losses", "Losses"),
]

# One row per ledger line, each carrying its wheel's id / ticker so the flat file
# stands alone.
TRADE_LOG_COLUMNS: list[tuple[str, str]] = [
    ("wheel_id", "Wheel"),
    ("underlying", "Ticker"),
    ("type", "Type"),
    ("date", "Date"),
    ("expiration", "Expiration"),
    ("strike", "Strike"),
    ("signed_quantity", "Quantity"),
    ("price", "Price"),
    ("initial_csp_collateral", "Initial CSP collateral"),
    ("fees", "Fees"),
    ("commission", "Commission"),
    ("net_cash_flow", "Net cash flow"),
    ("close_return_pct", "Close return %"),
    ("running_cash_flow", "Running cash flow"),
    ("running_break_even", "Running break-even"),
    ("is_settled", "Settled"),
    ("synthetic", "Synthetic"),
]

CLOSED_LOT_COLUMNS: list[tuple[str, str]] = [
    ("symbol", "Symbol"),
    ("description", "Description"),
    ("underlying", "Underlying"),
    ("is_option", "Is option"),
    ("date_acquired", "Date acquired"),
    ("date_sold", "Date sold"),
    ("quantity", "Quantity"),
    ("cost_basis", "Cost basis"),
    ("proceeds", "Proceeds"),
    ("st_gain", "Short-term gain/loss"),
    ("lt_gain", "Long-term gain/loss"),
    ("realized", "Realized gain/loss"),
    ("term", "Term"),
]


def flatten_trade_log(wheels: Iterable[dict[str, Any]], only_wheel: str | None = None) -> list[dict[str, Any]]:
    """``trade_log.wheels`` -> one flat row per ledger line."""
    out: list[dict[str, Any]] = []
    for wheel in wheels:
        if only_wheel and wheel.get("cycle_id") != only_wheel:
            continue
        for txn in wheel.get("transactions") or []:
            out.append({**txn, "wheel_id": wheel.get("cycle_id"), "underlying": wheel.get("underlying")})
    return out
