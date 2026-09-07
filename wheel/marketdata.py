"""Daily closes for any ticker, fetched with the standard library and cached
locally -- SPY for the benchmark comparison, any wheel underlying for Stock
Unrealized P&L.

The project is stdlib-only, so price history comes from Yahoo Finance's
no-key chart JSON endpoint via :mod:`urllib.request` rather than a
third-party market-data package -- this module (prices, plus the fundamentals
fetch at the bottom) is the only network access anywhere in this project.
(An earlier version of this module used Stooq's CSV endpoint; Stooq
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

import http.cookiejar
import json
import os
import re
import time
import urllib.error
import urllib.parse
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


# Per-process, per-day memo of resolved series, keyed by cache-file path. A
# long-lived server builds the dashboard once per account (six-plus times for
# the Combined view) and every build asks for the same tickers; without this
# each build re-reads every cache file and, on a stale day, re-fetches every
# ticker once per account. The memo is only trusted for the current calendar
# date, so tomorrow's first build still refreshes. Bypassed entirely when a
# caller injects ``fetch``/``cache_path`` (tests), so it never leaks across them.
_SERIES_MEMO: dict[str, tuple[date, list["PricePoint"]]] = {}


def _sessions_elapsed(last_day: date, today: date) -> int:
    """Weekday count in ``(last_day, today]`` -- how many regular-session
    closes could exist that ``last_day`` does not yet cover.

    Using this instead of a raw calendar-day delta stops a Friday close from
    reading as "stale" all weekend (Sat/Sun add nothing to fetch), which was
    turning every Saturday-through-Monday dashboard load into a full re-fetch
    of every ticker. Market holidays are not modelled -- at worst that costs
    one redundant fetch a year per ticker, not worth a holiday calendar in a
    stdlib-only project.
    """
    if last_day >= today:
        return 0
    sessions = 0
    day = last_day
    while day < today:
        day += timedelta(days=1)
        if day.weekday() < 5:
            sessions += 1
    return sessions


def get_price_series(
    ticker: str = "SPY",
    *,
    fetch: Callable[[], str] | None = None,
    cache_path: str | None = None,
    max_age_days: int = 1,
    force_refresh: bool = False,
    local_only: bool = False,
) -> tuple[list[PricePoint], list[str]] | None:
    """The best available daily-close series for ``ticker``, refreshing the
    cache when stale.

    With ``local_only=True`` the network is never touched: returns the resolved
    series when it can be answered from the in-process memo or a still-fresh
    cache file, or ``None`` when a fetch would otherwise be needed (so the
    caller can batch those). Every other path returns a ``(points, warnings)``
    tuple and never raises.

    Never raises. Order of attempts:

    1. If ``force_refresh`` is set, or no cache exists, or more than
       ``max_age_days`` regular market sessions (weekdays) have closed since
       the cache's last point, try ``fetch()``. On success the parsed series
       is cached and returned with no warning.
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
    memoable = fetch is None and cache_path is None and not force_refresh
    path = cache_path or _default_cache_path(ticker)
    fetch = fetch or (lambda: fetch_yahoo_chart(yahoo_chart_url(ticker)))
    today = date.today()

    if memoable:
        memo = _SERIES_MEMO.get(path)
        if memo is not None and memo[0] == today:
            return list(memo[1]), []

    cached = load_cache(path)
    stale = (
        force_refresh
        or not cached
        or _sessions_elapsed(cached[-1].day, today) > max_age_days
    )
    if not stale:
        if memoable:
            _SERIES_MEMO[path] = (today, cached)
        return cached, []

    if local_only:
        return None

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
    if memoable:
        _SERIES_MEMO[path] = (today, points)
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


# --------------------------------------------------------------------------
# Fundamentals -- market cap, 10-day average volume, security type, and the
# next earnings date, from Yahoo's quote endpoint. Same contract as prices:
# cached locally (``data/fundamentals_cache.json``), refreshed when stale,
# and never raised past the public entry point -- a fetch failure just leaves
# the affected tickers "unknown" (the CSP-candidate filter shows them flagged
# rather than hidden). ``data/fundamentals.json`` / ``data/earnings.json``
# stay as hand-maintained per-field overrides on top of what's fetched here.
# --------------------------------------------------------------------------

_FUNDAMENTALS_CACHE_PATH = os.path.join(PROJECT_ROOT, "data", "fundamentals_cache.json")
_QUOTE_URL = "https://query2.finance.yahoo.com/v7/finance/quote"
_CRUMB_URL = "https://query2.finance.yahoo.com/v1/test/getcrumb"
_COOKIE_URL = "https://fc.yahoo.com/"

# Fund names that mean "not an ordinary ETF you'd wheel".
_LEVERAGED_RE = re.compile(
    r"\b(?:ultra(?:pro)?|[1-9](?:\.5)?x|-[1-9]x|leveraged|geared|"
    r"bull\s+[2-9]x|bear\s+[2-9]x|(?:bull|bear)\s+[2-9]x)\b",
    re.IGNORECASE,
)
_INVERSE_RE = re.compile(r"\b(?:inverse|-1x|short)\b", re.IGNORECASE)
# Yahoo tags many closed-end funds as EQUITY; a name ending "... Fund" gives
# them away (operating companies almost never do).
_FUND_NAME_RE = re.compile(r"\bfund\b", re.IGNORECASE)

# Per-process Yahoo auth (cookie jar opener + crumb). Fetched once; a failure
# is remembered as (None, None) so a broken run doesn't retry the handshake
# on every ticker batch.
_YAHOO_AUTH: tuple[urllib.request.OpenerDirector | None, str | None] | None = None


@dataclass(frozen=True)
class Fundamentals:
    """What the CSP-candidate filter needs about a ticker. ``as_of`` is the
    fetch date; every other field may be ``None`` when Yahoo didn't carry it."""

    ticker: str
    as_of: date
    market_cap_b: float | None = None
    avg_vol_10d_m: float | None = None
    quote_type: str | None = None  # Yahoo's raw: EQUITY / ETF / MUTUALFUND / ...
    kind: str | None = None  # mapped: common / etf / leveraged_etf / mutual_fund / ...
    earnings_date: date | None = None
    long_name: str | None = None


def _leverage_kind(name: str | None, quote_type: str | None) -> str | None:
    """Map Yahoo's ``quoteType`` (+ the fund name) to one of the ``type``
    values ``wheel.api._EXCLUDED_TYPES`` / the CSP filter understand."""
    qt = (quote_type or "").upper()
    if qt == "EQUITY":
        return "closed_end_fund" if name and _FUND_NAME_RE.search(name) else "common"
    if qt == "ETF":
        if name and _INVERSE_RE.search(name):
            return "inverse_etf"
        if name and _LEVERAGED_RE.search(name):
            return "leveraged_etf"
        return "etf"
    if qt in ("MUTUALFUND", "MONEYMARKET"):
        return "mutual_fund"
    return None


def _epoch_to_date(value: object) -> date | None:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).date()  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _yahoo_auth() -> tuple[urllib.request.OpenerDirector | None, str | None]:
    """A cookie-jar opener plus a matching crumb -- Yahoo's quote endpoint
    rejects requests without both. Memoized for the process; on any failure
    returns ``(None, None)`` and callers fall back to a crumbless request
    (which usually 401s, handled as a normal fetch failure)."""
    global _YAHOO_AUTH
    if _YAHOO_AUTH is not None:
        return _YAHOO_AUTH
    try:
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        opener.addheaders = [("User-Agent", _USER_AGENT)]
        try:
            opener.open(_COOKIE_URL, timeout=10.0)
        except urllib.error.HTTPError:
            pass  # 404 is expected; we only want the Set-Cookie it carries
        with opener.open(_CRUMB_URL, timeout=10.0) as response:
            crumb = response.read().decode("utf-8", errors="replace").strip()
        if not crumb or "<" in crumb:  # an HTML challenge page, not a crumb
            raise MarketDataError("no crumb")
        _YAHOO_AUTH = (opener, crumb)
    except (urllib.error.URLError, OSError, ValueError, MarketDataError):
        _YAHOO_AUTH = (None, None)
    return _YAHOO_AUTH


def fetch_yahoo_quotes(tickers: Sequence[str], timeout: float = 10.0) -> str:
    """Raw JSON text from Yahoo's batched quote endpoint. MarketDataError on
    any transport failure (the crumb handshake included)."""
    opener, crumb = _yahoo_auth()
    query = {"symbols": ",".join(sorted({t.upper() for t in tickers}))}
    if crumb:
        query["crumb"] = crumb
    url = f"{_QUOTE_URL}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    opener = opener or urllib.request.build_opener()
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise MarketDataError(f"could not fetch quotes for {query['symbols']}: {error}") from error


def parse_yahoo_quotes(text: str) -> dict[str, dict]:
    """Yahoo ``quoteResponse.result`` -> ``{TICKER: raw quote dict}``. Raises
    MarketDataError on a response with no parseable ``quoteResponse`` (an error
    payload or a challenge page), the same way :func:`parse_yahoo_chart` does."""
    try:
        results = json.loads(text)["quoteResponse"]["result"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise MarketDataError(f"unrecognized Yahoo quote response: {error}") from error
    out: dict[str, dict] = {}
    for row in results or []:
        symbol = str(row.get("symbol", "")).upper()
        if symbol:
            out[symbol] = row
    return out


def _fundamentals_from_quote(ticker: str, row: dict, as_of: date) -> Fundamentals:
    name = row.get("longName") or row.get("shortName")
    cap = row.get("marketCap")
    vol = row.get("averageDailyVolume10Day") or row.get("averageDailyVolume3Month")
    quote_type = row.get("quoteType")
    earn = _epoch_to_date(row.get("earningsTimestampStart") or row.get("earningsTimestamp"))
    return Fundamentals(
        ticker=ticker,
        as_of=as_of,
        market_cap_b=round(cap / 1e9, 4) if isinstance(cap, (int, float)) and cap > 0 else None,
        avg_vol_10d_m=round(vol / 1e6, 4) if isinstance(vol, (int, float)) and vol > 0 else None,
        quote_type=quote_type,
        kind=_leverage_kind(name, quote_type),
        earnings_date=earn,
        long_name=name,
    )


def _load_fundamentals_cache(path: str) -> dict[str, Fundamentals]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return {}
    out: dict[str, Fundamentals] = {}
    for ticker, entry in (raw or {}).items():
        if not isinstance(entry, dict):
            continue
        as_of = _parse_date(str(entry.get("as_of", "")))
        if as_of is None:
            continue
        out[ticker.upper()] = Fundamentals(
            ticker=ticker.upper(),
            as_of=as_of,
            market_cap_b=entry.get("market_cap_b"),
            avg_vol_10d_m=entry.get("avg_vol_10d_m"),
            quote_type=entry.get("quote_type"),
            kind=entry.get("kind"),
            earnings_date=_parse_date(str(entry.get("earnings_date", ""))),
            long_name=entry.get("long_name"),
        )
    return out


def _save_fundamentals_cache(cache: dict[str, Fundamentals], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    serializable = {
        ticker: {
            "as_of": f.as_of.isoformat(),
            "market_cap_b": f.market_cap_b,
            "avg_vol_10d_m": f.avg_vol_10d_m,
            "quote_type": f.quote_type,
            "kind": f.kind,
            "earnings_date": f.earnings_date.isoformat() if f.earnings_date else None,
            "long_name": f.long_name,
        }
        for ticker, f in sorted(cache.items())
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(serializable, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _fundamentals_stale(entry: Fundamentals | None, today: date, max_age_days: int) -> bool:
    if entry is None:
        return True
    age = (today - entry.as_of).days
    if age >= max_age_days:
        return True
    # A known earnings date that's now in the past means the *next* one is out
    # there unfetched -- chase it, but no more than daily.
    if entry.earnings_date is not None and entry.earnings_date < today and age >= 1:
        return True
    # A stock that never got an earnings date at all: retry every few days
    # (an ETF / fund legitimately has none -- don't churn on those).
    if entry.earnings_date is None and entry.kind == "common" and age >= 3:
        return True
    return False


def get_fundamentals(
    tickers: Sequence[str],
    *,
    fetch: Callable[[Sequence[str]], str] | None = None,
    cache_path: str | None = None,
    max_age_days: int = 14,
    local_only: bool = False,
    force_refresh: bool = False,
    today: date | None = None,
) -> tuple[dict[str, Fundamentals], list[str]]:
    """``({TICKER: Fundamentals}, warnings)`` for ``tickers``, refreshing the
    local cache when an entry is missing or stale. Never raises.

    One batched network call covers every stale ticker. ``local_only`` skips
    the network entirely and returns whatever the cache holds (fresh or not).
    A fetch failure keeps the existing cache and adds one warning; tickers
    with no cache entry are simply absent from the result.

    ``fetch`` (symbols -> JSON text) and ``cache_path`` default to Yahoo and
    ``data/fundamentals_cache.json``; tests inject both.
    """
    path = cache_path or _FUNDAMENTALS_CACHE_PATH
    fetch = fetch or fetch_yahoo_quotes
    today = today or date.today()
    wanted = sorted({t.upper() for t in tickers if t})

    cache = _load_fundamentals_cache(path)
    if local_only:
        return {t: cache[t] for t in wanted if t in cache}, []

    stale = [
        t for t in wanted if force_refresh or _fundamentals_stale(cache.get(t), today, max_age_days)
    ]
    warnings: list[str] = []
    if stale:
        try:
            quotes = parse_yahoo_quotes(fetch(stale))
            touched = False
            for ticker in stale:
                row = quotes.get(ticker)
                if not row or not (row.get("quoteType") or row.get("marketCap")):
                    continue  # nothing usable came back -- don't cache a blank
                cache[ticker] = _fundamentals_from_quote(ticker, row, today)
                touched = True
            if touched:
                _save_fundamentals_cache(cache, path)
        except MarketDataError as error:
            warnings.append(f"could not refresh fundamentals ({len(stale)} ticker(s)): {error}")

    return {t: cache[t] for t in wanted if t in cache}, warnings
