"""Plain-rules commentary: what is working, where to improve.

Two entry points, same shape (``{"strengths": [...], "improvements": [...]}``)
and same house style -- no model, no network, every line a threshold on figures
:mod:`wheel.metrics` / :mod:`wheel.api` already produce, phrased as advice.
Strengths keep a curated priority order; improvements are ranked by dollar
impact so the costliest problem shows first.

* :func:`wheel_insights` -- one wheel (Trade Log), up to two strengths / three
  improvements.
* :func:`portfolio_insights` -- the whole book (Dashboard), up to three each,
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

    # Not a wheel -- a lone directional/long-only cycle. Wheel coaching (strike
    # selection, buyback drag, break-even, idle shares) does not apply; every
    # rule below is wheel-shaped, so short-circuit with one honest line.
    if not cycle.is_wheel:
        net = metrics.net_realized_pl + metrics.option_open_premium
        outcome = f"closed up {_money(net)}" if net > 0 else f"closed down {_money(net)}" if net < 0 else "closed flat"
        return {
            "strengths": [],
            "improvements": [
                f"This was a directional long-option position, not a wheel ({outcome}). "
                "It is kept out of the wheel-return figures; only its P&L counts."
            ],
        }

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
            "Premium and profit already banked exceed your remaining share cost -- "
            "anything the stock does from here is upside."
        )
    if above_water:
        strengths.append(
            f"Shares sit above the wheel's break-even ({_price(break_even_price)}) "
            f"at {_price(current_price)} -- you could close flat-plus right now."
        )
    if metrics.win_rate_pct is not None and metrics.win_rate_pct >= 70 and decided >= 5:
        strengths.append(
            f"{metrics.wins} of {decided} closed legs finished green "
            f"({metrics.win_rate_pct:.0f}% win rate) -- strike selection is working."
        )
    if metrics.wheel_core_realized_pl > 50 and metrics.premium_received > 0:
        kept = 100 * metrics.wheel_core_realized_pl / metrics.premium_received
        strengths.append(
            f"The core wheel kept +{_money(metrics.wheel_core_realized_pl)} of the "
            f"{_money(metrics.premium_received)} premium sold ({kept:.0f}%)."
        )
    if metrics.hedge_realized_pl > 50:
        strengths.append(
            f"Protective/long options netted +{_money(metrics.hedge_realized_pl)} -- "
            "the hedge more than paid for itself."
        )
    if (
        metrics.annualized_wheel_roc_pct is not None
        and metrics.annualized_wheel_roc_pct >= 20
        and metrics.option_realized_pl > 0
    ):
        strengths.append(
            f"Option writing has returned {metrics.annualized_wheel_roc_pct:.0f}% "
            "annualized on the capital it tied up."
        )
    if not cycle.is_open and mark_to_market_pl is not None and mark_to_market_pl > 0:
        strengths.append(f"This wheel is flat and green right now at +{_money(mark_to_market_pl)}.")

    # ---------------- improvements (ranked by $ impact) ----------------

    if metrics.wheel_core_realized_pl < -50 and metrics.premium_received > 0:
        improvements.append(
            (
                metrics.wheel_core_realized_pl,
                "Buying short options back has cost more than they collected -- the core "
                f"wheel is {_money(metrics.wheel_core_realized_pl)}. Letting more puts "
                "expire, or taking assignment, keeps more premium than rolling losers at a debit.",
            )
        )
    if underwater:
        gap = break_even_price - current_price
        improvements.append(
            (
                -gap * shares_held,
                f"Stock must reach {_price(break_even_price)} to close flat "
                f"(now {_price(current_price)}, +{_price(gap)}/share). Selling covered calls "
                "at or above that strike closes the gap without adding downside risk.",
            )
        )
    if shares_held >= 100 and not has_open_covered_call:
        ref = break_even_price if break_even_price is not None else avg_basis
        tail = f" at or above {_price(ref)}" if ref is not None else ""
        improvements.append(
            (
                None,
                f"{shares_held:,.0f} shares are held with no covered call written -- that "
                f"capital earns nothing right now. A call{tail} adds premium against stock "
                "you already own.",
            )
        )
    if open_long_debit < -50:
        improvements.append(
            (
                open_long_debit,
                f"An open long option is {_money(open_long_debit)} if it expires worthless; "
                f"long options have returned {_money(metrics.hedge_realized_pl)} overall on "
                "this wheel. Size protection to the risk you actually need.",
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
                    f"The average loser here ({_money(-avg_loss)}) is {avg_loss / avg_win:.1f}x "
                    f"the average winner (+{_money(avg_win)}). A fixed roll-or-close rule on "
                    "losers would flatten that.",
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
                    f"Puts were added at falling strikes ({strikes[0]:g} -> {strikes[-1]:g}) as "
                    f"the stock dropped -- averaging down deepened the unrealized loss "
                    f"({_money(metrics.stock_unrealized_pl)}).",
                )
            )

    if any(not lot.basis_known for lot in cycle.share_lots):
        improvements.append(
            (
                None,
                "Some held shares pre-date the export, so their cost basis is unknown -- "
                "break-even and stock P&L here are estimates.",
            )
        )
    elif metrics.capital_estimated:
        improvements.append(
            (
                None,
                "Committed capital uses a strike-based proxy for shares bought before the "
                "export -- the ROC figures are approximate.",
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
    benchmark: dict | None = None,
    wheel_state: dict | None = None,
) -> dict[str, list[str]]:
    """Book-level commentary for the Dashboard, from the already-built payload.

    Everything here reads serialized dicts -- ``portfolio`` (the filtered
    ``PortfolioMetrics``), the full-history Trade Log ``wheels``, the
    ``open_hedges`` list, and the two XIRR blocks -- so it is trivially testable
    and never re-derives a figure the API already computed.
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
        added_s = f", {_money(added)} ahead of that replay" if added else ""
        bench_name = (wr.get("benchmark") or {}).get("name", "SPY")
        strengths.append(
            f"The wheel's money-weighted return is {wr_xirr:.0f}% vs {wr_bench:.0f}% for a "
            f"same-timing {bench_name} replay{added_s}."
        )

    win_rate = _num(portfolio.get("win_rate_pct"))
    decided = (portfolio.get("wins") or 0) + (portfolio.get("losses") or 0)
    if win_rate is not None and win_rate >= 70 and decided >= 20:
        strengths.append(
            f"{portfolio.get('wins', 0)} of {decided} decided legs finished green "
            f"({win_rate:.0f}% win rate across the book)."
        )

    roc = _num(portfolio.get("annualized_wheel_roc_pct"))
    if roc is not None and roc >= 10 and (portfolio.get("option_realized_pl") or 0) > 0:
        avg_cap = _num(portfolio.get("avg_capital")) or 0.0
        strengths.append(
            f"Option writing has returned {roc:.0f}% annualized on about {_money(avg_cap)} of "
            "average committed capital."
        )

    div = _num(portfolio.get("dividends_received")) or 0.0
    if div >= 500:
        strengths.append(f"{_money(div)} in dividends on assigned shares, on top of option premium.")

    runway_hedges = [h for h in open_hedges if h.get("phase") == "runway"]
    if runway_hedges:
        names = ", ".join(dict.fromkeys(h["underlying"] for h in runway_hedges[:3]))
        strengths.append(
            f"{len(runway_hedges)} protective hedge(s) in place with runway ({names}) -- "
            "downside is capped while premium keeps coming in."
        )

    # ---------------- improvements (ranked by $ impact) ----------------

    bm = benchmark or {}
    act_xirr = _num((bm.get("actual") or {}).get("xirr_pct"))
    ref_xirr = _num((bm.get("benchmark") or {}).get("xirr_pct"))
    if bm.get("available") and act_xirr is not None and ref_xirr is not None and act_xirr < ref_xirr - 2:
        va = _num(bm.get("value_added"))
        bench_name = (bm.get("benchmark") or {}).get("name", "SPY")
        gap_s = f" -- about {_money(abs(va))} of return forgone" if va else ""
        improvements.append(
            (
                va,
                f"The whole account's money-weighted return ({act_xirr:.0f}%) trails a "
                f"{bench_name} buy-and-hold ({ref_xirr:.0f}%){gap_s}. The wheel itself "
                "outperformed; the drag is elsewhere -- idle cash or non-wheel holdings.",
            )
        )

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
                f"{len(underwater)} active wheels are underwater by {_money(total)} "
                f"mark-to-market (worst: {worst}). Covered calls at or above their break-even "
                "close the gap without adding downside.",
            )
        )

    holding = ((wheel_state or {}).get("buckets") or {}).get("holding") or {}
    hold_amt = _num(holding.get("amount")) or 0.0
    hold_n = holding.get("cycles") or 0
    if hold_amt >= 20000 and hold_n >= 2:
        improvements.append(
            (
                -hold_amt * 0.01,
                f"{_money(hold_amt)} of assigned shares across {hold_n} wheels have no covered "
                "call written -- that capital collects no premium. Selling calls at or above "
                "break-even adds income against stock already owned.",
            )
        )

    if wheels:
        top = max(wheels, key=lambda w: _num(w.get("capital_committed_pct")) or 0.0)
        pct = _num(top.get("capital_committed_pct")) or 0.0
        amt = _num(top.get("capital_committed_now")) or 0.0
        # >=15% of the book AND a real position size -- the Combined view keeps
        # each wheel's own account-scoped %, so a lone small wheel can read 100%.
        if pct >= 15 and amt >= 25000:
            of = top.get("capital_committed_pct_of", "the book")
            improvements.append(
                (
                    amt,
                    f"{top['underlying']} is {pct:.0f}% of {of} ({_money(amt)}). A drawdown "
                    "there moves the whole book.",
                )
            )

    nw_net = sum(_num(w.get("net_realized_pl")) or 0.0 for w in wheels if not w.get("is_wheel"))
    if nw_net < -100:
        improvements.append(
            (
                nw_net,
                f"Directional (non-wheel) trades have cost {_money(nw_net)} net. They stay out "
                "of the wheel-return figures, but the loss is real.",
            )
        )

    urgent = [h for h in open_hedges if h.get("phase") in ("wind_down", "expiring")]
    if urgent:
        names = ", ".join(f"{h['underlying']} ({h['days_to_expiry']}d)" for h in urgent[:3])
        improvements.append(
            (
                None,
                f"{len(urgent)} protective hedge(s) are inside the wind-down window ({names}) -- "
                "sell them for their remaining time value or roll them out before they decay.",
            )
        )

    est = list(
        dict.fromkeys(w["underlying"] for w in active if w.get("capital_estimated"))
    )
    if est:
        improvements.append(
            (
                None,
                f"{len(est)} active wheels ({', '.join(est)}) price capital with a strike-based "
                "proxy for pre-export shares -- their ROC figures are approximate.",
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
