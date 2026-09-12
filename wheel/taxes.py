"""Per-ticker cross-check of Fidelity's closed-lots export against the wheel
engine's own option P/L.

The two are made to measure the same thing before they are compared:

* **Windowed by disposition date.** Fidelity books a closed lot on the day the
  position was *closed* -- the buy-to-close or expiry of a short, the sell of a
  long -- which is the later of the lot's two dates (``wheel/closed_lots.py``
  explains why ``date_sold`` can precede ``date_acquired``). The engine side
  counts only leg closes dated inside that same span, and is built from the full
  transaction history rather than a date-filtered slice, so a leg opened before
  the window but closed inside it still reconciles instead of dropping out as an
  orphaned close.
* **Assignment excluded.** An assigned short option never becomes a closed-lot
  row: its premium moves into the assigned shares' cost basis, and this export
  carries no equity rows at all. The engine books that premium as option P/L, so
  it is removed here.
* **One account.** A Fidelity closed-lots export covers a single account.
  :func:`best_fit_account` recovers which one by scoring each account's
  comparably-measured P/L against the file, so the cross-check never sums the
  whole book against one account's broker report.

What remains after all that is a small, expected drift right at the end date: a
position that expires on the export date is realized by the engine before the
broker books it. ``close`` vs ``review`` flags "within tolerance" vs "worth a
look", nothing more.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from wheel.closed_lots import ClosedLot, realized_by_underlying, realized_totals
from wheel.parser import ASSIGNED

# "close" = same ballpark: within $50 or 15% of Fidelity's figure. Wider than a
# rounding tolerance on purpose -- a position expiring on the export date is
# realized here a day before the broker books it, and near the window edge a
# roll's two halves can fall on opposite sides.
_CLOSE_ABS = 50.0
_CLOSE_FRAC = 0.15


def _tolerance(fidelity_realized: float) -> float:
    return max(_CLOSE_ABS, _CLOSE_FRAC * abs(fidelity_realized))


def disposition_window(lots: Sequence[ClosedLot]) -> tuple[date | None, date | None]:
    """The span of *closing* dates the export reflects: ``(earliest, latest)``.

    Each lot's disposition is the later of its two dates. The opening dates run
    earlier and must not widen the window -- an engine scoped to them would
    count premium from legs whose disposition the report never recorded.
    """
    closes = [
        max(lot.date_acquired, lot.date_sold)
        for lot in lots
        if lot.date_acquired and lot.date_sold
    ]
    if not closes:
        return None, None
    return min(closes), max(closes)


def comparable_option_pl(
    cycles: Iterable[Any], start: date | None, end: date | None
) -> dict[str, float]:
    """``{underlying: option P/L}`` measured the way the closed-lots export
    measures it -- see the module docstring.

    ``cycles`` must be built from the full transaction history (through ``end``),
    not a start-filtered slice, or a leg opened before ``start`` loses its
    opening premium and its in-window close reads as a pure debit.
    """
    out: dict[str, float] = {}
    for cycle in cycles:
        for leg in cycle.legs:
            premium_per_contract = leg.cash_per_contract
            for close in leg.closes:
                if close.action == ASSIGNED:
                    continue
                if start and close.date < start:
                    continue
                if end and close.date > end:
                    continue
                out[cycle.underlying] = out.get(cycle.underlying, 0.0) + (
                    premium_per_contract * close.contracts + close.cash
                )
    return {ticker: round(value, 2) for ticker, value in out.items()}


def best_fit_account(
    engine_pl_by_account: Mapping[str, Mapping[str, float]],
    closed_lots: Sequence[ClosedLot],
) -> str | None:
    """Which account the closed-lots file belongs to.

    A Fidelity export has no account column, so each account's comparably
    measured option P/L is scored against the file: the owner is the account
    that lands the most tickers inside tolerance, then -- as a tie-break -- the
    least total absolute error. ``None`` only when there are no accounts.
    """
    targets = {t: agg["options"] for t, agg in realized_by_underlying(closed_lots).items()}
    if not targets:
        return None
    best: str | None = None
    best_key: tuple[int, float] | None = None
    for account_id, engine in engine_pl_by_account.items():
        matched = sum(
            1
            for ticker, want in targets.items()
            if engine.get(ticker) is not None and abs(engine[ticker] - want) <= _tolerance(want)
        )
        error = sum(abs(engine.get(ticker, 0.0) - want) for ticker, want in targets.items())
        key = (matched, -error)
        if best_key is None or key > best_key:
            best, best_key = account_id, key
    return best


def reconcile(
    engine_option_pl: Mapping[str, float],
    closed_lots: Sequence[ClosedLot],
    *,
    window: tuple[date | None, date | None] | None = None,
) -> dict[str, Any]:
    """``{"rows": [...], "totals": {...}, "lots": [...], "notes": "..."}``.

    ``engine_option_pl`` maps ``underlying`` to the owning account's option P/L
    for that ticker, already windowed and measured comparably (see
    :func:`comparable_option_pl`). ``window`` is the disposition span the notes
    quote; it defaults to :func:`disposition_window` over ``closed_lots``.
    """
    fidelity = realized_by_underlying(closed_lots)

    rows: list[dict[str, Any]] = []
    for underlying in sorted(fidelity):
        agg = fidelity[underlying]
        # Compare option P/L to option P/L: an equity lot's realized would
        # otherwise pollute the difference (and this export has none today).
        fidelity_realized = agg["options"]
        engine_pl = engine_option_pl.get(underlying)
        if engine_pl is None:
            difference = None
            status = "review"
        else:
            difference = round(engine_pl - fidelity_realized, 2)
            status = "close" if abs(difference) <= _tolerance(fidelity_realized) else "review"
        rows.append(
            {
                "underlying": underlying,
                "wheel_engine_pl": round(engine_pl, 2) if engine_pl is not None else None,
                "fidelity_realized": fidelity_realized,
                "fidelity_equity": agg["equity"],
                "difference": difference,
                "lot_count": agg["lot_count"],
                "status": status,
            }
        )

    start, end = window or disposition_window(closed_lots)
    totals = realized_totals(closed_lots)
    totals["wheel_engine_pl"] = round(sum(r["wheel_engine_pl"] or 0.0 for r in rows), 2)
    totals["close"] = sum(1 for r in rows if r["status"] == "close")
    totals["review"] = sum(1 for r in rows if r["status"] == "review")
    # Quote the disposition span, not realized_totals' wider min/max over both
    # date columns -- that one reaches back to the earliest sell-to-open.
    if start and end:
        totals["coverage_start"] = start.isoformat()
        totals["coverage_end"] = end.isoformat()

    span = f"{start} → {end}" if start and end else "its coverage window"
    return {
        "rows": rows,
        "totals": totals,
        "lots": [_lot_payload(lot) for lot in closed_lots],
        "notes": (
            f"This cross-check reconciles Fidelity's closed-lots export, which records "
            f"option lots disposed of between {span}, against the wheel engine. Each "
            "ticker's engine figure is its option P/L over that same span, counting only "
            "closes the export would show: assignments are excluded (their premium moves "
            "to the assigned shares' cost basis) and still-open contracts do not count. A "
            "small gap near the end date is expected; a position that expires on the "
            "export date is realized here before the broker books it. 'close' means within "
            "tolerance, 'review' means worth a look."
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
