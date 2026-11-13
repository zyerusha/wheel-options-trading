"""Runs the Credit Spreads scan in stages, off the request thread.

    1. Universe    index lists and the reader's ETFs (wheel/spreaduniverse.py)
    2. Screen      one batched quote call per ~150 tickers: price, volume, cap,
                   earnings. No option data is touched yet.
    3. Chains      option dates and chains for the stocks that passed (most
                   actively traded first, up to ``max_stocks``), plus history.
    4. Rank        build spreads; filters and scores apply when viewed, so a
                   filter change needs no download (wheel/spreadscanner.py).
    5. Verify      re-download the top candidates' chains and rank again.

A scan never touches the Wheel dashboard's data. It deliberately doesn't use
``Dashboard._option_chains``: that cache remembers every chain read so the next
price refresh can refetch it, and hundreds of scanner chains would slow every
Wheel refresh from then on.

Market data arrives through :class:`ScanDataProvider`. Yahoo's free feed is the
only implementation today. It has no real-time option quotes, implied
volatility history or broker probabilities, so those show as unavailable. A
better feed only needs a new class here; nothing downstream changes.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence, TypeVar

from wheel import marketdata, spreadjournal, spreaduniverse
from wheel import spreadscanner as scanner
from wheel.creditspreads import ChainQuotes, SpreadPosition, SpreadSettings, chain_quotes_from_snapshot
from wheel.marketdata import MarketDataError, PricePoint
from wheel.reference import load_earnings
from wheel.spreadscanner import ScannerSettings, Underlying

log = logging.getLogger("wheel.spreadscan")

QUOTE_BATCH_SIZE = 150
CONCURRENT_REQUESTS = 8
# About 10 requests a second: quick enough for a scan, gentle on a free feed.
MIN_SECONDS_BETWEEN_REQUESTS = 0.10
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 1.0
FULL_SCAN_COOLDOWN_SECONDS = 120.0
REFRESH_COOLDOWN_SECONDS = 15.0
# Stage 5 re-downloads the chains behind this many of the best candidates.
VERIFY_CANDIDATE_FACTOR = 2
BARS_KEPT = 300
VIX_TICKER = "^VIX"
WARNINGS_KEPT = 8

T = TypeVar("T")


class RateLimiter:
    """Spaces out requests across every thread using it."""

    def __init__(self, min_interval: float, *, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self._min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._min_interval
        if slot > now:
            self._sleep(slot - now)


def with_retry(call: Callable[[], T], *, attempts: int = RETRY_ATTEMPTS, sleep: Callable[[float], None] = time.sleep) -> T:
    """Retries a failed download with growing waits. A 404 means the data
    doesn't exist, so it isn't retried."""
    for attempt in range(attempts):
        try:
            return call()
        except MarketDataError as error:
            if attempt == attempts - 1 or "404" in str(error):
                raise
            sleep(RETRY_BASE_DELAY_SECONDS * 2**attempt)
    raise AssertionError("unreachable")


class ScanDataProvider(Protocol):
    """Everything the scan needs from a market-data source."""

    name: str
    realtime: bool  # True only if option quotes are real-time; decides the LIVE label

    def universe(self, today: date) -> tuple[list[spreaduniverse.UniverseMember], list[spreaduniverse.SourceStatus], list[str]]: ...

    def underlyings(self, members: Mapping[str, spreaduniverse.UniverseMember], today: date) -> tuple[dict[str, Underlying], list[str]]: ...

    def expirations(self, tickers: Sequence[str], today: date) -> tuple[dict[str, list[date]], list[str]]: ...

    def chain(self, ticker: str, expiry: date) -> ChainQuotes: ...

    def bars(self, ticker: str) -> tuple[list[PricePoint], list[str]]: ...

    def local_bars(self, ticker: str) -> list[PricePoint]: ...


def underlying_from_quote(
    ticker: str,
    row: Mapping[str, Any],
    member: spreaduniverse.UniverseMember,
    today: date,
    earnings_override: date | None = None,
) -> Underlying:
    """One row of Yahoo's batched quote. Yahoo's ``earningsTimestamp`` is often
    the report just passed, so only a date from today on counts as upcoming; a
    hand-kept date in ``earnings.json`` wins over both."""
    name = row.get("longName") or row.get("shortName") or member.name
    kind = marketdata._leverage_kind(name, row.get("quoteType"))
    is_etf = str(row.get("quoteType") or "").upper() == "ETF"
    excluded = {
        None: "not a stock or ETF",
        "leveraged_etf": "leveraged ETF",
        "inverse_etf": "inverse ETF",
        "closed_end_fund": "closed-end fund",
        "mutual_fund": "mutual fund",
    }.get(kind)

    def number(key: str) -> float | None:
        raw = row.get(key)
        return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None

    upcoming = earnings_override
    if upcoming is None:
        for key in ("earningsTimestampStart", "earningsTimestamp"):
            day = marketdata._epoch_to_date(row.get(key))
            if day is not None and day >= today:
                upcoming = day
                break
    cap = number("marketCap")
    volume = number("averageDailyVolume10Day") or number("averageDailyVolume3Month")
    return Underlying(
        ticker=ticker,
        is_etf=is_etf,
        name=name,
        sector=member.sector,
        price=number("regularMarketPrice"),
        market_cap_b=cap / 1e9 if cap else None,
        avg_volume_m=volume / 1e6 if volume else None,
        earnings_date=upcoming,
        eps_ttm=number("epsTrailingTwelveMonths"),
        excluded_kind=excluded,
        sources=member.sources,
    )


class YahooScanProvider:
    """Yahoo's free endpoints through ``wheel/marketdata.py``."""

    name = "Yahoo Finance (free feed, option quotes delayed)"
    realtime = False

    def __init__(self, limiter: RateLimiter | None = None) -> None:
        self._limiter = limiter or RateLimiter(MIN_SECONDS_BETWEEN_REQUESTS)

    def _paced(self, call: Callable[[], T]) -> T:
        def attempt() -> T:
            self._limiter.wait()
            return call()

        return with_retry(attempt)

    def universe(self, today: date):
        return spreaduniverse.get_universe(today)

    def underlyings(self, members, today):
        tickers = sorted(members)
        overrides = load_earnings()
        out: dict[str, Underlying] = {}
        warnings: list[str] = []
        for start in range(0, len(tickers), QUOTE_BATCH_SIZE):
            batch = tickers[start : start + QUOTE_BATCH_SIZE]
            try:
                rows = marketdata.parse_yahoo_quotes(self._paced(lambda: marketdata.fetch_yahoo_quotes(batch)))
            except MarketDataError as error:
                warnings.append(f"Couldn't load quotes for {len(batch)} tickers: {error}")
                continue
            for ticker in batch:
                row = rows.get(ticker)
                if row:
                    out[ticker] = underlying_from_quote(ticker, row, members[ticker], today, overrides.get(ticker))
        return out, warnings

    def expirations(self, tickers, today):
        def fetch(ticker: str) -> str:
            return self._paced(lambda: marketdata.fetch_yahoo_option_expirations(ticker))

        return marketdata.get_option_expirations(tickers, fetch=fetch, today=today)

    def chain(self, ticker, expiry):
        text = self._paced(lambda: marketdata.fetch_yahoo_option_chain(ticker, expiry))
        snapshot = marketdata.parse_yahoo_option_chain(text, expiry)
        return chain_quotes_from_snapshot(replace(snapshot, ticker=ticker), datetime.now(timezone.utc))

    def bars(self, ticker):
        local = marketdata.get_price_series(ticker, local_only=True, require_ranges=True)
        if local is None:
            self._limiter.wait()
            local = marketdata.get_price_series(ticker, require_ranges=True)
        points, warnings = local
        return points[-BARS_KEPT:], warnings

    def local_bars(self, ticker):
        local = marketdata.get_price_series(ticker, local_only=True)
        return [] if local is None else local[0][-BARS_KEPT:]


@dataclass
class ScanProgress:
    """What the page shows while a scan runs. Guarded by the service's lock."""

    stage: str = "Starting"
    done: int = 0
    total: int = 0


@dataclass(frozen=True)
class ScanSnapshot:
    """One finished scan's market data. Immutable; a refresh makes a new one."""

    scan_id: int
    today: date
    settings: ScannerSettings
    started_at: datetime
    finished_at: datetime
    duration_seconds: float
    provider_name: str
    realtime: bool
    underlyings: Mapping[str, Underlying]
    technicals: Mapping[str, scanner.Technicals]
    market: scanner.MarketConditions
    chains: Mapping[tuple[str, date], ChainQuotes]
    spreads: tuple[scanner.RawSpread, ...]
    construction_rejects: Counter
    screen_rejects: Counter
    screen_examples: tuple[tuple[str, str], ...]
    source_statuses: tuple[spreaduniverse.SourceStatus, ...]
    sector_by_ticker: Mapping[str, str | None]
    universe_size: int
    stocks_passed: int
    underlyings_scanned: int
    chains_fetched: int
    chains_failed: int
    warnings: tuple[str, ...]
    verified_at: datetime | None = None
    verify_note: str | None = None


def build_spreads(
    chains: Mapping[tuple[str, date], ChainQuotes], *, today: date, risk_free_rate: float, fee_per_contract: float
) -> tuple[tuple[scanner.RawSpread, ...], Counter]:
    spreads: list[scanner.RawSpread] = []
    rejects: Counter = Counter()
    for pair in sorted(chains):
        found, thrown_out = scanner.construct_spreads(
            chains[pair], today=today, risk_free_rate=risk_free_rate, fee_per_contract=fee_per_contract
        )
        spreads.extend(found)
        rejects.update(thrown_out)
    return tuple(spreads), rejects


def _empty_exposure() -> scanner.Exposure:
    return scanner.Exposure(None, "", 0.0, {}, {}, {})


def _rank(
    snapshot: ScanSnapshot,
    settings: ScannerSettings,
    exposure: scanner.Exposure,
    now: datetime,
) -> scanner.RankedView:
    return scanner.rank_opportunities(
        snapshot.spreads,
        underlyings=snapshot.underlyings,
        technicals=snapshot.technicals,
        market=snapshot.market,
        exposure=exposure,
        settings=settings,
        today=snapshot.today,
        now=now,
        realtime=snapshot.realtime,
    )


def _fetch_chains(
    provider: ScanDataProvider, pairs: Sequence[tuple[str, date]], on_done: Callable[[], None]
) -> tuple[dict[tuple[str, date], ChainQuotes], list[str]]:
    """Every pair it can get. A failed chain is left out and reported, never
    replaced by an older copy."""

    def fetch(pair: tuple[str, date]) -> tuple[tuple[str, date], ChainQuotes | None, str | None]:
        try:
            return pair, provider.chain(*pair), None
        except MarketDataError as error:
            return pair, None, f"{pair[0]} {pair[1].isoformat()}: {error}"
        finally:
            on_done()

    chains: dict[tuple[str, date], ChainQuotes] = {}
    failures: list[str] = []
    if pairs:
        with ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS) as pool:
            for pair, chain, failure in pool.map(fetch, pairs):
                if chain is not None:
                    chains[pair] = chain
                else:
                    failures.append(failure or "")
    return chains, failures


def run_scan(
    provider: ScanDataProvider,
    settings: ScannerSettings,
    *,
    scan_id: int,
    today: date,
    now: Callable[[], datetime],
    risk_free_rate: float,
    fee_per_contract: float,
    report: Callable[[str, int, int], None] = lambda stage, done, total: None,
) -> ScanSnapshot:
    """The full staged scan, start to finish, on the calling thread."""
    started = now()
    clock_start = time.monotonic()
    warnings: list[str] = []

    report("Finding companies to scan", 0, 0)
    listed, statuses, universe_warnings = provider.universe(today)
    warnings += universe_warnings
    members = {m.ticker: m for m in listed}
    etf_sector = {etf: sector for sector, etf in spreaduniverse.SECTOR_ETFS.items()}
    for etf in settings.etfs:
        members[etf] = spreaduniverse.UniverseMember(etf, None, etf_sector.get(etf), ("etf_list",))

    report(f"Screening {len(members):,} tickers", 0, len(members))
    quotes, quote_warnings = provider.underlyings(members, today)
    warnings += quote_warnings
    screen_rejects: Counter = Counter()
    examples: list[tuple[str, str]] = []
    passed: list[Underlying] = []
    for ticker in sorted(members):
        underlying = quotes.get(ticker)
        reason = "No quote from the data source" if underlying is None else scanner.screen_underlying(underlying, settings, today)
        if reason:
            screen_rejects[reason] += 1
            examples.append((ticker, reason))
        elif underlying is not None:
            passed.append(underlying)
    stocks = scanner.prioritize_stocks(passed, settings, today)
    etfs = [u for u in passed if u.is_etf]
    skipped = sum(1 for u in passed if not u.is_etf) - len(stocks)
    if skipped > 0:
        screen_rejects[f"Passed, but outside the {settings.max_stocks} most actively traded"] += skipped
    scanned = etfs + stocks
    tickers = [u.ticker for u in scanned]

    report("Checking listed option dates", 0, len(tickers))
    listed_dates, expiry_warnings = provider.expirations(tickers, today)
    warnings += expiry_warnings[:WARNINGS_KEPT]
    pairs: list[tuple[str, date]] = []
    for ticker in tickers:
        dates = listed_dates.get(ticker)
        chosen = scanner.pick_expirations(dates or [], today, settings)
        if not dates:
            screen_rejects["No listed options"] += 1
            examples.append((ticker, "No listed options"))
        elif not chosen:
            screen_rejects[f"No expiration between {settings.dte_min} and {settings.dte_max} DTE"] += 1
            examples.append((ticker, "No expiration in the DTE range"))
        pairs += [(ticker, d) for d in chosen]

    done = [0]
    progress_lock = threading.Lock()

    def tick(stage: str, total: int) -> Callable[[], None]:
        def on_done() -> None:
            with progress_lock:
                done[0] += 1
                report(stage, done[0], total)

        return on_done

    report("Downloading option chains", 0, len(pairs))
    chains, failures = _fetch_chains(provider, pairs, tick("Downloading option chains", len(pairs)))
    if failures:
        warnings.append(f"{len(failures)} of {len(pairs)} option chains couldn't be loaded. First: {failures[0]}")

    # History for the tickers that returned a chain, plus what market conditions need.
    chain_tickers = sorted({ticker for ticker, _ in chains})
    context_tickers = {"SPY", "QQQ", VIX_TICKER, *spreaduniverse.SECTOR_ETFS.values()}
    history_tickers = sorted(set(chain_tickers) | context_tickers)
    done[0] = 0
    report("Loading price history", 0, len(history_tickers))
    history_done = tick("Loading price history", len(history_tickers))

    def history(ticker: str) -> tuple[str, list[PricePoint], list[str]]:
        try:
            points, notes = provider.bars(ticker)
            return ticker, points, notes
        finally:
            history_done()

    bars: dict[str, list[PricePoint]] = {}
    history_warnings: list[str] = []
    if history_tickers:
        with ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS) as pool:
            for ticker, points, notes in pool.map(history, history_tickers):
                bars[ticker] = points
                history_warnings += notes
    if history_warnings:
        warnings.append(f"{len(history_warnings)} price histories had problems. First: {history_warnings[0]}")

    report("Scoring spreads", 0, 0)
    technicals = {t: scanner.analyze_technicals(t, bars.get(t, [])) for t in chain_tickers}

    def closes(ticker: str) -> list[float]:
        return [b.close for b in bars.get(ticker, [])]

    market = scanner.market_conditions(
        closes("SPY"),
        closes("QQQ"),
        closes(VIX_TICKER),
        {sector: closes(etf) for sector, etf in spreaduniverse.SECTOR_ETFS.items()},
    )
    spreads, construction_rejects = build_spreads(chains, today=today, risk_free_rate=risk_free_rate, fee_per_contract=fee_per_contract)
    sector_by_ticker = {m.ticker: m.sector for m in members.values()}
    snapshot = ScanSnapshot(
        scan_id=scan_id,
        today=today,
        settings=settings,
        started_at=started,
        finished_at=now(),
        duration_seconds=time.monotonic() - clock_start,
        provider_name=provider.name,
        realtime=provider.realtime,
        underlyings={u.ticker: u for u in scanned},
        technicals=technicals,
        market=market,
        chains=chains,
        spreads=spreads,
        construction_rejects=construction_rejects,
        screen_rejects=screen_rejects,
        screen_examples=tuple(examples),
        source_statuses=tuple(statuses),
        sector_by_ticker=sector_by_ticker,
        universe_size=len(members),
        stocks_passed=len(passed),
        underlyings_scanned=len(scanned),
        chains_fetched=len(chains),
        chains_failed=len(failures),
        warnings=tuple(warnings),
    )
    verified = verify_top(snapshot, provider, settings, now=now, risk_free_rate=risk_free_rate, fee_per_contract=fee_per_contract, report=report)
    return replace(verified, finished_at=now(), duration_seconds=time.monotonic() - clock_start)


def verify_top(
    snapshot: ScanSnapshot,
    provider: ScanDataProvider,
    settings: ScannerSettings,
    *,
    now: Callable[[], datetime],
    risk_free_rate: float,
    fee_per_contract: float,
    report: Callable[[str, int, int], None] = lambda stage, done, total: None,
) -> ScanSnapshot:
    """Stage 5: re-download only the chains behind the best candidates and
    rebuild from them. A chain that can't be re-downloaded loses its spreads,
    so a top trade can never rest on prices the refresh failed to confirm."""
    ranked = _rank(snapshot, settings, _empty_exposure(), now())
    wanted = settings.top_n * VERIFY_CANDIDATE_FACTOR
    candidates = list(ranked.top) + [s for s, _ in ranked.also_considered]
    pairs = list(dict.fromkeys((s.raw.ticker, s.raw.expiry) for s in candidates[:wanted]))
    before = {s.raw.id for s in ranked.top}
    if not pairs:
        return replace(snapshot, verified_at=now(), verify_note="No candidates to verify.")

    done = [0]
    lock = threading.Lock()

    def on_done() -> None:
        with lock:
            done[0] += 1
            report("Rechecking quotes on the top candidates", done[0], len(pairs))

    report("Rechecking quotes on the top candidates", 0, len(pairs))
    fresh, failures = _fetch_chains(provider, pairs, on_done)
    chains = dict(snapshot.chains)
    for pair in pairs:
        chains.pop(pair, None)
    chains.update(fresh)
    spreads, rejects = build_spreads(chains, today=snapshot.today, risk_free_rate=risk_free_rate, fee_per_contract=fee_per_contract)
    updated = replace(snapshot, chains=chains, spreads=spreads, construction_rejects=rejects)
    after = {s.raw.id for s in _rank(updated, settings, _empty_exposure(), now()).top}
    dropped = len(before - after)
    note = f"Rechecked {len(fresh)} of {len(pairs)} chains behind the top candidates."
    if dropped:
        note += f" {dropped} of the top trades changed after the recheck."
    if failures:
        note += f" {len(failures)} chains failed, so their spreads were removed."
    return replace(updated, verified_at=now(), verify_note=note)


@dataclass
class _ServiceState:
    running: str | None = None  # "full" | "refresh" | None
    progress: ScanProgress = field(default_factory=ScanProgress)
    snapshot: ScanSnapshot | None = None
    error: str | None = None
    next_scan_id: int = 1
    last_full_started: float | None = None
    last_refresh_started: float | None = None


class ScanService:
    """One scan at a time, one latest result, kept in memory. The result is
    market-wide; each view applies its own filters and account. Whatever goes
    wrong in a scan stays in :attr:`_state.error`: it can't reach the Wheel
    dashboard, and the previous result stays visible."""

    def __init__(
        self,
        provider: ScanDataProvider | None = None,
        *,
        risk_free_rate: float,
        fee_per_contract: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = provider
        self._risk_free_rate = risk_free_rate
        self._fee = fee_per_contract
        self._clock = clock
        self._lock = threading.Lock()
        self._state = _ServiceState()
        self._journaled: set[tuple[int, str]] = set()

    def _get_provider(self) -> ScanDataProvider:
        if self._provider is None:
            self._provider = YahooScanProvider()
        return self._provider

    def _report(self, stage: str, done: int, total: int) -> None:
        with self._lock:
            self._state.progress = ScanProgress(stage, done, total)

    def start(self, mode: str, settings: ScannerSettings, *, background: bool = True) -> tuple[bool, str]:
        """``(started, message)``. Refused while one is running or too soon
        after the last, so a repeated click can't hammer the data source."""
        now = self._clock()
        with self._lock:
            state = self._state
            if state.running:
                return False, "A scan is already running."
            if mode == "refresh":
                if state.snapshot is None:
                    return False, "Run a scan first; there is nothing to refresh yet."
                if state.last_refresh_started is not None and now - state.last_refresh_started < REFRESH_COOLDOWN_SECONDS:
                    return False, "Quotes were just refreshed. Try again in a few seconds."
                state.last_refresh_started = now
            else:
                if state.last_full_started is not None and now - state.last_full_started < FULL_SCAN_COOLDOWN_SECONDS:
                    wait = FULL_SCAN_COOLDOWN_SECONDS - (now - state.last_full_started)
                    return False, f"The last scan just ran. You can start another in {wait:.0f} seconds."
                state.last_full_started = now
            state.running = mode
            state.error = None
            state.progress = ScanProgress()
            scan_id = state.next_scan_id
            state.next_scan_id += 1
        if background:
            threading.Thread(target=self._work, args=(mode, settings, scan_id), name="spread-scan", daemon=True).start()
        else:
            self._work(mode, settings, scan_id)
        return True, "Refreshing the top quotes." if mode == "refresh" else "Scan started."

    def _work(self, mode: str, settings: ScannerSettings, scan_id: int) -> None:
        now = lambda: datetime.now(timezone.utc)  # noqa: E731
        try:
            provider = self._get_provider()
            if mode == "refresh":
                with self._lock:
                    current = self._state.snapshot
                snapshot = verify_top(
                    current, provider, settings, now=now, risk_free_rate=self._risk_free_rate,
                    fee_per_contract=self._fee, report=self._report,
                )
            else:
                snapshot = run_scan(
                    provider, settings, scan_id=scan_id, today=date.today(), now=now,
                    risk_free_rate=self._risk_free_rate, fee_per_contract=self._fee, report=self._report,
                )
            with self._lock:
                self._state.snapshot = snapshot
            log.info(
                "spread scan %s finished: %s underlyings, %s chains, %s spreads",
                mode, snapshot.underlyings_scanned, snapshot.chains_fetched, len(snapshot.spreads),
            )
        except Exception as error:  # noqa: BLE001 - a scan failure must only ever reach the scan's own status line
            log.exception("spread scan %s failed", mode)
            with self._lock:
                self._state.error = f"The scan stopped: {error}"
        finally:
            with self._lock:
                self._state.running = None

    def status_payload(self) -> dict[str, Any]:
        with self._lock:
            state = self._state
            snapshot = state.snapshot
            progress = state.progress
            return {
                "running": state.running,
                "stage": progress.stage if state.running else None,
                "done": progress.done,
                "total": progress.total,
                "error": state.error,
                "last_scan_at": snapshot.finished_at.isoformat() if snapshot else None,
                "duration_seconds": round(snapshot.duration_seconds, 1) if snapshot else None,
                "verified_at": snapshot.verified_at.isoformat() if snapshot and snapshot.verified_at else None,
                "verify_note": snapshot.verify_note if snapshot else None,
                "universe_size": snapshot.universe_size if snapshot else None,
                "stocks_passed": snapshot.stocks_passed if snapshot else None,
                "underlyings_scanned": snapshot.underlyings_scanned if snapshot else None,
                "chains_fetched": snapshot.chains_fetched if snapshot else None,
                "chains_failed": snapshot.chains_failed if snapshot else None,
                "spreads_built": len(snapshot.spreads) if snapshot else None,
                "data_source": snapshot.provider_name if snapshot else None,
            }

    def view(
        self,
        settings: ScannerSettings,
        *,
        management: SpreadSettings,
        positions: Sequence[SpreadPosition],
        equity: tuple[float | None, str],
        settings_warnings: Sequence[str] = (),
        journal_path: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """The page's whole payload for one account and one set of filters.
        Reads only what the last scan gathered and local files: no network."""
        now = now or datetime.now(timezone.utc)
        payload: dict[str, Any] = {
            "api_version": scanner.SCANNER_API_VERSION,
            "scan": self.status_payload(),
            "settings": settings.as_dict(),
            "settings_warnings": list(settings_warnings),
            "score_categories": [
                {"label": c.label, "weight": c.weight, "how": c.how} for c in scanner.SCORE_CATEGORIES
            ],
            "ratings": [{"rating": rating, "min_score": floor} for rating, floor in scanner.RATING_MIN_SCORE]
            + [{"rating": scanner.WATCH, "min_score": settings.min_score}],
            "has_results": False,
        }
        with self._lock:
            snapshot = self._state.snapshot
        open_positions = [p for p in positions if not p.is_closed]
        sector_of = dict(snapshot.sector_by_ticker) if snapshot else {}
        account_value, account_source = equity
        closes: dict[str, Mapping[date, float]] = {}
        for ticker in {p.ticker for p in open_positions}:
            tech = snapshot.technicals.get(ticker) if snapshot else None
            if tech is not None and tech.closes_by_day:
                closes[ticker] = tech.closes_by_day
            elif snapshot:
                bars = self._get_provider().local_bars(ticker)
                closes[ticker] = {b.day: b.close for b in bars[-(scanner.CORRELATION_SESSIONS + 1):]}
        exposure = scanner.build_exposure(
            open_positions, sector_of=sector_of, account_value=account_value, account_source=account_source, closes_by_ticker=closes
        )
        payload["exposure"] = exposure.payload(settings)
        records = spreadjournal.load(journal_path) if journal_path else []
        if snapshot is None:
            payload["track_record"] = spreadjournal.track_record(records, positions, management)
            return payload

        ranked = _rank(snapshot, settings, exposure, now)
        list_warnings = scanner.apply_diversification_warnings(ranked.top, settings)
        rows = [
            scanner.opportunity_payload(
                scored, rank, technicals=snapshot.technicals.get(scored.raw.ticker), settings=settings, management=management,
                account_value=account_value, risk_free_rate=self._risk_free_rate, list_warnings=list_warnings.get(scored.raw.id, ()),
            )
            for rank, scored in enumerate(ranked.top, start=1)
        ]
        if journal_path:
            key = (snapshot.scan_id, journal_path)
            # Only trades priced on current quotes: last session's prices aren't a
            # real recommendation. A later scan during market hours records them.
            current = [row for row in rows if row["quote_status"] in (scanner.QUOTE_LIVE, scanner.QUOTE_DELAYED)]
            if key not in self._journaled and current:
                records = spreadjournal.record(journal_path, current, snapshot.today)
                self._journaled.add(key)
        statuses = Counter(row["quote_status"] for row in rows)
        payload.update(
            has_results=True,
            rescan_needed=snapshot.settings.scan_key != settings.scan_key,
            opportunities=rows,
            also_considered=[{**scanner.opportunity_summary(s), "reason": reason} for s, reason in ranked.also_considered],
            evaluated=ranked.evaluated,
            qualified=ranked.qualified,
            rejected={
                "spread_reasons": [{"reason": r, "count": n} for r, n in ranked.reject_counts],
                "quote_reasons": [{"reason": r, "count": n} for r, n in snapshot.construction_rejects.most_common()],
                "stock_reasons": [{"reason": r, "count": n} for r, n in snapshot.screen_rejects.most_common()],
                "spread_examples": [
                    {
                        "ticker": raw.ticker,
                        "strategy_label": scanner.STRATEGY_LABELS[raw.strategy],
                        "short_strike": raw.short.strike,
                        "long_strike": raw.long.strike,
                        "expiry": raw.expiry.isoformat(),
                        "reason": reason,
                    }
                    for raw, reason in ranked.rejected_examples
                ],
                "stock_examples": [{"ticker": t, "reason": r} for t, r in snapshot.screen_examples[:scanner.REJECTED_EXAMPLES_SHOWN]],
            },
            market=snapshot.market.payload(),
            quote_counts=dict(statuses),
            warnings=list(snapshot.warnings),
            universe_sources=[
                {"label": s.label, "count": s.count, "live": s.live, "note": s.note} for s in snapshot.source_statuses
            ],
            track_record=spreadjournal.track_record(records, positions, management),
        )
        return payload
