"""SPY daily closes, fetched with the standard library and cached locally.

The project is stdlib-only, so the benchmark price history comes from Stooq's
plain, no-key CSV endpoint via :mod:`urllib.request` rather than a third-party
market-data package. The result is cached under ``data/`` (already gitignored)
in a small ``date,close`` format of our own -- decoupled from Stooq's own column
layout, which is read only at fetch time.

Nothing here ever raises past :func:`get_price_series`: a stale cache is used
when a refresh fails, and an empty result with an explanatory warning is
returned when there is neither a usable cache nor a working fetch, so a missing
internet connection degrades the benchmark section instead of crashing the
dashboard.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Sequence

from wheel.parser import _num, _parse_date

STOOQ_URL = "https://stooq.com/q/d/l/?s=spy.us&i=d"
DEFAULT_CACHE_NAME = "spy_daily_closes.csv"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MarketDataError(RuntimeError):
    """A benchmark price fetch failed -- network, HTTP, or malformed response."""


@dataclass(frozen=True)
class PricePoint:
    day: date
    close: float


def _default_cache_path() -> str:
    return os.path.join(PROJECT_ROOT, "data", DEFAULT_CACHE_NAME)


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------


def fetch_stooq_csv(url: str = STOOQ_URL, timeout: float = 10.0) -> str:
    """Download the raw CSV text. Any failure becomes a MarketDataError."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise MarketDataError(f"could not fetch {url}: {error}") from error


def parse_stooq_csv(text: str) -> list[PricePoint]:
    """Stooq's 'Date,Open,High,Low,Close,Volume' -> ascending PricePoints.

    Rows that fail to parse (a header line, a truncated line, a non-numeric
    close) are skipped rather than failing the whole fetch.
    """
    points: list[PricePoint] = []
    for line in text.splitlines():
        cells = line.strip().split(",")
        if len(cells) < 5:
            continue
        day = _parse_date(cells[0])
        close = _num(cells[4])
        if day is None or close is None:
            continue
        points.append(PricePoint(day=day, close=close))
    points.sort(key=lambda point: point.day)
    return points


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


def load_cache(cache_path: str | None = None) -> list[PricePoint]:
    path = cache_path or _default_cache_path()
    if not os.path.isfile(path):
        return []
    points: list[PricePoint] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            cells = line.strip().split(",")
            if len(cells) != 2:
                continue
            day = _parse_date(cells[0])
            close = _num(cells[1])
            if day is None or close is None:
                continue
            points.append(PricePoint(day=day, close=close))
    points.sort(key=lambda point: point.day)
    return points


def save_cache(points: Sequence[PricePoint], cache_path: str | None = None) -> None:
    path = cache_path or _default_cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for point in sorted(points, key=lambda p: p.day):
            handle.write(f"{point.day.isoformat()},{point.close}\n")


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def get_price_series(
    *,
    fetch: Callable[[], str] = fetch_stooq_csv,
    cache_path: str | None = None,
    max_age_days: int = 1,
    force_refresh: bool = False,
) -> tuple[list[PricePoint], list[str]]:
    """The best available SPY price series, refreshing the cache when stale.

    Never raises. Order of attempts:

    1. If ``force_refresh`` is set, or no cache exists, or the cache's last
       point is more than ``max_age_days`` behind today, try ``fetch()``. On
       success the parsed series is cached and returned with no warning.
    2. If the fetch was skipped (cache already fresh), the cache is returned
       as-is, no warning.
    3. If the fetch was attempted and failed, fall back to the existing cache
       (stale but usable) with one warning naming the failure.
    4. If there is no cache and the fetch failed, return ``([], [warning])`` --
       callers must treat an empty series as "benchmark unavailable."
    """
    cached = load_cache(cache_path)
    stale = (
        force_refresh
        or not cached
        or (date.today() - cached[-1].day) > timedelta(days=max_age_days)
    )
    if not stale:
        return cached, []

    try:
        points = parse_stooq_csv(fetch())
    except MarketDataError as error:
        if cached:
            return cached, [f"could not refresh SPY prices, using cached data through {cached[-1].day}: {error}"]
        return [], [f"could not fetch SPY prices and no cached data is available: {error}"]

    if not points:
        if cached:
            return cached, ["SPY price fetch returned no usable rows; using cached data"]
        return [], ["SPY price fetch returned no usable rows and no cached data is available"]

    save_cache(points, cache_path)
    return points, []


def price_on_or_before(points: Sequence[PricePoint], day: date) -> PricePoint | None:
    """Nearest prior-or-equal trading-day close, or None before the series starts."""
    if not points:
        return None
    days = [point.day for point in points]
    index = bisect_right(days, day) - 1
    if index < 0:
        return None
    return points[index]
