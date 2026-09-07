"""Fidelity "Closed Positions / Realized Gain & Loss" export parser.

One row per closed lot: option or equity, with the acquire/dispose dates,
quantity, cost basis, proceeds, and the short-/long-term gain split Fidelity
already worked out. We ingest it to show the broker's own realized figures and to
cross-check them against the wheel engine (see ``wheel/taxes.py``).

Shape (header row, ``$``-prefixed money, thousands-comma inside quotes,
``--`` for the term that doesn't apply, a trailing empty field):

    Symbol(CUSIP),Security description,Date acquired,Date sold,Quantity,Cost basis,Proceeds,Short-term gain/loss,Long-term gain/loss
    AMZN260102P225(8265299PN),PUT (AMZN) AMAZON.COM INC JAN 02 26 $225 (100 SHS),2026-01-02,2025-12-26,3,$6.07,$106.01, --,$99.94,

For a short option the "acquired" date is the buy-to-close and "sold" is the
sell-to-open, so ``date_sold`` can precede ``date_acquired`` -- that is expected,
not an error.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass
from datetime import date
from typing import Sequence

from wheel.fileio import peek_text
from wheel.parser import _num, _parse_date, parse_occ_symbol

CLOSED_LOTS_HEADER_KEY = "symbol(cusip)"
CLOSED_LOTS_DIRS = (".", "data")

_FILENAME_DATE_RE = re.compile(r"([A-Za-z]{3}-\d{2}-\d{4})")
_DESC_TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,9})\)")


class ClosedLotsFormatError(ValueError):
    """Raised when a file does not look like a Closed-Lots export."""


@dataclass(frozen=True)
class ClosedLot:
    symbol: str
    cusip: str | None
    description: str
    underlying: str | None
    is_option: bool
    right: str | None
    strike: float | None
    expiry: date | None
    date_acquired: date | None
    date_sold: date | None
    quantity: float | None
    cost_basis: float | None
    proceeds: float | None
    st_gain: float | None
    lt_gain: float | None
    realized: float | None
    term: str | None  # "SHORT" | "LONG" | "MIXED" | None
    source_file: str


@dataclass(frozen=True)
class ClosedLotsReport:
    lots: list[ClosedLot]
    warnings: list[str]
    as_of: date | None


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def looks_like_closed_lots(path: str) -> bool:
    head = peek_text(path)
    if head is None:
        return False
    return any(line.lower().replace(" ", "").startswith(CLOSED_LOTS_HEADER_KEY) for line in head.splitlines())


def discover_closed_lots(directories: Sequence[str] = CLOSED_LOTS_DIRS) -> list[str]:
    found: list[str] = []
    for directory in directories:
        if not os.path.isdir(directory):
            continue
        for entry in sorted(os.listdir(directory)):
            path = os.path.join(directory, entry)
            if entry.lower().endswith(".csv") and looks_like_closed_lots(path):
                found.append(path)
    return found


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _split_symbol(raw: str) -> tuple[str, str | None]:
    raw = (raw or "").strip().lstrip("-")
    if "(" in raw:
        symbol, _, cusip = raw.partition("(")
        return symbol.strip(), cusip.strip().rstrip(")") or None
    return raw, None


def _underlying_from_description(description: str) -> str | None:
    match = _DESC_TICKER_RE.search(description or "")
    return match.group(1) if match else None


def _as_of_from_filename(path: str) -> date | None:
    match = _FILENAME_DATE_RE.search(os.path.basename(path))
    if not match:
        return None
    try:
        from datetime import datetime

        return datetime.strptime(match.group(1), "%b-%d-%Y").date()
    except ValueError:
        return None


def parse_closed_lots(path: str) -> ClosedLotsReport:
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))

    header_idx = next(
        (i for i, row in enumerate(rows) if row and row[0].lower().replace(" ", "").startswith(CLOSED_LOTS_HEADER_KEY)),
        None,
    )
    if header_idx is None:
        raise ClosedLotsFormatError(f"{os.path.basename(path)}: no Closed-Lots header row found")

    lots: list[ClosedLot] = []
    warnings: list[str] = []
    base = os.path.basename(path)
    for row in rows[header_idx + 1 :]:
        if not row or not row[0].strip():
            continue
        # A real lot row has an "Date acquired" in column 3; a trailing
        # disclaimer paragraph (one long quoted cell, or a stray sentence) does
        # not, so date-parseability is the reliable "is this a data row" test.
        if len(row) < 7 or _parse_date(row[2]) is None:
            continue
        symbol, cusip = _split_symbol(row[0])
        description = row[1].strip() if len(row) > 1 else ""
        occ = parse_occ_symbol(symbol)
        is_option = occ is not None
        underlying = occ[0] if occ else (_underlying_from_description(description) or symbol or None)
        st_gain = _num(row[7]) if len(row) > 7 else None
        lt_gain = _num(row[8]) if len(row) > 8 else None
        cost_basis = _num(row[5]) if len(row) > 5 else None
        proceeds = _num(row[6]) if len(row) > 6 else None
        if st_gain is not None or lt_gain is not None:
            realized = round((st_gain or 0.0) + (lt_gain or 0.0), 2)
        elif cost_basis is not None and proceeds is not None:
            realized = round(proceeds - cost_basis, 2)
        else:
            realized = None
        term = (
            "MIXED"
            if st_gain is not None and lt_gain is not None
            else "SHORT"
            if st_gain is not None
            else "LONG"
            if lt_gain is not None
            else None
        )
        lots.append(
            ClosedLot(
                symbol=symbol,
                cusip=cusip,
                description=description,
                underlying=underlying,
                is_option=is_option,
                right=occ[1] if occ else None,
                strike=occ[2] if occ else None,
                expiry=occ[3] if occ else None,
                date_acquired=_parse_date(row[2]) if len(row) > 2 else None,
                date_sold=_parse_date(row[3]) if len(row) > 3 else None,
                quantity=_num(row[4]) if len(row) > 4 else None,
                cost_basis=cost_basis,
                proceeds=proceeds,
                st_gain=st_gain,
                lt_gain=lt_gain,
                realized=realized,
                term=term,
                source_file=base,
            )
        )

    return ClosedLotsReport(lots=lots, warnings=warnings, as_of=_as_of_from_filename(path))


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def realized_by_underlying(lots: Sequence[ClosedLot]) -> dict[str, dict[str, float]]:
    """``{underlying: {"realized", "options", "equity", "lot_count"}}``."""
    out: dict[str, dict[str, float]] = {}
    for lot in lots:
        key = lot.underlying or lot.symbol
        agg = out.setdefault(key, {"realized": 0.0, "options": 0.0, "equity": 0.0, "lot_count": 0})
        value = lot.realized or 0.0
        agg["realized"] += value
        agg["options" if lot.is_option else "equity"] += value
        agg["lot_count"] += 1
    return {k: {kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()} for k, v in out.items()}


def realized_totals(lots: Sequence[ClosedLot]) -> dict[str, object]:
    st = round(sum(lot.st_gain or 0.0 for lot in lots), 2)
    lt = round(sum(lot.lt_gain or 0.0 for lot in lots), 2)
    options = round(sum((lot.realized or 0.0) for lot in lots if lot.is_option), 2)
    equity = round(sum((lot.realized or 0.0) for lot in lots if not lot.is_option), 2)
    acquired = [lot.date_acquired for lot in lots if lot.date_acquired]
    sold = [lot.date_sold for lot in lots if lot.date_sold]
    return {
        "st": st,
        "lt": lt,
        "total": round(st + lt, 2),
        "options_total": options,
        "equity_total": equity,
        "proceeds": round(sum(lot.proceeds or 0.0 for lot in lots), 2),
        "cost_basis": round(sum(lot.cost_basis or 0.0 for lot in lots), 2),
        "lot_count": len(lots),
        "coverage_start": min(acquired + sold).isoformat() if (acquired or sold) else None,
        "coverage_end": max(acquired + sold).isoformat() if (acquired or sold) else None,
    }
