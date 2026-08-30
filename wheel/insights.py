"""Plain-rules commentary for one wheel: what is working, where to improve.

No model and no network -- every line is a threshold on figures the engine and
:mod:`wheel.metrics` already produce, phrased as advice. Strengths keep a
curated priority order; improvements are ranked by dollar impact so the
costliest problem shows first. The Trade Log renders up to two of each.
"""

from __future__ import annotations

from wheel.engine import COVERED_CALL, CSP, LONG, Cycle
from wheel.metrics import CycleMetrics

_MAX_STRENGTHS = 2
_MAX_IMPROVEMENTS = 3


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
