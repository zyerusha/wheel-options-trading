"""Monthly cash-flow reporting for the Wheel strategy.

This is a deliberately different lens from :mod:`wheel.metrics`. That module
answers "how is the wheel performing" by reconstructing cycles and dating P/L
to when a leg *closes*. This module answers a narrower, income-statement-style
question -- "how much cash did the account actually take in or pay out, and
when did it settle" -- and every figure here is dated to the transaction's own
:attr:`~wheel.parser.Transaction.event_date`, independent of option expiry,
cycle boundaries, or whether a position is still open. A CSP sold in March and
assigned in April books its premium in March; the assignment itself carries no
option cash of its own (Fidelity settles that on the equity leg), so it does
not appear here at all -- see "Capital allocations" below.

**Realized credits** are premium received selling to open (``STO``) and
dividends (including reinvestments and in-lieu-of-dividend payouts).
**Realized debits** are premium paid buying to close a short (``BTC``) or
opening a long hedge (``BTO`` -- a protective put or a long call). Closing a
long option (``STC``) can land on either side, since a profitable exit is a
credit and a loss is a debit. **Fees** are commissions/exchange fees on option
fills, plus broker ledger rows that are a real cost of running the account
(a charged fee, foreign tax withheld, margin interest) -- reported on their
own line rather than folded into gross debits, so the Monthly Breakdown table
shows "what was paid to trade" separately from "what the broker charged".

A subtlety in the math: :class:`~wheel.parser.Transaction.amount` is already
net of commission and fees (the parser's own invariant -- see
``wheel/parser.py``). Splitting a fill into credit/debit *and* reporting its
fee separately would double-subtract that fee if done naively, so
:func:`_split_option_row` adds the fee back to ``amount`` to recover the
row's gross cash flow before splitting it by sign; the fee is then the one
and only place it is subtracted. ``Net Monthly Cash Flow`` therefore always
equals the sum of every included row's ``amount`` -- there is no rounding
drift between the three columns and the total.

**Capital allocations are excluded.** ``BUY_STOCK``/``SELL_STOCK`` (share
assignment costs and call-away proceeds) and ``ASSIGNED``/``EXPIRED`` (which
carry no option cash of their own) never contribute to monthly cash flow --
they move capital between cash and shares, they do not generate wheel income.
Ledger rows that are not real cash at all (a collateral mark, a corporate-
action symbol rename) are excluded the same way; anything else in the ledger
that this module does not recognize is also excluded, on the theory that a
report should never guess at an unfamiliar broker phrase's economic meaning.
"""

from __future__ import annotations

import argparse
import re
from calendar import monthrange
from datetime import date, timedelta
from typing import Sequence

from wheel.parser import BTC, BTO, OTHER, STC, STO, Transaction
from wheel.statmath import time_weighted_average

OPTION_CASH_ACTIONS = frozenset({STO, BTC, BTO, STC})

# --------------------------------------------------------------------------
# Ledger-row classification (dividends, fees, and everything excluded)
# --------------------------------------------------------------------------

_EXCLUDED = "EXCLUDED"  # real ledger row, but not a cash-flow event
_DIVIDEND = "DIVIDEND"
_CHARGE = "CHARGE"  # fee, foreign tax, margin interest -- broker overhead
_UNCLASSIFIED = "UNCLASSIFIED"

# Ordered most-specific-first, the same idiom as wheel.parser._ACTION_PATTERNS
# and wheel.benchmark._CASHFLOW_PATTERNS (which this list deliberately mirrors,
# refined into DIVIDEND/CHARGE instead of a single undifferentiated INTERNAL --
# a cash-flow *statement* needs to know which side of the ledger a row belongs
# on, which "money stayed inside the account" alone doesn't say).
_LEDGER_PATTERNS: Sequence[tuple[str, str]] = (
    (r"DISTRIBUTION\s+NAME/SYMBOL\s+CHANGE", _EXCLUDED),
    (r"DECREASE\s+COLLATERAL|INCREASE\s+COLLATERAL", _EXCLUDED),
    (r"IN\s+LIEU\s+OF.*PAYOUT", _DIVIDEND),
    (r"DIVIDEND\s+RECEIVED", _DIVIDEND),
    (r"REINVESTMENT", _DIVIDEND),
    (r"FEE\s+CHARGED", _CHARGE),
    (r"FOREIGN\s+TAX\s+PAID", _CHARGE),
    (r"INTEREST\s+(FULLY\s+)?PAID", _CHARGE),
)


def _classify_ledger_row(action_raw: str) -> str:
    text = (action_raw or "").upper()
    for pattern, category in _LEDGER_PATTERNS:
        if re.search(pattern, text):
            return category
    return _UNCLASSIFIED


def dividend_transactions(transactions: Sequence[Transaction]) -> list[Transaction]:
    """Every ``OTHER``-action row this module classifies as a dividend.

    Public (not underscore-prefixed): ``wheel.metrics``'s Total Position ROI
    needs the same dividend rows this module's own credits column already
    uses, attributed to whichever wheel cycle was open on each row's own
    underlying and date -- a different grouping than the monthly buckets
    :func:`monthly_cashflow_series` builds, so it reuses the classifier
    rather than the bucketing.
    """
    return [
        t for t in transactions if t.action == OTHER and _classify_ledger_row(t.action_raw) == _DIVIDEND
    ]


def _split_option_row(transaction: Transaction) -> tuple[float, float, float]:
    """(credit, debit, fee) for one option fill (STO/BTC/BTO/STC).

    See the module docstring: the fee is added back to ``amount`` to recover
    the gross premium flow before it is split by sign, so it is subtracted
    exactly once overall rather than once inside ``amount`` and again here.
    """
    fee = transaction.total_fees
    gross = transaction.amount + fee
    return max(gross, 0.0), max(-gross, 0.0), fee


def _split_ledger_row(transaction: Transaction) -> tuple[float, float, float]:
    """(credit, debit, fee) for a non-trade ledger row (action ``OTHER``).

    Dividends land in credits or debits by their own sign (a reversal is rare
    but not impossible). Fees, foreign tax and margin interest are broker
    overhead and are booked through the fee column rather than gross debits --
    that is what lets the Monthly Breakdown table's "Total Fees" column mean
    the same thing whether the fee came from an option fill's commission or
    from a standalone ledger charge.
    """
    category = _classify_ledger_row(transaction.action_raw)
    if category in (_EXCLUDED, _UNCLASSIFIED):
        return 0.0, 0.0, 0.0
    amount = transaction.amount
    if category == _DIVIDEND:
        return max(amount, 0.0), max(-amount, 0.0), 0.0
    return max(amount, 0.0), 0.0, max(-amount, 0.0)


def _cash_effect(transaction: Transaction) -> tuple[float, float, float] | None:
    """(credit, debit, fee) this row contributes, or ``None`` if it is a
    capital allocation (share assignment/purchase/sale, or an ASSIGNED/EXPIRED
    row with no option cash of its own) or ledger noise excluded above.
    """
    if transaction.action in OPTION_CASH_ACTIONS and transaction.is_option:
        return _split_option_row(transaction)
    if transaction.action == OTHER:
        return _split_ledger_row(transaction)
    return None


# --------------------------------------------------------------------------
# Monthly bucketing
# --------------------------------------------------------------------------


def _month_key(day: date) -> tuple[int, int]:
    return day.year, day.month


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def month_average_collateral(capital_points: Sequence[tuple[date, float]], year: int, month: int) -> float:
    """Time-weighted average committed capital over the days of one calendar
    month -- ``wheel.statmath.time_weighted_average``, re-scoped from a
    cycle's lifetime to one month. Days at zero capital are excluded, so a
    month that closes a position on the 3rd and opens nothing else isn't
    diluted by 27 empty days.

    Public (not underscore-prefixed): ``wheel.accounts``'s Combined view calls
    this directly against its own already-combined capital series rather than
    averaging each account's monthly yield -- see that module's docstring on
    why combined figures are always recomputed from combined absolutes.
    """
    start, end = _month_bounds(year, month)
    in_month = [(day, total) for day, total in capital_points if start <= day <= end]
    return time_weighted_average(in_month, value=lambda point: point[1])


def monthly_cashflow_series(
    transactions: Sequence[Transaction],
    capital_points: Sequence[tuple[date, float]],
    through: date,
    since: date | None = None,
) -> list[dict]:
    """Realized wheel cash flow, one row per calendar month.

    ``capital_points`` is a ``(day, total_committed_capital)`` series -- e.g.
    built from :func:`wheel.metrics.portfolio_capital_series` as
    ``[(point.day, point.total) for point in series]`` -- used only to compute
    each month's average collateral for the yield-% denominator; it plays no
    part in deciding which month a cash flow lands in, which is entirely a
    property of the transaction's own ``event_date``.

    Months between the first and last month that saw any cash flow are filled
    in at zero rather than skipped, the same reasoning as
    ``portfolio_capital_series``: a chart drawing a line (or a bar series)
    between non-adjacent months would otherwise misrepresent a quiet stretch
    as a gap in the data rather than a month of genuine zero activity. Months
    are never fabricated before the first cash flow or after ``through``.

    ``since``, if given, crops the *returned* rows to that month onward, after
    the full month range has already been walked -- a display crop, not a
    reason to compute a shorter table.
    """
    buckets: dict[tuple[int, int], list[float]] = {}
    for transaction in transactions:
        effect = _cash_effect(transaction)
        if effect is None:
            continue
        credit, debit, fee = effect
        if credit == 0.0 and debit == 0.0 and fee == 0.0:
            continue
        slot = buckets.setdefault(_month_key(transaction.event_date), [0.0, 0.0, 0.0])
        slot[0] += credit
        slot[1] += debit
        slot[2] += fee

    if not buckets:
        return []

    first_year, first_month = min(buckets)
    last_year, last_month = max(buckets)
    if (last_year, last_month) > (through.year, through.month):
        last_year, last_month = through.year, through.month

    rows: list[dict] = []
    year, month = first_year, first_month
    while (year, month) <= (last_year, last_month):
        credit, debit, fee = buckets.get((year, month), [0.0, 0.0, 0.0])
        avg_collateral = month_average_collateral(capital_points, year, month)
        net = credit - debit - fee
        rows.append(
            {
                "year": year,
                "month": month,
                "period": f"{year:04d}-{month:02d}",
                "gross_credits": round(credit, 2),
                "gross_debits": round(debit, 2),
                "fees": round(fee, 2),
                "net_cash_flow": round(net, 2),
                "avg_collateral": round(avg_collateral, 2),
                "monthly_yield_pct": (
                    round(100.0 * net / avg_collateral, 4) if avg_collateral > 1e-9 else None
                ),
            }
        )
        year, month = _next_month(year, month)

    if since is not None:
        rows = [row for row in rows if (row["year"], row["month"]) >= (since.year, since.month)]
    return rows


# --------------------------------------------------------------------------
# Weekly bucketing
# --------------------------------------------------------------------------


def _week_start(day: date) -> date:
    """The Monday on or before ``day`` -- ISO week anchor. ``date.weekday()``
    is 0 for Monday, so subtracting it lands on that week's Monday."""
    return day - timedelta(days=day.weekday())


def weekly_cashflow_series(
    transactions: Sequence[Transaction],
    through: date,
    since: date | None = None,
) -> list[dict]:
    """Realized wheel cash flow, one row per ISO week (Monday-anchored).

    A finer-grained companion to :func:`monthly_cashflow_series` for the
    "Cash flow vs. wheel P/L gap" chart, which wants more x-axis resolution
    than one point per month. Only the cash columns are reported -- no
    average collateral or yield-%, since that chart uses neither.

    Bucketing is by each transaction's own ``event_date``, exactly as the
    monthly series does. Weeks between the first and last active week are
    filled in at zero rather than skipped, so a quiet stretch draws as real
    zero activity rather than a gap in the data. Weeks are never fabricated
    before the first cash flow or after ``through``.

    ``since``, if given, crops the returned rows to the week containing that
    date onward -- a display crop applied after the full week range has been
    walked, mirroring ``monthly_cashflow_series``'s own ``since`` handling.
    """
    buckets: dict[date, list[float]] = {}
    for transaction in transactions:
        effect = _cash_effect(transaction)
        if effect is None:
            continue
        credit, debit, fee = effect
        if credit == 0.0 and debit == 0.0 and fee == 0.0:
            continue
        slot = buckets.setdefault(_week_start(transaction.event_date), [0.0, 0.0, 0.0])
        slot[0] += credit
        slot[1] += debit
        slot[2] += fee

    if not buckets:
        return []

    first_week = min(buckets)
    last_week = min(max(buckets), _week_start(through))

    rows: list[dict] = []
    week = first_week
    while week <= last_week:
        credit, debit, fee = buckets.get(week, [0.0, 0.0, 0.0])
        net = credit - debit - fee
        rows.append(
            {
                "period": week.isoformat(),
                "week_start": week.isoformat(),
                "week_end": (week + timedelta(days=6)).isoformat(),
                "gross_credits": round(credit, 2),
                "gross_debits": round(debit, 2),
                "fees": round(fee, 2),
                "net_cash_flow": round(net, 2),
            }
        )
        week += timedelta(days=7)

    if since is not None:
        cutoff = _week_start(since)
        rows = [row for row in rows if date.fromisoformat(row["week_start"]) >= cutoff]
    return rows


# --------------------------------------------------------------------------
# Range summary
# --------------------------------------------------------------------------


def range_summary(rows: Sequence[dict], capital_points: Sequence[tuple[date, float]], as_of: date) -> dict:
    """Cash flow, average monthly income, and annualized cash-on-cash return
    over the SAME range already shown in ``rows`` -- whatever date filter is
    currently selected, not a fixed trailing-12-month lookback. Widen or
    narrow the dashboard's date range and this summary moves with it, exactly
    as the Monthly Breakdown table above it does, because both are built from
    the same ``rows``.

    ``avg_collateral`` is a single time-weighted average over every day in
    ``capital_points`` -- the same series, and the same
    ``wheel.statmath.time_weighted_average``, that
    ``wheel.metrics.portfolio_metrics`` uses for Annualized Wheel ROC's own
    denominator. Deliberately not the mean of each month's own average: that
    would silently diverge from ROC's figure even over an identical window,
    since a month with capital deployed for 3 days counts the same as one
    with 30 in an average of averages, but not in a true time-weighted one.
    The annualized figure scales ``avg_monthly_income`` up to a full year
    (``x 12``) against that capital base -- correct regardless of how many
    months are actually in ``rows``, since "average dollars per month" is
    already a monthly rate before the scaling.
    """
    if not rows:
        return {
            "as_of": as_of.isoformat(),
            "months_counted": 0,
            "cash_flow": 0.0,
            "avg_monthly_income": None,
            "avg_collateral": 0.0,
            "annualized_cash_on_cash_return_pct": None,
        }

    months_counted = len(rows)
    cash_flow = sum(row["net_cash_flow"] for row in rows)
    avg_monthly_income = cash_flow / months_counted

    avg_collateral = time_weighted_average(capital_points, value=lambda point: point[1])

    annualized_cash_on_cash_return_pct = None
    if avg_collateral > 1e-9:
        annualized_cash_on_cash_return_pct = round(100.0 * (avg_monthly_income * 12.0) / avg_collateral, 4)

    return {
        "as_of": as_of.isoformat(),
        "months_counted": months_counted,
        "cash_flow": round(cash_flow, 2),
        "avg_monthly_income": round(avg_monthly_income, 2),
        "avg_collateral": round(avg_collateral, 2),
        "annualized_cash_on_cash_return_pct": annualized_cash_on_cash_return_pct,
    }


# --------------------------------------------------------------------------
# Terminal / text report
# --------------------------------------------------------------------------

_TABLE_HEADERS = ("Month", "Credits", "Debits", "Fees", "Net", "Avg Collateral", "Yield %")
_TABLE_WIDTHS = (7, 14, 14, 12, 14, 15, 8)


def format_monthly_table(rows: Sequence[dict]) -> str:
    """Plain-text rendering of the Monthly Breakdown table, for a terminal or
    a log -- the same columns the dashboard's table view shows.
    """
    if not rows:
        return "(no cash-flow activity in range)"
    lines = ["  ".join(h.ljust(w) for h, w in zip(_TABLE_HEADERS, _TABLE_WIDTHS))]
    lines.append("  ".join("-" * w for w in _TABLE_WIDTHS))
    for row in rows:
        yield_text = "n/a" if row["monthly_yield_pct"] is None else f"{row['monthly_yield_pct']:.2f}%"
        cells = (
            row["period"],
            f"{row['gross_credits']:,.2f}",
            f"{row['gross_debits']:,.2f}",
            f"{row['fees']:,.2f}",
            f"{row['net_cash_flow']:,.2f}",
            f"{row['avg_collateral']:,.2f}",
            yield_text,
        )
        lines.append("  ".join(cell.ljust(w) for cell, w in zip(cells, _TABLE_WIDTHS)))
    return "\n".join(lines)


def format_ascii_chart(rows: Sequence[dict], *, width: int = 30) -> str:
    """One line per month: a bar sized to net cash flow, scaled to the
    largest magnitude in the series so a single outsized month doesn't
    crowd every other bar down to a sliver. ``+`` marks a net-credit month,
    ``-`` a net-debit one.
    """
    if not rows:
        return "(no cash-flow activity in range)"
    max_abs = max(abs(row["net_cash_flow"]) for row in rows) or 1.0
    lines = []
    for row in rows:
        value = row["net_cash_flow"]
        bar_len = round(abs(value) / max_abs * width)
        bar = ("+" if value >= 0 else "-") * bar_len
        lines.append(f"{row['period']} | {bar:<{width}} {value:>12,.2f}")
    return "\n".join(lines)


def format_report(rows: Sequence[dict], summary: dict) -> str:
    """The full terminal report: table, month-over-month bar chart, range
    summary -- everything :func:`monthly_cashflow_series` and
    :func:`range_summary` produce, in one printable block.
    """
    avg_income = summary["avg_monthly_income"]
    coc = summary["annualized_cash_on_cash_return_pct"]
    lines = [
        "Monthly Cash Flow -- Wheel Strategy",
        "=" * 36,
        "",
        format_monthly_table(rows),
        "",
        "Month-over-month net cash flow",
        "-" * 31,
        format_ascii_chart(rows),
        "",
        f"Summary over this range (as of {summary['as_of']}, {summary['months_counted']} month(s))",
        "-" * 31,
        f"Cash flow:                {summary['cash_flow']:,.2f}",
        "Avg monthly income:       " + ("n/a" if avg_income is None else f"{avg_income:,.2f}"),
        "Annualized cash-on-cash:  " + ("n/a" if coc is None else f"{coc:.2f}%"),
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _build_report(csv_paths: Sequence[str] | None = None) -> str:
    from wheel.api import Dashboard, Filters
    from wheel.metrics import portfolio_capital_series

    dashboard = Dashboard(csv_paths)
    filters = Filters()
    transactions = [t for t in dashboard.transactions if filters.matches_transaction(t)]
    through = dashboard.last_date or date.today()
    capital = portfolio_capital_series(dashboard.all_cycles, through)
    capital_points = [(point.day, point.total) for point in capital]
    rows = monthly_cashflow_series(transactions, capital_points, through)
    summary = range_summary(rows, capital_points, through)
    return format_report(rows, summary)


def main() -> None:
    parser = argparse.ArgumentParser(description="Print the monthly Wheel cash-flow report to the terminal.")
    parser.add_argument(
        "--csv",
        nargs="+",
        default=None,
        help="one or more broker exports; several are combined into one timeline. "
        "Defaults to every export found in . and data/",
    )
    args = parser.parse_args()
    print(_build_report(args.csv))


if __name__ == "__main__":
    main()
