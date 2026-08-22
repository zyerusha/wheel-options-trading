"""Small, dependency-free statistics helpers shared across ``wheel.*`` modules.

Kept in their own module, with no other ``wheel.*`` imports, specifically so
:mod:`wheel.cashflow` can reuse :func:`time_weighted_average` without creating
an import cycle -- :mod:`wheel.metrics` already imports from
:mod:`wheel.cashflow` (``dividend_transactions``), so the reverse direction
can't run through ``wheel.metrics`` directly.
"""

from __future__ import annotations

from typing import Sequence

DAYS_PER_YEAR = 365.0


def time_weighted_average(points: Sequence, value=lambda point: point.total) -> float:
    """Mean of ``value(point)`` over the days it was actually engaged (> $0).

    Days at zero are excluded: a cycle (or, with ``value=lambda p:
    p.working_capital``, a stretch of idle holding-shares-only days, or a
    single calendar month) that sits flat should not dilute the denominator
    for the days it wasn't engaged at all.

    Generic over any ``points`` sequence -- ``value`` is the only thing that
    ever reads an item's shape, so callers reuse this exact function on
    :class:`~wheel.metrics.CapitalPoint` objects, ``(date, float)`` tuples
    (:mod:`wheel.cashflow`), or JSON ``dict``s with a ``"total"`` key
    (:mod:`wheel.accounts`'s Combined rollup) with their own ``value=`` lambda,
    rather than re-deriving the same "skip zero days, then mean" idiom by hand.
    """
    engaged = [value(point) for point in points if value(point) > 1e-9]
    return sum(engaged) / len(engaged) if engaged else 0.0


def safe_pct(numerator: float, denominator: float) -> float | None:
    """``100 * numerator / denominator``, or ``None`` when ``denominator`` is
    too small to divide by safely -- never a division error, never a
    misleading ``0%``/``inf``.
    """
    return 100.0 * numerator / denominator if denominator > 1e-9 else None


def roi_and_annualized(
    numerator: float, denominator: float, days_span: float
) -> tuple[float | None, float | None]:
    """``(pct, annualized_pct)`` for one ROI/ROC/yield figure: ``pct`` is
    ``numerator / denominator`` as a percentage (``None`` when ``denominator``
    is too small to divide by), and ``annualized_pct`` scales it to a full
    year via ``365 / days_span`` (``None`` alongside ``pct``, never computed
    from it once it's ``None``).

    Every ROI-shaped figure in :mod:`wheel.metrics` (Wheel ROC, Active Wheel
    ROC, Net Option Yield, Total Position ROI, at both the portfolio and
    ticker level) and :mod:`wheel.accounts`'s Combined rollup share this exact
    "percentage, then annualize" shape -- centralizing it here means the
    epsilon guard and the annualizing formula can't drift between any of
    those call sites.
    """
    pct = safe_pct(numerator, denominator)
    if pct is None or not days_span:
        return pct, None
    return pct, pct * (DAYS_PER_YEAR / days_span)
