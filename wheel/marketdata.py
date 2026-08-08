"""Daily closes for any ticker, fetched with the standard library and cached
locally -- SPY for the benchmark comparison, any wheel underlying for Stock
Unrealized P&L.

The project is stdlib-only, so price history comes from Stooq's plain, no-key
CSV endpoint via :mod:`urllib.request` rather than a third-party market-data
package -- the only network access anywhere in this project. Each ticker gets
its own cache file under ``data/prices/`` (already gitignored) in a small
``date,close`` format of our own -- decoupled from Stooq's own column layout,
which is read only at fetch time. SPY keeps its original, pre-existing cache
path (``data/spy_daily_closes.csv``) rather than moving under ``data/prices/``
with everything else, so an existing cache on disk keeps working unchanged.

Nothing here ever raises past :func:`get_price_series`: a stale cache is used
when a refresh fails, and an empty result with an explanatory warning is
returned when there is neither a usable cache nor a working fetch, so a
missing internet connection -- or one ticker's fetch failing -- degrades just
that ticker's figures instead of crashing the dashboard.
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

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SPY_LEGACY_CACHE_NAME = "spy_daily_closes.csv"


class MarketDataError(RuntimeError):
    """A price fetch failed -- network, HTTP, or malformed response."""


@dataclass(frozen=True)
class PricePoint:
    day: date
    close: float


def stooq_url(ticker: str) -> str:
    return f"https://stooq.com/q/d/l/?s={ticker.strip().lower()}.us&i=d"


def _default_cache_path(ticker: str) -> str:
    if ticker.upper() == "SPY":
        return os.path.join(PROJECT_ROOT, "data", _SPY_LEGACY_CACHE_NAME)
    return os.path.join(PROJECT_ROOT, "data", "prices", f"{ticker.upper()}.csv")


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------


def fetch_stooq_csv(url: str, timeout: float = 10.0) -> str:
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


def load_cache(cache_path: str) -> list[PricePoint]:
    if not os.path.isfile(cache_path):
        return []
    points: list[PricePoint] = []
    with open(cache_path, "r", encoding="utf-8") as handle:
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


def save_cache(points: Sequence[PricePoint], cache_path: str) -> None:
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as handle:
        for point in sorted(points, key=lambda p: p.day):
            handle.write(f"{point.day.isoformat()},{point.close}\n")


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def get_price_series(
    ticker: str = "SPY",
    *,
    fetch: Callable[[], str] | None = None,
    cache_path: str | None = None,
    max_age_days: int = 1,
    force_refresh: bool = False,
) -> tuple[list[PricePoint], list[str]]:
    """The best available daily-close series for ``ticker``, refreshing the
    cache when stale.

    Never raises. Order of attempts:

    1. If ``force_refresh`` is set, or no cache exists, or the cache's last
       point is more than ``max_age_days`` behind today, try ``fetch()``. On
       success the parsed series is cached and returned with no warning.
    2. If the fetch was skipped (cache already fresh), the cache is returned
       as-is, no warning.
    3. If the fetch was attempted and failed, fall back to the existing cache
       (stale but usable) with one warning naming the failure.
    4. If there is no cache and the fetch failed, return ``([], [warning])`` --
       callers must treat an empty series as "unavailable for this ticker,"
       and one ticker failing never affects another's already-cached series.

    ``fetch`` and ``cache_path`` default to Stooq and this ticker's own cache
    file (see :func:`stooq_url`, :func:`_default_cache_path`) when omitted;
    tests inject both to avoid any real network access.
    """
    path = cache_path or _default_cache_path(ticker)
    fetch = fetch or (lambda: fetch_stooq_csv(stooq_url(ticker)))

    cached = load_cache(path)
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
            return cached, [f"could not refresh {ticker} prices, using cached data through {cached[-1].day}: {error}"]
        return [], [f"could not fetch {ticker} prices and no cached data is available: {error}"]

    if not points:
        if cached:
            return cached, [f"{ticker} price fetch returned no usable rows; using cached data"]
        return [], [f"{ticker} price fetch returned no usable rows and no cached data is available"]

    save_cache(points, path)
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
