"""Fidelity Portfolio Positions parser.

A Positions export is a different shape from the transaction-history export
``wheel/parser.py`` handles: one row per current holding (or cash sweep) rather
than one row per historical fill, with no notion of time except the single
"as of" moment the file was downloaded.

The header row starts with ``Account number`` (transaction history starts with
``Run Date``), which is what :func:`looks_like_position_snapshot` sniffs on, so
the two formats never collide in directory discovery.

Quirks this module absorbs:

* A UTF-8 BOM, and a trailing disclaimer footer -- several quoted, multi-sentence
  "rows" plus a blank line -- after the real data. These are skipped by requiring
  the first field to look like a real account number, not by line position, so
  reordering or extra boilerplate never breaks parsing.
* Money and gain/loss cells are ``$``-prefixed, percent cells are ``%``-suffixed,
  and unknown figures print as ``--`` (e.g. a position Fidelity has no cost basis
  for). ``$``/``--``/blank handling reuses :func:`wheel.parser._num`; percent
  cells need their own stripping since ``_num`` was never asked to handle ``%``.
* A short option's Symbol field carries a leading space before the dash, e.g.
  ``" -CROX260821C150"`` -- one CSV field, so :mod:`csv` splits it correctly, but
  it must be stripped before :func:`wheel.parser.parse_occ_symbol` (which already
  strips the dash) sees it.
* The same symbol can appear on two rows with different ``Type`` values (Fidelity's
  Cash/Margin/Financing activity tag, not a separate brokerage account) -- these
  are two distinct positions/lots and must never be merged into one row.
* The authoritative as-of moment is the footer line ``"Date downloaded
  Aug-03-2026 5:45 p.m ET"``, not the filename -- the filename is only a fallback
  for a file whose footer is missing or unparseable.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Sequence

from wheel.fileio import find_line, peek_text
from wheel.parser import _num, parse_occ_symbol

POSITION_HEADER_KEY = "account number"
POSITION_DIRS = (".", "data")

CASH = "CASH"
EQUITY = "EQUITY"
OPTION = "OPTION"
UNKNOWN = "UNKNOWN"

_ASOF_RE = re.compile(
    r"Date downloaded\s+([A-Za-z]{3}-\d{2}-\d{4})\s+(\d{1,2}:\d{2})\s*([ap])\.?m\.?",
    re.IGNORECASE,
)
_FILENAME_DATE_RE = re.compile(r"([A-Za-z]{3}-\d{2}-\d{4})")


class PositionsFormatError(ValueError):
    """Raised when a file does not look like a Fidelity Positions export."""


# --------------------------------------------------------------------------
# Field-level helpers
# --------------------------------------------------------------------------


def _looks_like_account_number(value: str) -> bool:
    """Does ``value`` look like a real Fidelity account number, not the
    disclaimer paragraph or a blank trailing line?

    Fidelity account numbers aren't always pure digits -- IRAs, Joint
    accounts, Fidelity Go (robo-advisor), and custodial/"Youth" accounts
    commonly carry a one-letter prefix (e.g. ``Z05826863``, ``X43128422``).
    Checking *shape* (short, letters-and-digits only) rather than requiring
    digits catches all of those while still rejecting the multi-sentence
    disclaimer footer and blank lines, which are long and/or contain spaces
    or punctuation that no real account number does.
    """
    return bool(value) and value.isalnum() and len(value) <= 20


def _pct(value: object) -> float | None:
    """Parse a broker percent cell.  Blank and '--' become None, like ``_num``."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("%", "")
    if not text or text in {"--", "-", "n/a", "N/A"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Row model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionRow:
    """One holding (or cash line) on one account, as of one snapshot."""

    account_number: str
    account_name: str
    symbol_raw: str
    symbol: str
    description: str

    quantity: float | None
    last_price: float | None
    current_value: float | None
    today_gain_dollar: float | None
    today_gain_pct: float | None
    total_gain_dollar: float | None
    total_gain_pct: float | None
    percent_of_account: float | None
    cost_basis_total: float | None
    average_cost_basis: float | None
    account_type: str  # Fidelity's "Type" column: Cash/Margin/Financing -- a
    # sub-account activity tag, not a separate brokerage account.

    kind: str = UNKNOWN
    underlying: str | None = None
    right: str | None = None
    strike: float | None = None
    expiry: date | None = None
    occ_symbol: str | None = None
    source: str = ""


def _classify_row(
    symbol: str, quantity: float | None, last_price: float | None
) -> tuple[str, str | None, str | None, float | None, date | None, str | None]:
    """CASH / OPTION / EQUITY / UNKNOWN, plus OCC fields for options.

    A row with neither a quantity nor a price is a cash line (money-market
    sweep, "Pending activity") regardless of what its Symbol column says.
    Everything else is classified from the symbol itself, never dropped when it
    fails to parse -- an unrecognized symbol becomes UNKNOWN and is surfaced as
    a warning by the caller.
    """
    if quantity is None and last_price is None:
        return CASH, None, None, None, None, None

    occ = parse_occ_symbol(symbol) if symbol else None
    if occ:
        underlying, right, strike, expiry = occ
        occ_symbol = symbol.lstrip("-").upper()
        return OPTION, underlying, right, strike, expiry, occ_symbol

    if symbol:
        return EQUITY, symbol.upper(), None, None, None, None

    return UNKNOWN, None, None, None, None, None


# --------------------------------------------------------------------------
# Snapshot model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountSnapshot:
    """Every position on one account, as of one moment in time."""

    account_number: str
    account_name: str
    as_of: datetime
    as_of_source: str  # "footer" | "filename"
    rows: list[PositionRow] = field(default_factory=list)
    source: str = ""

    @property
    def cash_total(self) -> float:
        return sum(row.current_value or 0.0 for row in self.rows if row.kind == CASH)

    @property
    def equity_value(self) -> float:
        return sum(row.current_value or 0.0 for row in self.rows if row.kind == EQUITY)

    @property
    def option_value(self) -> float:
        return sum(row.current_value or 0.0 for row in self.rows if row.kind == OPTION)

    @property
    def total_value(self) -> float:
        return sum(row.current_value or 0.0 for row in self.rows)

    @property
    def cost_basis_known_total(self) -> float:
        return sum(row.cost_basis_total for row in self.rows if row.cost_basis_total is not None)

    @property
    def cost_basis_unknown_rows(self) -> int:
        return sum(
            1
            for row in self.rows
            if row.kind in (EQUITY, OPTION) and row.cost_basis_total is None
        )


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def looks_like_position_snapshot(path: str) -> bool:
    """Cheap header peek: does this CSV look like a Positions export?"""
    head = peek_text(path)
    if head is None:
        return False
    return any(line.lower().startswith(POSITION_HEADER_KEY) for line in head.splitlines())


def discover_position_snapshots(directories: Sequence[str] = POSITION_DIRS) -> list[str]:
    """Every Positions snapshot findable in ``directories``, non-recursive."""
    found: list[str] = []
    for directory in directories:
        if not os.path.isdir(directory):
            continue
        for entry in sorted(os.listdir(directory)):
            path = os.path.join(directory, entry)
            if entry.lower().endswith(".csv") and looks_like_position_snapshot(path):
                found.append(path)
    return found


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _read_lines(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        return handle.read().splitlines()


def _extract_as_of(lines: list[str], path: str) -> tuple[datetime, str]:
    """The footer 'Date downloaded ...' line wins; the filename is a fallback."""
    for line in lines:
        match = _ASOF_RE.search(line)
        if match:
            date_str, time_str, ampm = match.groups()
            try:
                dt = datetime.strptime(f"{date_str} {time_str} {ampm.upper()}M", "%b-%d-%Y %I:%M %p")
                return dt, "footer"
            except ValueError:
                continue

    match = _FILENAME_DATE_RE.search(os.path.basename(path))
    if match:
        try:
            return datetime.strptime(match.group(1), "%b-%d-%Y"), "filename"
        except ValueError:
            pass

    raise PositionsFormatError(
        f"could not determine an as-of date for {path!r} "
        "(no 'Date downloaded ...' footer and no date in the filename)"
    )


def parse_position_snapshot(path: str) -> tuple[list[AccountSnapshot], list[str]]:
    """One file -> one or more :class:`AccountSnapshot`, grouped by account.

    Returns ``(snapshots, warnings)``. Rows that fail to classify are kept as
    ``UNKNOWN`` and produce one warning each -- never silently dropped. The
    trailing blank line and the disclaimer/footer boilerplate are skipped by
    requiring ``Account number`` to look like a short alphanumeric token,
    which is robust to their exact position or wording.
    """
    lines = _read_lines(path)
    header_index = find_line(lines, lambda line: line.lower().split(",")[0].strip() == POSITION_HEADER_KEY)
    if header_index is None:
        raise PositionsFormatError(f"no 'Account number' header row found in {path!r}")

    as_of, as_of_source = _extract_as_of(lines, path)
    source = os.path.basename(path)

    reader = csv.DictReader(lines[header_index:])
    warnings: list[str] = []
    grouped: dict[str, list[PositionRow]] = {}
    account_names: dict[str, str] = {}

    for raw_row in reader:
        account_number = (raw_row.get("Account number") or "").strip()
        if not _looks_like_account_number(account_number):
            continue  # disclaimer/footer text or the trailing blank line

        account_name = (raw_row.get("Account name") or "").strip()
        symbol_raw = raw_row.get("Symbol") or ""
        symbol = symbol_raw.strip()
        description = (raw_row.get("Description") or "").strip()
        quantity = _num(raw_row.get("Quantity"))
        last_price = _num(raw_row.get("Last price"))

        kind, underlying, right, strike, expiry, occ_symbol = _classify_row(symbol, quantity, last_price)
        if kind == UNKNOWN:
            warnings.append(f"{source}: could not classify position {symbol_raw!r} ({description!r})")

        row = PositionRow(
            account_number=account_number,
            account_name=account_name,
            symbol_raw=symbol_raw,
            symbol=symbol,
            description=description,
            quantity=quantity,
            last_price=last_price,
            current_value=_num(raw_row.get("Current value")),
            today_gain_dollar=_num(raw_row.get("Today's gain/loss dollar")),
            today_gain_pct=_pct(raw_row.get("Today's gain/loss percent")),
            total_gain_dollar=_num(raw_row.get("Total gain/loss dollar")),
            total_gain_pct=_pct(raw_row.get("Total gain/loss percent")),
            percent_of_account=_pct(raw_row.get("Percent of account")),
            cost_basis_total=_num(raw_row.get("Cost basis total")),
            average_cost_basis=_num(raw_row.get("Average cost basis")),
            account_type=(raw_row.get("Type") or "").strip(),
            kind=kind,
            underlying=underlying,
            right=right,
            strike=strike,
            expiry=expiry,
            occ_symbol=occ_symbol,
            source=source,
        )
        grouped.setdefault(account_number, []).append(row)
        account_names.setdefault(account_number, account_name)

    if not grouped:
        warnings.append(f"{source}: no position rows with a numeric account number were found")

    snapshots = [
        AccountSnapshot(
            account_number=number,
            account_name=account_names[number],
            as_of=as_of,
            as_of_source=as_of_source,
            rows=rows,
            source=source,
        )
        for number, rows in grouped.items()
    ]
    return snapshots, warnings


# path -> (mtime, snapshots, warnings) at that mtime. A file is one account's
# (or a handful of linked accounts') Positions export, unlikely to change
# mid-session -- and the same "all accounts" file is routinely handed to
# load_snapshots() several times in one AccountRegistry.refresh() (once per
# configured folder that widens its search to it, plus once more to scan for
# unclaimed accounts -- see wheel/accounts.py), so caching by (path, mtime)
# avoids re-reading and re-parsing an unchanged multi-KB CSV several times
# over for work whose answer cannot have changed. Keyed by mtime, not
# invalidated any other way, so an edited file is transparently re-parsed the
# next time its own mtime moves -- the same fingerprint idiom the rest of this
# codebase (DashboardState._fingerprint, AccountRegistry._subfolder_fingerprint)
# already uses to decide when a rebuild is actually needed.
_parse_cache: dict[str, tuple[float, list[AccountSnapshot], list[str]]] = {}


def _cached_parse_position_snapshot(path: str) -> tuple[list[AccountSnapshot], list[str]]:
    mtime = os.path.getmtime(path)
    cached = _parse_cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1], cached[2]
    parsed, warnings = parse_position_snapshot(path)
    _parse_cache[path] = (mtime, parsed, warnings)
    return parsed, warnings


def load_snapshots(paths: Sequence[str]) -> tuple[list[AccountSnapshot], list[str]]:
    """Parse every file, then sort the combined list by (account, as_of).

    A duplicate ``(account_number, as_of)`` pair across two files -- the same
    snapshot re-downloaded or re-uploaded -- keeps the first occurrence and
    warns, rather than double counting a re-imported file.

    Each file is parsed in isolation: one malformed or unreadable Positions
    CSV produces a warning and is skipped, rather than aborting every other
    (unrelated) file in ``paths``. This matters because a caller can widen
    ``paths`` to every Positions file discovered across every account folder
    (see ``wheel.accounts.AccountRegistry.refresh``'s ``all_position_paths``)
    -- without per-file isolation, a single corrupted export for one account
    would silently take down every other account's Dashboard construction too.
    """
    snapshots: list[AccountSnapshot] = []
    warnings: list[str] = []
    seen: dict[tuple[str, datetime], str] = {}

    for path in paths:
        try:
            parsed, file_warnings = _cached_parse_position_snapshot(path)
        except (OSError, PositionsFormatError) as error:
            warnings.append(f"{os.path.basename(path)}: could not read this Positions file ({error}); skipped")
            continue
        warnings.extend(file_warnings)
        for snapshot in parsed:
            key = (snapshot.account_number, snapshot.as_of)
            if key in seen:
                warnings.append(
                    f"{snapshot.source}: duplicate snapshot for account {snapshot.account_number} "
                    f"at {snapshot.as_of.isoformat()} (already loaded from {seen[key]}); skipped"
                )
                continue
            seen[key] = snapshot.source
            snapshots.append(snapshot)

    snapshots.sort(key=lambda snapshot: (snapshot.account_number, snapshot.as_of))
    return snapshots, warnings


def latest_snapshot_per_account(snapshots: Sequence[AccountSnapshot]) -> dict[str, AccountSnapshot]:
    """The most recent snapshot for each account number present."""
    latest: dict[str, AccountSnapshot] = {}
    for snapshot in snapshots:
        current = latest.get(snapshot.account_number)
        if current is None or snapshot.as_of > current.as_of:
            latest[snapshot.account_number] = snapshot
    return latest


def latest_snapshot(snapshots: Sequence[AccountSnapshot]) -> AccountSnapshot | None:
    """The single most-recently-seen snapshot across every account in
    ``snapshots``, or ``None`` if ``snapshots`` is empty.

    The ``max(latest_snapshot_per_account(x).values(), key=lambda s:
    s.as_of)`` idiom this wraps was independently re-derived at several call
    sites in :mod:`wheel.accounts` and :mod:`wheel.api` -- centralized here so
    the "most recent" tie-break rule only needs to change in one place.
    """
    per_account = latest_snapshot_per_account(snapshots)
    return max(per_account.values(), key=lambda snapshot: snapshot.as_of) if per_account else None
