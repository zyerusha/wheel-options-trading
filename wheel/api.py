"""JSON payload assembly for the dashboard.

Filtering rebuilds the cycles from the filtered transaction slice rather than
post-filtering finished cycles.  That costs a few milliseconds and buys the
guarantee the dataviz brief asks for: every stat, chart and table on the page is
derived from the same slice, so the numbers always agree with each other.
"""

from __future__ import annotations

import math
import os
import re
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Sequence

from wheel import assignment as assignment_mod
from wheel import benchmark as bm
from wheel import cashflow as cf
from wheel import expiration as expiration_mod
from wheel import marketdata
from wheel import workflow as workflow_mod
from wheel.engine import (
    COVERED_CALL,
    CSP,
    FROM_PURCHASE,
    FROM_PUT_ASSIGNMENT,
    LONG,
    SHORT,
    Cycle,
    WheelEngine,
    build_cycles,
)
from wheel.fileio import peek_text
from wheel.insights import portfolio_insights, wheel_insights
from wheel.metrics import (
    capital_timeline,
    cycle_metrics,
    dividends_by_cycle,
    leg_rows,
    net_adjusted_cost_basis,
    periodic_pl_series,
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
from wheel.paths import DISCOVERY_DIRS
from wheel.positions import (
    EQUITY,
    discover_position_snapshots,
    latest_snapshot,
    latest_snapshot_per_account,
    load_snapshots,
)
from wheel.reference import (
    load_earnings,
    load_fundamentals,
    sector_is_fund,
    sector_is_leveraged_etf,
    sector_of,
)

EXPORT_DIRS = DISCOVERY_DIRS


# --------------------------------------------------------------------------
# CSP-candidate recommender: 0-5 stars synthesised from this ticker's past
# wheels plus its current market shape, then nudged by earnings timing and
# how concentrated the book already is in its sector. All of the numbers
# below are deliberately soft -- a heuristic to rank names, not a model.
# --------------------------------------------------------------------------

# Weights sum to 1.0; each component is scored 0..1, the weighted sum x5 is
# the base star count before the earnings / sector modifiers.
CSP_STAR_WEIGHTS = {
    "roc": 0.22,  # annualized wheel ROC (how the wheel actually returned)
    "monthly_premium": 0.16,  # gross premium / collateral per 30d (premium richness)
    "ppd_yield": 0.10,  # annualized blended PPD on capital (kept-premium efficiency)
    "profit": 0.09,  # total realized $ banked on this ticker, saturating
    "win_rate": 0.15,  # share of past legs that won
    "consistency": 0.09,  # how many wheels of evidence there is
    "recency": 0.07,  # how long ago the last wheel wrapped
    "volatility": 0.08,  # realized vol now -- enough IV to sell, not a casino
    "price_position": 0.04,  # where price sits in its 1y range -- not a falling knife
}


def _sat(value: float | None, scale: float) -> float:
    """Saturating 0..1 curve: ``1 - exp(-value/scale)`` (~0.63 at value==scale)."""
    if value is None or value <= 0:
        return 0.0
    return 1.0 - math.exp(-value / scale)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _vol_fit_score(vol_annual_pct: float | None) -> float:
    """Tent curve: too calm = thin premium, sweet spot ~30-55% annualized,
    then a decline into "this is a gamble" past ~90%."""
    if vol_annual_pct is None:
        return 0.4
    v = vol_annual_pct
    if v <= 12:
        return 0.15
    if v <= 30:
        return 0.15 + (v - 12) / 18 * 0.75
    if v <= 55:
        return 0.90
    if v <= 90:
        return 0.90 - (v - 55) / 35 * 0.70
    return 0.15


def _price_position_score(pos: float | None) -> float:
    """Favor the middle-to-upper part of the 1y range; dock a falling knife
    (bottom ~15%) and shave a little right at the highs."""
    if pos is None:
        return 0.5
    if pos < 0.15:
        return 0.25
    if pos < 0.30:
        return 0.25 + (pos - 0.15) / 0.15 * 0.55
    if pos <= 0.85:
        return 0.85
    return 0.85 - (pos - 0.85) / 0.15 * 0.35


def _earnings_modifier(days_to_earnings: int | None) -> tuple[float, str]:
    """Star adjustment for where the next earnings print falls. Inside a week
    is a real penalty (gap risk, no time to react); ~2-4 weeks out is a bonus
    -- fat IV to sell, and it clears before a typical 30-45 DTE put expires."""
    d = days_to_earnings
    if d is None or d < 0:
        return 0.0, "no earnings date on file"
    if d <= 7:
        return -1.8, f"earnings in {d}d — gap risk, no time to react"
    if d <= 14:
        return -0.3, f"earnings in {d}d — a little close"
    if d <= 28:
        return 0.5, f"earnings in {d}d — sell into elevated IV, clears before a ~30-45 DTE put"
    if d <= 45:
        return 0.2, f"earnings in {d}d"
    return 0.0, f"earnings in {d}d — too far to matter"


def _sector_modifier(current_weight: float | None, sector: str | None) -> tuple[float, str]:
    """Reward a sector the book has little/none of; penalize piling into one
    that is already a big share of committed capital."""
    if not sector:
        return 0.0, "sector unknown"
    w = current_weight or 0.0
    if w <= 0.02:
        return 0.5, f"{sector}: not in the book yet — diversifies"
    if w < 0.15:
        return 0.2, f"{sector}: lightly held ({w * 100:.0f}% of committed capital)"
    if w < 0.30:
        return 0.0, f"{sector}: {w * 100:.0f}% of committed capital"
    if w < 0.45:
        return -0.3, f"{sector}: already {w * 100:.0f}% of the book — concentration"
    return -0.6, f"{sector}: already {w * 100:.0f}% of the book — heavy concentration"


def csp_star_score(
    comp: dict[str, Any],
    sector_current_weight: float | None,
    days_to_earnings: int | None,
) -> dict[str, Any]:
    """Turn a ticker's aggregated signals into a 0-5 star rating plus a full
    breakdown (every sub-score and modifier) for the tooltip. Pure -- the
    Combined view calls it again on re-aggregated inputs.
    """
    scores = {
        "roc": _sat(comp.get("roc_pct"), 35.0),
        "monthly_premium": _sat(comp.get("monthly_premium_pct"), 2.5),
        "ppd_yield": _sat(comp.get("ppd_yield_pct"), 25.0),
        "profit": _sat(comp.get("net_realized_pl"), 6000.0),
        "win_rate": (
            _clamp01((comp["win_rate"] - 0.4) / 0.6) if comp.get("win_rate") is not None else 0.4
        ),
        "consistency": _sat(comp.get("wheels", 0), 3.0),
        "recency": (
            math.exp(-comp["days_since_last_wheel"] / 400.0)
            if comp.get("days_since_last_wheel") is not None
            else 0.5
        ),
        "volatility": _vol_fit_score(comp.get("vol_annual_pct")),
        "price_position": _price_position_score(comp.get("price_position")),
    }
    base01 = sum(CSP_STAR_WEIGHTS[key] * scores[key] for key in CSP_STAR_WEIGHTS)
    base_stars = base01 * 5.0

    earn_mod, earn_note = _earnings_modifier(days_to_earnings)
    sector_mod, sector_note = _sector_modifier(sector_current_weight, comp.get("sector"))

    raw_stars = max(0.0, min(5.0, base_stars + earn_mod + sector_mod))
    # Whole stars, 0-5. `raw_stars` is the absolute score; the dashboard
    # re-grades it on a curve across only the tickers it actually shows (those
    # the current free cash can sell a contract on) -- see `spreadStars` in
    # app.js -- so the displayed 0-5 range tracks this list, not the whole book.
    stars = int(round(raw_stars))

    return {
        "stars": stars,
        "raw_stars": round(raw_stars, 3),
        "base_stars": round(base_stars, 2),
        "components": {
            key: {"score": round(scores[key], 3), "weight": CSP_STAR_WEIGHTS[key]}
            for key in CSP_STAR_WEIGHTS
        },
        "modifiers": {
            "earnings": {"stars": earn_mod, "note": earn_note},
            "sector": {"stars": round(sector_mod, 2), "note": sector_note},
        },
        "values": {
            "roc_pct": comp.get("roc_pct"),
            "monthly_premium_pct": comp.get("monthly_premium_pct"),
            "ppd_yield_pct": comp.get("ppd_yield_pct"),
            "net_realized_pl": comp.get("net_realized_pl"),
            "win_rate": comp.get("win_rate"),
            "wheels": comp.get("wheels"),
            "days_since_last_wheel": comp.get("days_since_last_wheel"),
            "vol_annual_pct": comp.get("vol_annual_pct"),
            "price_position": comp.get("price_position"),
        },
    }


def sector_exposure(wheels: Sequence[dict[str, Any]]) -> dict[str, float]:
    """``{sector: share of currently-committed capital}`` from the open wheels,
    for the CSP recommender's diversification nudge. Unknown-sector capital is
    bucketed under ``"Unknown"`` so the shares still sum to 1."""
    by_sector: dict[str, float] = {}
    total = 0.0
    for wheel in wheels:
        capital = wheel.get("capital_committed_now") or 0.0
        if capital <= 0:
            continue
        key = sector_of(wheel["underlying"]) or "Unknown"
        by_sector[key] = by_sector.get(key, 0.0) + capital
        total += capital
    return {key: value / total for key, value in by_sector.items()} if total else {}


# --------------------------------------------------------------------------
# CSP-candidate eligibility -- keep the list to names that are actually
# reasonable to write a cash-secured put on. Cap / volume come from the
# hand-maintained data/fundamentals.json (see wheel/reference.py); when a
# figure is missing the row still shows, flagged "unvetted", never hidden.
# --------------------------------------------------------------------------

CSP_PRICE_MIN = 10.0
CSP_PRICE_MAX = 350.0
CSP_MIN_MARKET_CAP_B = 1.0
CSP_MIN_AVG_VOL_M = 1.0
# Security types that can't be wheeled like a stock. Plain ``etf`` is *not*
# here -- ordinary ETFs (index, sector, commodity) are allowed; leveraged /
# inverse ETFs and closed-end / mutual funds are not.
_EXCLUDED_TYPES = {
    "leveraged_etf", "inverse_etf", "fund", "mutual_fund", "closed_end_fund",
    "cef", "mlp", "lp", "note", "etn",
}
_LP_IN_NAME = re.compile(r"\bL\.?\s?P\.?\b", re.IGNORECASE)


def _csp_ticker_verdict(
    ticker: str, name: str | None, last_price: float | None, fund: dict | None
) -> tuple[str | None, list[str]]:
    """``(exclude_reason, unvetted_notes)`` for one candidate ticker.

    A non-``None`` reason drops the row outright: a leveraged/inverse ETF, a
    closed-end / mutual fund, an ``LP`` in the name, a last price outside
    ``$10-$350``, or a *known* sub-$1B cap / sub-1M volume. Ordinary ETFs and
    ADRs of operating companies are allowed. ``unvetted_notes`` lists what we
    simply don't know (missing cap, missing volume, unknown security type) --
    the row stays, with the notes shown on hover. Market cap isn't asked of an
    ETF (AUM, not cap, and we're choosing to allow them).
    """
    fund = fund or {}
    kind = fund.get("type")
    cap = fund.get("market_cap_b")
    vol = fund.get("avg_vol_10d_m")
    is_etf = kind == "etf"

    if name and _LP_IN_NAME.search(name):
        return f"'{name.strip()}' looks like an LP", []
    if kind in _EXCLUDED_TYPES:
        return f"excluded security type ({kind.replace('_', ' ')})", []
    if kind is None and sector_is_leveraged_etf(ticker):
        return "leveraged/inverse ETF", []
    if kind is None and sector_is_fund(ticker):
        return "closed-end / mutual fund, not common stock", []
    if last_price is not None and not (CSP_PRICE_MIN <= last_price <= CSP_PRICE_MAX):
        return f"last price ${last_price:,.2f} outside ${CSP_PRICE_MIN:.0f}-${CSP_PRICE_MAX:.0f}", []
    if cap is not None and not is_etf and cap < CSP_MIN_MARKET_CAP_B:
        return f"market cap ${cap:.2f}B < ${CSP_MIN_MARKET_CAP_B:.0f}B", []
    if vol is not None and vol < CSP_MIN_AVG_VOL_M:
        return f"10d avg volume {vol:.2f}M < {CSP_MIN_AVG_VOL_M:.0f}M", []

    notes: list[str] = []
    if kind is None and not sector_of(ticker):  # "adr" is fine; "etf" is fine
        notes.append("security type unknown")
    if cap is None and not is_etf:
        notes.append("market cap unknown")
    if vol is None:
        notes.append("10d volume unknown")
    if last_price is None:
        notes.append("last price unknown")
    return None, notes


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

# Wheel target economics: how far past the CC strike floor the profitable-exit
# cushion sits, and how deep OTM the CSP entry cushion should sit. See
# _profit_target / _cc_strike_floor / _preferred_csp_entry_explanation below --
# three distinct concepts (profitability threshold, market-facing strike
# floor, preferred entry), never blended into one number.
PROFIT_TARGET_CUSHION_PCT = 2.0
CSP_ENTRY_OTM_PCT = 93.0


def _money(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _round_up_half(value: float) -> float:
    """Real strikes sit on 0.50-or-wider increments; round up so a floor
    stays a floor (never below what it's built from)."""
    return math.ceil(round(value / 0.5, 4)) * 0.5


def _cc_strike_floor_fundamentals(
    cost_basis: float | None,
    position_breakeven: float | None,
    wheel_breakeven: float | None,
) -> list[tuple[str, float]]:
    """The non-``None`` cost/breakeven inputs to the profit-target floor,
    labeled -- shared by `_profit_target_floor` and its explanation so the two
    can never disagree about which candidates actually fed it."""
    candidates = [
        ("cost basis", cost_basis),
        ("breakeven", position_breakeven),
        ("wheel breakeven", wheel_breakeven),
    ]
    return [(label, value) for label, value in candidates if value is not None]


def _profit_target_floor(
    cost_basis: float | None,
    position_breakeven: float | None,
    wheel_breakeven: float | None,
) -> float | None:
    """The raw (uncushioned) floor both Profit Target and CC TO EXIT are
    built from: the highest of whichever of cost basis / position breakeven /
    wheel breakeven are known. Deliberately excludes current price -- see
    `_cc_strike_floor` for the one place price enters."""
    fundamentals = _cc_strike_floor_fundamentals(cost_basis, position_breakeven, wheel_breakeven)
    return max(value for _, value in fundamentals) if fundamentals else None


def _cc_strike_floor(
    cost_basis: float | None,
    position_breakeven: float | None,
    wheel_breakeven: float | None,
    current_price: float | None,
    cushion_pct: float = PROFIT_TARGET_CUSHION_PCT,
) -> float | None:
    """The lowest strike worth writing a call at right now: never below the
    cushioned Profit Target floor (`_profit_target_floor`, then the same
    cushion `_profit_target` applies) -- a call struck below that price would
    lock in a below-target exit if assigned, defeating the point of a
    "floor" -- and never below the current price either, so a rallied stock
    still gets a market-reactive number. Whichever of the two is higher
    wins, rounded up to the next $0.50. A floor, not a recommendation -- it
    says nothing about where the premium is richest. The one shared
    implementation of this formula: `_build_cc_candidates`' `target_cc_strike`
    and the wheel-level `cc_strike_floor` both call it, and because it shares
    `_profit_target_floor` with Profit Target itself, CC TO EXIT can never
    come out below it."""
    floor = _profit_target_floor(cost_basis, position_breakeven, wheel_breakeven)
    cushioned_floor = floor * (1 + cushion_pct / 100) if floor is not None else None
    candidates = [v for v in (cushioned_floor, current_price) if v is not None]
    if not candidates:
        return None
    return _money(_round_up_half(max(candidates)))


def _profit_target(floor: float, cushion_pct: float = PROFIT_TARGET_CUSHION_PCT) -> float:
    """A profitable-exit target: a floor (see `_profit_target_floor`), marked
    up by a cushion so it reads as "worthwhile", not merely "breakeven-safe",
    then rounded up to the next $0.50."""
    return _money(_round_up_half(floor * (1 + cushion_pct / 100)))


def _profit_target_explanation(floor: float, target: float) -> str:
    """Same 4-part shape as `_cc_strike_floor_explanation`: a formula line,
    the substitution, the result, then a one-line caveat."""
    return (
        f"Profit Target = highest of cost basis, breakeven, wheel breakeven, "
        f"+{PROFIT_TARGET_CUSHION_PCT:g}%, rounded up to $0.50\n"
        f"= ${floor:.2f} + {PROFIT_TARGET_CUSHION_PCT:g}%\n"
        f"= ${target:.2f}\n\n"
        f"A profitable-exit threshold; never reacts to price."
    )


def _preferred_csp_entry_explanation(current_price: float, entry: float) -> str:
    """Same shape as the client's live cspEntryTargetTooltip (app.js) --
    this fixed-93% figure isn't shown anywhere in the UI right now (every
    surface uses the adjustable client-side version instead), but is kept
    in the same format in case a future surface reads it."""
    return (
        f"Preferred CSP entry = {CSP_ENTRY_OTM_PCT:g}% of last close, rounded down to $0.50\n"
        f"= {CSP_ENTRY_OTM_PCT:g}% × ${current_price:.2f}\n"
        f"= ${entry:.2f}\n\n"
        f"A conservative entry floor."
    )


def _cc_strike_floor_explanation(
    strike: float,
    cost_basis: float | None,
    position_breakeven: float | None,
    wheel_breakeven: float | None,
    current_price: float | None,
) -> str:
    """Short and simple, like the client's CSP TO ENTER tooltip
    (cspEntryTargetTooltip in app.js) -- not a line-by-line accounting of
    every input. Profit Target is the one figure worth naming, since it's
    already the adjacent column and its own tooltip has the cost basis /
    breakeven / wheel breakeven math behind it; repeating that here would
    just be the same numbers twice."""
    floor = _profit_target_floor(cost_basis, position_breakeven, wheel_breakeven)
    profit_target = _profit_target(floor) if floor is not None else None
    target_text = f"${profit_target:.2f}" if profit_target is not None else "n/a"
    price_text = f"${current_price:.2f}" if current_price is not None else "n/a"
    return (
        f"CC TO EXIT = higher of Profit Target and last price, rounded up to $0.50\n"
        f"= higher of {target_text} and {price_text}\n"
        f"= ${strike:.2f}\n\n"
        f"A floor, not a recommendation."
    )


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
        # `stock` split by whether a covered call is currently written against
        # the shares (see CapitalPoint.idle_stock_basis / .working_capital):
        #   idle_stock = shares held with no call -- capital not earning premium
        #   call_stock = shares held whose real cost basis backs an open call
        # These two + `call` (the strike proxy for pre-export call-backed shares)
        # are the full "shares held" story the Capital deployed chart bands.
        "idle_stock": idle_stock,
        "call_stock": round((stock or 0.0) - (idle_stock or 0.0), 2),
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
    payload["kind"] = cycle.kind  # "wheel" | "directional" | "hold"
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
            # When shares from this lot left, so the timeline can draw the
            # holding period and mark each sale/call-away.
            "disposals": [
                {
                    "date": _iso(d["date"]),
                    "shares": d["shares"],
                    "price": round(d["proceeds"] / d["shares"], 4) if d["shares"] else None,
                    "realized": _money(d["realized"]),
                }
                for d in lot.disposals
            ],
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

# Trade Log row types that move shares on or off the book (never contracts) --
# used to carry a running share count down the ledger for the break-even column.
_SHARE_ROW_TYPES = frozenset(
    {"Buy Shares", "Sell Shares", "Shares Assigned", "Shares Called Away"}
)

# Open-hedge banner: how many days before a long protective leg's expiry the
# advice flips from "keep writing premium against it" to "wind it down."
HEDGE_WIND_DOWN_DAYS = 60  # the user's "two months"
HEDGE_EXPIRING_DAYS = 7

# The whole-account and wheel-only XIRR comparisons replay the same cash-flow
# timing into each of these indices. SPY stays first (and keeps the legacy
# ``benchmark`` payload key to itself); the rest are additional lines.
BENCHMARK_TICKERS = ("SPY", "QQQ")


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


# CSP / CC / LP / LC -- the same leg-class codes the Open option positions table
# shows. `None` for a stock or dividend row.
def _code_for(right: str, short: bool) -> str:
    if short:
        return "CSP" if right == "P" else "CC"
    return "LP" if right == "P" else "LC"


def _leg_type_code(right: str | None, side: str) -> str | None:
    if right not in ("P", "C"):
        return None
    return _code_for(right, side == SHORT)


def _txn_type_code(transaction: Transaction) -> str | None:
    right = transaction.right
    if right not in ("P", "C"):
        return None
    action = transaction.action
    if action == STO:
        short = True
    elif action == BTO:
        short = False
    elif action in (BTC, ASSIGNED):
        short = True  # closing / assignment of a short leg
    elif action == STC:
        short = False  # selling to close a long leg
    elif action == EXPIRED:
        # side isn't in the action; closing a short reads +, a long reads -.
        short = (transaction.contracts or 0.0) >= 0
    else:
        return None
    return _code_for(right, short)


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
        "type_code": _txn_type_code(transaction),
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
                "type_code": _leg_type_code(leg.right, leg.side),
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
                    "type_code": _leg_type_code(leg.right, leg.side),
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
        "type_code": None,  # a stock leg, no option-class badge
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
    also_tickers: frozenset[str] | set[str] = frozenset(),
) -> dict[str, Any]:
    metrics = cycle_metrics(cycle, through, current_price=current_price, dividends=dividends)

    # A cycle holding no shares right now -- the fallback when a share-acquiring
    # row can't be matched to its own lot below.
    share_flat = sum(lot.remaining for lot in cycle.share_lots) <= 1e-9

    # Per-lot "is this row's stock gone?" -- a share-acquiring row (Buy Shares,
    # Shares Assigned) is greyed once *its own* lot is fully disposed, not only
    # once the whole cycle goes flat. A cycle that bought, sold, then bought
    # again still holds shares (share_flat is False), but the first purchase's
    # row should still read as settled. Keyed by (acquired date, lot size); when
    # two lots share a key the row greys only once *all* of them are gone (a
    # rare same-day, same-size pair the raw ledger can't tell apart anyway).
    def _disposed_index(source: str) -> dict[tuple[date, float], bool]:
        by_key: dict[tuple[date, float], list[float]] = {}
        for lot in cycle.share_lots:
            if lot.source == source:
                by_key.setdefault((lot.acquired, round(lot.shares, 4)), []).append(lot.remaining)
        return {key: all(r <= 1e-9 for r in remaining) for key, remaining in by_key.items()}

    _purchase_disposed = _disposed_index(FROM_PURCHASE)
    _assignment_disposed = _disposed_index(FROM_PUT_ASSIGNMENT)

    def _acquire_row_settled(disposed: dict[tuple[date, float], bool], when: date, shares: float) -> bool:
        return disposed.get((when, round(abs(shares), 4)), share_flat)

    def _assignment_settled(assignment) -> bool:
        if assignment.direction == "DISPOSE":
            return True  # a call-away / sale is complete on arrival
        return _acquire_row_settled(_assignment_disposed, assignment.date, assignment.shares)

    if engine_exact:
        rows = _trade_log_engine_rows(cycle)
        rows += [
            _trade_log_assignment_row(a, is_settled=_assignment_settled(a)) for a in cycle.assignments
        ]
        # Dividends are not an engine structure, so the leg/close/assignment
        # derivation above misses them -- pull them from the raw ledger by the
        # same underlying + inclusive-window rule the common path uses, so the
        # cash column (and the running break-even) stays complete.
        end = cycle.end_date or through
        rows += [
            _trade_log_raw_row(t, "Dividend", is_settled=True)
            for t in transactions
            if t.row_id in dividend_row_ids
            and (t.underlying == cycle.underlying or t.underlying in also_tickers)
            and cycle.start_date <= t.event_date <= end
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
                return _acquire_row_settled(_purchase_disposed, t.event_date, t.contracts)
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
            if (t.underlying == cycle.underlying or t.underlying in also_tickers)
            and cycle.start_date <= t.event_date <= end
            and (
                t.action in OPTION_ACTIONS
                or t.action in (BUY_STOCK, SELL_STOCK)
                or t.row_id in dividend_row_ids
            )
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
    shares_running = 0.0
    last_break_even_idx = None
    # FIFO cost-basis replay, mirroring the engine's own _sell_shares_fifo: a
    # queue of [remaining, basis_price] lots in acquisition order, so a running
    # "cost basis of shares held right now" can build up (and step down on a
    # sale/call-away) the same way the summary's cost_basis does from
    # cycle.share_lots -- just recomputed at every row instead of only today.
    # An unpriced acquisition (basis_price None) tracks its shares for the
    # count but is excluded from the weighted average, same as an unknown-
    # basis lot is excluded from the summary's cost_basis.
    lot_queue: list[list[float | None]] = []
    for idx, row in enumerate(rows):
        running += row.get("net_cash_flow") or 0.0
        row["running_cash_flow"] = _money(running)
        if row["type"] in _SHARE_ROW_TYPES:
            qty = row.get("signed_quantity") or 0.0
            shares_running += qty
            if qty > 0:
                lot_queue.append([qty, row.get("price")])
            elif qty < 0:
                to_remove = -qty
                while to_remove > 1e-9 and lot_queue:
                    lot_remaining, lot_price = lot_queue[0]
                    take = min(lot_remaining, to_remove)
                    lot_queue[0][0] -= take
                    to_remove -= take
                    if lot_queue[0][0] <= 1e-9:
                        lot_queue.pop(0)
        known_lots = [(r, p) for r, p in lot_queue if p is not None and r > 1e-9]
        known_shares = sum(r for r, _ in known_lots)
        running_cost_basis = (
            sum(r * p for r, p in known_lots) / known_shares if known_shares > 1e-9 else None
        )
        row["running_cost_basis"] = _money(running_cost_basis)
        # Break-even after this fill: the price at which, if every share on the
        # book right now were sold, the campaign's cash (premium in/out, share
        # cost, sales, dividends -- the Cumulative cash flow column) would net to
        # $0. That is just -Cumulative cash flow / shares held: any open option
        # premium sitting in the cash total is exactly offset by valuing those
        # legs at expiry, and the raw share cost cancels the tax-lot basis term,
        # so this collapses out of the same identity `break_even_price` is built
        # from below -- and the last share-holding row is snapped to that summary
        # figure once it is known (see below). A dash while the wheel holds under
        # a whole share: a break-even on fractional DRIP dust is meaningless and
        # divides a tiny denominator into noise.
        if shares_running >= 1.0 - 1e-9:
            row_be = -running / shares_running
            # <= 0 means premium/gains already banked exceed what's still held
            # -- there is no price left to reach (an insight covers it), not a
            # negative stock price. Same guard the summary's break_even_price
            # already applies; this row-level figure never had it.
            row["running_break_even"] = _money(row_be) if row_be > 0 else None
            # Profit Target's floor is the higher of running cost basis and
            # running break-even -- same two-way max the summary's
            # profit_target uses (see below), so the line this builds
            # shouldn't jump against the pinned final point the way a
            # break-even-only floor did. Never negative/zero: enough banked
            # premium to push the floor at or below $0 means there's no price
            # left to reach yet at this row (an insight covers it at the
            # summary level), not a negative target.
            floor_candidates = [v for v in (running_cost_basis, row_be) if v is not None]
            row_floor = max(floor_candidates) if floor_candidates else None
            row["running_profit_target"] = _profit_target(row_floor) if row_floor and row_floor > 0 else None
            last_break_even_idx = idx
        else:
            row["running_break_even"] = None
            row["running_profit_target"] = None

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

    # Three distinct, phase-gated concepts -- never blended into one number:
    #   wheel_phase == "cc"  (holding shares): profit_target (a stable,
    #     cost-basis-anchored profitable-exit threshold -- deliberately NOT
    #     including current_price/last_close, so it stays a fixed line price
    #     can actually close in on and cross) and cc_strike_floor (the
    #     lowest strike worth writing a call at *right now*, reusing
    #     _build_cc_candidates' target_cc_strike formula exactly, current
    #     price included -- a market-facing floor, not a recommendation).
    #   wheel_phase == "csp" (no shares yet): preferred_csp_entry (a
    #     cushion off today's last close for a fresh put).
    wheel_phase = None
    profit_target = profit_target_explanation = None
    preferred_csp_entry = preferred_csp_entry_explanation = None
    cc_strike_floor = cc_strike_floor_explanation = None
    if shares_held > 1e-9:
        wheel_phase = "cc"
        floor = _profit_target_floor(cost_basis, break_even, break_even_price)
        if floor is not None:
            profit_target = _profit_target(floor)
            profit_target_explanation = _profit_target_explanation(floor, profit_target)
        strike_floor = _cc_strike_floor(cost_basis, break_even, break_even_price, current_price)
        if strike_floor is not None:
            cc_strike_floor = strike_floor
            cc_strike_floor_explanation = _cc_strike_floor_explanation(
                strike_floor, cost_basis, break_even, break_even_price, current_price
            )
    elif current_price is not None:
        wheel_phase = "csp"
        preferred_csp_entry = _money(
            math.floor(round(current_price * (CSP_ENTRY_OTM_PCT / 100), 4) / 0.5) * 0.5
        )
        preferred_csp_entry_explanation = _preferred_csp_entry_explanation(current_price, preferred_csp_entry)

    # If the wheel still holds shares, pin the last share-holding row of the
    # ledger exactly to the summary's Break-even price. The running figure above
    # is a per-row sum of already cent-rounded cash flows, so after dozens of
    # fills it can sit a cent or two off the engine's own realized-P&L math; the
    # progression down the column stays useful, but its final value should read
    # back as the number in the summary. When that summary value is withheld
    # (an unknown-basis PRE_HISTORY lot, or premium banked already past the share
    # cost) the row follows it to a dash. A wheel that is flat now keeps its
    # earlier rows untouched -- they are the historical progression, and the
    # summary's dash is only about the present.
    if last_break_even_idx is not None and shares_held > 1e-9:
        rows[last_break_even_idx]["running_break_even"] = _money(break_even_price)
        rows[last_break_even_idx]["running_profit_target"] = profit_target
        rows[last_break_even_idx]["running_cost_basis"] = _money(cost_basis)

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
        "kind": cycle.kind,  # "wheel" | "directional" | "hold"
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
        "wheel_phase": wheel_phase,  # "cc" | "csp" | None -- current snapshot only
        "profit_target": profit_target,
        "profit_target_explanation": profit_target_explanation,
        "profit_target_cushion_pct": PROFIT_TARGET_CUSHION_PCT,
        "preferred_csp_entry": preferred_csp_entry,
        "preferred_csp_entry_explanation": preferred_csp_entry_explanation,
        "cc_strike_floor": cc_strike_floor,
        "cc_strike_floor_explanation": cc_strike_floor_explanation,
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

    label = f"long ${leg.strike:g} {kind}"
    pl_phrase = _hedge_pl_phrase(wheel_pl_now)
    losing = wheel_pl_now is not None and wheel_pl_now < -1
    intrinsic_phrase = f" (${intrinsic:,.0f} today)" if intrinsic is not None else ""

    if not cycle.is_wheel:
        headline = f"DIRECTIONAL · {days_to_expiry}d"
        if phase == "runway":
            message = (
                f"Directional {kind}, no wheel premium behind it. Theta eats its cost daily; "
                f"cut it or keep the exposure on purpose."
            )
        else:
            message = (
                f"Directional {kind}, {days_to_expiry}d left. Close for time value or hold "
                f"for the move, it finances nothing."
            )
    elif phase == "runway":
        headline = f"RUNWAY · {days_to_expiry}d"
        if losing:
            message = (
                f"Wheel is {pl_phrase}"
                f"{' with shares below cost' if shares_held > 1e-9 else ''}. Keep this hedge "
                f"and keep selling puts to carry its cost. Do not close it while the wheel "
                f"is underwater."
            )
        else:
            message = (
                f"Runway left to sell puts against this hedge, plan to sell it around two "
                f"months out to salvage its time value."
            )
    elif phase == "wind_down":
        headline = f"WIND DOWN · {days_to_expiry}d"
        message = (
            f"{days_to_expiry}d left, inside two months. Sell the hedge now to recover its "
            f"time value{intrinsic_phrase}, then stop adding puts against it."
        )
        if losing:
            message += f" Wheel is {pl_phrase}; roll to a later expiry if you still want protection."
    else:  # expiring
        headline = f"EXPIRING · {days_to_expiry}d"
        message = f"{days_to_expiry}d left, time value nearly gone{intrinsic_phrase}. Close it or let it lapse."

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


def _recent_assigned_closes(
    cycles: Sequence[Cycle],
    since: date,
    names: dict[str, str],
) -> list[dict[str, Any]]:
    """Wheel legs that were assigned (a put -> shares bought, a call -> shares
    called away) and finished closing on or after ``since`` (the calendar passes
    ~6 months back) -- flattened for the expiration calendar's faded "what just
    happened" bars. ``capital`` is the strike notional that changed hands
    (``strike x 100 x contracts``), so an assigned covered call reads the same
    size whether or not its shares were tracked in this export.
    """
    out: list[dict[str, Any]] = []
    for cycle in cycles:
        for leg in cycle.legs:
            if leg.is_open or leg.outcome != "ASSIGNED":
                continue
            if leg.strategy not in (CSP, COVERED_CALL):
                continue
            close_date = leg.close_date
            if close_date is None or close_date < since:
                continue
            strike = leg.strike or 0.0
            contracts = leg.contracts or 0.0
            out.append(
                {
                    "underlying": cycle.underlying,
                    "name": names.get(cycle.underlying),
                    "cycle_id": cycle.cycle_id,
                    "type": "CSP" if leg.strategy == CSP else "CC",
                    "strike": leg.strike,
                    "contracts": round(contracts, 4),
                    "close_date": _iso(close_date),
                    "outcome": "ASSIGNED",
                    "capital": round(strike * OPTION_MULTIPLIER * contracts, 2),
                    "realized_pl": _money(leg.realized_pl),
                }
            )
    out.sort(key=lambda r: (r["close_date"], r["underlying"]))
    return out


def _open_position_row(
    cycle: Cycle,
    leg,
    through: date,
    *,
    name: str | None,
    last_close: float | None,
    prev_close: float | None,
    cost_basis: float | None,
    wheel_breakeven: float | None,
    wheel_phase: str | None = None,
    profit_target: float | None = None,
    profit_target_explanation: str | None = None,
    preferred_csp_entry: float | None = None,
    preferred_csp_entry_explanation: str | None = None,
    cc_strike_floor: float | None = None,
    cc_strike_floor_explanation: str | None = None,
) -> dict[str, Any]:
    """One open option leg -- a short covered call / cash-secured put, or a long
    put / call (a protective hedge or a directional punt) -- framed the way the
    Open option positions table wants it. Raw numbers only; the frontend formats
    and colors.

    ``net_premium`` is the cash still standing on the un-closed portion: a credit
    (positive) for a short leg, a debit (negative) for a long one -- the sign is
    the table's "premium paid" cue, reinforced by the row background.

    ``breakeven`` is per position. Short put: ``strike - premium/share``. Short
    call: the backing shares' break-even (``cost_basis - premium/share``), or
    ``None`` when the pre-history shares carry no known basis. Long put:
    ``strike - cost/share``; long call: ``strike + cost/share`` -- the buyer's
    at-expiry break-even. ``wheel_breakeven`` is the whole cycle's campaign
    break-even price (the Trade Log's "Break-even price"), passed in from the
    already-built Trade Log and ``None`` for a cycle holding no shares yet.

    Despite the field name, ``last_close`` is the *live* last-trade price when
    Dashboard._current_prices() had one (falling back to the latest completed
    session's close otherwise) -- kept as ``last_close`` throughout this
    module and the JSON payload for API stability, not because it's still
    literally a close.

    ``moneyness_pct`` is signed so positive means the strike is out-of-the-money
    and negative means in-the-money, measured against ``last_close`` -- a raw
    geometric reading, side-agnostic; the frontend decides which sign is
    "favorable" per side. ``annualized_yield_pct`` scales the net credit over
    ``strike x 100 x contracts`` to a year over the contract's open->expiry span,
    and is ``None`` for a long leg (premium paid is a cost, not a yield on
    committed collateral).
    """
    contracts = leg.remaining_contracts
    is_put = leg.right == "P"
    is_long = leg.side == LONG
    # Cash still standing on the un-closed portion (fees already netted into every
    # cash figure the parser produces): a credit for a short leg, a debit (so a
    # negative number here) for a long one.
    net_premium = leg.open_premium
    shares = contracts * OPTION_MULTIPLIER
    premium_per_share = net_premium / shares if shares else None

    if is_long:
        cost_per_share = abs(premium_per_share or 0.0)
        breakeven = (leg.strike - cost_per_share) if is_put else (leg.strike + cost_per_share)
    elif is_put:
        breakeven = leg.strike - (premium_per_share or 0.0)
    elif cost_basis is not None:
        breakeven = cost_basis - (premium_per_share or 0.0)
    else:
        breakeven = None

    moneyness_pct = None
    in_the_money = None
    if last_close:
        cushion = (last_close - leg.strike) if is_put else (leg.strike - last_close)
        moneyness_pct = 100.0 * cushion / last_close
        in_the_money = moneyness_pct < 0

    last_close_pct = (
        100.0 * (last_close - prev_close) / prev_close
        if last_close is not None and prev_close
        else None
    )

    contract_days = (leg.expiry - leg.open_date).days
    denom = leg.strike * OPTION_MULTIPLIER * contracts
    annualized_yield_pct = (
        None
        if is_long or not denom or contract_days <= 0
        else 100.0 * (net_premium / denom) * (365.0 / contract_days)
    )

    if is_long:
        type_ = "LP" if is_put else "LC"
    else:
        type_ = "CSP" if is_put else "CC"

    # "Min. Profit Captured" -- an intrinsic-only estimate of how much of the
    # credit is banked. Intrinsic value is the *smallest* a buy-to-close could
    # cost (time value only ever adds to it), so this is an OPTIMISTIC upper
    # estimate: the real captured %, once time value is paid to close, is lower.
    # An out-of-the-money short reads 100 -- "no intrinsic left to buy back",
    # not "fully realized". None for longs, or with no last close / no credit.
    min_profit_captured_pct = None
    est_close_cost = None
    if not is_long and last_close and net_premium and net_premium > 0:
        intrinsic_per_share = (
            max(leg.strike - last_close, 0.0) if is_put else max(last_close - leg.strike, 0.0)
        )
        est_close_cost = intrinsic_per_share * shares
        min_profit_captured_pct = min(100.0, 100.0 * (net_premium - est_close_cost) / net_premium)

    return {
        "cycle_id": cycle.cycle_id,
        "underlying": cycle.underlying,
        "name": name,
        "type": type_,
        "side": "LONG" if is_long else "SHORT",
        "right": leg.right,
        "strike": leg.strike,
        "expiration": _iso(leg.expiry),
        "days_to_expiry": (leg.expiry - through).days,
        "open_date": _iso(leg.open_date),
        "contracts": round(contracts, 4),
        # Signed like the Trade Log's signed_quantity: a long leg reads positive,
        # a short leg negative. Not color-coded on the frontend.
        "signed_contracts": round(contracts if is_long else -contracts, 4),
        "open_price": leg.open_price,
        "net_premium": _money(net_premium),
        "breakeven": _money(breakeven),
        "wheel_breakeven": _money(wheel_breakeven),
        "wheel_phase": wheel_phase,
        "profit_target": _money(profit_target),
        "profit_target_explanation": profit_target_explanation,
        "preferred_csp_entry": _money(preferred_csp_entry),
        "preferred_csp_entry_explanation": preferred_csp_entry_explanation,
        "cc_strike_floor": _money(cc_strike_floor),
        "cc_strike_floor_explanation": cc_strike_floor_explanation,
        "moneyness_pct": round(moneyness_pct, 2) if moneyness_pct is not None else None,
        "in_the_money": in_the_money,
        "last_close": _money(last_close),
        "last_close_pct": round(last_close_pct, 2) if last_close_pct is not None else None,
        "annualized_yield_pct": (
            round(annualized_yield_pct, 2) if annualized_yield_pct is not None else None
        ),
        "collateral": None if is_long else _money(denom),
        "cost_basis": _money(cost_basis),
        "shares_tracked": leg.shares_tracked,
        "min_profit_captured_pct": (
            round(min_profit_captured_pct, 1) if min_profit_captured_pct is not None else None
        ),
        "est_close_cost": _money(est_close_cost),
    }


def _no_contract_open_position_row(
    wheel: dict[str, Any],
    gap_pct: float | None,
    earnings_row: dict[str, Any] | None,
    today: date,
    prev_close: float | None,
) -> dict[str, Any]:
    """A position with no open option leg right now: shares held with
    nothing written against them (a wheel awaiting a call, a plain
    buy-and-hold lot, assigned shares never covered), or a wheel with a
    current phase but no leg at all (e.g. between cycles, ready for a fresh
    CSP entry). Shaped exactly like `_open_position_row`'s return so the
    merged Open option positions table needs no special-casing beyond the
    null checks it already makes for missing target fields. Wheel-level
    fields (cost basis, wheel breakeven, shares held) come straight off
    ``wheel``, the same Trade Log wheel dict `_build_open_positions` already
    draws its real leg rows' wheel-level fields from; ``gap_pct`` is the one
    figure only the Wheel price targets banner computes (so it's `None` for
    a position with no phase target), passed in rather than redone here.

    Two fields that look leg-specific are filled anyway because they aren't:
    ``last_close_pct`` is the ticker's own daily move, unrelated to any
    contract, computed the same way `_open_position_row` computes it. And
    while shares are held, ``breakeven`` is the raw cost basis --
    `_open_position_row`'s own CC formula, cost basis minus this leg's
    premium/share, with that premium at its natural zero since there is no
    leg. Every other leg-specific field (strike, expiration, moneyness,
    yield, collateral, quantity, premium, ...) has no such contract-free
    reading and stays `None`, read by the frontend as the "no current
    contract" dash.
    """
    earnings_date = (earnings_row or {}).get("earnings_date")
    current_price = wheel.get("current_price")
    last_close_pct = (
        100.0 * (current_price - prev_close) / prev_close
        if current_price is not None and prev_close
        else None
    )
    cost_basis = wheel.get("cost_basis_per_share")
    shares_held = wheel.get("shares_held") or 0.0
    breakeven = cost_basis if shares_held > 1e-9 and cost_basis is not None else None
    return {
        "cycle_id": wheel["cycle_id"],
        "underlying": wheel["underlying"],
        "name": wheel.get("name"),
        "type": None,
        "side": None,
        "right": None,
        "strike": None,
        "expiration": None,
        "days_to_expiry": None,
        "open_date": None,
        "contracts": None,
        "signed_contracts": None,
        "open_price": None,
        "net_premium": None,
        "breakeven": _money(breakeven),
        "wheel_breakeven": _money(wheel.get("break_even_price")),
        "wheel_phase": wheel.get("wheel_phase"),
        "profit_target": wheel.get("profit_target"),
        "profit_target_explanation": wheel.get("profit_target_explanation"),
        "preferred_csp_entry": wheel.get("preferred_csp_entry"),
        "preferred_csp_entry_explanation": wheel.get("preferred_csp_entry_explanation"),
        "cc_strike_floor": wheel.get("cc_strike_floor"),
        "cc_strike_floor_explanation": wheel.get("cc_strike_floor_explanation"),
        "moneyness_pct": None,
        "in_the_money": None,
        "last_close": _money(current_price),
        "last_close_pct": round(last_close_pct, 2) if last_close_pct is not None else None,
        "annualized_yield_pct": None,
        "collateral": None,
        "cost_basis": _money(cost_basis),
        "shares_tracked": None,
        "min_profit_captured_pct": None,
        "est_close_cost": None,
        "earnings_date": earnings_date,
        "days_to_earnings": (date.fromisoformat(earnings_date) - today).days if earnings_date else None,
        "gap_pct": gap_pct,
        "shares_held": wheel.get("shares_held"),
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

# How long Dashboard._current_prices() trusts its own in-memory live-quote
# fetch before re-fetching. Long enough that a burst of requests (Combined's
# six-plus per-account builds, a user clicking through filters) shares one
# fetch; short enough that a live price left open in a browser tab all
# session doesn't read stale minutes into the close. Matches the frontend's
# LIVE_PRICE_POLL_MS (wheel/static/app.js) so its periodic poll actually
# lands a fresh quote each time instead of re-reading this cache.
_LIVE_PRICE_TTL_SECONDS = 15.0


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
                    + f", this dashboard is scoped to account {account_number}"
                ]
        self._net_worth = self._build_net_worth()
        self._benchmark = self._build_benchmark()
        # Lazily populated on the first build() call and reused for
        # _LIVE_PRICE_TTL_SECONDS at a time, not once per Dashboard instance --
        # this Dashboard object is reused for the registry's whole lifetime
        # (many page loads over hours or days), and a live price actually
        # moves through the session, unlike a daily close. The TTL keeps
        # repeated filter changes / Combined's six-plus per-account builds
        # from each paying for their own fetch, without freezing the mark at
        # whatever it was on the first request after the last data-file-
        # triggered rebuild.
        self._price_cache: dict[str, float | None] | None = None
        self._price_cache_at: float = 0.0
        # Prior trading day's close per ticker, filled in beside _price_cache --
        # the "Last Close %" column of the Open option positions table.
        self._prev_closes: dict[str, float | None] = {}
        self._price_warnings: list[str] = []
        # The _price_cache_at generation the price-derived caches below were
        # last built against -- see the comment further down where they're
        # declared for why this exists (in short: _current_prices() now
        # refreshes on its own TTL, and these must not go on quietly reusing
        # a mark from several refreshes ago just because they're non-None).
        self._price_marked_at: float | None = None
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
        # Filter-independent (built from all_cycles), so cached across build()
        # calls that land within the same _current_prices() TTL window --
        # invalidated (see build()) whenever that refreshes, since every one
        # of these is marked to current_prices and would otherwise go on
        # showing the first live price this Dashboard instance ever fetched,
        # forever, no matter how many refreshes _current_prices() itself does.
        self._trade_log: dict[str, Any] | None = None
        self._open_hedges: list[dict[str, Any]] | None = None
        self._open_positions: list[dict[str, Any]] | None = None
        self._cc_candidates: list[dict[str, Any]] | None = None
        self._csp_candidates: list[dict[str, Any]] | None = None

    # ---- market data ----

    def _current_prices(self) -> dict[str, float | None]:
        """Live last-trade price for every ticker this dashboard holds open
        shares or an open option leg in, falling back to the latest completed
        session's close for any ticker the live quote can't answer.

        Cached for ``_LIVE_PRICE_TTL_SECONDS``, not once per Dashboard
        instance -- see the comment in __init__. A ticker whose every fetch
        fails yields ``None`` for that ticker only (wheel.marketdata never
        raises), which flows through to that cycle's stock_unrealized_pl as
        "unavailable," not a crash.

        Two passes for the daily-close series so the common case pays nothing
        for threads: first resolve every ticker that a fresh cache or the
        in-process memo can answer without network (``local_only=True``),
        then fan the genuine misses -- typically only the first build after a
        trading session closes -- out across a thread pool, since each is an
        independent network round trip (its own URL, its own cache file under
        ``data/prices/``). A cold pull of a few dozen tickers one at a time
        turned a single-digit-second page load into a multi-second one;
        spinning the pool up when there is nothing to fetch was itself
        costing ~1.5s per Combined build. The live quotes on top of that are
        one extra batched HTTP round trip for the whole ticker list, not
        per-ticker.
        """
        now = time.monotonic()
        if self._price_cache is not None and now - self._price_cache_at < _LIVE_PRICE_TTL_SECONDS:
            return self._price_cache

        # Every ticker with open shares *or* an open option leg -- the latter so a
        # pure cash-secured-put wheel (no shares yet) still gets a current mark for
        # the Open option positions table and the hedge banner.
        tickers = sorted(
            {
                cycle.underlying
                for cycle in self.all_cycles
                if any(lot.remaining > 1e-9 for lot in cycle.share_lots)
                or any(leg.is_open for leg in cycle.legs)
            }
        )
        prev: dict[str, float | None] = {}
        warnings: list[str] = []
        misses: list[str] = []

        def _record(ticker: str, points) -> None:
            # The latest *completed* session's close (see wheel.marketdata's
            # session-aware staleness) -- "prev" now that the live quote below
            # is the actual current mark, and the fallback current mark itself
            # when no live quote is available for this ticker.
            prev[ticker] = points[-1].close if points else None

        for ticker in tickers:
            local = marketdata.get_price_series(ticker, local_only=True)
            if local is None:
                misses.append(ticker)
                continue
            points, ticker_warnings = local
            warnings.extend(ticker_warnings)
            _record(ticker, points)

        if misses:
            with ThreadPoolExecutor(max_workers=min(8, len(misses))) as pool:
                for ticker, (points, ticker_warnings) in zip(misses, pool.map(marketdata.get_price_series, misses)):
                    warnings.extend(ticker_warnings)
                    _record(ticker, points)

        live, live_warnings = marketdata.get_last_prices(tickers)
        warnings.extend(live_warnings)
        prices: dict[str, float | None] = {
            ticker: live.get(ticker) if live.get(ticker) is not None else prev.get(ticker)
            for ticker in tickers
        }

        self._price_cache = prices
        self._price_cache_at = now
        self._prev_closes = prev
        self._price_warnings = warnings
        return prices

    def _price_stats(self, tickers: Sequence[str]) -> dict[str, dict[str, float | None]]:
        """Per-ticker ``{"last", "vol_annual_pct", "price_position"}`` -- the same
        two-pass fetch ``_current_prices`` documents (cache/memo first, then a
        thread pool for misses), for an arbitrary list. The CSP-candidates
        recommender wants a current mark *and* two shape features for tickers
        this account is no longer in, so they aren't in ``_current_prices``'s
        set. Fetch warnings append to ``self._price_warnings``.

        * ``last`` -- the live last-trade price when the quote fetch answers
          for this ticker, falling back to its latest completed session's
          close otherwise (see :func:`wheel.marketdata.get_last_prices`).
        * ``vol_annual_pct`` -- stdev of the last ~30 daily log returns,
          annualized (x sqrt(252)); "how much premium is on the table."
        * ``price_position`` -- where the last close sits in the trailing
          ~1y range, 0 (at the low) to 1 (at the high); flags a falling knife.
          Computed off the daily-close series, not the live price -- a shape
          stat, not a precise current-price readout.
        """
        out: dict[str, dict[str, float | None]] = {}
        misses: list[str] = []

        def _compute(ticker: str, points) -> None:
            if not points:
                out[ticker] = {"last": None, "vol_annual_pct": None, "price_position": None}
                return
            closes = [p.close for p in points]
            last = closes[-1]
            vol = None
            window = [c for c in closes[-31:] if c > 0]
            if len(window) >= 6:
                rets = [math.log(window[i] / window[i - 1]) for i in range(1, len(window))]
                if len(rets) >= 5:
                    vol = statistics.pstdev(rets) * math.sqrt(252) * 100.0
            year = closes[-252:]
            pos = None
            if len(year) >= 20:
                lo, hi = min(year), max(year)
                if hi - lo > 1e-9:
                    pos = (last - lo) / (hi - lo)
            out[ticker] = {
                "last": last,
                "vol_annual_pct": round(vol, 1) if vol is not None else None,
                "price_position": round(pos, 3) if pos is not None else None,
            }

        for ticker in tickers:
            local = marketdata.get_price_series(ticker, local_only=True)
            if local is None:
                misses.append(ticker)
                continue
            points, warns = local
            self._price_warnings.extend(warns)
            _compute(ticker, points)
        if misses:
            with ThreadPoolExecutor(max_workers=min(8, len(misses))) as pool:
                for ticker, (points, warns) in zip(misses, pool.map(marketdata.get_price_series, misses)):
                    self._price_warnings.extend(warns)
                    _compute(ticker, points)

        live, live_warnings = marketdata.get_last_prices(tickers)
        self._price_warnings.extend(live_warnings)
        for ticker, price in live.items():
            if price is not None and ticker in out:
                out[ticker]["last"] = price
        return out

    def _fundamentals(self, tickers: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Per-ticker ``{"type", "market_cap_b", "avg_vol_10d_m",
        "earnings_date"}`` for the CSP- / CC-candidate filters.

        Fetched from Yahoo and cached (``marketdata.get_fundamentals``), then
        overlaid with the hand-maintained ``data/fundamentals.json`` and
        ``data/earnings.json`` -- a non-``None`` field there wins, so a wrong
        or missing fetched value can always be corrected by hand without
        having to fill the rest in. Fetch warnings append to
        ``self._price_warnings``.
        """
        fetched, warns = marketdata.get_fundamentals(sorted({t for t in tickers if t}))
        self._price_warnings.extend(warns)
        fund_overrides = load_fundamentals()
        earn_overrides = load_earnings()

        out: dict[str, dict[str, Any]] = {}
        for ticker in tickers:
            f = fetched.get(ticker)
            row: dict[str, Any] = {
                "type": f.kind if f else None,
                "market_cap_b": f.market_cap_b if f else None,
                "avg_vol_10d_m": f.avg_vol_10d_m if f else None,
                "earnings_date": f.earnings_date if f else None,
            }
            for key, value in (fund_overrides.get(ticker) or {}).items():
                if key in row and value is not None:
                    row[key] = value
            if ticker in earn_overrides:
                row["earnings_date"] = earn_overrides[ticker]
            out[ticker] = row
        return out

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
                + "); showing the most recently seen, put each account in its own "
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

        # How much of the account's *equity* market value the transaction-
        # history model has no real share lot for at all -- a position bought
        # before every loaded export begins, with nothing since but maybe a
        # stray dividend-reinvestment fraction of a share. The Positions
        # snapshot still reports its full market value in `equity_value` (and
        # therefore `total_value`), but with no real share count to attach a
        # cost basis to, `wheel_capital_deployed` counts essentially none of
        # it -- the single biggest reason "True capital deployed" can
        # undercount `total_value` by a wide margin even for a fully-tracked
        # wheel. Compared per ticker so a *partially* tracked position (e.g.
        # 30 of 60 real shares visible) only contributes its untracked
        # fraction, not the whole position.
        tracked_shares: dict[str, float] = {}
        for cycle in self.all_cycles:
            for lot in cycle.share_lots:
                tracked_shares[cycle.underlying] = tracked_shares.get(cycle.underlying, 0.0) + lot.remaining
        untracked_equity_value = 0.0
        for row in latest.rows:
            if row.kind != EQUITY or not row.quantity or not row.current_value:
                continue
            untracked_shares = max(row.quantity - tracked_shares.get(row.symbol, 0.0), 0.0)
            untracked_equity_value += row.current_value * (untracked_shares / row.quantity)

        account_history = sorted(
            (s for s in self.snapshots if s.account_number == latest.account_number),
            key=lambda snapshot: snapshot.as_of,
        )

        timeline = [
            {
                "as_of": snapshot.as_of.date().isoformat(),
                "total_value": _money(snapshot.total_value),
                "cash_total": _money(snapshot.cash_total),
            }
            for snapshot in account_history
        ]
        # A configured opening balance (data/accounts.json's "opening_balances")
        # that reaches earlier than any real snapshot -- same splice as
        # _build_benchmark()'s `series`, so the Capital deployed chart's
        # Cash + Unrealized shading has a starting point to draw from too, not
        # just the growth-over-time comparison. Booked entirely as cash, since
        # a single manual balance carries no real cash/equity split, and
        # flagged `estimated` so the frontend can render it distinctly (the
        # same dashed/faded "this figure is a guess" treatment already used
        # for an estimated capital band) rather than silently blending a
        # guess in among real, itemized Positions data.
        if account_history and self.opening_balance and self.opening_balance[0] < account_history[0].as_of.date():
            opening_day, opening_value = self.opening_balance
            timeline.insert(
                0,
                {
                    "as_of": opening_day.isoformat(),
                    "total_value": _money(opening_value),
                    "cash_total": _money(opening_value),
                    "estimated": True,
                },
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
            "untracked_equity_value": _money(untracked_equity_value),
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
            "timeline": timeline,
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
                    "needed to compute a return, only one is available so far (or add an "
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

        valuation_dates = sorted({opening_day, *(snapshot.as_of.date() for snapshot in account_snapshots)})
        actual_terminal_value = account_snapshots[-1].total_value

        # One replay per benchmark index, all against the same cash-flow timing.
        index_values: dict[str, dict[date, float | None]] = {}
        benchmark_entries: list[dict[str, Any]] = []
        for ticker in BENCHMARK_TICKERS:
            points, index_warnings = marketdata.get_price_series(ticker)
            warnings.extend(index_warnings)
            values = bm.simulate_benchmark_series(
                all_events,
                valuation_dates,
                lambda day, _points=points: marketdata.price_on_or_before(_points, day),
            )
            index_values[ticker] = values
            index_result = bm.compare_to_benchmark(
                all_events, actual_terminal_value, values.get(as_of), as_of
            )
            benchmark_entries.append(
                {
                    "name": ticker,
                    "terminal_value": _money(index_result.benchmark_terminal_value),
                    "xirr_pct": _money(index_result.benchmark_xirr_pct),
                    "value_added": _money(index_result.value_added),
                }
            )

        primary = BENCHMARK_TICKERS[0]
        result = bm.compare_to_benchmark(
            all_events, actual_terminal_value, index_values[primary].get(as_of), as_of
        )

        def _series_point(day: date, actual: float) -> dict[str, Any]:
            point = {"as_of": day.isoformat(), "actual_value": _money(actual)}
            # Legacy key = the primary index; one extra key per additional index.
            point["benchmark_value"] = _money(index_values[primary].get(day))
            for ticker in BENCHMARK_TICKERS[1:]:
                point[f"benchmark_value_{ticker.lower()}"] = _money(index_values[ticker].get(day))
            return point

        series = [_series_point(s.as_of.date(), s.total_value) for s in account_snapshots]
        if opening_day < earliest_real:
            # A configured opening point that reaches earlier than any real
            # snapshot -- give the growth-over-time chart a starting point to
            # draw from too, not just the return math above.
            series.insert(0, _series_point(opening_day, opening_value))

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
            "benchmark": benchmark_entries[0],
            "benchmarks": benchmark_entries,
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
                    "not enough wheel activity yet to compute a return, need at least one "
                    "full open/close (or assignment) on record"
                ],
            }

        through = self.last_date or date.today()
        terminal_value = wheel_terminal_value(self.all_cycles, through, current_prices)
        cash_flow_events = [
            bm.CashFlowEvent(date=event_date, amount=amount, label=label, source="wheel", kind="WHEEL")
            for event_date, amount, label in events
        ]

        warnings: list[str] = []
        benchmark_entries: list[dict[str, Any]] = []
        result = None
        for ticker in BENCHMARK_TICKERS:
            points, index_warnings = marketdata.get_price_series(ticker)
            warnings.extend(index_warnings)
            index_terminal = bm.simulate_benchmark(
                cash_flow_events,
                through,
                lambda day, _points=points: marketdata.price_on_or_before(_points, day),
            )
            index_result = bm.compare_to_benchmark(
                cash_flow_events, terminal_value, index_terminal, through
            )
            if result is None:
                result = index_result  # the primary index drives the headline
            benchmark_entries.append(
                {
                    "name": ticker,
                    "terminal_value": _money(index_result.benchmark_terminal_value),
                    "xirr_pct": _money(index_result.benchmark_xirr_pct),
                    "value_added": _money(index_result.value_added),
                }
            )

        if result.actual_xirr_pct is None:
            return {
                "available": False,
                "warnings": [
                    "wheel cash flows don't yet have enough sign variation (money in AND "
                    "money out) to solve for a rate, typically means every tracked "
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
            "benchmark": benchmark_entries[0],
            "benchmarks": benchmark_entries,
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

        # A corporate action renamed one ticker into another mid-campaign (see
        # WheelEngine._merge_renamed_cycle): the engine folded both into the new
        # ticker's cycle, so the raw-transaction filter below has to accept the
        # old ticker's rows too or the pre-rename legs go missing.
        former_of: dict[str, set[str]] = {}
        for old, new in getattr(self.engine, "_ticker_alias", {}).items():
            former_of.setdefault(new, set()).add(old)

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
                            "overlap in time, Trade Log attributed them via the engine"
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
                also_tickers=former_of.get(cycle.underlying, frozenset()),
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

    def _build_open_positions(
        self,
        current_prices: dict[str, float | None],
        prev_closes: dict[str, float | None],
        wheels: Sequence[dict[str, Any]] = (),
    ) -> list[dict[str, Any]]:
        """One row per open option leg across ``all_cycles`` -- every covered
        call and cash-secured put still on the books, plus every open long put /
        call (a protective hedge or a directional punt). Buy-and-hold share lots
        are left out; the long legs are the same ones the hedge banner reasons
        about, shown here as compact data rows with an amber row background
        marking that their premium was *paid*, not received.

        ``wheels`` is the already-built Trade Log's ``wheels`` list; each row picks
        its cycle's campaign break-even price out of it rather than recomputing.

        Filter-independent, same reasoning as the Trade Log and hedge banner: an
        open contract needs watching whatever date window is on screen. Sorted by
        underlying then expiry so a symbol's rows sit together and read
        soonest-first within the group.
        """
        through = self.last_date or date.today()
        wheel_breakeven = {w["cycle_id"]: w.get("break_even_price") for w in wheels}
        wheel_target = {w["cycle_id"]: w for w in wheels}
        rows: list[dict[str, Any]] = []
        for cycle in self.all_cycles:
            held = [
                lot
                for lot in cycle.share_lots
                if lot.remaining > 1e-9 and lot.basis_known and lot.basis_per_share is not None
            ]
            cost_basis = (
                sum(lot.basis_per_share * lot.remaining for lot in held)
                / sum(lot.remaining for lot in held)
                if held
                else None
            )
            for leg in cycle.legs:
                if not leg.is_open or leg.expiry is None:
                    continue
                short_wheel_leg = leg.side == SHORT and leg.strategy in (CSP, COVERED_CALL)
                if not (short_wheel_leg or leg.side == LONG):
                    continue
                rows.append(
                    _open_position_row(
                        cycle,
                        leg,
                        through,
                        name=self._company_names.get(cycle.underlying),
                        last_close=current_prices.get(cycle.underlying),
                        prev_close=prev_closes.get(cycle.underlying),
                        cost_basis=cost_basis,
                        wheel_breakeven=wheel_breakeven.get(cycle.cycle_id),
                        wheel_phase=wheel_target.get(cycle.cycle_id, {}).get("wheel_phase"),
                        profit_target=wheel_target.get(cycle.cycle_id, {}).get("profit_target"),
                        profit_target_explanation=wheel_target.get(cycle.cycle_id, {}).get(
                            "profit_target_explanation"
                        ),
                        preferred_csp_entry=wheel_target.get(cycle.cycle_id, {}).get("preferred_csp_entry"),
                        preferred_csp_entry_explanation=wheel_target.get(cycle.cycle_id, {}).get(
                            "preferred_csp_entry_explanation"
                        ),
                        cc_strike_floor=wheel_target.get(cycle.cycle_id, {}).get("cc_strike_floor"),
                        cc_strike_floor_explanation=wheel_target.get(cycle.cycle_id, {}).get(
                            "cc_strike_floor_explanation"
                        ),
                    )
                )
        rows.sort(key=lambda r: (r["underlying"], r["expiration"] or "", r["strike"] or 0.0))
        return rows

    def _build_earnings_in_view(
        self,
        open_positions: Sequence[dict[str, Any]],
        wheels: Sequence[dict[str, Any]],
        through: date,
    ) -> dict[str, Any]:
        """Next earnings date for every ticker that currently has an open option
        leg or held shares, plus a short "reports within 7 days" list for the
        Planner header.

        The dates come straight from :meth:`_fundamentals` (the same Yahoo fetch
        + ``data/earnings.json`` override the CC-/CSP-candidate tables already
        use -- no extra network call). ``before_expiry`` is the wheel-relevant
        bit: a report that lands on or before an open leg's expiry is a gap-risk
        the position can't dodge, so the expiration calendar flags that week and
        the workflow "Evaluate" bucket keys off it.
        """
        legs_by_ticker: dict[str, list[str]] = {}
        for row in open_positions:
            if row.get("expiration"):
                legs_by_ticker.setdefault(row["underlying"], []).append(row["expiration"])
        held = {w["underlying"] for w in wheels if (w.get("shares_held") or 0.0) > 1e-9}
        tickers = sorted(set(legs_by_ticker) | held)
        if not tickers:
            return {"tickers": [], "within_7d": []}

        fundamentals = self._fundamentals(tickers)
        out: list[dict[str, Any]] = []
        for ticker in tickers:
            earn = (fundamentals.get(ticker) or {}).get("earnings_date")
            if not earn:
                continue
            days = (earn - through).days
            if days < 0:
                continue  # a stale date the fetch hasn't refreshed yet
            expiries = sorted(legs_by_ticker.get(ticker, []))
            before_expiry = any(earn.isoformat() <= exp for exp in expiries)
            out.append(
                {
                    "ticker": ticker,
                    "earnings_date": earn.isoformat(),
                    "days_to_earnings": days,
                    "before_expiry": before_expiry,
                    "soonest_leg_expiry": expiries[0] if expiries else None,
                }
            )
        out.sort(key=lambda r: r["days_to_earnings"])
        return {
            "tickers": out,
            "within_7d": [r["ticker"] for r in out if r["days_to_earnings"] <= 7],
        }

    def _real_share_quantities(self) -> dict[str, float]:
        """Each ticker's real total share count, straight off the latest
        Portfolio Positions snapshot -- the broker's own count, unfiltered by
        how much of it the wheel/cycle model can trace to a known lot. Shared
        by `_build_cc_candidates` below (a covered call can be written
        against shares with no known cost basis just as well as ones with
        one -- the broker doesn't check) and by the Open option positions
        payload's `shares_untracked` enrichment (build()); `{}` when there's
        no Positions snapshot to read at all.
        """
        if not self._net_worth.get("available"):
            return {}
        totals: dict[str, float] = {}
        for row in self._net_worth.get("positions", []):
            if row.get("kind") == EQUITY and row.get("symbol"):
                totals[row["symbol"]] = totals.get(row["symbol"], 0.0) + (row.get("quantity") or 0.0)
        return totals

    def _build_cc_candidates(
        self,
        wheels: Sequence[dict[str, Any]],
        open_positions: Sequence[dict[str, Any]],
        real_qty_by_symbol: dict[str, float],
    ) -> list[dict[str, Any]]:
        """Every position holding shares with no covered call currently written
        against it. Positions with >= 100 shares are the actionable ones -- a
        covered call could be written -- and carry a ``target_cc_strike`` and a
        (negative) ``contracts_available``. Positions under 100 shares are still
        listed for visibility, with those two fields ``None``. Built from the
        already-assembled Trade Log ``wheels`` (share count, cost basis, both
        break-evens, last close) and ``open_positions`` (which cycles already
        have a live CC leg); filter-independent like both.

        The 100-share threshold (and ``contracts_available``) is checked
        against ``real_qty_by_symbol`` -- the broker's real count -- not just
        ``wheel["shares_held"]`` (the model's own, cost-basis-traceable
        count): writing a covered call needs real shares in the account, not
        a provable cost basis on them. A ticker with more real shares than
        tracked ones carries the gap as ``shares_untracked``; the tracked
        count still drives ``target_cc_strike`` and the P&L columns, since
        there is no cost basis to price the untracked shares against.

        ``target_cc_strike`` is the lowest strike worth writing a call at: the
        greatest of the share cost basis, the position break-even (cost basis
        net of premium already banked on these lots), the whole-wheel
        break-even, and the last close, then rounded **up** to the next $0.50
        (real strikes sit on 0.50-or-wider increments, and rounding up keeps it
        a valid floor). At or above it an assignment sells the stock for at
        least what it cost, on top of every premium already collected, and
        never below the current market. It is a floor, not a recommendation --
        it says nothing about where the premium is richest.

        ``unrealized_pl`` / ``unrealized_pl_pct`` are the shares' total gain or
        loss against their raw average cost basis (premium already banked is
        deliberately *not* netted in), marked to the last close. ``sector``
        comes from ``wheel.reference``; ``earnings_date`` / ``days_to_earnings``
        from :meth:`_fundamentals` (Yahoo, cached, hand-file override).
        """
        cc_cycle_ids = {p["cycle_id"] for p in open_positions if p.get("type") == "CC"}
        # Same "tracked or real" share count the row loop below computes,
        # just ahead of time -- a wheel with 0 tracked shares but a real,
        # open position (JXN) still needs its sector/earnings prefetched.
        fundamentals = self._fundamentals(
            [
                w["underlying"]
                for w in wheels
                if (w.get("shares_held") or 0.0) > 1e-9
                or (w.get("status") != "CLOSED" and (real_qty_by_symbol.get(w["underlying"]) or 0.0) > 1e-9)
            ]
        )
        today = date.today()
        rows: list[dict[str, Any]] = []
        for wheel in wheels:
            if wheel["cycle_id"] in cc_cycle_ids:
                continue
            tracked_shares = wheel.get("shares_held") or 0.0
            # The real count wins when it's larger -- e.g. shares bought
            # before every loaded export begins still exist in the account
            # and can still back a call, even with no cost basis on file for
            # them, or (JXN: 0 tracked, 300 real) an assignment the engine
            # never saw at all. It never loses to the tracked count: a real
            # snapshot that's simply older than the latest trades
            # undercounting would wrongly hide an otherwise-actionable
            # position. Only an open cycle can claim the real count -- a
            # CLOSED wheel has sold out in the model's own terms, and a
            # ticker keeps at most one open cycle at a time, so this can't
            # double-count the same real shares onto two rows.
            real_shares = real_qty_by_symbol.get(wheel["underlying"])
            is_open = wheel.get("status") != "CLOSED"
            shares = (
                real_shares
                if is_open and real_shares is not None and real_shares > tracked_shares
                else tracked_shares
            )
            if shares <= 1e-9:
                continue
            untracked_shares = shares - tracked_shares if shares > tracked_shares else 0.0
            meets_threshold = shares >= 100 - 1e-9
            cost_basis = wheel.get("cost_basis_per_share")
            last_close = wheel.get("current_price")
            target = (
                _cc_strike_floor(cost_basis, wheel.get("break_even_per_share"), wheel.get("break_even_price"), last_close)
                if meets_threshold
                else None
            )
            # Total unrealized gain/loss on the *tracked* shares vs. their raw
            # average cost basis (not a break-even -- premium already banked
            # is not netted in here), marked to the last close. Left off the
            # untracked ones: there is no cost basis on file to measure a
            # gain or loss against.
            has_marks = cost_basis is not None and last_close is not None
            gain = round(tracked_shares * (last_close - cost_basis), 2) if has_marks else None
            gain_pct = (
                round(100.0 * (last_close - cost_basis) / cost_basis, 2)
                if has_marks and cost_basis
                else None
            )
            rows.append(
                {
                    "cycle_id": wheel["cycle_id"],
                    "underlying": wheel["underlying"],
                    "name": wheel.get("name"),
                    "is_wheel": bool(wheel.get("is_wheel")),
                    # Shown in the "Wheel" column; a plain buy-and-hold lot has none.
                    "wheel": wheel["cycle_id"] if wheel.get("is_wheel") else None,
                    "shares_held": round(shares, 4),
                    "shares_untracked": round(untracked_shares, 4) if untracked_shares > 1e-6 else None,
                    "meets_threshold": meets_threshold,
                    # The covered-call position that could be opened, so it reads
                    # negative (a short call), e.g. 175 shares -> -1. None when
                    # there aren't 100 shares to cover a contract.
                    "contracts_available": -(int(shares // 100)) if meets_threshold else None,
                    "cost_basis_per_share": cost_basis,
                    "breakeven": wheel.get("break_even_per_share"),
                    "wheel_breakeven": wheel.get("break_even_price"),
                    "last_close": last_close,
                    "target_cc_strike": target,
                    "unrealized_pl": gain,
                    "unrealized_pl_pct": gain_pct,
                    "sector": sector_of(wheel["underlying"]),
                    "earnings_date": (
                        earn.isoformat()
                        if (earn := (fundamentals.get(wheel["underlying"], {}) or {}).get("earnings_date"))
                        else None
                    ),
                    "days_to_earnings": (
                        (earn - today).days
                        if (earn := (fundamentals.get(wheel["underlying"], {}) or {}).get("earnings_date"))
                        else None
                    ),
                }
            )
        # Actionable positions first, then the sub-100 lots, each alphabetical.
        rows.sort(key=lambda r: (not r["meets_threshold"], r["underlying"]))
        return rows

    def _build_csp_candidates(
        self,
        wheels: Sequence[dict[str, Any]],
        exposure: dict[str, float],
    ) -> list[dict[str, Any]]:
        """Tickers this account has wheeled **profitably** in the past -- names
        worth a fresh cash-secured put -- one row per underlying, summed over
        every wheel on it, keeping only the net-positive ones. Each carries its
        latest close (so the frontend can size it against free cash) and a
        0-5 **``stars``** rating with a full ``star_breakdown`` -- see
        :func:`csp_star_score`. ``exposure`` is :func:`sector_exposure` for the
        book, feeding the diversification nudge.
        """
        by_ticker: dict[str, dict[str, Any]] = {}
        for wheel in wheels:
            is_wheel = bool(wheel.get("is_wheel"))
            has_options = bool(wheel.get("gross_premium_received") or wheel.get("option_realized_pl"))
            # Full wheels *and* bare option cycles (a CSP sold and closed, never
            # assigned) both count -- a put that had to be bought back at a loss
            # is exactly the history that should drag a ticker's rating down.
            # Pure buy-and-hold stock cycles (no premium either way) don't.
            if not is_wheel and not has_options:
                continue
            agg = by_ticker.setdefault(
                wheel["underlying"],
                {
                    "net_realized_pl": 0.0,
                    "wheels": 0,
                    "roc_sum": 0.0,
                    "roc_n": 0,
                    "gross_premium": 0.0,
                    "option_pl": 0.0,
                    "avg_collateral": 0.0,
                    "days": 0,
                    "wins": 0,
                    "losses": 0,
                    "last_dt": None,
                },
            )
            agg["net_realized_pl"] += wheel.get("net_realized_pl") or 0.0
            # "wheels" (and the consistency sub-score) counts only true wheels,
            # so tacking on a losing CSP can't *raise* the score via consistency.
            if is_wheel:
                agg["wheels"] += 1
            roc = wheel.get("annualized_wheel_roc_pct")
            if roc is not None:
                agg["roc_sum"] += roc
                agg["roc_n"] += 1
            agg["gross_premium"] += wheel.get("gross_premium_received") or 0.0
            agg["option_pl"] += wheel.get("option_realized_pl") or 0.0
            agg["avg_collateral"] += wheel.get("avg_collateral") or 0.0
            agg["days"] += wheel.get("days_active") or 0
            agg["wins"] += wheel.get("wins") or 0
            agg["losses"] += wheel.get("losses") or 0
            edge = wheel.get("end_date") or wheel.get("start_date")
            if edge and (agg["last_dt"] is None or edge > agg["last_dt"]):
                agg["last_dt"] = edge

        # Keep names actually wheeled at least once (>= 1 true wheel) and
        # net-positive once every option cycle on them is counted.
        winners = {
            t: a for t, a in by_ticker.items() if a["wheels"] >= 1 and a["net_realized_pl"] > 0
        }
        fundamentals = self._fundamentals(sorted(winners))
        # Drop the outright-ineligible (non-common, LP in the name) before
        # paying for a price fetch; the price-band check waits for last close.
        winners = {
            t: a
            for t, a in winners.items()
            if _csp_ticker_verdict(t, self._company_names.get(t), None, fundamentals.get(t))[0] is None
        }
        stats = self._price_stats(sorted(winners))
        today = date.today()

        rows: list[dict[str, Any]] = []
        for ticker, agg in winners.items():
            reason, unvetted = _csp_ticker_verdict(
                ticker, self._company_names.get(ticker), stats.get(ticker, {}).get("last"),
                fundamentals.get(ticker),
            )
            if reason:  # price now known: outside the $10-$350 band, or a known thin/small name
                continue
            avg_roc = round(agg["roc_sum"] / agg["roc_n"], 2) if agg["roc_n"] else None
            monthly_premium_pct = (
                round(100.0 * agg["gross_premium"] / agg["avg_collateral"] * (30.0 / max(agg["days"], 1)), 2)
                if agg["avg_collateral"] > 1e-9
                else None
            )
            ppd = agg["option_pl"] / agg["days"] if agg["days"] else None
            ppd_yield_pct = (
                round(365.0 * (ppd or 0.0) / agg["avg_collateral"] * 100.0, 2)
                if agg["avg_collateral"] > 1e-9 and ppd is not None
                else None
            )
            win_rate = (
                agg["wins"] / (agg["wins"] + agg["losses"])
                if (agg["wins"] + agg["losses"]) > 0
                else None
            )
            days_since = (
                max((today - date.fromisoformat(agg["last_dt"])).days, 0) if agg["last_dt"] else None
            )
            stat = stats.get(ticker, {})
            sector = sector_of(ticker)
            earn = (fundamentals.get(ticker) or {}).get("earnings_date")
            dte = (earn - today).days if earn else None

            comp = {
                "roc_pct": avg_roc,
                "monthly_premium_pct": monthly_premium_pct,
                "ppd_yield_pct": ppd_yield_pct,
                "net_realized_pl": agg["net_realized_pl"],
                "win_rate": win_rate,
                "wheels": agg["wheels"],
                "days_since_last_wheel": days_since,
                "vol_annual_pct": stat.get("vol_annual_pct"),
                "price_position": stat.get("price_position"),
                "sector": sector,
            }
            scored = csp_star_score(comp, exposure.get(sector or "Unknown", 0.0), dte)

            rows.append(
                {
                    "underlying": ticker,
                    "name": self._company_names.get(ticker),
                    "wheels": agg["wheels"],
                    "net_realized_pl": _money(agg["net_realized_pl"]),
                    "avg_annualized_roc_pct": avg_roc,
                    "monthly_premium_pct": monthly_premium_pct,
                    "ppd": _money(ppd),
                    "last_close": _money(stat.get("last")),
                    "sector": sector,
                    "earnings_date": earn.isoformat() if earn else None,
                    "days_to_earnings": dte,
                    "stars": scored["stars"],
                    "star_breakdown": scored,
                    "vetting": {
                        "unvetted": unvetted,
                        "type": (fundamentals.get(ticker) or {}).get("type"),
                        "market_cap_b": (fundamentals.get(ticker) or {}).get("market_cap_b"),
                        "avg_vol_10d_m": (fundamentals.get(ticker) or {}).get("avg_vol_10d_m"),
                    },
                    # Raw signal inputs, carried so the Combined view can
                    # re-aggregate and re-score without the underlying wheels.
                    "roc_pct": avg_roc,
                    "ppd_yield_pct": ppd_yield_pct,
                    "win_rate": round(win_rate, 4) if win_rate is not None else None,
                    "wins": agg["wins"],
                    "losses": agg["losses"],
                    "days_since_last_wheel": days_since,
                    "vol_annual_pct": stat.get("vol_annual_pct"),
                    "price_position": stat.get("price_position"),
                }
            )
        rows.sort(key=lambda r: (-r["stars"], -(r["net_realized_pl"] or 0.0)))
        return rows

    def _build_wheel_targets_banner(self, wheels: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Running wheels with a phase-specific target to aim for right now,
        for the Dashboard's price-target banner. ``profit_target`` /
        ``preferred_csp_entry`` / ``cc_strike_floor`` (plus their
        explanations) are already computed once, in ``_trade_log_entry`` --
        this just filters to open wheels with a known phase and sorts the
        wheel closest to its phase-appropriate target first (same "surface
        what needs attention" spirit as the open-hedge banner). ``gap_pct``
        is against whichever of profit_target/preferred_csp_entry applies to
        that wheel's phase -- a sort key only, not a stand-in "primary value"
        field (the frontend still picks between the two full fields itself).
        """
        rows: list[dict[str, Any]] = []
        for w in wheels:
            if not w.get("is_wheel") or not w.get("is_open") or w.get("wheel_phase") is None:
                continue
            last_close = w.get("current_price")
            primary = w.get("profit_target") if w.get("wheel_phase") == "cc" else w.get("preferred_csp_entry")
            gap_pct = (
                100.0 * (primary - last_close) / last_close
                if primary is not None and last_close
                else None
            )
            rows.append(
                {
                    "cycle_id": w["cycle_id"],
                    "underlying": w["underlying"],
                    "name": w.get("name"),
                    "wheel_phase": w.get("wheel_phase"),
                    "profit_target": w.get("profit_target"),
                    "profit_target_explanation": w.get("profit_target_explanation"),
                    "preferred_csp_entry": w.get("preferred_csp_entry"),
                    "preferred_csp_entry_explanation": w.get("preferred_csp_entry_explanation"),
                    "cc_strike_floor": w.get("cc_strike_floor"),
                    "cc_strike_floor_explanation": w.get("cc_strike_floor_explanation"),
                    "last_close": last_close,
                    "shares_held": w.get("shares_held"),
                    "gap_pct": round(gap_pct, 2) if gap_pct is not None else None,
                }
            )
        rows.sort(key=lambda r: (abs(r["gap_pct"]) if r["gap_pct"] is not None else float("inf"), r["underlying"]))
        return rows

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
        if self._price_marked_at != self._price_cache_at:
            # A newer live-price fetch landed since these were last built (or
            # this is the first build ever) -- every mark below is stale, not
            # just whichever of these happens to still be None. Without this,
            # _current_prices() refreshing every _LIVE_PRICE_TTL_SECONDS did
            # nothing observable: these six are what the frontend actually
            # renders, and an `is None` guard alone means "built once, kept
            # forever" regardless of how often the live price underneath it
            # moves on.
            self._wheel_return = None
            self._trade_log = None
            self._open_hedges = None
            self._open_positions = None
            self._cc_candidates = None
            self._csp_candidates = None
        dividends = dividends_by_cycle(cycles, transactions)
        if self._wheel_return is None:
            self._wheel_return = self._build_wheel_return(current_prices)
        if self._trade_log is None:
            self._trade_log = self._build_trade_log(current_prices)
        if self._open_hedges is None:
            self._open_hedges = self._build_open_hedges(current_prices)
        if self._open_positions is None:
            self._open_positions = self._build_open_positions(
                current_prices, self._prev_closes, (self._trade_log or {}).get("wheels", [])
            )
        if self._cc_candidates is None:
            self._cc_candidates = self._build_cc_candidates(
                (self._trade_log or {}).get("wheels", []),
                self._open_positions or [],
                self._real_share_quantities(),
            )
        if self._csp_candidates is None:
            _wheels = (self._trade_log or {}).get("wheels", [])
            self._csp_candidates = self._build_csp_candidates(_wheels, sector_exposure(_wheels))
        self._price_marked_at = self._price_cache_at
        earnings_in_view = self._build_earnings_in_view(
            self._open_positions or [], (self._trade_log or {}).get("wheels", []), through
        )
        # {ticker: next-earnings ISO date} for every ticker with an open leg or
        # held shares -- the calendar does its own per-leg "before this expiry"
        # check; the workflow "Evaluate" rule keeps its simpler flag list.
        _earn_rows = {row["ticker"]: row for row in earnings_in_view["tickers"]}
        _earn_dates = {t: r["earnings_date"] for t, r in _earn_rows.items()}
        _earn_before_expiry = [
            row["ticker"] for row in earnings_in_view["tickers"] if row["before_expiry"]
        ]
        # Stamp the next-earnings date onto each Open option positions row (the
        # table shows an Earnings column with the same amber-within-14-days rule
        # as the candidate tables -- days counted from today, like they do).
        _today = date.today()
        for _row in self._open_positions or []:
            _ed = (_earn_rows.get(_row["underlying"]) or {}).get("earnings_date")
            _row["earnings_date"] = _ed
            _row["days_to_earnings"] = (date.fromisoformat(_ed) - _today).days if _ed else None

        # Fold Gap to Target (the Wheel price targets banner's own column)
        # and Shares Held onto every Open option positions row, and
        # synthesize a row -- strike, expiration and every other
        # leg-specific field left blank -- for any position with no open leg
        # at all, so the table is a full account snapshot rather than just
        # the legged rows: a wheel with a current phase (awaiting a call, or
        # ready for a fresh CSP entry) AND a plain position that's simply
        # holding shares with nothing written against them (a buy-and-hold
        # lot, or a wheel between phases) both get a row, distinguishable
        # from a real leg by their blank Type/Strike/Expiration. `wheels`
        # already covers every currently-open position: a closed cycle has
        # sold out of its shares, so `shares_held` is 0 and it is excluded
        # by the same check `_build_cc_candidates` uses. `self._open_positions`
        # itself gets only the two extra keys, in place: it was already
        # consumed above by the candidates/earnings builders and still feeds
        # assignment_risk / expiration_calendar / workflow below, none of
        # which expect a legless row, so the synthetic rows are appended only
        # to the payload copy built at the end of this method, not here.
        _wheels = (self._trade_log or {}).get("wheels", [])
        _wheels_by_cycle = {w["cycle_id"]: w for w in _wheels}
        wheel_targets_banner = self._build_wheel_targets_banner(_wheels)
        _wt_by_cycle = {w["cycle_id"]: w for w in wheel_targets_banner}
        for _row in self._open_positions or []:
            _wt = _wt_by_cycle.get(_row["cycle_id"])
            _row["gap_pct"] = _wt.get("gap_pct") if _wt else None
            _row["shares_held"] = (_wheels_by_cycle.get(_row["cycle_id"]) or {}).get("shares_held")
        _legged_cycles = {row["cycle_id"] for row in self._open_positions or []}
        _open_positions_for_payload = (self._open_positions or []) + [
            _no_contract_open_position_row(
                w,
                _wt_by_cycle.get(w["cycle_id"], {}).get("gap_pct"),
                _earn_rows.get(w["underlying"]),
                _today,
                self._prev_closes.get(w["underlying"]),
            )
            for w in _wheels
            if w["cycle_id"] not in _legged_cycles
            and (w["cycle_id"] in _wt_by_cycle or (w.get("shares_held") or 0.0) > 1e-9)
        ]
        # Cross-reference each row's `shares_held` (the wheel/cycle model's own
        # count -- only shares it can trace to a known lot) against the broker
        # Positions snapshot's real quantity for that ticker (same helper
        # `_build_cc_candidates` above uses). The gap is the same concept
        # `_build_net_worth`'s `untracked_equity_value` already names at the
        # portfolio level (shares bought before every loaded export begins)
        # -- surfaced per-row here too, so "shares held" in the Open option
        # positions table doesn't just quietly show a smaller number than the
        # broker reports with no way to tell why.
        _real_qty_by_symbol = self._real_share_quantities()
        for _row in _open_positions_for_payload:
            _held = _row.get("shares_held") or 0.0
            _real_qty = _real_qty_by_symbol.get(_row.get("underlying"))
            if _real_qty is not None and _real_qty - _held > 1e-6:
                _row["shares_untracked"] = round(_real_qty - _held, 4)
        # Same cross-reference, folded onto the Trade Log's own wheel entries
        # (`_wheels` is `self._trade_log["wheels"]` itself, mutated in place)
        # so a wheel currently in its Covered Call phase shows the broker's
        # real share count there too, not just in the Open option positions
        # table above.
        for _w in _wheels:
            _held = _w.get("shares_held") or 0.0
            if _held <= 1e-9:
                continue
            _real_qty = _real_qty_by_symbol.get(_w.get("underlying"))
            if _real_qty is not None and _real_qty - _held > 1e-6:
                _w["shares_untracked"] = round(_real_qty - _held, 4)
        assignment_risk = assignment_mod.assignment_risk(
            self._open_positions or [], self._net_worth
        )
        # Assigned / called-away legs that closed over roughly the trailing six
        # months -- the calendar draws them as faded bars left of "today" (the
        # daily view still only reaches back two weeks; the weekly / monthly
        # views show the whole span).
        recent_closes = _recent_assigned_closes(
            self.all_cycles, _today - timedelta(days=182), self._company_names
        )
        # No `through` -- the calendar's "today" anchor is the real current date,
        # not a filtered end, so "how far ahead" reads honestly.
        expiration_calendar = expiration_mod.expiration_calendar(
            self._open_positions or [], _earn_dates, recent_closes=recent_closes
        )
        workflow = workflow_mod.classify_open_legs(
            self._open_positions or [], _earn_before_expiry
        )

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

        # Periodic P/L histogram: realized-only flows (Net Premium / Closed P/L)
        # from the display-filtered `cycles`, matching `pnl_series`. `since` is
        # applied afterward as a pure display crop, since neither series carries
        # anything cumulative across buckets that a cropped-off earlier bucket
        # could be feeding.
        period_weeks = periodic_pl_series(cycles, through, "week")
        period_months = periodic_pl_series(cycles, through, "month")
        if since is not None:
            week_cutoff = since - timedelta(days=since.weekday())
            period_weeks = [row for row in period_weeks if date.fromisoformat(row["week_start"]) >= week_cutoff]
            period_months = [
                row for row in period_months if (row["year"], row["month"]) >= (since.year, since.month)
            ]
        period_pl = {"weeks": period_weeks, "months": period_months}

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
            "period_pl": period_pl,
            "wheel_state": wheel_state,
            "reconciliation": _reconciliation(
                transactions, built_cycles, self.reports, engine.unmatched_cash
            ),
            "net_worth": self._net_worth,
            "benchmark": self._benchmark,
            "wheel_return": self._wheel_return,
            "trade_log": self._trade_log,
            "open_hedges": self._open_hedges,
            "wheel_targets": wheel_targets_banner,
            "open_positions": _open_positions_for_payload,
            "cc_candidates": self._cc_candidates,
            "csp_candidates": self._csp_candidates,
            "earnings_in_view": earnings_in_view,
            "assignment_risk": assignment_risk,
            "expiration_calendar": expiration_calendar,
            "recent_closes": recent_closes,
            "workflow": workflow,
        }
