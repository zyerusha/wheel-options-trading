"""Expiration calendar -- the still-open option book (the Open option positions
table) laid out on a time axis, per symbol, so "what lands on me, when, and where
it hurts" reads at a glance.

Forward-looking only: it groups the open legs from ``open_positions``
(``wheel/api.py`` ``_open_position_row``) by the date they expire -- three ways
in one payload (``days`` / ``weeks`` / ``months``), the frontend picks one. Each
bucket keeps a ``positions`` list, one entry per open leg, carrying the strike,
break-evens, moneyness and an ``at_a_loss`` verdict (strike below the wheel's
break-even for a covered call; underwater vs. the assignment break-even for a
cash-secured put; no intrinsic value left for a long leg).

Buckets are **sparse** (only the dates that actually carry an expiry); the
frontend places each one at its real date on a shared time axis.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Iterable, Mapping, Sequence

_OPTION_MULTIPLIER = 100
# Same window the candidate / open-positions tables use for the earnings warning.
_EARNINGS_WARN_DAYS = 14

# Open-position "type" -> the bar family the frontend colours (CC green, CSP
# blue, everything long grey).
_FAMILY = {"CC": "cc", "CSP": "csp", "LP": "long", "LC": "long"}

# How a realized (already-closed) leg reads in its tooltip / loss note.
_OUTCOME_LABEL = {"ASSIGNED": "assigned", "EXPIRED": "expired", "CLOSED": "closed"}


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _leg_capital(row: dict[str, Any]) -> float:
    """Position value a single open leg puts on the board: a short leg's
    collateral (strike x 100 x contracts), or a long leg's paid debit."""
    if row.get("side") == "LONG":
        return abs(row.get("net_premium") or 0.0)
    collateral = row.get("collateral")
    if collateral is not None:
        return collateral
    strike = row.get("strike") or 0.0
    contracts = row.get("contracts") or 0.0
    return strike * _OPTION_MULTIPLIER * contracts


def _loss_verdict(row: dict[str, Any]) -> tuple[bool, str | None]:
    """Is this open position sitting at a loss right now, and why -- keyed off
    strike vs. break-even, the number the Open positions table already shows."""
    kind = row.get("type")
    strike = row.get("strike")
    wheel_be = row.get("wheel_breakeven")
    be = row.get("breakeven")
    last = row.get("last_close")

    if kind == "CC":
        if wheel_be is not None and strike is not None and strike < wheel_be:
            return True, f"call strike ${strike:g} is below the wheel break-even ${wheel_be:,.2f} -- assignment locks in a loss on the shares"
        return False, None
    if kind == "CSP":
        if last is not None and be is not None and last < be:
            return True, f"{row.get('underlying')} ${last:,.2f} is below the put's break-even ${be:,.2f} -- assignment starts underwater"
        return False, None
    # Long protective / directional leg: at a loss once it has no intrinsic left.
    if not row.get("in_the_money"):
        return True, "long leg is out of the money -- the debit is at risk if it expires here"
    return False, None


def _new_bucket(seed: dict[str, Any]) -> dict[str, Any]:
    return {
        **seed,
        "positions": [],
        "capital_exposure": 0.0,
        "count": 0,
        "has_itm": False,
        "has_earnings": False,
        "has_loss": False,
        # Per-family capital totals, kept for the compact summary / older callers.
        "types": {fam: {"capital": 0.0, "count": 0, "has_itm": False} for fam in ("cc", "csp", "long")},
    }


def expiration_calendar(
    open_positions: Sequence[dict[str, Any]],
    earnings_dates: Mapping[str, str] | Iterable[str] = (),
    through: date | None = None,
    recent_closes: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """``{"days": [...], "weeks": [...], "months": [...], "as_of": iso}``. Each
    bucket carries a ``start`` ISO date and a ``positions`` list (one per open
    leg). ``as_of`` is the "today" anchor for the frontend axis.

    ``earnings_dates`` is ``{ticker: ISO date}`` -- a position is flagged
    ``earnings_before_expiry`` (with the date, for its tooltip) when the ticker's
    next report lands on or before that leg's own expiry. A bare iterable of
    tickers is still accepted (flag only, no date).

    ``recent_closes`` is a list of already-closed wheel legs (assigned / called
    away) to show as faded "what just happened" bars left of today. Each carries
    ``close_date``, ``capital``, ``realized_pl``, ``outcome`` plus the usual
    ticker / type / strike / contracts. They are bucketed by their **close
    date** into ``days`` and ``weeks`` always, and into ``months`` only for
    months already fully past -- the current month's slot is kept for the legs
    still open and expiring in it, which a past close can't be told apart from.
    """
    if isinstance(earnings_dates, Mapping):
        earn_map: dict[str, str] = dict(earnings_dates)
    else:
        earn_map = {t: "" for t in earnings_dates}
    _today = date.today()
    as_of = (through or _today).isoformat()
    legs = [row for row in open_positions if row.get("expiration")]
    if not legs and not any(r.get("close_date") for r in recent_closes):
        return {"days": [], "weeks": [], "months": [], "as_of": as_of}

    by_day: dict[date, dict[str, Any]] = {}
    by_week: dict[date, dict[str, Any]] = {}
    by_month: dict[str, dict[str, Any]] = {}
    for row in legs:
        exp = date.fromisoformat(row["expiration"])
        wk = _monday(exp)
        month_key = f"{exp.year:04d}-{exp.month:02d}"
        itm = bool(row.get("in_the_money"))
        family = _FAMILY.get(row.get("type"), "long")
        cap = _leg_capital(row)
        at_a_loss, loss_note = _loss_verdict(row)
        earn_raw = earn_map.get(row.get("underlying"))  # ISO date, "" (flag-only), or None
        # earnings land on or before THIS leg's expiry (kept for reference)
        earn_before = earn_raw is not None and (earn_raw == "" or earn_raw <= row["expiration"])
        # ...and the actual warning flag, identical to the candidate / open-
        # positions tables: the next report is 0-14 days out from today.
        days_to_earnings = (date.fromisoformat(earn_raw) - _today).days if earn_raw else None
        earn_soon = days_to_earnings is not None and 0 <= days_to_earnings <= _EARNINGS_WARN_DAYS
        earn_date = earn_raw or None

        position = {
            "underlying": row.get("underlying"),
            "name": row.get("name"),
            "cycle_id": row.get("cycle_id"),
            "type": row.get("type"),
            "family": family,
            "strike": row.get("strike"),
            "contracts": row.get("contracts"),
            "expiration": row["expiration"],
            "days_to_expiry": row.get("days_to_expiry"),
            "in_the_money": itm,
            "moneyness_pct": row.get("moneyness_pct"),
            "breakeven": row.get("breakeven"),
            "wheel_breakeven": row.get("wheel_breakeven"),
            "last_close": row.get("last_close"),
            "annualized_yield_pct": row.get("annualized_yield_pct"),
            "capital": round(cap, 2),
            "at_a_loss": at_a_loss,
            "loss_note": loss_note,
            "earnings_before_expiry": earn_before,
            "earnings_soon": earn_soon,
            "days_to_earnings": days_to_earnings,
            "earnings_date": earn_date,
        }

        for bucket_map, key, seed in (
            (by_day, exp, {"start": exp.isoformat(), "date": exp.isoformat()}),
            (by_week, wk, {"start": wk.isoformat(), "week_start": wk.isoformat()}),
            (by_month, month_key, {"start": f"{month_key}-01", "month": month_key}),
        ):
            bucket = bucket_map.setdefault(key, _new_bucket(seed))
            bucket["positions"].append(position)
            bucket["capital_exposure"] += cap
            bucket["count"] += 1
            bucket["has_itm"] = bucket["has_itm"] or itm
            bucket["has_earnings"] = bucket["has_earnings"] or earn_soon
            bucket["has_loss"] = bucket["has_loss"] or at_a_loss
            fam = bucket["types"][family]
            fam["capital"] += cap
            fam["count"] += 1
            fam["has_itm"] = fam["has_itm"] or itm

    # ---- realized closes: assigned / called-away legs, bucketed by close date.
    # Day and week always; month only for months already fully past -- the
    # current month's slot is reserved for the still-open legs expiring in it,
    # and a past close can't be told apart from a future expiry there.
    _cur_month = f"{_today.year:04d}-{_today.month:02d}"
    rby_day: dict[date, dict[str, Any]] = {}
    rby_week: dict[date, dict[str, Any]] = {}
    rby_month: dict[str, dict[str, Any]] = {}
    for row in recent_closes:
        cd_raw = row.get("close_date")
        if not cd_raw:
            continue
        cd = date.fromisoformat(cd_raw)
        wk = _monday(cd)
        cd_month = f"{cd.year:04d}-{cd.month:02d}"
        family = _FAMILY.get(row.get("type"), "long")
        cap = float(row.get("capital") or 0.0)
        rpl = row.get("realized_pl")
        at_a_loss = rpl is not None and rpl < 0
        outcome = row.get("outcome") or "CLOSED"
        verb = _OUTCOME_LABEL.get(outcome, "closed")
        position = {
            "underlying": row.get("underlying"),
            "name": row.get("name"),
            "cycle_id": row.get("cycle_id"),
            "type": row.get("type"),
            "family": family,
            "strike": row.get("strike"),
            "contracts": row.get("contracts"),
            "expiration": row.get("expiration"),
            "close_date": cd_raw,
            "days_to_expiry": None,
            "in_the_money": False,
            "moneyness_pct": None,
            "breakeven": row.get("breakeven"),
            "wheel_breakeven": row.get("wheel_breakeven"),
            "last_close": row.get("last_close"),
            "annualized_yield_pct": None,
            "capital": round(cap, 2),
            "realized": True,
            "realized_pl": round(rpl, 2) if rpl is not None else None,
            "outcome": outcome,
            "at_a_loss": at_a_loss,
            "loss_note": (
                f"{row.get('underlying')} {verb} at a loss ({rpl:,.0f})" if at_a_loss else None
            ),
            "earnings_before_expiry": False,
            "earnings_soon": False,
            "days_to_earnings": None,
            "earnings_date": None,
        }
        targets = [
            (rby_day, cd, {"start": cd.isoformat(), "date": cd.isoformat(), "realized": True}),
            (rby_week, wk, {"start": wk.isoformat(), "week_start": wk.isoformat(), "realized": True}),
        ]
        if cd_month < _cur_month:
            targets.append(
                (rby_month, cd_month, {"start": f"{cd_month}-01", "month": cd_month, "realized": True})
            )
        for bucket_map, key, seed in targets:
            bucket = bucket_map.setdefault(key, _new_bucket(seed))
            bucket["positions"].append(position)
            bucket["capital_exposure"] += cap
            bucket["count"] += 1
            bucket["has_loss"] = bucket["has_loss"] or at_a_loss
            fam = bucket["types"][family]
            fam["capital"] += cap
            fam["count"] += 1

    def _finish(bucket: dict[str, Any]) -> dict[str, Any]:
        bucket["capital_exposure"] = round(bucket["capital_exposure"], 2)
        for fam in bucket["types"].values():
            fam["capital"] = round(fam["capital"], 2)
        # Losers first, then biggest capital, then ticker -- the order the bars
        # are drawn left-to-right in each bucket.
        bucket["positions"].sort(
            key=lambda p: (not p["at_a_loss"], -(p["capital"] or 0.0), p["underlying"] or "")
        )
        return bucket

    def _merge(upcoming: dict, realized: dict) -> list[dict[str, Any]]:
        combined = [_finish(upcoming[k]) for k in upcoming]
        combined += [_finish(realized[k]) for k in realized]
        combined.sort(key=lambda b: b["start"])
        return combined

    return {
        "days": _merge(by_day, rby_day),
        "weeks": _merge(by_week, rby_week),
        "months": _merge(by_month, rby_month),
        "as_of": as_of,
    }
