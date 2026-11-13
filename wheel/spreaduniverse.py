"""Where the Credit Spreads scanner's list of companies comes from.

Stage 1 of the scan (``wheel/spreadscanservice.py``). Each source is one small
class with a ``fetch()``; adding a source means adding a class to
``default_sources``. Lists are downloaded, merged, de-duplicated and cached on
disk for ``UNIVERSE_MAX_AGE_DAYS``, so no constituent list is kept by hand.
The ETF list is the reader's setting (``ScannerSettings.etfs``), not part of
this cache, so changing it never refetches anything.

Sources today:

* S&P 500: the ``datasets/s-and-p-500-companies`` CSV (GICS sectors).
* Nasdaq-100: Nasdaq's own list endpoint.
* Largest 1,000 US-listed companies by market cap: Nasdaq's stock screener.
  This is a stand-in for the Russell 1000, whose official list is not freely
  downloadable (iShares' IWB holdings link returned a web page, not a CSV,
  when tried). It is labelled as a stand-in wherever it is named.

A source that fails keeps its members from the previous cache and adds a
warning; a source with no previous cache simply contributes nothing, and the
warning says so. Nothing is invented to fill the gap.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterable, Protocol, Sequence

from wheel.paths import DATA_DIR

UNIVERSE_MAX_AGE_DAYS = 7
UNIVERSE_CACHE_PATH = os.path.join(DATA_DIR, "spread_universe_cache.json")
LARGEST_US_COUNT = 1000

SP500_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
NASDAQ100_URL = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"
NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
# Plain share classes only: BRK-B yes, preferred shares and warrants (ABC^D, ABCW/U) no.
_SYMBOL_RE = re.compile(r"[A-Z]{1,5}(-[A-Z])?")

# One sector vocabulary (GICS names) so concentration checks compare like with
# like. Nasdaq's "Miscellaneous" has no GICS meaning, so it stays unknown.
_NASDAQ_SECTOR_TO_GICS = {
    "technology": "Information Technology",
    "finance": "Financials",
    "health care": "Health Care",
    "telecommunications": "Communication Services",
    "basic materials": "Materials",
    "consumer discretionary": "Consumer Discretionary",
    "consumer staples": "Consumer Staples",
    "energy": "Energy",
    "industrials": "Industrials",
    "real estate": "Real Estate",
    "utilities": "Utilities",
}
# The SPDR sector fund for each GICS sector: how a sector's own trend is measured.
SECTOR_ETFS = {
    "Information Technology": "XLK",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Utilities": "XLU",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
}


class UniverseError(RuntimeError):
    """A constituent list could not be downloaded or understood."""


@dataclass(frozen=True)
class UniverseMember:
    ticker: str
    name: str | None = None
    sector: str | None = None
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceStatus:
    key: str
    label: str
    count: int
    live: bool  # False: this run's download failed and the previous list was kept
    note: str | None = None


class UniverseSource(Protocol):
    key: str
    label: str

    def fetch(self) -> list[UniverseMember]: ...


def normalize_symbol(raw: str) -> str | None:
    """A listed symbol in Yahoo's spelling (BRK.B and BRK/B become BRK-B), or
    ``None`` for anything that isn't a plain share class."""
    symbol = raw.strip().upper().replace(".", "-").replace("/", "-")
    return symbol if _SYMBOL_RE.fullmatch(symbol) else None


def _http_get(url: str, timeout: float = 25.0) -> str:
    request = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json,text/csv,*/*", "Accept-Language": "en-US,en"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8-sig", errors="replace")
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise UniverseError(f"could not download {url}: {error}") from error


def parse_sp500_csv(text: str, source_key: str = "sp500") -> list[UniverseMember]:
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "Symbol" not in rows[0]:
        raise UniverseError("the S&P 500 list had no Symbol column")
    members = []
    for row in rows:
        symbol = normalize_symbol(row.get("Symbol") or "")
        if symbol:
            members.append(
                UniverseMember(symbol, row.get("Security") or None, row.get("GICS Sector") or None, (source_key,))
            )
    return members


def parse_nasdaq100_json(text: str, source_key: str = "nasdaq100") -> list[UniverseMember]:
    try:
        rows = json.loads(text)["data"]["data"]["rows"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise UniverseError(f"unrecognized Nasdaq-100 response: {error}") from error
    members = []
    for row in rows or []:
        symbol = normalize_symbol(str(row.get("symbol") or ""))
        if symbol:
            members.append(UniverseMember(symbol, row.get("companyName") or None, None, (source_key,)))
    return members


def _money_number(raw: object) -> float | None:
    try:
        return float(str(raw).replace(",", "").replace("$", ""))
    except ValueError:
        return None


def parse_nasdaq_screener_json(
    text: str, *, count: int = LARGEST_US_COUNT, source_key: str = "largest_us"
) -> list[UniverseMember]:
    """The ``count`` largest US companies by market cap. Rows with no market
    cap can't be ranked, so they are left out rather than guessed."""
    try:
        rows = json.loads(text)["data"]["rows"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise UniverseError(f"unrecognized Nasdaq screener response: {error}") from error
    sized: list[tuple[float, UniverseMember]] = []
    for row in rows or []:
        symbol = normalize_symbol(str(row.get("symbol") or ""))
        cap = _money_number(row.get("marketCap"))
        if not symbol or not cap or str(row.get("country") or "") != "United States":
            continue
        sector = _NASDAQ_SECTOR_TO_GICS.get(str(row.get("sector") or "").strip().lower())
        sized.append((cap, UniverseMember(symbol, row.get("name") or None, sector, (source_key,))))
    sized.sort(key=lambda pair: (-pair[0], pair[1].ticker))
    return [member for _, member in sized[:count]]


class Sp500Source:
    key = "sp500"
    label = "S&P 500"

    def __init__(self, get: Callable[[str], str] = _http_get) -> None:
        self._get = get

    def fetch(self) -> list[UniverseMember]:
        return parse_sp500_csv(self._get(SP500_URL), self.key)


class Nasdaq100Source:
    key = "nasdaq100"
    label = "Nasdaq-100"

    def __init__(self, get: Callable[[str], str] = _http_get) -> None:
        self._get = get

    def fetch(self) -> list[UniverseMember]:
        return parse_nasdaq100_json(self._get(NASDAQ100_URL), self.key)


class LargestUsSource:
    key = "largest_us"
    label = f"Largest {LARGEST_US_COUNT:,} US companies (stand-in for the Russell 1000)"

    def __init__(self, get: Callable[[str], str] = _http_get) -> None:
        self._get = get

    def fetch(self) -> list[UniverseMember]:
        return parse_nasdaq_screener_json(self._get(NASDAQ_SCREENER_URL), source_key=self.key)


def default_sources() -> list[UniverseSource]:
    # Order matters: the first source naming a sector wins, and the S&P list
    # carries true GICS sectors.
    return [Sp500Source(), Nasdaq100Source(), LargestUsSource()]


def merge_members(groups: Iterable[Sequence[UniverseMember]]) -> list[UniverseMember]:
    """One entry per ticker, sorted, remembering every source that listed it.
    The first name and first sector found win."""
    merged: dict[str, UniverseMember] = {}
    for group in groups:
        for member in group:
            seen = merged.get(member.ticker)
            if seen is None:
                merged[member.ticker] = member
                continue
            merged[member.ticker] = UniverseMember(
                member.ticker,
                seen.name or member.name,
                seen.sector or member.sector,
                tuple(dict.fromkeys(seen.sources + member.sources)),
            )
    return [merged[ticker] for ticker in sorted(merged)]


def _load_cache(path: str) -> tuple[date | None, list[UniverseMember]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        as_of = date.fromisoformat(raw["as_of"])
        members = [
            UniverseMember(m["ticker"], m.get("name"), m.get("sector"), tuple(m.get("sources") or ()))
            for m in raw["members"]
        ]
        return as_of, members
    except (OSError, ValueError, KeyError, TypeError):
        return None, []


def _save_cache(path: str, as_of: date, members: Sequence[UniverseMember]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "as_of": as_of.isoformat(),
        "members": [
            {"ticker": m.ticker, "name": m.name, "sector": m.sector, "sources": list(m.sources)} for m in members
        ],
    }
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(temporary, path)


def get_universe(
    today: date,
    *,
    sources: Sequence[UniverseSource] | None = None,
    cache_path: str | None = None,
    max_age_days: int = UNIVERSE_MAX_AGE_DAYS,
) -> tuple[list[UniverseMember], list[SourceStatus], list[str]]:
    """``(members, per-source status, warnings)``. Never raises."""
    sources = default_sources() if sources is None else list(sources)
    path = cache_path or UNIVERSE_CACHE_PATH
    cached_on, cached = _load_cache(path)
    fresh = cached_on is not None and (today - cached_on).days < max_age_days

    def from_cache(key: str) -> list[UniverseMember]:
        return [m for m in cached if key in m.sources]

    if fresh:
        statuses = [
            SourceStatus(s.key, s.label, len(from_cache(s.key)), True, f"List saved {cached_on.isoformat()}")
            for s in sources
        ]
        return cached, statuses, []

    groups: list[Sequence[UniverseMember]] = []
    statuses: list[SourceStatus] = []
    warnings: list[str] = []
    any_downloaded = False
    for source in sources:
        try:
            members = source.fetch()
            if not members:
                raise UniverseError("the list came back empty")
            statuses.append(SourceStatus(source.key, source.label, len(members), True))
            any_downloaded = True
        except UniverseError as error:
            members = from_cache(source.key)
            saved_on = cached_on.isoformat() if cached_on else None
            note = f"Using the list saved {saved_on}" if members else "No saved list to fall back on"
            statuses.append(SourceStatus(source.key, source.label, len(members), False, note))
            warnings.append(f"{source.label}: {error}. {note}.")
        groups.append(members)
    merged = merge_members(groups)
    if any_downloaded and merged:
        _save_cache(path, today, merged)
    return merged, statuses, warnings
