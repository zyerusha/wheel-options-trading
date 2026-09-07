"""Workflow buckets -- a daily triage of the open book into
Attention / Take-Profit Candidate / Evaluate / Working.

These are **flags with a reason, not trade advice.** Each leg lands in exactly
one bucket (first rule that matches wins) and carries a one-phrase ``reason``
saying why. The tracker never says *what* to do about it.

Thresholds live here as named constants, the same way the open-hedge phase
boundaries live in ``wheel/api.py``; a ``data/workflow_rules.json`` override is a
possible follow-up, not built.
"""

from __future__ import annotations

from typing import Any, Sequence

# ITM with this many days or fewer to expiry is an "act this week" flag.
ATTENTION_DTE = 7
# Most of the intrinsic-basis credit is banked (see F3's Min. Profit Captured).
TAKE_PROFIT_CAPTURED_PCT = 80.0
# ...and only worth flagging when there is not much time left to keep earning by
# holding -- otherwise every out-of-the-money short (which reads ~100 on the
# intrinsic-only estimate) lands here and the bucket stops being a signal.
TAKE_PROFIT_MAX_DTE = 21
# Not yet ITM but within this % of the strike -- worth a look.
NEAR_THE_MONEY_PCT = 2.0

_OPTION_MULTIPLIER = 100
_BUCKETS = ("attention", "take_profit_candidate", "evaluate", "working")
_BUCKET_LABELS = {
    "attention": "Attention",
    "take_profit_candidate": "Take-Profit Candidate",
    "evaluate": "Evaluate",
    "working": "Working",
}


def _leg_capital(row: dict[str, Any]) -> float:
    if row.get("side") == "LONG":
        return abs(row.get("net_premium") or 0.0)
    collateral = row.get("collateral")
    if collateral is not None:
        return collateral
    return (row.get("strike") or 0.0) * _OPTION_MULTIPLIER * (row.get("contracts") or 0.0)


def _classify(row: dict[str, Any], earnings_before_expiry: set[str]) -> tuple[str, str]:
    """(bucket, reason) for one open leg."""
    kind = row.get("type")  # CSP | CC | LP | LC
    is_long = row.get("side") == "LONG"
    dte = row.get("days_to_expiry")
    itm = bool(row.get("in_the_money"))
    moneyness = row.get("moneyness_pct")
    captured = row.get("min_profit_captured_pct")
    breakeven = row.get("wheel_breakeven")
    last_close = row.get("last_close")

    # --- Attention -------------------------------------------------------
    if itm and dte is not None and dte <= ATTENTION_DTE:
        return "attention", f"in the money, {dte}d to expiry"
    if is_long and dte is not None and dte <= ATTENTION_DTE:
        return "attention", f"protective leg expiring in {dte}d"
    if (
        kind == "CC"
        and breakeven is not None
        and last_close is not None
        and last_close < breakeven
    ):
        return "attention", "shares below the wheel's break-even"

    # --- Take-Profit Candidate ----------------------------------------------
    if (
        not is_long
        and captured is not None
        and captured >= TAKE_PROFIT_CAPTURED_PCT
        and dte is not None
        and dte <= TAKE_PROFIT_MAX_DTE
    ):
        return (
            "take_profit_candidate",
            f"~{round(captured)}% of the credit banked (est.), {dte}d left",
        )

    # --- Evaluate ------------------------------------------------------------
    if row.get("underlying") in earnings_before_expiry:
        return "evaluate", "earnings before this leg expires"
    if itm:
        return "evaluate", f"in the money, {dte}d to expiry" if dte is not None else "in the money"
    if moneyness is not None and 0.0 <= moneyness <= NEAR_THE_MONEY_PCT and not is_long:
        return "evaluate", f"{moneyness:.1f}% from the strike"

    # --- Working -----------------------------------------------------------
    if is_long:
        return "working", f"{dte}d of protection left" if dte is not None else "protection open"
    return "working", "out of the money, time left"


def classify_open_legs(
    open_positions: Sequence[dict[str, Any]],
    earnings_before_expiry: Sequence[str] = (),
) -> dict[str, Any]:
    """Bucket every open leg. Returns
    ``{"buckets": {name: {label, legs, count, capital, open_premium}}, "rules": [...]}``.
    """
    earn = set(earnings_before_expiry)
    buckets: dict[str, dict[str, Any]] = {
        name: {"label": _BUCKET_LABELS[name], "legs": [], "count": 0,
               "capital": 0.0, "open_premium": 0.0}
        for name in _BUCKETS
    }

    for row in open_positions:
        name, reason = _classify(row, earn)
        b = buckets[name]
        b["legs"].append(
            {
                "cycle_id": row.get("cycle_id"),
                "underlying": row.get("underlying"),
                "name": row.get("name"),
                "type": row.get("type"),
                "side": row.get("side"),
                "strike": row.get("strike"),
                "contracts": row.get("contracts"),
                "expiration": row.get("expiration"),
                "days_to_expiry": row.get("days_to_expiry"),
                "reason": reason,
            }
        )
        b["count"] += 1
        b["capital"] += _leg_capital(row)
        b["open_premium"] += row.get("net_premium") or 0.0

    for b in buckets.values():
        b["capital"] = round(b["capital"], 2)
        b["open_premium"] = round(b["open_premium"], 2)
        b["legs"].sort(key=lambda leg: (leg["expiration"] or "", leg["underlying"] or ""))

    return {
        "buckets": buckets,
        "rules": [
            "Attention: in the money with <=7 days to expiry, a protective leg in its "
            "last week, or held shares below the wheel's break-even.",
            f"Take-Profit Candidate: an estimated >={int(TAKE_PROFIT_CAPTURED_PCT)}% of the "
            f"credit already banked (intrinsic-only estimate) with <={TAKE_PROFIT_MAX_DTE} days left.",
            "Evaluate: earnings before the leg expires, in the money with room left, or "
            f"within {NEAR_THE_MONEY_PCT:.0f}% of the strike.",
            "Working: everything else.",
        ],
    }
