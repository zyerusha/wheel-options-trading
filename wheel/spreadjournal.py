"""The scanner's own track record: what it recommended, and how those exact
spreads turned out if they were later traded.

Each listed opportunity priced on current quotes is written once to a small
JSON file, keeping the first time it was recommended. Outcomes are never stored. They are worked out when
read, by matching the broker ledger on ticker, expiration, type and strikes, so
the ledger stays the only source of truth for what actually happened.

"Reached the target" and "hit the risk threshold" are judged by the closing
price, since the ledger doesn't show what the spread traded at while open.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date
from typing import Any, Sequence

from wheel.creditspreads import (
    STRATEGY_LABELS,
    SpreadPosition,
    SpreadSettings,
    loss_trigger_debit,
    target_closing_debit,
)

# Under this many closed trades, comparing estimates with results is mostly noise.
MIN_CLOSED_TO_COMPARE = 30
JOURNAL_FILE_NAME = "spread_recommendations.json"
_RECORD_FIELDS = (
    "id", "ticker", "strategy", "short_strike", "long_strike", "expiry", "dte", "score", "rating",
    "pop_pct", "expected_value", "credit", "max_loss",
)
_CENT = 0.005
_WRITE_LOCK = threading.Lock()


def load(path: str) -> list[dict[str, Any]]:
    """Every record, or an empty list if the file is missing or unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return []
    return [r for r in raw if isinstance(r, dict) and r.get("id")] if isinstance(raw, list) else []


def record(path: str, rows: Sequence[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """Add listed opportunities not yet in the file and return them all. A
    spread already on file keeps its first recommendation: that is the one
    being judged. A failed write only costs the history, never the page."""
    with _WRITE_LOCK:
        records = load(path)
        known = {r["id"] for r in records}
        added = [
            {**{name: row.get(name) for name in _RECORD_FIELDS}, "recommended_on": today.isoformat()}
            for row in rows
            if row["id"] not in known
        ]
        if not added:
            return records
        records += added
        temporary = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(records, handle, indent=1)
            os.replace(temporary, path)
        except OSError:
            pass
        return records


def _matches(rec: dict[str, Any], position: SpreadPosition) -> bool:
    return (
        rec.get("ticker") == position.ticker
        and rec.get("strategy") == position.strategy
        and rec.get("expiry") == position.expiry.isoformat()
        and abs(float(rec.get("short_strike") or 0) - position.short_strike) < _CENT
        and abs(float(rec.get("long_strike") or 0) - position.long_strike) < _CENT
        and str(rec.get("recommended_on") or "") <= position.open_date.isoformat()
    )


def outcome(rec: dict[str, Any], position: SpreadPosition, management: SpreadSettings) -> dict[str, Any]:
    """One recommendation joined to the trade that followed it."""
    credit = position.credit
    debit = position.closing_debit
    closed = position.is_closed
    settled = closed and position.result_settled
    return {
        "id": rec["id"],
        "ticker": position.ticker,
        "strategy_label": STRATEGY_LABELS.get(position.strategy, position.strategy),
        "short_strike": position.short_strike,
        "long_strike": position.long_strike,
        "expiry": position.expiry.isoformat(),
        "recommended_on": rec.get("recommended_on"),
        "score_at_entry": rec.get("score"),
        "rating_at_entry": rec.get("rating"),
        "pop_estimate": rec.get("pop_pct"),
        "expected_value": rec.get("expected_value"),
        "credit_recommended": rec.get("credit"),
        "credit_received": round(credit, 2),
        "closed": closed,
        "closing_price": round(debit, 2) if closed and debit is not None else None,
        "realized_net": round(position.realized_net, 2) if settled else None,
        "days_held": (position.close_date - position.open_date).days if closed and position.close_date else None,
        "reached_target": closed and debit is not None and debit <= target_closing_debit(credit, management.profit_target_pct) + _CENT,
        "hit_risk_threshold": closed and debit is not None and debit + _CENT >= loss_trigger_debit(credit, management.loss_multiple),
    }


def track_record(
    records: Sequence[dict[str, Any]], positions: Sequence[SpreadPosition], management: SpreadSettings
) -> dict[str, Any]:
    """Recommendations vs results. Win rate only counts spreads whose result
    the ledger settles; assigned-on-one-leg spreads are left out, as in the
    tab's Performance section."""
    traded = []
    for rec in records:
        match = next((p for p in positions if _matches(rec, p)), None)
        if match is not None:
            traded.append(outcome(rec, match, management))
    settled = [t for t in traded if t["realized_net"] is not None]
    wins = sum(1 for t in settled if t["realized_net"] > 0)
    estimates = [t["pop_estimate"] for t in settled if t["pop_estimate"] is not None]
    enough = len(settled) >= MIN_CLOSED_TO_COMPARE
    return {
        "recommended": len(records),
        "traded": len(traded),
        "closed": len(settled),
        "actual_win_rate_pct": round(wins / len(settled) * 100, 1) if settled else None,
        "avg_estimated_pop_pct": round(sum(estimates) / len(estimates), 1) if estimates else None,
        "enough_history": enough,
        "note": (
            "Estimates and results can start to be compared, but past results don't prove the score predicts anything."
            if enough
            else f"Only {len(settled)} closed so far. At least {MIN_CLOSED_TO_COMPARE} are needed before comparing "
            "them means much, so don't read the score as predictive yet."
        ),
        "trades": traded[-20:],
    }
