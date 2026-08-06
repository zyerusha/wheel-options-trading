"""Fidelity transaction-history parser.

The Fidelity "History_for_Account_*.csv" export has four quirks this module
absorbs so that nothing downstream has to think about them:

1. A UTF-8 BOM and one or more blank lines before the real header row.
2. **Two column-naming dialects**, seen across different accounts (so far,
   retirement accounts export the first and non-retirement accounts the
   second -- but this is detected per file, not assumed from account type).
   The classic dialect suffixes every dollar column with " ($)"
   (``Price ($)``, ``Commission ($)``, ``Fees ($)``, ``Amount ($)``). A newer
   dialect drops the suffix (``Price``, ``Commission``, ``Fees``, ``Amount``)
   and adds a few FX columns this parser has no use for (``Exchange
   Quantity``, ``Exchange Currency``, ``Exchange Rate``). :func:`_resolve_columns`
   picks the right column names from whichever header the file actually has,
   once per file -- every lookup downstream goes through that map rather than
   a literal column name, so both dialects reach the same parsing logic.
3. The ``Quantity`` and price columns are **swapped** relative to their labels
   in some exports: ``Quantity`` holds the per-share price and the price
   column holds the signed contract count. Rather than hard-coding that,
   :func:`parse_fidelity_csv` tests both interpretations against the
   authoritative ``Amount`` column and picks whichever one reconciles -- so a
   correctly-labelled export still works too.
4. ``ASSIGNED`` / ``EXPIRED`` rows are booked on the *following* business day but
   carry the real event date inline as ``as of Nov-20-2025``.  The parser exposes
   that as :attr:`Transaction.event_date`.

Every monetary figure that reaches the engine comes from the file's own
``Amount`` column, which is already net of commission and fees.  Derived
prices are never used to re-compute cash.
"""

from __future__ import annotations

import csv
import os
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Iterable, Iterator, Sequence

# --------------------------------------------------------------------------
# Action vocabulary
# --------------------------------------------------------------------------

STO = "STO"  # sell to open   -- short a new option (CSP or covered call)
BTC = "BTC"  # buy to close   -- close a short option
BTO = "BTO"  # buy to open    -- long option
STC = "STC"  # sell to close  -- close a long option
ASSIGNED = "ASSIGNED"
EXPIRED = "EXPIRED"
BUY_STOCK = "BUY_STOCK"
SELL_STOCK = "SELL_STOCK"
OTHER = "OTHER"

OPTION_ACTIONS = frozenset({STO, BTC, BTO, STC, ASSIGNED, EXPIRED})
# Everything the engine acts on.  Dividends, collateral marks and other ledger
# noise fall outside this and must not, for example, invent a ticker filter.
TRADE_ACTIONS = OPTION_ACTIONS | frozenset({BUY_STOCK, SELL_STOCK})
OPENING_ACTIONS = frozenset({STO, BTO})
CLOSING_ACTIONS = frozenset({BTC, STC, ASSIGNED, EXPIRED})

OPTION_MULTIPLIER = 100

# Ordered most-specific first.  Two rules must precede the bare ASSIGNED rule:
# "YOU BOUGHT ASSIGNED PUTS" and "YOU SOLD ASSIGNED CALLS" are the *equity* legs
# that settle an assignment, not the option event itself.  Matching them as
# ASSIGNED would drop the share movement and its cash on the floor.
_ACTION_PATTERNS: Sequence[tuple[str, str]] = (
    (r"SOLD\s+OPENING", STO),
    (r"BOUGHT\s+CLOSING", BTC),
    (r"BOUGHT\s+OPENING", BTO),
    (r"SOLD\s+CLOSING", STC),
    (r"YOU\s+BOUGHT\s+(ASSIGNED|EXERCISED)\s+(PUTS?|CALLS?)", BUY_STOCK),
    (r"YOU\s+SOLD\s+(ASSIGNED|EXERCISED)\s+(PUTS?|CALLS?)", SELL_STOCK),
    (r"\bASSIGNED\b", ASSIGNED),
    (r"\bEXPIRED\b", EXPIRED),
    (r"\bEXERCISED\b", ASSIGNED),
    (r"YOU\s+BOUGHT", BUY_STOCK),
    (r"YOU\s+SOLD", SELL_STOCK),
)

# "YOU BOUGHT ASSIGNED PUTS AS OF 02-20-26 ..." -- the share leg of an assignment.
_SETTLEMENT_RE = re.compile(r"YOU\s+(BOUGHT|SOLD)\s+(ASSIGNED|EXERCISED)\s+(PUTS?|CALLS?)", re.IGNORECASE)

# -MU251003P157.5 / TQQQ251128C51.75 / -BRK.B260116P400
_OCC_RE = re.compile(r"^-?([A-Z][A-Z0-9./]*?)(\d{2})(\d{2})(\d{2})([CP])(\d+(?:\.\d+)?)$")

# Fidelity writes the same phrase three ways across exports: "as of Nov-20-2025"
# on option events, "AS OF 02-20-26" on equity settlement legs, and ISO
# "as of 2025-09-17" in newer files.  All three have to be understood, or the
# event lands on its ledger date instead of the day it actually happened.
_AS_OF_RE = re.compile(r"as of\s+([A-Za-z]{3}-\d{2}-\d{4})", re.IGNORECASE)
_AS_OF_ISO_RE = re.compile(r"as of\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
_AS_OF_NUMERIC_RE = re.compile(r"as of\s+(\d{2}-\d{2}-\d{2,4})", re.IGNORECASE)
# Any of the above, for stripping the volatile phrase out of a comparison key.
_AS_OF_ANY_RE = re.compile(r"as of\s+[\w-]+", re.IGNORECASE)
_DESC_EXPIRY_RE = re.compile(r"\b([A-Z]{3})\s+(\d{2})\s+(\d{2})\b")
_DESC_STRIKE_RE = re.compile(r"\$(\d+(?:\.\d+)?)")

_HEADER_KEY = "run date"

# Two dialects of the same four dollar/quantity columns -- see the module
# docstring. "Quantity" itself is spelled identically in both and so isn't
# part of either map. "Accrued Interest" and "Cash Balance" also differ the
# same way between dialects but are never read by this parser, so they're
# left out rather than tracked for no reason.
_LEGACY_COLUMNS = {
    "price": "Price ($)",
    "commission": "Commission ($)",
    "fees": "Fees ($)",
    "amount": "Amount ($)",
}
_MODERN_COLUMNS = {
    "price": "Price",
    "commission": "Commission",
    "fees": "Fees",
    "amount": "Amount",
}


def _resolve_columns(fieldnames: Sequence[str] | None) -> dict[str, str]:
    """Which of Fidelity's two dollar-column dialects this export uses.

    Detected from the header actually present in *this* file, never guessed
    from account type or content -- a legacy file whose ``Price ($)`` column
    happens to be empty on every row must still be read as legacy, not
    mistaken for the modern dialect just because nothing reconciled.
    """
    return _LEGACY_COLUMNS if "Price ($)" in (fieldnames or ()) else _MODERN_COLUMNS


class FidelityFormatError(ValueError):
    """Raised when the file does not look like a Fidelity history export."""


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Transaction:
    """One normalized row of broker history."""

    row_id: int
    run_date: date
    settlement_date: date | None
    action: str
    action_raw: str
    description: str

    underlying: str
    occ_symbol: str | None  # canonical, no leading dash
    right: str | None  # "C" | "P"
    strike: float | None
    expiry: date | None

    contracts: float  # signed: negative = sold, positive = bought
    price: float | None  # per share (per contract / 100)
    commission: float
    fees: float
    amount: float  # authoritative cash flow, already net of comm + fees

    account_type: str  # "Cash" | "Margin" | ""
    as_of_date: date | None
    source: str = ""  # basename of the export this row came from
    # True for the equity leg that settles an assignment ("YOU BOUGHT ASSIGNED
    # PUTS ..."). When present the engine uses it instead of synthesizing shares.
    assignment_settlement: bool = False

    @property
    def event_date(self) -> date:
        """Date the economic event actually happened.

        Assignments and expirations post to the ledger on the next business day
        but state their true date inline, so prefer that when present.
        """
        return self.as_of_date or self.run_date

    @property
    def is_option(self) -> bool:
        return self.occ_symbol is not None

    @property
    def abs_contracts(self) -> float:
        return abs(self.contracts)

    @property
    def total_fees(self) -> float:
        return self.commission + self.fees

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.event_date} {self.action:8s} {self.occ_symbol or self.underlying} x{self.contracts:g}"


@dataclass
class ParseReport:
    """Diagnostics from a parse run -- surfaced in the UI, not swallowed."""

    total_rows: int = 0
    parsed: int = 0
    skipped: int = 0
    columns_swapped: bool = False
    reconciled: int = 0
    reconcile_failures: list[dict] = field(default_factory=list)
    unparsed_symbols: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def reconcile_rate(self) -> float:
        checkable = self.reconciled + len(self.reconcile_failures)
        return self.reconciled / checkable if checkable else 1.0


# --------------------------------------------------------------------------
# Field-level helpers
# --------------------------------------------------------------------------


def _num(value: object) -> float | None:
    """Parse a broker numeric cell.  Blank, '--' and 'Processing' become None."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("$", "")
    if not text or text in {"--", "-", "n/a", "N/A"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        result = float(text)
    except ValueError:
        return None
    return -result if negative else result


def _parse_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    # Fidelity sometimes appends " as of ..." inside a date cell.
    text = text.split(" as of ")[0].strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%b-%d-%Y", "%b %d %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def classify_action(action_raw: str) -> str:
    """Map Fidelity's free-text action to a lifecycle verb."""
    text = (action_raw or "").upper()
    for pattern, verb in _ACTION_PATTERNS:
        if re.search(pattern, text):
            return verb
    return OTHER


def parse_occ_symbol(symbol: str) -> tuple[str, str, float, date] | None:
    """Split an OCC-style symbol into (underlying, right, strike, expiry)."""
    text = (symbol or "").strip().lstrip("-").upper()
    match = _OCC_RE.match(text)
    if not match:
        return None
    underlying, yy, mm, dd, right, strike = match.groups()
    try:
        expiry = date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        return None
    return underlying, right, float(strike), expiry


def _expiry_from_description(description: str) -> date | None:
    """Fallback expiry extraction from e.g. 'MICRON TECHNOLOGY OCT 03 25 $157.5'."""
    match = _DESC_EXPIRY_RE.search((description or "").upper())
    if not match:
        return None
    month, day, year = match.groups()
    try:
        return datetime.strptime(f"{month} {day} {year}", "%b %d %y").date()
    except ValueError:
        return None


def _as_of(text: str) -> date | None:
    """Pull the real event date out of an 'as of ...' phrase, either format."""
    match = _AS_OF_RE.search(text or "")
    if match:
        try:
            return datetime.strptime(match.group(1).title(), "%b-%d-%Y").date()
        except ValueError:
            return None

    match = _AS_OF_ISO_RE.search(text or "")
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            return None

    match = _AS_OF_NUMERIC_RE.search(text or "")
    if match:
        raw = match.group(1)
        for fmt in ("%m-%d-%y", "%m-%d-%Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
    return None


# --------------------------------------------------------------------------
# Column-order detection
# --------------------------------------------------------------------------


def _expected_amount(contracts: float, price: float, commission: float, fees: float, multiplier: int) -> float:
    """Cash effect of a fill: selling (negative contracts) credits the account."""
    return -contracts * price * multiplier - commission - fees


def _orientation_score(contracts: float, price: float, amount: float, is_sale: bool | None) -> int:
    """How plausible is it that these two cells are (contracts, price)?

    ``Amount = -contracts x price x multiplier - fees`` is symmetric in contracts
    and price, so it cannot tell the two apart -- it validates magnitude only.
    These three asymmetric facts can:

    * an option contract count is a whole number, a premium usually is not;
    * a quoted price is strictly positive, a signed quantity is not;
    * on a sale the quantity is negative and the cash is positive.
    """
    score = 0
    if abs(contracts - round(contracts)) < 1e-9:
        score += 1
    if price > 0:
        score += 1
    if is_sale is not None:
        expected_negative = is_sale
        if (contracts < 0) == expected_negative and (amount > 0) == expected_negative:
            score += 1
    return score


def _detect_swapped_columns(rows: Sequence[dict], columns: dict[str, str]) -> tuple[bool, str]:
    """Decide whether ``Quantity`` and the price column are transposed.

    Returns ``(swapped, explanation)``.  Only rows that carry both numbers and a
    resolvable option symbol are scored, since equity rows admit fractional
    share counts and would muddy the whole-number test.
    """
    price_col = columns["price"]
    amount_col = columns["amount"]
    straight = swapped = 0
    for row in rows:
        if not parse_occ_symbol(row.get("Symbol", "")):
            continue
        col_qty = _num(row.get("Quantity"))
        col_price = _num(row.get(price_col))
        amount = _num(row.get(amount_col))
        if col_qty is None or col_price is None or amount is None or amount == 0:
            continue

        action = classify_action(row.get("Action", ""))
        is_sale = action in {STO, STC, SELL_STOCK} if action != OTHER else None

        straight += _orientation_score(col_qty, col_price, amount, is_sale)
        swapped += _orientation_score(col_price, col_qty, amount, is_sale)

    if swapped > straight:
        return True, (
            f"'Quantity' and '{price_col}' are transposed in this export "
            f"(score {swapped} vs {straight}); reading them in swapped order"
        )
    if straight > swapped:
        return False, f"column labels verified correct (score {straight} vs {swapped})"
    return False, "column orientation indeterminate; assuming labels are correct"


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def _read_rows(path: str) -> tuple[list[dict], dict[str, str]]:
    """Read the CSV, tolerating the BOM, leading blanks and trailing junk.

    Also resolves which dollar-column dialect this file uses (see
    :func:`_resolve_columns`) from the header actually present, once, so
    every row downstream is read with the same column names.
    """
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        lines = handle.read().splitlines()

    header_index = next(
        (i for i, line in enumerate(lines) if _HEADER_KEY in line.lower().split(",")[0]),
        None,
    )
    if header_index is None:
        raise FidelityFormatError(f"no 'Run Date' header row found in {path!r}")

    reader = csv.DictReader(lines[header_index:])
    columns = _resolve_columns(reader.fieldnames)
    rows: list[dict] = []
    for row in reader:
        # Disclaimer text at the foot of the file parses as a row with no date.
        if _parse_date(row.get("Run Date")) is None:
            continue
        rows.append(row)
    return rows, columns


def parse_fidelity_csv(path: str, report: ParseReport | None = None) -> tuple[list[Transaction], ParseReport]:
    """Parse a Fidelity history export into normalized transactions.

    Returns the transactions in chronological order plus a :class:`ParseReport`
    describing what was skipped and how many rows reconciled against the
    broker's own ``Amount`` column.
    """
    report = report or ParseReport()
    rows, columns = _read_rows(path)
    report.total_rows = len(rows)

    swapped, reason = _detect_swapped_columns(rows, columns)
    report.columns_swapped = swapped
    report.warnings.append(reason)

    qty_col, price_col = (columns["price"], "Quantity") if swapped else ("Quantity", columns["price"])

    transactions: list[Transaction] = []
    for index, row in enumerate(rows):
        run_date = _parse_date(row.get("Run Date"))
        if run_date is None:  # pragma: no cover - filtered in _read_rows
            report.skipped += 1
            continue

        action_raw = (row.get("Action") or "").strip()
        action = classify_action(action_raw)
        raw_symbol = (row.get("Symbol") or "").strip()
        description = (row.get("Description") or "").strip()

        occ = parse_occ_symbol(raw_symbol)
        if occ:
            underlying, right, strike, expiry = occ
            occ_symbol = raw_symbol.lstrip("-").upper()
        else:
            underlying = raw_symbol.lstrip("-").upper()
            occ_symbol = right = strike = None
            expiry = None
            if raw_symbol and action in {STO, BTC, BTO, STC} and "CALL" in action_raw.upper() + description.upper():
                report.unparsed_symbols.append(raw_symbol)

        contracts = _num(row.get(qty_col))
        price = _num(row.get(price_col))
        commission = _num(row.get(columns["commission"])) or 0.0
        fees = _num(row.get(columns["fees"])) or 0.0
        amount = _num(row.get(columns["amount"])) or 0.0

        if action in {ASSIGNED, EXPIRED}:
            # These rows carry a bare, unsigned contract count in whichever of the
            # two numeric cells is populated, and never a price.  Reading them
            # independently of the detected orientation keeps them correct even in
            # an export that holds too few trades to orient from.  The engine
            # resolves the sign from the open position.
            count = contracts if contracts is not None else price
            contracts = abs(count) if count is not None else 0.0
            price = None
        elif contracts is None:
            contracts = 0.0

        if expiry is None and occ_symbol is None:
            expiry = _expiry_from_description(description)

        transaction = Transaction(
            row_id=index,
            run_date=run_date,
            settlement_date=_parse_date(row.get("Settlement Date")),
            action=action,
            action_raw=action_raw,
            description=description,
            underlying=underlying,
            occ_symbol=occ_symbol,
            right=right,
            strike=strike,
            expiry=expiry,
            contracts=contracts,
            price=price,
            commission=commission,
            fees=fees,
            amount=amount,
            account_type=(row.get("Type") or "").strip(),
            as_of_date=_as_of(action_raw) or _as_of(description),
            assignment_settlement=bool(_SETTLEMENT_RE.search(action_raw)),
            source=os.path.basename(path),
        )
        transactions.append(transaction)
        report.parsed += 1

    # Normalize before reconciling so the row ids in the failure report refer to
    # the same rows the caller will see.
    transactions = _normalize_direction(transactions, report)
    transactions.sort(key=lambda t: (t.event_date, t.row_id))
    for transaction in transactions:
        _reconcile(transaction, report)
    return transactions, report


def _normalize_direction(transactions: list[Transaction], report: ParseReport) -> list[Transaction]:
    """Make ``row_id`` follow the broker's own sequence, oldest first.

    Fidelity writes some exports oldest-first and others newest-first.  Because
    ``row_id`` is the tie-breaker for events on the same day, a newest-first file
    would otherwise process each day's fills backwards -- and two exports of the
    same account could disagree with each other.  Reversing the index on a
    descending file makes the ordering identical either way.
    """
    if len(transactions) < 2:
        return transactions

    dates = [t.run_date for t in transactions]
    forward = sum(1 for a, b in zip(dates, dates[1:]) if a < b)
    backward = sum(1 for a, b in zip(dates, dates[1:]) if a > b)
    if backward <= forward:
        return transactions

    report.warnings.append("export is newest-first; row order reversed to broker sequence")
    last = len(transactions) - 1
    return [replace(t, row_id=last - t.row_id) for t in transactions]


def _reconcile(transaction: Transaction, report: ParseReport) -> None:
    """Check price x quantity x multiplier - fees against the broker's Amount.

    Option premiums are quoted exactly, so a cent of tolerance is right there.
    Equity rows are not: Fidelity prints a *rounded* average fill price beside an
    exact total, so a 6,000-share fill at a true 3.1265 shows as 3.13 and misses
    by $21.  Allowing half a cent per share absorbs that display rounding without
    hiding a genuine error, which would be off by orders of magnitude more.
    """
    if transaction.action == OTHER:
        # Dividends, collateral marks and other non-trade rows put unrelated
        # figures in the quantity and price cells; there is no trade to check.
        return
    if transaction.price is None or transaction.contracts == 0:
        return  # assignment / expiration rows have no price to check
    is_option = transaction.is_option
    multiplier = OPTION_MULTIPLIER if is_option else 1
    expected = _expected_amount(
        transaction.contracts, transaction.price, transaction.commission, transaction.fees, multiplier
    )
    delta = expected - transaction.amount
    tolerance = 0.01 if is_option else 0.011 + abs(transaction.contracts) * 0.005
    if abs(delta) <= tolerance:
        report.reconciled += 1
    else:
        report.reconcile_failures.append(
            {
                "row_id": transaction.row_id,
                "date": transaction.run_date.isoformat(),
                "symbol": transaction.occ_symbol or transaction.underlying,
                "expected": round(expected, 2),
                "actual": round(transaction.amount, 2),
                "delta": round(delta, 2),
            }
        )


# --------------------------------------------------------------------------
# Combining several exports
# --------------------------------------------------------------------------


def _squash(text: str) -> str:
    """Normalize action text for comparison across exports.

    Two things vary between files for the same trade and must not defeat a match:
    the spacing (``FINL INC NOV`` vs ``FINL INCNOV``) and the date format inside
    the 'as of' phrase (``Sep-17-2025`` vs ``2025-09-17``).  The phrase is
    replaced wholesale because the parsed ``as_of_date`` already carries it.
    """
    return re.sub(r"\s+", "", _AS_OF_ANY_RE.sub("ASOF", text or "").upper())


@dataclass
class MergeReport:
    """What combining a set of exports actually did."""

    sources: list[dict] = field(default_factory=list)
    rows_parsed: int = 0
    rows_kept: int = 0
    duplicates_removed: int = 0
    first_date: date | None = None
    last_date: date | None = None

    @property
    def combined(self) -> bool:
        return len(self.sources) > 1


def dedup_key(transaction: Transaction) -> tuple:
    """Identity of a trade for the purpose of spotting it in two exports.

    Every economic field the broker prints, plus the action text with **all
    whitespace stripped**.  The spacing is not stable between exports -- the same
    call shows as ``BRIGHTHOUSE FINL INC NOV 21 25`` in one file and
    ``BRIGHTHOUSE FINL INCNOV 21 25`` in another -- so comparing it verbatim would
    treat identical trades as distinct.  ``row_id`` and ``source`` are excluded
    because they differ by construction.
    """
    return (
        transaction.run_date,
        transaction.settlement_date,
        transaction.action,
        _squash(transaction.action_raw),
        transaction.occ_symbol or transaction.underlying,
        transaction.contracts,
        transaction.price,
        transaction.commission,
        transaction.fees,
        transaction.amount,
        transaction.as_of_date,
    )


def merge_transactions(sources: Sequence[tuple[str, list[Transaction]]]) -> tuple[list[Transaction], MergeReport]:
    """Combine exports into one timeline, counting each real trade once.

    Overlapping exports repeat the trades in the shared window, but a single
    export can also legitimately contain the same fill twice -- selling one
    contract twice at the same price on the same day is one order book entry per
    fill, not a duplicate.  So the merged count for a trade is the **maximum**
    seen in any one file, never the sum: the file that saw a fill three times
    contributes three, and a second export showing the same three adds nothing.

    Taking all copies of a given trade from a single chosen file also keeps their
    relative order intact, which the intra-day sequencing depends on.
    """
    report = MergeReport()
    counted: list[Counter] = []

    for path, transactions in sources:
        keys = Counter(dedup_key(t) for t in transactions)
        counted.append(keys)
        report.rows_parsed += len(transactions)
        report.sources.append(
            {
                "path": path,
                "name": os.path.basename(path),
                "rows": len(transactions),
                "first_date": min((t.event_date for t in transactions), default=None),
                "last_date": max((t.event_date for t in transactions), default=None),
                "kept": 0,
                "duplicates": 0,
            }
        )

    # For each distinct trade, the file that saw it the most times wins outright.
    owner: dict[tuple, int] = {}
    for index, keys in enumerate(counted):
        for key, count in keys.items():
            if key not in owner or count > counted[owner[key]][key]:
                owner[key] = index

    merged: list[Transaction] = []
    for index, (path, transactions) in enumerate(sources):
        for transaction in transactions:
            if owner[dedup_key(transaction)] == index:
                merged.append(replace(transaction, row_id=(index, transaction.row_id)))
                report.sources[index]["kept"] += 1
            else:
                report.sources[index]["duplicates"] += 1

    merged.sort(key=lambda t: (t.event_date, t.row_id))
    merged = [replace(t, row_id=position) for position, t in enumerate(merged)]

    report.rows_kept = len(merged)
    report.duplicates_removed = report.rows_parsed - report.rows_kept
    report.first_date = min((t.event_date for t in merged), default=None)
    report.last_date = max((t.event_date for t in merged), default=None)
    return merged, report


def parse_exports(paths: Sequence[str]) -> tuple[list[Transaction], list[ParseReport], MergeReport]:
    """Parse and combine one or more Fidelity exports into a single timeline."""
    if not paths:
        raise ValueError("no exports given")

    sources: list[tuple[str, list[Transaction]]] = []
    reports: list[ParseReport] = []
    for path in paths:
        transactions, report = parse_fidelity_csv(path)
        sources.append((path, transactions))
        reports.append(report)

    merged, merge_report = merge_transactions(sources)
    return merged, reports, merge_report


def iter_by_underlying(transactions: Iterable[Transaction]) -> Iterator[tuple[str, list[Transaction]]]:
    """Group chronologically sorted transactions by underlying ticker."""
    grouped: dict[str, list[Transaction]] = {}
    for transaction in transactions:
        grouped.setdefault(transaction.underlying, []).append(transaction)
    for underlying in sorted(grouped):
        yield underlying, grouped[underlying]
