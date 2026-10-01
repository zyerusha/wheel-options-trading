"""Assignment risk -- the mirror image of the "Cash for Cash-Secured Puts" card.

That card answers *what could I deploy*; this one answers *what could be called
on me right now*: every open short leg that is in the money, and -- for the puts
-- whether the cash is there to take the shares if every one of them assigns at
once.

The three headline numbers are deliberately a **solvency test**, not an
"additional cash needed" figure:

* ``assignment_obligation`` -- total strike value of the in-the-money puts, the
  cash those assignments consume.
* ``cash_available`` -- the whole cash balance (``net_worth.cash_total``). That
  balance still *includes* the cash a broker has reserved as CSP collateral --
  the reservation is a hold on this same money, not a separate pot -- so this is
  the figure the obligation is measured against.
* ``potential_shortfall`` -- ``max(obligation - available, 0)``. Normally zero,
  because the collateral was set aside when the puts were sold. A positive value
  means every ITM put assigning together would overdraw cash: a
  forced-liquidation / margin risk worth surfacing.

Everything here is read off the already-built ``open_positions`` rows
(``wheel/api.py`` ``_open_position_row``), so it is filter-independent for the
same reason that list is.
"""

from __future__ import annotations

from typing import Any, Sequence

_OPTION_MULTIPLIER = 100
# A short leg not yet in the money but within this % cushion of its strike is
# worth an early-warning mention.
_NEAR_THE_MONEY_PCT = 2.0


def _position_value(row: dict[str, Any]) -> float:
    """Strike x 100 x contracts for a short leg -- the cash a put assignment
    consumes, or the proceeds a called-away covered call brings in."""
    strike = row.get("strike") or 0.0
    contracts = row.get("contracts") or 0.0
    return round(strike * _OPTION_MULTIPLIER * contracts, 2)


def _common(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "cycle_id": row.get("cycle_id"),
        "underlying": row.get("underlying"),
        "name": row.get("name"),
        "strike": row.get("strike"),
        "contracts": row.get("contracts"),
        "expiration": row.get("expiration"),
        "days_to_expiry": row.get("days_to_expiry"),
        "moneyness_pct": row.get("moneyness_pct"),
        # Same roll-instead-of-resolve pricing as the Recommended buy-to-close
        # table (wheel/api.py, _min_roll_premium) -- carried through from the
        # source open_positions row rather than recomputed, so the two tables
        # can never disagree.
        "min_roll_premium": row.get("min_roll_premium"),
        "roll_dte": row.get("roll_dte"),
        "roll_target_date": row.get("roll_target_date"),
    }


def assignment_risk(
    open_positions: Sequence[dict[str, Any]],
    net_worth: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assignment exposure across the open short book. See module docstring."""
    itm_puts: list[dict[str, Any]] = []
    itm_calls: list[dict[str, Any]] = []
    near: list[dict[str, Any]] = []

    for row in open_positions:
        if row.get("side") != "SHORT":
            continue
        kind = row.get("type")  # "CSP" | "CC"
        moneyness = row.get("moneyness_pct")
        if row.get("in_the_money"):
            if kind == "CSP":
                itm_puts.append(
                    {**_common(row), "shares": (row.get("contracts") or 0.0) * _OPTION_MULTIPLIER,
                     "obligation": _position_value(row)}
                )
            elif kind == "CC":
                itm_calls.append(
                    {**_common(row),
                     "shares_at_risk_of_call": (row.get("contracts") or 0.0) * _OPTION_MULTIPLIER,
                     "proceeds_if_called": _position_value(row),
                     "shares_tracked": bool(row.get("shares_tracked"))}
                )
        elif moneyness is not None and 0.0 <= moneyness <= _NEAR_THE_MONEY_PCT and kind in ("CSP", "CC"):
            near.append({**_common(row), "kind": "put" if kind == "CSP" else "call"})

    itm_puts.sort(key=lambda r: (r["expiration"] or "", r["underlying"] or ""))
    itm_calls.sort(key=lambda r: (r["expiration"] or "", r["underlying"] or ""))
    near.sort(key=lambda r: (r["expiration"] or "", r["underlying"] or ""))

    obligation = round(sum(r["obligation"] for r in itm_puts), 2)
    # The Combined view nests per-book totals under ``.combined``; a single
    # account has them at the top level (same shape ``renderNetWorthTiles`` reads).
    nw = (net_worth or {}).get("combined") or net_worth or {}
    available = nw.get("cash_total") if (net_worth or {}).get("available") else None
    shortfall = (
        round(max(obligation - available, 0.0), 2) if available is not None else None
    )

    itm_expiries = [r["expiration"] for r in (itm_puts + itm_calls) if r.get("expiration")]
    return {
        "itm_puts": itm_puts,
        "itm_calls": itm_calls,
        "near_the_money": near,
        "assignment_obligation": obligation,
        "cash_available": available,
        "potential_shortfall": shortfall,
        "shares_committed_if_all_puts_assigned": round(sum(r["shares"] for r in itm_puts), 4),
        "shares_at_risk_of_call": round(sum(r["shares_at_risk_of_call"] for r in itm_calls), 4),
        "soonest_itm_expiry": min(itm_expiries) if itm_expiries else None,
        "count": len(itm_puts) + len(itm_calls),
    }
