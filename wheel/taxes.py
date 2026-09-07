"""Realized-gains reference: Fidelity's closed-lots export, plus a rough
per-ticker cross-check against the wheel engine's own option P/L.

**A rough cross-check, not a strict reconciliation.** Fidelity dates a closed
option lot by its buy-to-close (acquired) and sell-to-open (sold), and this
export covers a fixed period; the engine sums a ticker's option P/L over whatever
falls inside that same window. A leg whose open and close land on opposite sides
of the window edge is counted by one side and not the other, so per-ticker
figures often differ by more than rounding -- ``close`` vs ``review`` flags
"same ballpark" vs "worth a look", nothing more.
"""

from __future__ import annotations

from typing import Any, Sequence

from wheel.closed_lots import ClosedLot, realized_by_underlying, realized_totals

# "close" = same ballpark: within $50 or 15% of Fidelity's figure. Wider than a
# rounding tolerance on purpose -- see the module docstring on why per-ticker
# figures legitimately drift at the coverage-window edge.
_CLOSE_ABS = 50.0
_CLOSE_FRAC = 0.15


def reconcile(
    ticker_summary_rows: Sequence[dict[str, Any]],
    closed_lots: Sequence[ClosedLot],
) -> dict[str, Any]:
    """``{"rows": [...], "totals": {...}, "lots": [...], "notes": "..."}``.

    ``ticker_summary_rows`` is the dashboard ``tickers`` payload list; only its
    ``underlying`` and ``option_realized_pl`` are used.
    """
    engine_by_ticker = {
        row["underlying"]: row.get("option_realized_pl") or 0.0 for row in ticker_summary_rows
    }
    fidelity = realized_by_underlying(closed_lots)

    # Only tickers the closed-lots export actually covers -- an engine-only
    # ticker just wasn't traded in this export's period, not a discrepancy.
    rows: list[dict[str, Any]] = []
    for underlying in sorted(fidelity):
        agg = fidelity[underlying]
        # Compare option P/L to option P/L: this report is options-only today,
        # but an equity lot's realized would otherwise pollute the difference.
        fidelity_realized = agg["options"]
        engine_pl = engine_by_ticker.get(underlying)
        lot_count = agg["lot_count"]
        if engine_pl is None:
            difference = None
            status = "review"
        else:
            difference = round(engine_pl - fidelity_realized, 2)
            tol = max(_CLOSE_ABS, _CLOSE_FRAC * abs(fidelity_realized))
            status = "close" if abs(difference) <= tol else "review"
        rows.append(
            {
                "underlying": underlying,
                "wheel_engine_pl": round(engine_pl, 2) if engine_pl is not None else None,
                "fidelity_realized": fidelity_realized,
                "fidelity_equity": agg["equity"],
                "difference": difference,
                "lot_count": lot_count,
                "status": status,
            }
        )

    totals = realized_totals(closed_lots)
    totals["wheel_engine_pl"] = round(
        sum(r["wheel_engine_pl"] or 0.0 for r in rows), 2
    )
    totals["close"] = sum(1 for r in rows if r["status"] == "close")
    totals["review"] = sum(1 for r in rows if r["status"] == "review")

    return {
        "rows": rows,
        "totals": totals,
        "lots": [_lot_payload(lot) for lot in closed_lots],
        "notes": (
            f"Fidelity's closed-lots export covers {totals.get('coverage_start')} → "
            f"{totals.get('coverage_end')}. This cross-check compares its realized "
            "option figure per ticker to the wheel engine's option P/L over that "
            "same window; the two date each lot differently, so figures near the "
            "window edge legitimately differ — 'close' means same ballpark, "
            "'review' means worth a look."
        ),
    }


def _lot_payload(lot: ClosedLot) -> dict[str, Any]:
    return {
        "symbol": lot.symbol,
        "cusip": lot.cusip,
        "description": lot.description,
        "underlying": lot.underlying,
        "is_option": lot.is_option,
        "date_acquired": lot.date_acquired.isoformat() if lot.date_acquired else None,
        "date_sold": lot.date_sold.isoformat() if lot.date_sold else None,
        "quantity": lot.quantity,
        "cost_basis": lot.cost_basis,
        "proceeds": lot.proceeds,
        "st_gain": lot.st_gain,
        "lt_gain": lot.lt_gain,
        "realized": lot.realized,
        "term": lot.term,
    }
