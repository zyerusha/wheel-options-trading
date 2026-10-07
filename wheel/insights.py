"""Plain-rules commentary: what is working, where to improve.

Two entry points, same shape (``{"strengths": [...], "improvements": [...]}``)
and same house style — no model, no network, every line a threshold on figures
:mod:`wheel.metrics` / :mod:`wheel.api` already produce, phrased as advice.
Strengths keep a curated priority order; improvements are ranked by dollar
impact so the costliest problem shows first.

* :func:`wheel_insights` — one wheel (Trade Log), up to two strengths / three
  improvements.
* :func:`portfolio_insights` — the whole book (Dashboard), up to three each,
  working purely off the already-serialized payload dicts.
"""

from __future__ import annotations

from typing import Any

from wheel.engine import COVERED_CALL, CSP, LONG, Cycle
from wheel.metrics import CycleMetrics

_MAX_STRENGTHS = 2
_MAX_IMPROVEMENTS = 3
_MAX_PORTFOLIO_STRENGTHS = 3
_MAX_PORTFOLIO_IMPROVEMENTS = 3


def _money(value: float) -> str:
    return f"-${abs(value):,.0f}" if value < 0 else f"${value:,.0f}"


def _price(value: float) -> str:
    return f"${value:.2f}"


def wheel_insights(
    cycle: Cycle,
    metrics: CycleMetrics,
    *,
    current_price: float | None = None,
    cost_basis: float | None = None,
    dividends: float = 0.0,
    break_even_price: float | None = None,
    mark_to_market_pl: float | None = None,
) -> dict[str, list[str]]:
    strengths: list[str] = []
    improvements: list[tuple[float | None, str]] = []

    # Not a wheel. Wheel coaching (strike selection, buyback drag, break-even,
    # idle shares) does not apply; every rule below is wheel-shaped, so
    # short-circuit with one honest line, phrased by kind.
    if not cycle.is_wheel:
        net = metrics.net_realized_pl + metrics.option_open_premium
        if cycle.kind == "hold":
            held = sum(lot.remaining for lot in cycle.share_lots if lot.remaining > 1e-9)
            state = (
                f"{held:,.0f} shares still held" if held > 1e-9 else f"realized {_money(net)}"
            )
            line = (
                f"{cycle.underlying} is held outright, no option has ever been sold against "
                f"it ({state}). Its profit and loss is counted, but percentage returns that "
                "measure option income against the cash an option ties up are left blank, "
                "because no option was ever sold here. Selling a covered call would start one."
            )
        else:
            outcome = (
                f"closed up {_money(net)}" if net > 0 else f"closed down {_money(net)}" if net < 0 else "closed flat"
            )
            line = (
                f"This was an option bought outright as a bet on the share price, not an "
                f"option sold for income ({outcome}). Its profit and loss is counted on its "
                "own; it is left out of the percentage returns, which measure only options sold."
            )
        return {"strengths": [], "improvements": [line]}

    closed = [leg for leg in cycle.legs if not leg.is_open]
    open_legs = [leg for leg in cycle.legs if leg.is_open]
    shares_held = sum(lot.remaining for lot in cycle.share_lots if lot.remaining > 1e-9)
    open_long_debit = sum(leg.open_premium for leg in open_legs if leg.side == LONG)  # <= 0
    has_open_covered_call = any(leg.strategy == COVERED_CALL for leg in open_legs)

    basis_lots = [
        lot
        for lot in cycle.share_lots
        if lot.remaining > 1e-9 and lot.basis_known and lot.basis_per_share is not None
    ]
    avg_basis = (
        sum(lot.basis_per_share * lot.remaining for lot in basis_lots)
        / sum(lot.remaining for lot in basis_lots)
        if basis_lots
        else None
    )

    non_stock_pl = (
        metrics.option_realized_pl
        + metrics.stock_realized_pl
        + dividends
        + metrics.option_open_premium
    )
    premium_covers_basis = (
        shares_held > 1e-9
        and cost_basis is not None
        and cost_basis - non_stock_pl / shares_held <= 0
    )

    decided = metrics.wins + metrics.losses
    above_water = (
        shares_held > 1e-9
        and break_even_price is not None
        and current_price is not None
        and current_price >= break_even_price
    )
    underwater = (
        shares_held > 1e-9
        and break_even_price is not None
        and current_price is not None
        and current_price < break_even_price
    )

    # ---------------- strengths (curated order) ----------------

    if premium_covers_basis:
        strengths.append(
            "The cash collected for selling options, plus profit already taken, now adds up "
            "to more than the shares cost you. Whatever the stock does from here, you are ahead."
        )
    if above_water:
        strengths.append(
            f"At {_price(current_price)} the shares are above {_price(break_even_price)}, the "
            "price they need to reach for every dollar paid and collected here to cancel out. "
            "Selling everything today would leave you even or ahead."
        )
    if metrics.win_rate_pct is not None and metrics.win_rate_pct >= 70 and decided >= 5:
        strengths.append(
            f"{metrics.wins} of {decided} finished option trades made money "
            f"({metrics.win_rate_pct:.0f}%), so the strike prices being chosen are working."
        )
    if metrics.wheel_core_realized_pl > 50 and metrics.premium_received > 0:
        kept = 100 * metrics.wheel_core_realized_pl / metrics.premium_received
        strengths.append(
            f"Of the {_money(metrics.premium_received)} collected for selling puts and covered "
            f"calls, {_money(metrics.wheel_core_realized_pl)} ({kept:.0f}%) was kept after "
            "buying any of them back."
        )
    if metrics.hedge_realized_pl > 50:
        strengths.append(
            f"Options bought here for protection have made {_money(metrics.hedge_realized_pl)} "
            "overall, more than covering what they cost."
        )
    if (
        metrics.annualized_wheel_roc_pct is not None
        and metrics.annualized_wheel_roc_pct >= 20
        and metrics.option_realized_pl > 0
    ):
        strengths.append(
            f"Selling options has returned {metrics.annualized_wheel_roc_pct:.0f}% a year, "
            "measured against the cash held aside for the puts plus the cost of any shares held."
        )
    if not cycle.is_open and mark_to_market_pl is not None and mark_to_market_pl > 0:
        strengths.append(
            f"Nothing is open here any more, and it finished ahead by {_money(mark_to_market_pl)}."
        )

    # ---------------- improvements (ranked by $ impact) ----------------

    if metrics.wheel_core_realized_pl < -50 and metrics.premium_received > 0:
        improvements.append(
            (
                metrics.wheel_core_realized_pl,
                "Buying back the puts and calls you sold has cost more than they brought in, "
                f"leaving {_money(metrics.wheel_core_realized_pl)} on those trades. Letting "
                "puts expire, or letting the shares be put to you, keeps more of the cash "
                "than paying to replace a losing option.",
            )
        )
    if underwater:
        gap = break_even_price - current_price
        improvements.append(
            (
                -gap * shares_held,
                f"The shares must reach {_price(break_even_price)} for every dollar paid and "
                f"collected here to cancel out; they are at {_price(current_price)}, "
                f"{_price(gap)} per share short. Selling covered calls at or above that price "
                "closes the gap and does not add to the risk.",
            )
        )
    if shares_held >= 100 and not has_open_covered_call:
        ref = break_even_price if break_even_price is not None else avg_basis
        tail = f" at or above {_price(ref)}" if ref is not None else ""
        improvements.append(
            (
                None,
                f"{shares_held:,.0f} shares are held with no covered call sold against them, "
                f"so that money earns nothing right now. Selling a call{tail} brings in cash "
                "against stock you already own.",
            )
        )
    if open_long_debit < -50:
        improvements.append(
            (
                open_long_debit,
                f"An option bought for protection cost {_money(abs(open_long_debit))} and is "
                "worth nothing if it expires with the share price on the wrong side of its "
                f"strike. Protection bought here has returned "
                f"{_money(metrics.hedge_realized_pl)} in total so far. Buy only as much as "
                "the risk actually calls for.",
            )
        )

    winners = [leg.realized_pl for leg in closed if leg.realized_pl > 0]
    losers = [leg.realized_pl for leg in closed if leg.realized_pl < 0]
    if len(winners) >= 2 and len(losers) >= 2:
        avg_win = sum(winners) / len(winners)
        avg_loss = -sum(losers) / len(losers)
        if avg_win > 0 and avg_loss >= 2.5 * avg_win:
            improvements.append(
                (
                    -(avg_loss - avg_win) * len(losers),
                    f"Finished option trades that lost money here average {_money(-avg_loss)} "
                    f"each, {avg_loss / avg_win:.1f} times the {_money(avg_win)} average "
                    "winning trade. Deciding in advance at what loss you will close a trade, "
                    "or replace it with a later-expiring one, would even that out.",
                )
            )

    csp_legs = sorted(
        (leg for leg in cycle.legs if leg.strategy == CSP), key=lambda leg: leg.open_date
    )
    if len(csp_legs) >= 3:
        strikes = [leg.strike for leg in csp_legs]
        steps_down = sum(1 for a, b in zip(strikes, strikes[1:]) if b < a - 1e-9)
        if (
            steps_down >= len(strikes) - 2
            and strikes[-1] < strikes[0] * 0.9
            and (metrics.stock_unrealized_pl or 0) < -100
        ):
            improvements.append(
                (
                    metrics.stock_unrealized_pl,
                    f"Each new put was sold at a lower strike price as the stock fell, from "
                    f"${strikes[0]:g} down to ${strikes[-1]:g}. Buying in lower each time "
                    f"deepened the loss you would take by selling the shares today, now "
                    f"{_money(metrics.stock_unrealized_pl)}.",
                )
            )

    if any(not lot.basis_known for lot in cycle.share_lots):
        improvements.append(
            (
                None,
                "Some shares you hold were bought before the oldest file you loaded, so what "
                "you paid for them is unknown. The price the shares must reach to cancel out, "
                "and their gain or loss, are estimates here. Load an earlier transaction "
                "export to fix it.",
            )
        )
    elif metrics.capital_estimated:
        improvements.append(
            (
                None,
                "Some shares here were bought before the oldest file you loaded, so their real "
                "cost is unknown and the strike price is used instead. Any percentage return "
                "on capital shown for this position is only an approximation.",
            )
        )

    improvements.sort(
        key=lambda item: (item[0] is not None, abs(item[0]) if item[0] is not None else 0.0),
        reverse=True,
    )
    return {
        "strengths": strengths[:_MAX_STRENGTHS],
        "improvements": [text for _, text in improvements[:_MAX_IMPROVEMENTS]],
    }


def _num(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) else None


def portfolio_insights(
    portfolio: dict,
    wheels: list[dict],
    open_hedges: list[dict],
    *,
    wheel_return: dict | None = None,
    benchmark: dict | None = None,  # whole-account XIRR block; accepted but not used — see below
    wheel_state: dict | None = None,
) -> dict[str, list[str]]:
    """Book-level commentary for the Dashboard, from the already-built payload.

    Everything here reads serialized dicts — ``portfolio`` (the filtered
    ``PortfolioMetrics``), the full-history Trade Log ``wheels``, the
    ``open_hedges`` list, and the wheel-only XIRR block — so it is trivially
    testable and never re-derives a figure the API already computed.

    ``benchmark`` (the *whole-account* XIRR vs SPY buy-and-hold) is deliberately
    not turned into an insight: it blends in idle cash and deliberate
    buy-and-hold holdings and rests on a hand-configured opening balance, so a
    "trails SPY" line there says nothing about the wheel. The wheel-vs-SPY
    comparison that *is* apples-to-apples — the same dollars, same dates, put
    in SPY instead — comes from ``wheel_return`` and is the first strength.
    """
    portfolio = portfolio or {}
    wheels = wheels or []
    open_hedges = open_hedges or []
    strengths: list[str] = []
    improvements: list[tuple[float | None, str]] = []

    active = [w for w in wheels if w.get("status") == "ACTIVE"]

    # ---------------- strengths (curated order) ----------------

    wr = wheel_return or {}
    wr_xirr = _num(wr.get("xirr_pct"))
    wr_bench = _num((wr.get("benchmark") or {}).get("xirr_pct"))
    if wr.get("available") and wr_xirr is not None and wr_bench is not None and wr_xirr - wr_bench >= 3:
        added = _num(wr.get("value_added"))
        added_s = f", {_money(added)} ahead" if added else ""
        bench_name = (wr.get("benchmark") or {}).get("name", "SPY")
        strengths.append(
            f"On the capital actually committed to the wheel, counting exactly when each "
            f"dollar went in and came out, it returned {wr_xirr:.0f}% a year vs {wr_bench:.0f}% "
            f"for those same dollars, on the same dates, put in {bench_name} instead{added_s}. "
            "(Idle cash and buy-and-hold positions are excluded from both sides.)"
        )

    win_rate = _num(portfolio.get("win_rate_pct"))
    decided = (portfolio.get("wins") or 0) + (portfolio.get("losses") or 0)
    if win_rate is not None and win_rate >= 70 and decided >= 20:
        strengths.append(
            f"{portfolio.get('wins', 0)} of {decided} finished option trades made money "
            f"({win_rate:.0f}% across every account shown)."
        )

    roc = _num(portfolio.get("annualized_wheel_roc_pct"))
    if roc is not None and roc >= 10 and (portfolio.get("option_realized_pl") or 0) > 0:
        # The denominator this percentage is actually computed against is the average
        # capital of the option-selling positions only, not of every position in the
        # book, so quote that field and fall back only if it is absent.
        avg_cap = _num(portfolio.get("wheel_avg_capital"))
        if avg_cap is None:
            avg_cap = _num(portfolio.get("avg_capital")) or 0.0
        strengths.append(
            f"Selling options has returned {roc:.0f}% a year, measured against the "
            f"{_money(avg_cap)} of cash those option-selling positions tied up on average."
        )

    div = _num(portfolio.get("dividends_received")) or 0.0
    if div >= 500:
        strengths.append(
            f"{_money(div)} in dividends on shares you ended up owning because a put you sold "
            "was exercised, on top of the cash the options themselves brought in."
        )

    runway_hedges = [h for h in open_hedges if h.get("phase") == "runway"]
    if runway_hedges:
        names = ", ".join(dict.fromkeys(h["underlying"] for h in runway_hedges[:3]))
        strengths.append(
            f"{len(runway_hedges)} protective option(s) are open with months still left on "
            f"them ({names}), so a big drop is covered while the options you sell keep "
            "bringing in cash."
        )

    # ---------------- improvements (ranked by $ impact) ----------------

    # NB: no whole-account "trails SPY buy-and-hold" rule here on purpose. That
    # comparison blends in idle cash and deliberate buy-and-hold positions, so a
    # cash-heavy account "trails" SPY for reasons that have nothing to do with
    # the wheel -- and it rests on a hand-configured opening balance. The valid
    # apples-to-apples comparison (wheel dollars vs the same dollars in SPY) is
    # the wheel_return strength above; the whole-account figure still lives on
    # the "Net worth & benchmark" card with its full context.

    underwater = sorted(
        (w for w in active if (_num(w.get("mark_to_market_pl")) or 0.0) < -200),
        key=lambda w: w["mark_to_market_pl"],
    )
    if underwater:
        total = sum(w["mark_to_market_pl"] for w in underwater)
        worst = ", ".join(f"{w['underlying']} {_money(w['mark_to_market_pl'])}" for w in underwater[:3])
        improvements.append(
            (
                total,
                f"{len(underwater)} active positions would lose {_money(total)} in total if "
                f"everything were closed at today's prices (worst: {worst}). Selling a covered "
                "call at or above the price those shares must reach to cancel out their cost "
                "brings in cash without raising the risk.",
            )
        )

    holding = ((wheel_state or {}).get("buckets") or {}).get("holding") or {}
    hold_amt = _num(holding.get("amount")) or 0.0
    hold_n = holding.get("cycles") or 0
    if hold_amt >= 20000 and hold_n >= 2:
        improvements.append(
            (
                -hold_amt * 0.01,
                f"{_money(hold_amt)} of held shares across {hold_n} positions have no covered "
                "call sold against them, so that money brings in nothing. Selling calls at or "
                "above the price those shares must reach to cancel out their cost adds income "
                "against stock already owned.",
            )
        )

    if wheels:
        top = max(wheels, key=lambda w: _num(w.get("capital_committed_pct")) or 0.0)
        pct = _num(top.get("capital_committed_pct")) or 0.0
        amt = _num(top.get("capital_committed_now")) or 0.0
        # >=15% of the book AND a real position size -- the Combined view keeps
        # each wheel's own account-scoped %, so a lone small wheel can read 100%.
        if pct >= 15 and amt >= 25000:
            of = top.get("capital_committed_pct_of", "the cash tied up across every wheel")
            improvements.append(
                (
                    amt,
                    f"{top['underlying']} is {pct:.0f}% of {of} ({_money(amt)}). A sharp fall "
                    "in that one stock would move your whole account.",
                )
            )

    dir_net = sum(_num(w.get("net_realized_pl")) or 0.0 for w in wheels if w.get("kind") == "directional")
    if dir_net < -100:
        improvements.append(
            (
                dir_net,
                f"Options bought outright as bets on the share price, rather than sold for "
                f"income, have cost {_money(dir_net)} in total. They are left out of the "
                "percentage returns, but the money is really gone.",
            )
        )

    urgent = [h for h in open_hedges if h.get("phase") in ("wind_down", "expiring")]
    if urgent:
        names = ", ".join(f"{h['underlying']} ({h['days_to_expiry']}d)" for h in urgent[:3])
        improvements.append(
            (
                None,
                f"{len(urgent)} protective option(s) expire soon ({names}). An option is worth "
                "something just for the time left on it, and that part runs out as expiry "
                "nears: sell them now to recover it, or replace them with ones expiring later.",
            )
        )

    est = list(
        dict.fromkeys(w["underlying"] for w in active if w.get("capital_estimated"))
    )
    if est:
        improvements.append(
            (
                None,
                f"{len(est)} active positions ({', '.join(est)}) hold shares bought before the "
                "oldest file you loaded, so the strike price stands in for their real cost. "
                "Their percentage returns on capital are only approximations.",
            )
        )

    improvements.sort(
        key=lambda item: (item[0] is not None, abs(item[0]) if item[0] is not None else 0.0),
        reverse=True,
    )
    return {
        "strengths": strengths[:_MAX_PORTFOLIO_STRENGTHS],
        "improvements": [text for _, text in improvements[:_MAX_PORTFOLIO_IMPROVEMENTS]],
    }
