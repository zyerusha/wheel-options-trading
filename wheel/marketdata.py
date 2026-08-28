"""Daily closes for any ticker, fetched with the standard library and cached
locally -- SPY for the benchmark comparison, any wheel underlying for Stock
Unrealized P&L.

The project is stdlib-only, so price history comes from Yahoo Finance's
no-key chart JSON endpoint via :mod:`urllib.request` rather than a
third-party market-data package -- the only network access anywhere in this
project. (An earlier version of this module used Stooq's CSV endpoint; Stooq
now fronts that endpoint with a JavaScript bot challenge that a stdlib-only
fetch cannot solve, so it stopped returning usable data.) Each ticker gets
its own cache file under ``data/prices/`` (already gitignored) in a small
``date,close`` format of our own -- decoupled from Yahoo's own response
shape, which is read only at fetch time. SPY keeps its original, pre-existing
cache path (``data/spy_daily_closes.csv``) rather than moving under
``data/prices/`` with everything else, so an existing cache on disk keeps
working unchanged.

Nothing here ever raises past :func:`get_price_series`: a stale cache is used
when a refresh fails, and an empty result with an explanatory warning is
returned when there is neither a usable cache nor a working fetch, so a
missing internet connection -- or one ticker's fetch failing -- degrades just
that ticker's figures instead of crashing the dashboard.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Sequence

from wheel.parser import _num, _parse_date

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SPY_LEGACY_CACHE_NAME = "spy_daily_closes.csv"

# Yahoo serves plain JSON to a browser-like User-Agent; the stdlib default
# ("Python-urllib/3.x") gets a 404 on this endpoint.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class MarketDataError(RuntimeError):
    """A price fetch failed -- network, HTTP, or malformed response."""


@dataclass(frozen=True)
class PricePoint:
    day: date
    close: float


def yahoo_chart_url(ticker: str, *, period2: int | None = None) -> str:
    """Yahoo's chart endpoint, full daily history from the ticker's first
    trading day through ``period2`` (Unix seconds, defaulting to now).

    ``period1=0`` plus an explicit ``interval=1d`` returns genuine daily
    bars for the ticker's entire history; ``range=max`` (Yahoo's other way to
    ask for "everything") silently downsamples to monthly bars instead, which
    is too coarse to resolve an arbitrary transaction date.
    """
    end = period2 if period2 is not None else int(time.time())
    return (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{ticker.strip().upper()}?period1=0&period2={end}&interval=1d"
    )


def _default_cache_path(ticker: str) -> str:
    if ticker.upper() == "SPY":
        return os.path.join(PROJECT_ROOT, "data", _SPY_LEGACY_CACHE_NAME)
    return os.path.join(PROJECT_ROOT, "data", "prices", f"{ticker.upper()}.csv")


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------


def fetch_yahoo_chart(url: str, timeout: float = 10.0) -> str:
    """Download the raw chart JSON text. Any failure becomes a MarketDataError."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise MarketDataError(f"could not fetch {url}: {error}") from error


def parse_yahoo_chart(text: str) -> list[PricePoint]:
    """Yahoo's ``chart.result[0]`` JSON -> ascending PricePoints.

    A response with no parseable ``chart.result`` (an error payload, an
    unrecognized ticker, a bot-challenge HTML page mistakenly handed to this
    parser) raises MarketDataError rather than returning an empty list --
    unlike a genuinely empty trading calendar, which never happens for a real
    ticker, so an empty list here would misreport a broken fetch as "no
    prices exist" instead of surfacing the warning callers rely on. A day
    with a ``null`` close (a market holiday inside the requested range) is
    skipped, not fatal.

    Each bar's timestamp marks 9:30am US/Eastern -- always 13:30 or 14:30
    UTC depending on DST, i.e. still the same calendar date -- so resolving
    it via ``date.fromtimestamp(..., tz=timezone.utc)`` never shifts the
    trading day.
    """
    try:
        result = json.loads(text)["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
        raise MarketDataError(f"unrecognized Yahoo Finance chart response: {error}") from error

    points = [
        PricePoint(day=datetime.fromtimestamp(ts, tz=timezone.utc).date(), close=close)
        for ts, close in zip(timestamps, closes)
        if close is not None
    ]
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

    ``fetch`` and ``cache_path`` default to Yahoo Finance and this ticker's
    own cache file (see :func:`yahoo_chart_url`, :func:`_default_cache_path`)
    when omitted; tests inject both to avoid any real network access.
    """
    path = cache_path or _default_cache_path(ticker)
    fetch = fetch or (lambda: fetch_yahoo_chart(yahoo_chart_url(ticker)))

    cached = load_cache(path)
    stale = (
        force_refresh
        or not cached
        or (date.today() - cached[-1].day) > timedelta(days=max_age_days)
    )
    if not stale:
        return cached, []

    try:
        points = parse_yahoo_chart(fetch())
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
