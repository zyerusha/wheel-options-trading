"""Wheel lifecycle engine.

Consumes normalized :class:`~wheel.parser.Transaction` rows and reconstructs the
strategy: option lots with FIFO closes, roll groups, share lots created by
assignment, and the *cycles* that tie them together.

Cycle model
-----------
A **cycle** is one campaign in a single underlying.  It opens on the first
position taken in that ticker and closes only when the ticker is completely flat
-- no open contracts and no shares.  Rolls, scaled entries, assignment and the
covered calls that follow it therefore all land inside one cycle without any
fragile contract-to-contract chaining, which matters because real rolls do not
preserve contract counts (e.g. closing 4 and opening 2).

Cash flows
----------
Every option cash figure is the broker's own ``Amount ($)``, already net of
commission and fees, allocated pro-rata across contracts when a fill closes more
than one lot.  Share legs are the one exception: this export contains no equity
rows, so assignment share movements are **synthesized at the strike** and marked
``synthetic=True`` wherever they surface.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Sequence

from wheel.parser import (
    ASSIGNED,
    BTC,
    BTO,
    BUY_STOCK,
    EXPIRED,
    OPTION_MULTIPLIER,
    SELL_STOCK,
    STC,
    STO,
    Transaction,
)

__all__ = [
    "WheelEngine",
    "Cycle",
    "OptionLeg",
    "LegClose",
    "ShareLot",
    "Roll",
    "Spread",
    "Assignment",
    "build_cycles",
]

SHORT = "SHORT"
LONG = "LONG"

# Leg strategy labels. Short options are always covered -- a short put is cash
# secured, a short call is backed by stock. There is deliberately no naked
# strategy: when the backing stock was bought before the export window the engine
# cannot see it, but the position is still covered, so the leg is a COVERED_CALL
# carrying `shares_tracked = False` rather than a different strategy.
CSP = "CSP"  # cash-secured put
COVERED_CALL = "COVERED_CALL"
LONG_PUT = "LONG_PUT"
LONG_CALL = "LONG_CALL"

WHEEL_STRATEGIES = frozenset({CSP, COVERED_CALL})

# Cycle status
ACTIVE = "ACTIVE"
CLOSED = "CLOSED"
ASSIGNED_STATUS = "ASSIGNED"

# Share-lot provenance
FROM_PUT_ASSIGNMENT = "PUT_ASSIGNMENT"
FROM_PURCHASE = "PURCHASE"
FROM_PRE_HISTORY = "PRE_HISTORY"


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


@dataclass
class LegClose:
    """One closing fill applied to a specific open lot."""

    date: date
    action: str  # BTC | STC | EXPIRED | ASSIGNED
    contracts: float
    price: float | None
    cash: float  # allocated share of the closing row's Amount
    fees: float
    roll_id: str | None = None


@dataclass
class OptionLeg:
    """One opening fill and everything that later closed it."""

    leg_id: str
    cycle_id: str
    underlying: str
    occ_symbol: str
    right: str  # "C" | "P"
    strike: float
    expiry: date
    side: str  # SHORT | LONG
    strategy: str
    open_date: date
    open_action: str  # STO | BTO
    contracts: float  # contracts opened
    open_price: float | None
    open_cash: float  # Amount of the opening row (credit positive)
    open_fees: float
    closes: list[LegClose] = field(default_factory=list)
    open_roll_id: str | None = None  # set when this leg was opened *by* a roll
    # False on a covered call whose backing stock was acquired before this export
    # begins. The call is still covered; the shares are simply not visible here,
    # so their capital has to be estimated.
    shares_tracked: bool = True
    # spread_id -> contracts of *this* leg claimed by that spread as of pairing.
    # A costing overlay only: contracts/closes/realized_pl/gross_premium never
    # change because of this -- see Spread below and capital_timeline in
    # wheel/metrics.py, which is where the paired portion's collateral is
    # actually netted.
    paired_contracts: dict[str, float] = field(default_factory=dict)

    # ---- position ----

    @property
    def closed_contracts(self) -> float:
        return sum(close.contracts for close in self.closes)

    @property
    def remaining_contracts(self) -> float:
        return round(self.contracts - self.closed_contracts, 6)

    @property
    def is_open(self) -> bool:
        return self.remaining_contracts > 1e-9

    @property
    def close_date(self) -> date | None:
        return max((close.date for close in self.closes), default=None) if not self.is_open else None

    @property
    def outcome(self) -> str:
        if self.is_open:
            return "OPEN"
        actions = {close.action for close in self.closes}
        if ASSIGNED in actions:
            return "ASSIGNED"
        if EXPIRED in actions and len(actions) == 1:
            return "EXPIRED"
        return "CLOSED"

    # ---- economics ----

    @property
    def cash_per_contract(self) -> float:
        return self.open_cash / self.contracts if self.contracts else 0.0

    @property
    def realized_pl(self) -> float:
        """P/L on the contracts that have been closed, net of all fees."""
        return self.cash_per_contract * self.closed_contracts + sum(c.cash for c in self.closes)

    @property
    def open_premium(self) -> float:
        """Cash still at risk on the un-closed portion (credit positive)."""
        return self.cash_per_contract * self.remaining_contracts

    @property
    def gross_premium(self) -> float:
        """Credit received at open (0 for long legs) before any buy-back."""
        return self.open_cash if self.side == SHORT else 0.0

    @property
    def total_fees(self) -> float:
        return self.open_fees + sum(close.fees for close in self.closes)

    @property
    def days_held(self) -> int | None:
        end = self.close_date
        return (end - self.open_date).days if end else None

    @property
    def collateral_per_contract(self) -> float:
        """Cash the position ties up per contract while open.

        A covered call whose shares this export contains contributes nothing
        here, because the capital already sits in the :class:`ShareLot` backing
        it -- counting both would double it.  When the shares were bought before
        the export begins they cannot be seen, so ``strike x 100`` stands in for
        them: still covered, just estimated.  Without it those tickers would
        report an undefined return on zero capital.
        """
        if self.strategy == CSP:
            return self.strike * OPTION_MULTIPLIER
        if self.strategy == COVERED_CALL:
            return 0.0 if self.shares_tracked else self.strike * OPTION_MULTIPLIER
        if self.side == LONG:
            return abs(self.cash_per_contract)
        return 0.0

    def collateral_on(self, day: date) -> float:
        """Collateral committed by this leg at end of ``day``, ignoring any
        spread pairing -- :func:`wheel.metrics.capital_timeline` subtracts the
        portion protected by an open :class:`Spread` before folding this in,
        so this always reflects the naked/unpaired formula.
        """
        return self.remaining_contracts_on(day) * self.collateral_per_contract

    def remaining_contracts_on(self, day: date) -> float:
        """Contracts of this leg still open at end of ``day``.

        Exposed separately from :meth:`collateral_on` so a :class:`Spread` can
        tell how much of its paired quantity is still protected -- a spread's
        netting only holds while *both* legs remain open, so this is checked
        against both legs independently rather than assumed from one.
        """
        if day < self.open_date:
            return 0.0
        still_open = self.contracts - sum(c.contracts for c in self.closes if c.date <= day)
        return max(still_open, 0.0)


@dataclass
class ShareLot:
    """A block of shares, real or synthesized from an assignment."""

    lot_id: str
    cycle_id: str
    underlying: str
    acquired: date
    shares: float
    basis_per_share: float | None
    source: str
    synthetic: bool = False
    basis_known: bool = True
    remaining: float = 0.0
    disposals: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.remaining:
            self.remaining = self.shares

    @property
    def cost(self) -> float:
        return (self.basis_per_share or 0.0) * self.shares

    def capital_on(self, day: date) -> float:
        if day < self.acquired or self.basis_per_share is None:
            return 0.0
        sold = sum(d["shares"] for d in self.disposals if d["date"] <= day)
        return max(self.shares - sold, 0.0) * self.basis_per_share


@dataclass
class Roll:
    """A same-day close-and-reopen on one underlying and option right."""

    roll_id: str
    cycle_id: str
    underlying: str
    date: date
    right: str
    closed: list[dict] = field(default_factory=list)  # {occ_symbol, strike, expiry, contracts, cash}
    opened: list[dict] = field(default_factory=list)

    @property
    def net_credit(self) -> float:
        return sum(item["cash"] for item in self.closed) + sum(item["cash"] for item in self.opened)

    @property
    def direction(self) -> str:
        """Human label: out / up / down / diagonal."""
        if not self.closed or not self.opened:
            return "UNKNOWN"
        old_exp = max(item["expiry"] for item in self.closed)
        new_exp = max(item["expiry"] for item in self.opened)
        old_strike = sum(i["strike"] * i["contracts"] for i in self.closed) / sum(i["contracts"] for i in self.closed)
        new_strike = sum(i["strike"] * i["contracts"] for i in self.opened) / sum(i["contracts"] for i in self.opened)
        later = new_exp > old_exp
        if abs(new_strike - old_strike) < 1e-9:
            return "OUT" if later else "FLAT"
        moved = "UP" if new_strike > old_strike else "DOWN"
        return f"OUT_AND_{moved}" if later else moved


@dataclass
class Spread:
    """A short and a long leg, same underlying/right/expiry, paired because
    they were opened on the same day -- the only pairing rule this engine
    applies (see :meth:`WheelEngine._detect_spreads`). Collateral for the
    paired portion is the strike distance, not the short leg's full
    CSP/covered-call collateral; P/L is untouched -- each leg's own
    ``realized_pl`` already carries its own cash flows, so a spread never
    double-counts anything, it only changes how collateral is reported.

    ``paired_contracts`` is the quantity paired at open time. It does not
    shrink as one side is later closed; instead capital_timeline checks both
    legs' own ``remaining_contracts_on(day)`` against it, since the netting
    only holds while both sides are still open.
    """

    spread_id: str
    cycle_id: str
    underlying: str
    right: str
    expiry: date
    open_date: date
    short_leg_id: str
    long_leg_id: str
    paired_contracts: float
    short_strike: float
    long_strike: float
    short_open_cash: float  # this spread's share of the short leg's open credit
    long_open_cash: float  # this spread's share of the long leg's open debit
    # True when either paired leg's own collateral was itself an estimate
    # (a covered call whose backing shares this export cannot see) -- carries
    # the same "~" caveat the leg already gets, see OptionLeg.collateral_per_contract.
    capital_estimated: bool = False

    @property
    def collateral_per_contract(self) -> float:
        return abs(self.short_strike - self.long_strike) * OPTION_MULTIPLIER

    @property
    def net_credit(self) -> float:
        """Short premium received minus long premium paid, for the paired
        contracts only -- both already signed (short credit positive, long
        debit negative), so this is a plain sum.
        """
        return self.short_open_cash + self.long_open_cash


@dataclass
class Assignment:
    """An assignment event and the share movement it implies."""

    date: date
    underlying: str
    cycle_id: str
    occ_symbol: str
    right: str
    strike: float
    contracts: float
    shares: float
    direction: str  # "ACQUIRE" | "DISPOSE"
    cash: float  # synthesized: negative when buying shares
    synthetic: bool = True
    note: str = ""


@dataclass
class Cycle:
    """One wheel campaign in a single underlying."""

    cycle_id: str
    underlying: str
    sequence: int
    start_date: date
    end_date: date | None = None
    legs: list[OptionLeg] = field(default_factory=list)
    share_lots: list[ShareLot] = field(default_factory=list)
    rolls: list[Roll] = field(default_factory=list)
    spreads: list[Spread] = field(default_factory=list)
    assignments: list[Assignment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # True when some committed capital is a proxy (short calls backed by stock
    # acquired before this export) rather than a figure derived from the file.
    capital_estimated: bool = False

    @property
    def is_open(self) -> bool:
        return self.end_date is None

    @property
    def status(self) -> str:
        if self.is_open:
            return ACTIVE
        return ASSIGNED_STATUS if self.assignments else CLOSED

    @property
    def had_assignment(self) -> bool:
        return bool(self.assignments)

    @property
    def last_activity(self) -> date:
        dates = [leg.open_date for leg in self.legs]
        dates += [close.date for leg in self.legs for close in leg.closes]
        dates += [lot.acquired for lot in self.share_lots]
        return max(dates, default=self.start_date)


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


class WheelEngine:
    """Rebuilds wheel cycles from a chronological transaction stream."""

    def __init__(self, transactions: Sequence[Transaction]):
        self.transactions = sorted(transactions, key=lambda t: (t.event_date, t.row_id))
        self.cycles: list[Cycle] = []
        self.warnings: list[str] = []
        self.unmatched_closes: list[dict] = []
        # Cash on closing rows whose opening leg is not in this slice -- non-zero
        # only when a date filter cuts a position in half.  Tracked so the cash
        # reconciliation can account for it instead of reading as a shortfall.
        self.unmatched_cash: float = 0.0

        self._open_legs: dict[str, list[OptionLeg]] = {}  # occ_symbol -> FIFO lots
        self._share_lots: dict[str, list[ShareLot]] = {}  # underlying -> FIFO lots
        self._active_cycle: dict[str, Cycle] = {}
        self._cycle_counter: dict[str, int] = {}
        self._ids = itertools.count(1)
        self._settlements = self._index_settlements()
        # Consumption is tracked here, not on the Transaction objects: those are
        # shared and frozen, and marking them would leak across engine runs.
        self._claimed_settlements: set[int] = set()

    def _index_settlements(self) -> dict[tuple[str, str], list[Transaction]]:
        """Index the equity legs that settle assignments, by ticker and direction.

        When the broker supplies the real share fill there is nothing to invent,
        so :meth:`_settle_assignment` consults this first and only synthesizes a
        share leg when the export genuinely lacks one.
        """
        index: dict[tuple[str, str], list[Transaction]] = {}
        for transaction in self.transactions:
            if not transaction.assignment_settlement:
                continue
            direction = "ACQUIRE" if transaction.action == BUY_STOCK else "DISPOSE"
            index.setdefault((transaction.underlying, direction), []).append(transaction)
        return index

    def _claim_settlement(
        self, underlying: str, direction: str, shares: float, when: date, strike: float | None
    ) -> list[Transaction]:
        """Find and consume the equity rows settling this assignment, if present.

        Matched on ticker, direction and share count within a few days -- the two
        post on the same run date but the option leg is back-dated to its 'as of'
        date, so they rarely share an event date exactly.

        Two refinements that plain size-matching gets wrong on real data:

        * **A single assignment can settle across several rows.** A 100-share call
          assignment shows up as ``-22`` from the cash account and ``-78`` from
          margin, so a subset summing to the full size is accepted, not just one
          row of the exact size.
        * **The fill price disambiguates same-size assignments.** When a 425 call
          and a 440 call are both assigned for 100 shares on one day, size alone
          could pair either with either; the settlement price equals the strike,
          so rows matching this strike are preferred.

        Returns the claimed rows, or an empty list if the export has no share leg
        for this assignment (in which case the caller synthesizes one).
        """
        pool = [
            candidate
            for candidate in self._settlements.get((underlying, direction), [])
            if candidate.row_id not in self._claimed_settlements
            and abs((candidate.event_date - when).days) <= 5
        ]
        if strike is not None:
            at_strike = [
                candidate
                for candidate in pool
                if candidate.price is not None and abs(candidate.price - strike) < 0.005
            ]
            pool = at_strike or pool

        pool.sort(key=lambda c: (abs((c.event_date - when).days), c.row_id))

        # A single row of the right size is the common case.
        for candidate in pool:
            if abs(abs(candidate.contracts) - shares) < 1e-6:
                self._claimed_settlements.add(candidate.row_id)
                return [candidate]

        # Otherwise accept a run of rows that together cover the assignment.
        taken: list[Transaction] = []
        total = 0.0
        for candidate in pool:
            if total + abs(candidate.contracts) > shares + 1e-6:
                continue
            taken.append(candidate)
            total += abs(candidate.contracts)
            if abs(total - shares) < 1e-6:
                self._claimed_settlements.update(c.row_id for c in taken)
                return taken
        return []

    # ---------------- public ----------------

    def run(self) -> list[Cycle]:
        for event_date, batch in _group_by_day(self.transactions):
            for underlying, rows in _group_by_underlying(batch):
                self._process_day(underlying, event_date, rows)
        self._finalize()
        return self.cycles

    # ---------------- cycle bookkeeping ----------------

    def _cycle_for(self, underlying: str, when: date) -> Cycle:
        cycle = self._active_cycle.get(underlying)
        if cycle is not None:
            return cycle
        sequence = self._cycle_counter.get(underlying, 0) + 1
        self._cycle_counter[underlying] = sequence
        cycle = Cycle(
            cycle_id=f"{underlying}-{when.year}-{sequence}",
            underlying=underlying,
            sequence=sequence,
            start_date=when,
        )
        self._active_cycle[underlying] = cycle
        self.cycles.append(cycle)
        return cycle

    def _is_flat(self, underlying: str) -> bool:
        legs_open = any(leg.is_open for leg in self._open_legs_for(underlying))
        shares_open = any(lot.remaining > 1e-9 for lot in self._share_lots.get(underlying, []))
        return not legs_open and not shares_open

    def _open_legs_for(self, underlying: str) -> list[OptionLeg]:
        return [leg for legs in self._open_legs.values() for leg in legs if leg.underlying == underlying and leg.is_open]

    def _close_cycle_if_flat(self, underlying: str, when: date) -> None:
        cycle = self._active_cycle.get(underlying)
        if cycle is not None and self._is_flat(underlying):
            cycle.end_date = when
            del self._active_cycle[underlying]

    # ---------------- per-day processing ----------------

    _CLOSING = frozenset({BTC, STC, EXPIRED, ASSIGNED, SELL_STOCK})
    _OPENING = frozenset({STO, BTO, BUY_STOCK})

    def _intraday_order(self, rows: list[Transaction]) -> list[Transaction]:
        """Sequence one ticker-day so every event sees the position it should.

        Three phases, each keeping the broker's own row order internally:

        0. closes of positions carried in from a previous day -- assignments,
           expirations and the buy-back half of a roll.  Running these first is
           what lets a covered call opened later the same day see the shares an
           assignment just delivered.
        1. opens.
        2. closes of symbols that were *also opened today* -- same-day scalps and
           expiry-day entries.  These must come after their own open, otherwise
           the close finds no lot to match.
        """
        opened_today = {
            row.occ_symbol for row in rows if row.action in self._OPENING and row.is_option
        }

        def phase(row: Transaction) -> int:
            if row.action in self._CLOSING:
                return 2 if row.is_option and row.occ_symbol in opened_today else 0
            return 1

        return sorted(rows, key=lambda row: (phase(row), row.row_id))

    def _process_day(self, underlying: str, when: date, rows: list[Transaction]) -> None:
        """Apply one ticker's activity for a single day."""
        closed_records: list[dict] = []
        opened_records: list[dict] = []

        for row in self._intraday_order(rows):
            if row.action in self._CLOSING:
                closed_records.extend(self._apply_close(underlying, row))
            elif row.action in self._OPENING:
                record = self._apply_open(underlying, row)
                if record:
                    opened_records.append(record)

        self._detect_rolls(underlying, when, closed_records, opened_records)
        self._detect_spreads(underlying, when, opened_records)
        self._close_cycle_if_flat(underlying, when)

    # ---------------- opens ----------------

    def _apply_open(self, underlying: str, row: Transaction) -> dict | None:
        cycle = self._cycle_for(underlying, row.event_date)

        if row.action == BUY_STOCK:
            shares = abs(row.contracts)
            basis = row.price if row.price is not None else (abs(row.amount) / shares if shares else None)
            self._add_share_lot(cycle, underlying, row.event_date, shares, basis, FROM_PURCHASE, synthetic=False)
            return None

        if not row.is_option:
            return None

        side = SHORT if row.action == STO else LONG
        strategy, shares_tracked = self._classify(underlying, row.right, side)
        leg = OptionLeg(
            leg_id=f"L{next(self._ids)}",
            cycle_id=cycle.cycle_id,
            underlying=underlying,
            occ_symbol=row.occ_symbol,
            right=row.right,
            strike=row.strike,
            expiry=row.expiry,
            side=side,
            strategy=strategy,
            open_date=row.event_date,
            open_action=row.action,
            contracts=abs(row.contracts),
            open_price=row.price,
            open_cash=row.amount,
            open_fees=row.total_fees,
            shares_tracked=shares_tracked,
        )
        if not shares_tracked:
            cycle.capital_estimated = True
        cycle.legs.append(leg)
        self._open_legs.setdefault(row.occ_symbol, []).append(leg)
        return {
            "leg": leg,
            "occ_symbol": leg.occ_symbol,
            "right": leg.right,
            "strike": leg.strike,
            "expiry": leg.expiry,
            "contracts": leg.contracts,
            "cash": leg.open_cash,
            "side": side,
        }

    def _classify(self, underlying: str, right: str, side: str) -> tuple[str, bool]:
        """Return the leg's strategy and whether its backing stock is visible.

        A short call is always a covered call.  The second value says whether
        this export actually contains the shares behind it -- when it does not,
        the position is unchanged but its capital has to be estimated.
        """
        if side == LONG:
            return (LONG_CALL if right == "C" else LONG_PUT), True
        if right == "P":
            return CSP, True
        covered = sum(lot.remaining for lot in self._share_lots.get(underlying, []))
        return COVERED_CALL, covered >= OPTION_MULTIPLIER

    # ---------------- closes ----------------

    def _apply_close(self, underlying: str, row: Transaction) -> list[dict]:
        if row.action == SELL_STOCK:
            shares = abs(row.contracts)
            proceeds = row.price if row.price is not None else (abs(row.amount) / shares if shares else 0.0)
            self._dispose_shares(underlying, row.event_date, shares, proceeds, synthetic=False)
            return []

        if not row.is_option:
            return []

        lots = [leg for leg in self._open_legs.get(row.occ_symbol, []) if leg.is_open]
        if not lots:
            lots = self._lots_under_former_ticker(row)
        wanted = abs(row.contracts)
        if wanted == 0:
            # ASSIGNED / EXPIRED rows occasionally omit the count; infer it.
            wanted = sum(leg.remaining_contracts for leg in lots)

        if not lots:
            self.unmatched_cash += row.amount
            self.unmatched_closes.append(
                {
                    "date": row.event_date.isoformat(),
                    "symbol": row.occ_symbol,
                    "action": row.action,
                    "contracts": wanted,
                    "cash": round(row.amount, 2),
                    "reason": "no open lot -- position opened before this window",
                }
            )
            if row.action == ASSIGNED:
                self._handle_assignment_without_leg(underlying, row, wanted)
            return []

        cycle = self._active_cycle.get(underlying) or self._cycle_for(underlying, row.event_date)
        total_available = sum(leg.remaining_contracts for leg in lots)
        matched = min(wanted, total_available)

        # Per-contract cash is always the row's own total spread over the *full*
        # requested size, never just the portion that matched a known lot.  A
        # single BTC of 3 contracts at a uniform fill price still costs 1/3 of
        # the row's Amount per contract even when this export only shows 1 of
        # those 3 as open (the other 2 were opened before the window) --
        # dividing by `matched` instead would load the whole 3-contract cost
        # onto the 1 visible contract, tripling its apparent realized P/L.
        cash_per_contract = row.amount / wanted if wanted else 0.0
        fees_per_contract = row.total_fees / wanted if wanted else 0.0

        if wanted - total_available > 1e-9:
            excess_contracts = wanted - total_available
            excess_cash = cash_per_contract * excess_contracts
            self.unmatched_cash += excess_cash
            self.unmatched_closes.append(
                {
                    "date": row.event_date.isoformat(),
                    "symbol": row.occ_symbol,
                    "action": row.action,
                    "contracts": excess_contracts,
                    "cash": round(excess_cash, 2),
                    "reason": (
                        "close exceeds open position; its pro-rata share of cash "
                        "has no matching lot and is excluded from any leg's P/L"
                    ),
                }
            )

        records: list[dict] = []
        remaining = matched
        for leg in lots:  # FIFO
            if remaining <= 1e-9:
                break
            take = min(leg.remaining_contracts, remaining)
            leg.closes.append(
                LegClose(
                    date=row.event_date,
                    action=row.action,
                    contracts=take,
                    price=row.price,
                    cash=cash_per_contract * take,
                    fees=fees_per_contract * take,
                )
            )
            remaining -= take
            records.append(
                {
                    "leg": leg,
                    "occ_symbol": leg.occ_symbol,
                    "right": leg.right,
                    "strike": leg.strike,
                    "expiry": leg.expiry,
                    "contracts": take,
                    "cash": cash_per_contract * take,
                    "side": leg.side,
                    "action": row.action,
                }
            )

        if row.action == ASSIGNED:
            self._settle_assignment(cycle, underlying, row, matched, lots[0].side if lots else SHORT)

        return records

    def _lots_under_former_ticker(self, row: Transaction) -> list[OptionLeg]:
        """Find open lots for a contract whose underlying ticker was renamed.

        A corporate action can rename the underlying mid-contract: this book
        sells ``AXL260220P8`` and is assigned ``DCH260220P8`` when American Axle
        becomes Dauch Corporation.  The option series -- expiry, right, strike --
        is unchanged, so an exact match on all three under a single other ticker
        identifies the position unambiguously.  Anything ambiguous is left alone
        and reported as unmatched rather than guessed at.
        """
        if not row.is_option or row.expiry is None or row.strike is None:
            return []

        matches: dict[str, list[OptionLeg]] = {}
        for symbol, legs in self._open_legs.items():
            if symbol == row.occ_symbol:
                continue
            for leg in legs:
                if (
                    leg.is_open
                    and leg.expiry == row.expiry
                    and leg.right == row.right
                    and abs(leg.strike - row.strike) < 1e-9
                ):
                    matches.setdefault(leg.underlying, []).append(leg)

        if len(matches) != 1:
            return []

        former, legs = next(iter(matches.items()))
        cycle = self._active_cycle.get(former)
        message = (
            f"{row.event_date}: {row.occ_symbol} matched open lots of {legs[0].occ_symbol}; "
            f"treating {former} -> {row.underlying} as a ticker change"
        )
        if cycle is not None and message not in cycle.warnings:
            cycle.warnings.append(message)
        self.warnings.append(message)
        return legs

    # ---------------- assignment -> shares ----------------

    def _settle_assignment(self, cycle: Cycle, underlying: str, row: Transaction, contracts: float, side: str) -> None:
        """Synthesize the share leg implied by an assignment.

        Fidelity's option history does not contain the equity fill, so the share
        movement is reconstructed at the strike.  Everything produced here is
        flagged ``synthetic`` so the UI can separate it from broker-sourced cash.
        """
        shares = contracts * OPTION_MULTIPLIER
        acquire = (row.right == "P") if side == SHORT else (row.right == "C")
        direction = "ACQUIRE" if acquire else "DISPOSE"

        # If the export carries the real equity fill, let the normal stock path
        # handle it -- synthesizing here as well would book the shares twice.
        settlement = self._claim_settlement(underlying, direction, shares, row.event_date, row.strike)
        if settlement:
            posted = ", ".join(sorted({str(item.run_date) for item in settlement}))
            rows_note = f"{len(settlement)} broker rows" if len(settlement) > 1 else "broker row"
            cycle.assignments.append(
                Assignment(
                    date=row.event_date,
                    underlying=underlying,
                    cycle_id=cycle.cycle_id,
                    occ_symbol=row.occ_symbol,
                    right=row.right,
                    strike=row.strike,
                    contracts=contracts,
                    shares=shares,
                    direction=direction,
                    cash=sum(item.amount for item in settlement),
                    synthetic=False,
                    note=(
                        f"{direction.lower()}d {shares:g} shares at {row.strike:g}; "
                        f"share leg from {rows_note} posted {posted}"
                    ),
                )
            )
            return

        if acquire:
            cash = -row.strike * shares
            self._add_share_lot(
                cycle, underlying, row.event_date, shares, row.strike, FROM_PUT_ASSIGNMENT, synthetic=True
            )
            note = f"short {row.right} assigned: acquired {shares:g} shares at {row.strike:g}"
        else:
            cash = row.strike * shares
            note = self._dispose_shares(underlying, row.event_date, shares, row.strike, synthetic=True, cycle=cycle)

        cycle.assignments.append(
            Assignment(
                date=row.event_date,
                underlying=underlying,
                cycle_id=cycle.cycle_id,
                occ_symbol=row.occ_symbol,
                right=row.right,
                strike=row.strike,
                contracts=contracts,
                shares=shares,
                direction=direction,
                cash=cash,
                note=note,
            )
        )

    def _handle_assignment_without_leg(self, underlying: str, row: Transaction, contracts: float) -> None:
        cycle = self._cycle_for(underlying, row.event_date)
        cycle.warnings.append(
            f"{row.event_date}: assignment of {row.occ_symbol} has no matching open leg in this export"
        )
        self._settle_assignment(cycle, underlying, row, contracts, SHORT)

    def _add_share_lot(
        self,
        cycle: Cycle,
        underlying: str,
        when: date,
        shares: float,
        basis: float | None,
        source: str,
        synthetic: bool,
    ) -> ShareLot:
        lot = ShareLot(
            lot_id=f"S{next(self._ids)}",
            cycle_id=cycle.cycle_id,
            underlying=underlying,
            acquired=when,
            shares=shares,
            basis_per_share=basis,
            source=source,
            synthetic=synthetic,
            basis_known=basis is not None,
        )
        self._share_lots.setdefault(underlying, []).append(lot)
        cycle.share_lots.append(lot)
        return lot

    def _dispose_shares(
        self,
        underlying: str,
        when: date,
        shares: float,
        price: float,
        synthetic: bool,
        cycle: Cycle | None = None,
    ) -> str:
        """Sell ``shares`` FIFO, recording realized stock P/L where basis is known."""
        lots = [lot for lot in self._share_lots.get(underlying, []) if lot.remaining > 1e-9]
        available = sum(lot.remaining for lot in lots)

        if available + 1e-9 < shares:
            # Called away stock that was bought before this export began.  Book a
            # basis-unknown lot so the position math stays consistent and the
            # stock P/L is reported as unknown rather than silently invented.
            deficit = shares - available
            target = cycle or self._active_cycle.get(underlying) or self._cycle_for(underlying, when)
            lot = self._add_share_lot(
                target, underlying, when, deficit, None, FROM_PRE_HISTORY, synthetic=True
            )
            lot.basis_known = False
            target.warnings.append(
                f"{when}: {deficit:g} shares called away were acquired before this export; "
                "cost basis unknown, stock P/L excluded"
            )
            lots.append(lot)

        remaining = shares
        realized = 0.0
        basis_unknown = 0.0
        for lot in lots:
            if remaining <= 1e-9:
                break
            take = min(lot.remaining, remaining)
            lot.remaining -= take
            if lot.basis_known and lot.basis_per_share is not None:
                realized += (price - lot.basis_per_share) * take
            else:
                basis_unknown += take
            lot.disposals.append(
                {
                    "date": when,
                    "shares": take,
                    "price": price,
                    "proceeds": price * take,
                    "realized": (price - lot.basis_per_share) * take if lot.basis_per_share is not None else 0.0,
                    "basis_known": lot.basis_known,
                    "synthetic": synthetic,
                }
            )
            remaining -= take

        note = f"sold {shares:g} shares at {price:g}; realized {realized:,.2f}"
        if basis_unknown:
            note += f" ({basis_unknown:g} shares of unknown basis excluded)"
        return note

    # ---------------- rolls ----------------

    def _detect_rolls(
        self, underlying: str, when: date, closed: list[dict], opened: list[dict]
    ) -> None:
        """Group same-day, same-right close+open activity into rolls.

        A roll is recognised when a short leg is closed and another short leg of
        the same right is opened the same day at an expiry no earlier than the
        one closed.  Quantities are deliberately not required to match -- real
        rolls resize (the MU book closes 4 and opens 2).
        """
        for right in ("P", "C"):
            closes = [
                record
                for record in closed
                if record["right"] == right
                and record["side"] == SHORT
                and record.get("action") in {BTC, STC}
            ]
            opens = [record for record in opened if record["right"] == right and record["side"] == SHORT]
            if not closes or not opens:
                continue

            newest_close = max(record["expiry"] for record in closes)
            closed_symbols = {record["occ_symbol"] for record in closes}
            # A roll moves the position forward in time or strike.  Re-entering
            # the very contract just closed is a same-day round trip, not a roll.
            forward = [
                record
                for record in opens
                if record["expiry"] >= newest_close and record["occ_symbol"] not in closed_symbols
            ]
            if not forward:
                continue

            cycle = self._active_cycle.get(underlying) or self._cycle_for(underlying, when)
            roll = Roll(
                roll_id=f"R{next(self._ids)}",
                cycle_id=cycle.cycle_id,
                underlying=underlying,
                date=when,
                right=right,
            )
            for record in closes:
                record["leg"].closes[-1].roll_id = roll.roll_id
                roll.closed.append(_roll_item(record))
            for record in forward:
                record["leg"].open_roll_id = roll.roll_id
                roll.opened.append(_roll_item(record))
            cycle.rolls.append(roll)

    # ---------------- spreads ----------------

    def _detect_spreads(self, underlying: str, when: date, opened: list[dict]) -> None:
        """Pair a short and a long leg opened the same day into a Spread.

        The only rule this engine applies: same underlying (guaranteed by the
        caller, one ticker-day at a time), same right, same expiry, opened on
        the same day. Quantities are paired down to whichever side is smaller;
        the excess on the larger side stays naked.

        When more than one short or more than one long candidate shares a
        (right, expiry) group, which pairs with which is genuinely ambiguous
        -- real data has this (one short put alongside two same-day long puts
        at different strikes). Pairing any of them would be a guess, so none
        of that group is paired; a cycle warning names it instead, mirroring
        the "ambiguous -> leave unmatched, warn" rule already used for
        ticker-rename matching (_lots_under_former_ticker).
        """
        by_group: dict[tuple[str, date], dict[str, list[dict]]] = {}
        for record in opened:
            key = (record["right"], record["expiry"])
            bucket = by_group.setdefault(key, {"short": [], "long": []})
            bucket["short" if record["side"] == SHORT else "long"].append(record)

        for (right, expiry), sides in by_group.items():
            shorts, longs = sides["short"], sides["long"]
            if not shorts or not longs:
                continue

            cycle = self._active_cycle.get(underlying) or self._cycle_for(underlying, when)
            if len(shorts) > 1 or len(longs) > 1:
                cycle.warnings.append(
                    f"{when}: {len(shorts)} short and {len(longs)} long {right} legs on "
                    f"{underlying} expiring {expiry} opened the same day -- which pairs "
                    "with which is ambiguous, so none were paired into a spread"
                )
                continue

            short_record, long_record = shorts[0], longs[0]
            short_leg: OptionLeg = short_record["leg"]
            long_leg: OptionLeg = long_record["leg"]
            paired = min(short_leg.contracts, long_leg.contracts)
            if paired <= 1e-9:
                continue

            short_fraction = paired / short_leg.contracts if short_leg.contracts else 0.0
            long_fraction = paired / long_leg.contracts if long_leg.contracts else 0.0
            spread = Spread(
                spread_id=f"SP{next(self._ids)}",
                cycle_id=cycle.cycle_id,
                underlying=underlying,
                right=right,
                expiry=expiry,
                open_date=when,
                short_leg_id=short_leg.leg_id,
                long_leg_id=long_leg.leg_id,
                paired_contracts=paired,
                short_strike=short_leg.strike,
                long_strike=long_leg.strike,
                short_open_cash=short_leg.open_cash * short_fraction,
                long_open_cash=long_leg.open_cash * long_fraction,
                capital_estimated=not short_leg.shares_tracked,
            )
            short_leg.paired_contracts[spread.spread_id] = paired
            long_leg.paired_contracts[spread.spread_id] = paired
            cycle.spreads.append(spread)

    # ---------------- finalize ----------------

    def _finalize(self) -> None:
        for underlying, legs in self._open_legs.items():
            for leg in legs:
                if leg.is_open and leg.expiry and leg.expiry < _last_date(self.transactions):
                    self.warnings.append(
                        f"{leg.occ_symbol}: {leg.remaining_contracts:g} contracts still open past expiry {leg.expiry}"
                    )
        if self.unmatched_closes:
            self.warnings.append(
                f"{len(self.unmatched_closes)} closing rows had no matching open lot in this export"
            )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _roll_item(record: dict) -> dict:
    return {
        "occ_symbol": record["occ_symbol"],
        "strike": record["strike"],
        "expiry": record["expiry"],
        "contracts": record["contracts"],
        "cash": record["cash"],
        "leg_id": record["leg"].leg_id,
    }


def _group_by_day(transactions: Sequence[Transaction]):
    for event_date, rows in itertools.groupby(transactions, key=lambda t: t.event_date):
        yield event_date, list(rows)


def _group_by_underlying(rows: Iterable[Transaction]):
    grouped: dict[str, list[Transaction]] = {}
    for row in rows:
        grouped.setdefault(row.underlying, []).append(row)
    for underlying in sorted(grouped):
        yield underlying, grouped[underlying]


def _last_date(transactions: Sequence[Transaction]) -> date:
    return max((t.event_date for t in transactions), default=date.min)


def build_cycles(transactions: Sequence[Transaction]) -> tuple[list[Cycle], WheelEngine]:
    engine = WheelEngine(transactions)
    return engine.run(), engine
