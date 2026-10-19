"""The shape and formatting shared by the CSP TO ENTER and CC TO EXIT
tooltips ("cards"), so the two read as one component.

Each strategy module builds its own card next to its own calculation
(``cspentry.tooltip_card``, ``ccexit.tooltip_card``); this module only fixes
the layout contract and the number formats. The frontend renders any card
with one function (``tipCardNode`` in ``static/app.js``) and never computes:
every value arrives here already formatted from the one backend result.

A card is a plain dict:

    strategy      "CSP TO ENTER" / "CC TO EXIT"
    ticker        the symbol, or None
    status        STATUS_* key (drives the badge style)
    status_label  the badge text; the status never relies on colour alone
    subtitle      one line: stock price, expiration, DTE and its as-of date
    quoted        when the option prices are from, or None
    headline      {label, value, note} the selected strike, or None
    headline_side {label, value, note} its delta, or None
    summary       one sentence shown when nothing qualified, or None
    metrics       up to four {label, value, note}: the key numbers
    why           up to three short sentences, built from the result
    risk          {title, text, stats: [{label, value}]} or None
    sections      [{title, open, rows: [[label, value]], table, notes}]
                  collapsible details; ``table`` is {columns, numeric, rows:
                  [{cells, picked, result, reason}]} (``table_row``)
    footer        a closing caveat, or None
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

STATUS_QUALIFIES = "qualifies"
STATUS_DOES_NOT_QUALIFY = "does_not_qualify"
# A market input was missing, so the rules couldn't be checked at all.
STATUS_UNVERIFIED = "unverified"
# Neither pass nor fail: e.g. every share already has a call against it.
STATUS_INFO = "info"

STATUS_BADGES = {
    STATUS_QUALIFIES: "QUALIFIES",
    STATUS_DOES_NOT_QUALIFY: "DOES NOT QUALIFY",
    STATUS_UNVERIFIED: "UNVERIFIED",
}

MAX_WHY_BULLETS = 3
MAX_METRICS = 4


def money(value: float | None) -> str:
    if value is None:
        return "Unavailable"
    return f"-${-value:,.2f}" if value < 0 else f"${value:,.2f}"


def pct(value: float | None, digits: int = 1) -> str:
    return "Unavailable" if value is None else f"{value:.{digits}f}%"


def delta(value: float | None) -> str:
    return "Unavailable" if value is None else f"{value:.2f}"


def shares(value: float) -> str:
    return f"{value:,.0f}" if abs(value - round(value)) < 1e-6 else f"{value:,.3f}"


def count(value: int | None) -> str:
    return "Not reported" if value is None else f"{value:,}"


def day(value: date | None) -> str:
    return "Unavailable" if value is None else value.strftime("%b %d, %Y").replace(" 0", " ")


def when(moment: datetime | None) -> str:
    """Local wall-clock time, e.g. "Oct 17, 2026 3:59 PM"."""
    if moment is None:
        return "Unavailable"
    return moment.astimezone().strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")


def item(label: str, value: str, note: str | None = None) -> dict[str, Any]:
    return {"label": label, "value": value, "note": note}


def subtitle(current_price: float | None, expiry: date | None, days_to_expiry: int | None, today: date | None) -> str:
    """"Stock $331.21 · Nov 20, 2026 · 34 DTE from Oct 17". DTE is counted
    from the calculation date (``today``), which can be a day or two after
    the quote time (a weekend, a holiday); naming it keeps the two apart."""
    parts = [f"Stock {money(current_price)}" if current_price is not None else "No stock price"]
    if expiry is not None:
        parts.append(day(expiry))
    if days_to_expiry is not None:
        basis = f" from {today.strftime('%b %d').replace(' 0', ' ')}" if today is not None else ""
        parts.append(f"{days_to_expiry} DTE{basis}")
    return " · ".join(parts)


def quoted(quotes_as_of: datetime | None, fetched_at: datetime | None) -> str | None:
    if quotes_as_of is not None:
        return f"Option prices as of {when(quotes_as_of)}"
    if fetched_at is not None:
        return f"Option prices fetched {when(fetched_at)}"
    return None


def card(
    *,
    strategy: str,
    ticker: str | None,
    status: str,
    subtitle_text: str,
    quoted_text: str | None = None,
    status_label: str | None = None,
    headline: dict[str, Any] | None = None,
    headline_side: dict[str, Any] | None = None,
    summary: str | None = None,
    metrics: list[dict[str, Any]] | None = None,
    why: list[str] | None = None,
    risk: dict[str, Any] | None = None,
    sections: list[dict[str, Any]] | None = None,
    footer: str | None = None,
) -> dict[str, Any]:
    metrics = metrics or []
    why = why or []
    if len(metrics) > MAX_METRICS or len(why) > MAX_WHY_BULLETS:
        raise ValueError("A tooltip card shows at most four key numbers and three reasons.")
    return {
        "strategy": strategy,
        "ticker": ticker,
        "status": status,
        "status_label": status_label or STATUS_BADGES[status],
        "subtitle": subtitle_text,
        "quoted": quoted_text,
        "headline": headline,
        "headline_side": headline_side,
        "summary": summary,
        "metrics": metrics,
        "why": why,
        "risk": risk,
        "sections": [section for section in (sections or []) if section],
        "footer": footer,
    }


def section(
    title: str,
    *,
    rows: list[tuple[str, str]] | None = None,
    table: dict[str, Any] | None = None,
    notes: list[str] | None = None,
    open_: bool = False,
) -> dict[str, Any] | None:
    """One collapsible details block; ``None`` (dropped) when it has nothing to show."""
    if not rows and not table and not notes:
        return None
    return {
        "title": title,
        "open": open_,
        "rows": [[label, value] for label, value in (rows or [])],
        "table": table,
        "notes": notes or [],
    }


def card_text(card_dict: dict[str, Any] | None) -> str:
    """Every visible string in a card, one per line, in display order: for
    plain-text uses (logs, tests) that need what the reader would see."""
    if not card_dict:
        return ""
    lines = [card_dict["strategy"], card_dict.get("ticker") or "", card_dict["status_label"], card_dict["subtitle"]]
    lines.append(card_dict.get("quoted") or "")
    for key in ("headline", "headline_side"):
        part = card_dict.get(key)
        if part:
            lines += [part["label"], part["value"], part.get("note") or ""]
    lines.append(card_dict.get("summary") or "")
    for metric in card_dict["metrics"]:
        lines += [metric["label"], metric["value"], metric.get("note") or ""]
    lines += card_dict["why"]
    risk = card_dict.get("risk")
    if risk:
        lines += [risk["title"], risk["text"]]
        lines += [f"{stat['label']}: {stat['value']}" for stat in risk["stats"]]
    for block in card_dict["sections"]:
        lines.append(block["title"])
        if block["table"]:
            lines.append(" | ".join(block["table"]["columns"]))
            for row in block["table"]["rows"]:
                lines.append(" | ".join(row["cells"]))
                if row["reason"]:
                    lines.append(f"  {row['reason']}")
        lines += [f"{label}: {value}" for label, value in block["rows"]]
        lines += block["notes"]
    lines.append(card_dict.get("footer") or "")
    return "\n".join(line for line in lines if line)


RESULT_SELECTED = "Selected"
RESULT_QUALIFIES = "Qualifies"
RESULT_REJECTED = "Rejected"


def table_row(cells: list[str], *, picked: bool, qualifies: bool, reason: str | None = None) -> dict[str, Any]:
    """One strike: its figures, then a one-word result (the table's last
    column) and, when there is one, the reason in words on its own line, so
    a long rejection never squeezes the numbers."""
    result = RESULT_SELECTED if picked else RESULT_QUALIFIES if qualifies else RESULT_REJECTED
    return {"cells": [*cells, result], "picked": picked, "result": result, "reason": reason}


def table(columns: list[str], numeric: list[bool], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """``columns``/``numeric`` name the figure columns; a "Result" column is
    appended. ``rows``: from ``table_row``."""
    if not rows:
        return None
    columns, numeric = [*columns, "Result"], [*numeric, False]
    if len(columns) != len(numeric) or any(len(row["cells"]) != len(columns) for row in rows):
        raise ValueError("Every strike-table row needs one cell per column.")
    return {"columns": columns, "numeric": numeric, "rows": rows}
