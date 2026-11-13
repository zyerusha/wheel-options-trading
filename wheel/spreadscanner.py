"""Credit spread opportunity scanner: decides which spreads deserve attention
and explains why. Pure: no network, no clock, no files. Callers pass quotes,
history, positions and the time; ``wheel/spreadscanservice.py`` runs the staged
scan that gathers them. The frontend only displays what this module returns.

Spread math (credit, max loss, breakeven, targets, delta, chance of profit) is
``wheel/creditspreads.py``'s, never recomputed here. Definitions added here,
per spread (one contract of each leg) unless a name says otherwise:

* Credit = short bid - long ask, the conservative price; every metric uses it.
  The midpoint credit is shown as an estimate.
* Return on risk = credit / (width - credit). Annualized = that x 365 / DTE
  (simple, not compounded; the same annualizing as the open-position Ann. Yield).
* Chance of profit = Black-Scholes chance of finishing past the breakeven from
  the short leg's implied volatility (``creditspreads.probability_of_profit``).
  Quoted implied volatility is trusted only if it is plausible and reproduces
  the quoted price (:func:`iv_problem`); otherwise the estimate is unavailable.
  ``1 - delta`` is shown beside it as a rough cross-check: it looks at the
  short strike, not the breakeven. Yahoo carries no broker probability.
* Chance of touch = about 2 x the chance of finishing past the short strike
  (the reflection rule for a driftless price). Rough, and labelled so.
* Expected value = chance of profit x max profit - (1 - chance of profit) x
  max loss - round-trip fees (four legs). Every loss counts as max loss, so it
  leans pessimistic. It uses market-implied odds, so it measures how much the
  market charges for the trade and is not evidence of an edge.

Score (0-100) = sum of category points - penalties. Each category earns its
weight x a 0-1 credit from a smooth curve (:data:`SCORE_CATEGORIES`). Data that
is missing earns no credit, so a gap can never lift a trade. The score is a
relative ranking from fixed rules; it has not been shown to predict results.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from wheel.creditspreads import (
    BEAR_CALL,
    BULL_PUT,
    STRATEGY_LABELS,
    STRATEGY_RIGHT,
    ChainQuotes,
    LegQuote,
    QuoteTiming,
    SpreadPosition,
    SpreadSettings,
    _age_text,
    _day,
    _iso,
    _iso_dt,
    _pct,
    _round,
    _strike,
    _usd,
    breakeven,
    loss_trigger_debit,
    market_context,
    max_loss_per_contract,
    max_profit_per_contract,
    mid_credit,
    probability_of_profit,
    short_leg_delta,
    spread_width,
    suggested_contracts,
    target_closing_debit,
)
from wheel.marketdata import PricePoint
from wheel.optionsmath import black_scholes_price, bid_ask_mid, probability_finish_above
from wheel.parser import OPTION_MULTIPLIER
from wheel.pricezones import RESISTANCE, SUPPORT, analyze, simple_moving_average

# Bumped when /api/spreads/opportunities changes a field the page reads. Must
# match SCANNER_API_VERSION in spreadscanner.js.
SCANNER_API_VERSION = 1

BOTH = "BOTH"
ETF_ONLY = "ETF"
STOCKS_ONLY = "STOCK"
EARNINGS_PENALIZE = "penalize"
EARNINGS_EXCLUDE = "exclude"

DEFAULT_ETFS = (
    "SPY", "QQQ", "IWM", "IVV", "DIA",
    "XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC",
)

# Spreads are built across this delta range so the delta filter can move
# without another download; the filter itself is the reader's setting.
CONSTRUCTION_DELTA_RANGE = (0.03, 0.40)
# Widths tried, kept only when listed and between these shares of the price:
# narrower is all commission, wider stops being a small defined-risk trade.
CANDIDATE_WIDTHS = (1.0, 2.0, 2.5, 5.0, 10.0, 15.0, 20.0, 25.0, 50.0)
WIDTH_SHARE_OF_PRICE = (0.002, 0.10)
# Implied volatility outside this is treated as bad data.
IV_PLAUSIBLE_RANGE = (0.03, 3.00)
# The quoted IV must reprice the option to within this share of its midpoint
# (or $0.10 for cheap options); otherwise it belongs to some other moment.
IV_REPRICE_TOLERANCE = 0.35
IV_REPRICE_TOLERANCE_USD = 0.10
FEE_LEGS_ROUND_TRIP = 4

# Quote status. Yahoo's option quotes are delayed, so LIVE needs a provider
# that says it is real-time.
QUOTE_LIVE = "LIVE"
QUOTE_DELAYED = "DELAYED"
QUOTE_CACHED = "CACHED"
QUOTE_STALE = "STALE"
QUOTE_UNAVAILABLE = "UNAVAILABLE"
QUOTE_STALE_MINUTES = 20
QUOTE_CACHED_MINUTES = 10

EXCELLENT = "Excellent"
VERY_GOOD = "Very Good"
GOOD = "Good"
WATCH = "Watch"
REJECTED = "Rejected"
RATING_ORDER = (REJECTED, WATCH, GOOD, VERY_GOOD, EXCELLENT)
RATING_MIN_SCORE = ((EXCELLENT, 80.0), (VERY_GOOD, 70.0), (GOOD, 60.0))

# What a rating demands beyond its score.
GOOD_CAP_MIN_POP = 0.70
GOOD_CAP_MIN_ROR_PCT = 12.0
GOOD_CAP_MIN_LIQUIDITY = 50.0
# Expected value worse than this share of max loss means the premium doesn't
# pay for the risk, however high the score.
GOOD_CAP_MIN_EV_PCT_OF_RISK = -8.0

PENALTY_EARNINGS_IN_WINDOW = 15.0
PENALTY_EARNINGS_UNKNOWN = 3.0
PENALTY_UNPROFITABLE = 3.0
PENALTY_POOR_LIQUIDITY_MAX = 15.0
POOR_LIQUIDITY_BELOW = 50.0
PENALTY_SAME_TICKER = 5.0
PENALTY_OVER_LIMIT = 5.0
PENALTY_CORRELATED = 3.0
CORRELATION_SESSIONS = 60
HIGH_CORRELATION = 0.80

# A zone weaker than this isn't a level worth citing (wheel.pricezones 0-100).
MEANINGFUL_ZONE_SCORE = 25.0
VIX_CALM = 20.0
VIX_STRESSED = 30.0
VIX_DIRECTION_SESSIONS = 5
IV_VERY_HIGH = 1.00
CHART_SESSIONS = 120
# Wider spreads pay less for crossing the bid and ask relative to their credit,
# which nudges their score up without making them better trades. Scores this
# close count as a tie, and the tie goes to the width risking less money.
WIDTH_SCORE_TIE = 2.0
REJECTED_EXAMPLES_SHOWN = 40
ALSO_CONSIDERED_SHOWN = 15


@dataclass(frozen=True)
class ScoreCategory:
    key: str
    label: str
    weight: float
    how: str


SCORE_CATEGORIES = (
    ScoreCategory("pop", "Chance of profit", 25.0, "S-curve centered on 72%: about half credit at 72%, nearly full by 85%. No reliable estimate earns nothing."),
    ScoreCategory("ror", "Return on risk", 20.0, "S-curve centered on 18%: small returns earn little however likely they are."),
    ScoreCategory("annualized", "Annualized return", 15.0, "S-curve centered on 170%, so trades with different DTE compare fairly."),
    ScoreCategory("liquidity", "Liquidity and fills", 15.0, "Bid/ask cost against the credit (50%), open interest (35%) and today's volume (15%)."),
    ScoreCategory("technical", "Support or resistance", 10.0, "Short strike against the nearest real price zone (60%) and price against its 20, 50 and 200-day averages (40%)."),
    ScoreCategory("volatility", "Volatility and premium", 10.0, "Implied vs realized volatility: best at 1.1-1.6x, less above and below. Halved when earnings or very high volatility suggest the premium is pay for real risk."),
    ScoreCategory("market", "Market and sector", 5.0, "SPY trend, QQQ trend, VIX and the stock's sector trend, each judged for this spread's direction."),
)
_CATEGORY_BY_KEY = {c.key: c for c in SCORE_CATEGORIES}


# ---------------------------------------------------------------- settings


@dataclass(frozen=True)
class ScannerSettings:
    """Every scanner setting, grouped by what changing it does. Deltas are
    fractions (0.25), percentages are percent (60.0)."""

    # Hard filters: a spread failing one is rejected, with the reason shown.
    strategy: str = BOTH
    asset_class: str = BOTH
    min_pop_pct: float = 60.0
    min_ror_pct: float = 8.0
    min_annualized_ror_pct: float = 50.0
    delta_min: float = 0.10
    delta_max: float = 0.25
    min_credit: float = 0.20
    max_width: float = 20.0
    min_liquidity: float = 30.0
    min_open_interest: int = 50
    min_score: float = 45.0
    earnings_policy: str = EARNINGS_PENALIZE
    # Ranking preferences: they order and shape the list, never reject.
    dte_pref_min: int = 30
    dte_pref_max: int = 45
    top_n: int = 10
    max_per_ticker: int = 1
    max_per_sector: int = 3
    # Risk limits, as % of account value: breaking one warns and lowers the score.
    max_trade_pct: float = 2.0
    max_total_pct: float = 20.0
    max_ticker_pct: float = 5.0
    max_sector_pct: float = 10.0
    # Market data: these decide what gets downloaded, so they need a new scan.
    dte_min: int = 25
    dte_max: int = 60
    min_price: float = 20.0
    max_price: float = 500.0
    min_market_cap_b: float = 1.0
    min_avg_volume_m: float = 1.0
    max_stocks: int = 60
    max_expirations: int = 3
    etfs: tuple[str, ...] = DEFAULT_ETFS

    @property
    def scan_key(self) -> tuple:
        """The settings a finished scan's downloads depended on."""
        return (
            self.dte_min, self.dte_max, self.min_price, self.max_price, self.min_market_cap_b,
            self.min_avg_volume_m, self.max_stocks, self.max_expirations, self.etfs,
        )

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for query_name, (attribute, *_rest, scale) in _NUMBER_FIELDS.items():
            out[query_name] = round(getattr(self, attribute) * scale, 4)
        out.update(
            strategy=self.strategy,
            asset_class=self.asset_class,
            earnings_policy=self.earnings_policy,
            etfs=list(self.etfs),
        )
        return out

    @classmethod
    def from_query(cls, query: Mapping[str, Sequence[str]]) -> tuple["ScannerSettings", list[str]]:
        """Settings from query parameters (the names :meth:`as_dict` emits). A
        missing one keeps its default; an invalid one keeps its default and
        adds a warning, so a bad value is never silently used."""
        defaults = cls()
        warnings: list[str] = []
        values: dict[str, Any] = {}

        def raw(name: str) -> str | None:
            found = query.get(name)
            text = found[0].strip() if found else ""
            return text or None

        for query_name, (attribute, low, high, integer, label, scale) in _NUMBER_FIELDS.items():
            text = raw(query_name)
            if text is None:
                continue
            try:
                parsed = float(text)
            except ValueError:
                parsed = math.nan
            if not math.isfinite(parsed) or not low <= parsed <= high or (integer and parsed != int(parsed)):
                kind = "a whole number" if integer else "a number"
                warnings.append(
                    f"{label} must be {kind} from {low:g} to {high:g}; using {getattr(defaults, attribute) * scale:g}."
                )
                continue
            values[attribute] = int(parsed) if integer else parsed / scale

        for query_name, allowed, label in (
            ("strategy", (BOTH, BULL_PUT, BEAR_CALL), "Strategy"),
            ("asset_class", (BOTH, ETF_ONLY, STOCKS_ONLY), "Stocks or ETFs"),
            ("earnings_policy", (EARNINGS_PENALIZE, EARNINGS_EXCLUDE), "Earnings rule"),
        ):
            text = raw(query_name)
            if text is None:
                continue
            if text in allowed:
                values[query_name] = text
            else:
                warnings.append(f"{label} value {text!r} isn't one of the choices; using the default.")

        text = raw("etfs")
        if text is not None:
            tickers = [t.strip().upper() for t in text.split(",") if t.strip()]
            if tickers and len(tickers) <= 40 and all(t.replace("-", "").isalpha() and len(t) <= 8 for t in tickers):
                values["etfs"] = tuple(dict.fromkeys(tickers))
            else:
                warnings.append("ETFs must be 1 to 40 ticker symbols separated by commas; using the default list.")

        settings = cls(**values)
        for low_name, high_name, label in (
            ("dte_min", "dte_max", "DTE range"),
            ("dte_pref_min", "dte_pref_max", "Preferred DTE range"),
            ("delta_min", "delta_max", "Short delta range"),
            ("min_price", "max_price", "Stock price range"),
        ):
            if getattr(settings, low_name) > getattr(settings, high_name):
                warnings.append(f"{label}: the low end is above the high end; using the defaults.")
                values.pop(low_name, None)
                values.pop(high_name, None)
                settings = cls(**values)
        return settings, warnings


# query name: (attribute, low, high, whole number, label, display scale)
_NUMBER_FIELDS: dict[str, tuple[str, float, float, bool, str, float]] = {
    "min_pop": ("min_pop_pct", 0, 99, False, "Minimum chance of profit", 1.0),
    "min_ror": ("min_ror_pct", 0, 500, False, "Minimum return on risk", 1.0),
    "min_ann_ror": ("min_annualized_ror_pct", 0, 5000, False, "Minimum annualized return", 1.0),
    "delta_min": ("delta_min", 1, 50, False, "Lowest short delta", 100.0),
    "delta_max": ("delta_max", 1, 50, False, "Highest short delta", 100.0),
    "min_credit": ("min_credit", 0, 50, False, "Minimum credit", 1.0),
    "max_width": ("max_width", 0.5, 100, False, "Maximum width", 1.0),
    "min_liquidity": ("min_liquidity", 0, 100, False, "Minimum liquidity", 1.0),
    "min_oi": ("min_open_interest", 0, 100000, True, "Minimum open interest", 1.0),
    "min_score": ("min_score", 0, 100, False, "Minimum score", 1.0),
    "dte_pref_min": ("dte_pref_min", 1, 365, True, "Preferred DTE from", 1.0),
    "dte_pref_max": ("dte_pref_max", 1, 365, True, "Preferred DTE to", 1.0),
    "top_n": ("top_n", 1, 50, True, "Opportunities shown", 1.0),
    "max_per_ticker": ("max_per_ticker", 1, 10, True, "Most per ticker", 1.0),
    "max_per_sector": ("max_per_sector", 1, 50, True, "Most per sector", 1.0),
    "max_trade_pct": ("max_trade_pct", 0.01, 100, False, "Maximum per trade", 1.0),
    "max_total_pct": ("max_total_pct", 0.01, 100, False, "Maximum total", 1.0),
    "max_ticker_pct": ("max_ticker_pct", 0.01, 100, False, "Maximum per ticker", 1.0),
    "max_sector_pct": ("max_sector_pct", 0.01, 100, False, "Maximum per sector", 1.0),
    "dte_min": ("dte_min", 1, 365, True, "DTE from", 1.0),
    "dte_max": ("dte_max", 1, 365, True, "DTE to", 1.0),
    "min_price": ("min_price", 0, 100000, False, "Lowest stock price", 1.0),
    "max_price": ("max_price", 1, 100000, False, "Highest stock price", 1.0),
    "min_cap_b": ("min_market_cap_b", 0, 10000, False, "Minimum market cap", 1.0),
    "min_vol_m": ("min_avg_volume_m", 0, 10000, False, "Minimum average volume", 1.0),
    "max_stocks": ("max_stocks", 1, 500, True, "Stocks to scan", 1.0),
    "max_expirations": ("max_expirations", 1, 8, True, "Expirations per ticker", 1.0),
}


# ---------------------------------------------------------------- stage 2: stock screen


@dataclass(frozen=True)
class Underlying:
    """What the batched quote call says about one ticker. Any field may be
    ``None`` when Yahoo didn't carry it."""

    ticker: str
    is_etf: bool
    name: str | None = None
    sector: str | None = None
    price: float | None = None
    market_cap_b: float | None = None
    avg_volume_m: float | None = None
    earnings_date: date | None = None
    eps_ttm: float | None = None
    excluded_kind: str | None = None  # leveraged, inverse, closed-end or mutual fund
    sources: tuple[str, ...] = ()


def screen_underlying(u: Underlying, settings: ScannerSettings, today: date) -> str | None:
    """The reason this ticker can't be traded under the hard rules, or ``None``.
    Profitability and volatility only affect ranking, later. ETFs have no
    market cap, earnings or price ceiling, so those rules don't apply to them."""
    if u.excluded_kind:
        return f"Excluded fund type: {u.excluded_kind}"
    if u.price is None:
        return "No current price"
    if u.price < settings.min_price:
        return f"Price under ${settings.min_price:g}"
    if u.avg_volume_m is None:
        return "No average volume"
    if u.avg_volume_m < settings.min_avg_volume_m:
        return f"Average volume under {settings.min_avg_volume_m:g}M shares"
    if u.is_etf:
        return None
    if u.price > settings.max_price:
        return f"Price over ${settings.max_price:g}"
    if u.market_cap_b is None:
        return "No market cap"
    if u.market_cap_b < settings.min_market_cap_b:
        return f"Market cap under ${settings.min_market_cap_b:g}B"
    if settings.earnings_policy == EARNINGS_EXCLUDE and u.earnings_date is not None:
        if today <= u.earnings_date <= today + timedelta(days=settings.dte_min):
            return "Earnings before the earliest expiration"
    return None


def prioritize_stocks(passed: Sequence[Underlying], settings: ScannerSettings, today: date) -> list[Underlying]:
    """The stocks worth downloading option chains for: the most actively traded
    by dollars, since a thin stock's options rarely fill well, with stocks
    reporting earnings soon moved behind the rest. At most ``max_stocks``."""

    def key(u: Underlying) -> tuple:
        reports_soon = u.earnings_date is not None and today <= u.earnings_date <= today + timedelta(days=settings.dte_pref_max)
        return (reports_soon, -((u.price or 0) * (u.avg_volume_m or 0)), u.ticker)

    return sorted((u for u in passed if not u.is_etf), key=key)[: settings.max_stocks]


def pick_expirations(listed: Sequence[date], today: date, settings: ScannerSettings) -> list[date]:
    """Listed dates inside the DTE window: preferred-window dates first, then
    nearest the preferred middle, at most ``max_expirations``, in date order."""
    middle = (settings.dte_pref_min + settings.dte_pref_max) / 2

    def key(day: date) -> tuple:
        dte = (day - today).days
        return (not settings.dte_pref_min <= dte <= settings.dte_pref_max, abs(dte - middle), day)

    inside = [d for d in listed if settings.dte_min <= (d - today).days <= settings.dte_max]
    return sorted(sorted(inside, key=key)[: settings.max_expirations])


# ---------------------------------------------------------------- history and market


@dataclass(frozen=True)
class Zone:
    low: float
    high: float
    score: float
    touches: int


@dataclass(frozen=True)
class Technicals:
    ma20: float | None = None
    ma50: float | None = None
    ma200: float | None = None
    realized_vol_pct: float | None = None
    support: tuple[Zone, ...] = ()
    resistance: tuple[Zone, ...] = ()
    chart: tuple[tuple[str, float], ...] = ()
    closes_by_day: Mapping[date, float] = field(default_factory=dict)
    limitation: str | None = None


def analyze_technicals(ticker: str, bars: Sequence[PricePoint]) -> Technicals:
    """Averages and price zones from real history. Too little history leaves
    fields empty with the reason; no level is ever made up."""
    if not bars:
        return Technicals(limitation="No daily price history could be loaded.")
    closes = [b.close for b in bars]

    def zones(side: str) -> tuple[Zone, ...]:
        found = analyze(bars, side)
        return tuple(
            Zone(z.low, z.high, z.score, z.touches) for z in found.zones if z.score >= MEANINGFUL_ZONE_SCORE
        )

    limitation = None if len(closes) >= 200 else f"Only {len(closes)} sessions of history, so some averages are missing."
    return Technicals(
        ma20=simple_moving_average(closes, 20),
        ma50=simple_moving_average(closes, 50),
        ma200=simple_moving_average(closes, 200),
        realized_vol_pct=market_context(ticker, closes).realized_vol_pct,
        support=zones(SUPPORT),
        resistance=zones(RESISTANCE),
        chart=tuple((b.day.isoformat(), b.close) for b in bars[-CHART_SESSIONS:]),
        closes_by_day={b.day: b.close for b in bars[-(CORRELATION_SESSIONS + 1):]},
        limitation=limitation,
    )


@dataclass(frozen=True)
class MarketConditions:
    spy_up: bool | None = None
    qqq_up: bool | None = None
    vix: float | None = None
    vix_rising: bool | None = None
    sector_up: Mapping[str, bool] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        def trend(up: bool | None) -> str:
            return "Unknown" if up is None else "Above its 50-day average" if up else "Below its 50-day average"

        vix_text = "Unavailable"
        if self.vix is not None:
            direction = "" if self.vix_rising is None else ", rising" if self.vix_rising else ", falling"
            vix_text = f"{self.vix:.1f}{direction}"
        return {
            "spy": trend(self.spy_up),
            "qqq": trend(self.qqq_up),
            "vix": vix_text,
            "sectors_up": sorted(s for s, up in self.sector_up.items() if up),
            "sectors_down": sorted(s for s, up in self.sector_up.items() if not up),
            "iv_rank_note": "IV rank and IV percentile are unavailable: Yahoo keeps no implied volatility history.",
        }


def above_fifty_day(closes: Sequence[float]) -> bool | None:
    average = simple_moving_average(closes, 50)
    return None if average is None else closes[-1] >= average


def market_conditions(
    spy_closes: Sequence[float],
    qqq_closes: Sequence[float],
    vix_closes: Sequence[float],
    sector_closes: Mapping[str, Sequence[float]],
) -> MarketConditions:
    sector_up = {s: up for s, closes in sector_closes.items() if (up := above_fifty_day(closes)) is not None}
    vix = vix_closes[-1] if vix_closes else None
    rising = vix_closes[-1] > vix_closes[-1 - VIX_DIRECTION_SESSIONS] if len(vix_closes) > VIX_DIRECTION_SESSIONS else None
    return MarketConditions(above_fifty_day(spy_closes), above_fifty_day(qqq_closes), vix, rising, sector_up)


# ---------------------------------------------------------------- stage 3: build spreads


def quote_status(timing: QuoteTiming | None, now: datetime, *, realtime: bool = False) -> tuple[str, float | None]:
    """``(status, age in minutes)``. Age runs from the quote's own time; a
    quote downloaded a while ago is CACHED even if it was current then."""
    if timing is None or timing.as_of is None:
        return QUOTE_UNAVAILABLE, None
    age = max((now - timing.as_of).total_seconds() / 60, 0.0)
    if age > QUOTE_STALE_MINUTES:
        return QUOTE_STALE, age
    if timing.fetched_at is not None and (now - timing.fetched_at).total_seconds() / 60 > QUOTE_CACHED_MINUTES:
        return QUOTE_CACHED, age
    return (QUOTE_LIVE if realtime else QUOTE_DELAYED), age


def quote_label(status: str, age: float | None) -> str:
    if status == QUOTE_LIVE:
        return "Live quote"
    if status == QUOTE_DELAYED:
        return f"Delayed quote, {age:.0f} min old (Yahoo's free feed)"
    if status == QUOTE_CACHED:
        return f"Cached quote from this scan, {age:.0f} min old. Refresh before relying on it."
    if status == QUOTE_STALE:
        return f"Old quote, {_age_text(age)} old, likely the last session's prices. Recheck before trading."
    return "Quote time unavailable"


def iv_problem(short: LegQuote, *, spot: float, right: str, dte: int, risk_free_rate: float) -> str | None:
    """Why the short leg's quoted implied volatility can't support a
    probability, or ``None``. Yahoo's IV can come from a different moment than
    its bid and ask, most of all outside market hours."""
    if short.iv is None:
        return "No implied volatility on the short leg."
    low, high = IV_PLAUSIBLE_RANGE
    if not low <= short.iv <= high:
        return f"The short leg's implied volatility of {short.iv * 100:.1f}% isn't believable."
    mid = bid_ask_mid(short.bid, short.ask)
    model = black_scholes_price(
        spot=spot, strike=short.strike, days_to_expiry=dte, implied_volatility=short.iv, right=right, risk_free_rate=risk_free_rate
    )
    if mid is None or model is None:
        return "The short leg has no usable price to check its implied volatility against."
    if abs(model - mid) > max(mid * IV_REPRICE_TOLERANCE, IV_REPRICE_TOLERANCE_USD):
        return (
            f"The short leg's implied volatility prices it at {_usd(model)} but it is quoted around {_usd(mid)}, "
            "so the volatility and the quote don't match."
        )
    return None


@dataclass(frozen=True, slots=True)
class RawSpread:
    """A spread with a usable market. Hard filters and scores are applied when
    it is viewed, so a filter change never needs another download."""

    ticker: str
    strategy: str
    expiry: date
    dte: int
    spot: float
    timing: QuoteTiming
    short: LegQuote
    long: LegQuote
    credit: float
    credit_mid: float
    short_delta: float
    pop: float | None
    pop_problem: str | None
    fee_per_contract: float

    @property
    def id(self) -> str:
        return f"{self.ticker}|{self.expiry.isoformat()}|{self.strategy}|{_strike(self.short.strike)}|{_strike(self.long.strike)}"

    @property
    def width(self) -> float:
        return spread_width(self.short.strike, self.long.strike)

    @property
    def max_profit(self) -> float:
        return max_profit_per_contract(self.credit)

    @property
    def max_loss(self) -> float:
        return max_loss_per_contract(self.width, self.credit)

    @property
    def breakeven(self) -> float:
        return breakeven(self.strategy, self.short.strike, self.credit)

    @property
    def return_on_risk_pct(self) -> float:
        return self.credit / (self.width - self.credit) * 100

    @property
    def annualized_ror_pct(self) -> float:
        return self.return_on_risk_pct * 365 / self.dte

    @property
    def distance_pct(self) -> float:
        """How far the price must move to reach the short strike, % of price."""
        return abs(self.spot - self.short.strike) / self.spot * 100

    @property
    def expected_value(self) -> float | None:
        if self.pop is None:
            return None
        fees = FEE_LEGS_ROUND_TRIP * self.fee_per_contract
        return self.pop * self.max_profit - (1 - self.pop) * self.max_loss - fees

    @property
    def round_trip_fees(self) -> float:
        return FEE_LEGS_ROUND_TRIP * self.fee_per_contract

    @property
    def risk_per_contract(self) -> float:
        return self.max_loss + 2 * self.fee_per_contract

    @property
    def leg_spread_cost(self) -> float:
        """Credit lost by trading at the bid and ask instead of the midpoints, x 2."""
        return (self.short.ask - self.short.bid) + (self.long.ask - self.long.bid)


def practical_widths(spot: float) -> tuple[float, ...]:
    low, high = WIDTH_SHARE_OF_PRICE
    return tuple(w for w in CANDIDATE_WIDTHS if spot * low <= w <= spot * high)


def touch_probability(raw: RawSpread, risk_free_rate: float) -> float | None:
    if raw.pop is None:
        return None
    above = probability_finish_above(
        spot=raw.spot, level=raw.short.strike, days_to_expiry=raw.dte, implied_volatility=raw.short.iv, risk_free_rate=risk_free_rate
    )
    if above is None:
        return None
    past_short = 1 - above if raw.strategy == BULL_PUT else above
    return min(2 * past_short, 1.0)


def construct_spreads(
    chain: ChainQuotes, *, today: date, risk_free_rate: float, fee_per_contract: float
) -> tuple[list[RawSpread], Counter]:
    """Every spread on this chain with an out-of-the-money short leg, across
    the practical widths, that has a real market. Returns the spreads and a
    count of what was thrown out for bad quotes. Last-trade prices are never
    used: a leg without a two-sided quote is simply out."""
    spreads: list[RawSpread] = []
    rejects: Counter = Counter()
    dte = (chain.expiry - today).days
    if chain.spot is None or chain.spot <= 0 or dte <= 0:
        rejects["No underlying price on the chain"] += 1
        return spreads, rejects
    delta_low, delta_high = CONSTRUCTION_DELTA_RANGE
    widths = practical_widths(chain.spot)
    for strategy in (BULL_PUT, BEAR_CALL):
        right = STRATEGY_RIGHT[strategy]
        by_strike = {round(q.strike, 2): q for q in chain.side(right)}
        for short in chain.side(right):
            out_of_the_money = short.strike < chain.spot if strategy == BULL_PUT else short.strike > chain.spot
            if not out_of_the_money:
                continue
            delta = short_leg_delta(spot=chain.spot, short=short, right=right, days=dte, risk_free_rate=risk_free_rate)
            if delta is None or not delta_low <= delta <= delta_high:
                continue
            if short.bid is None or short.bid <= 0:
                rejects["Short leg has no bid"] += 1
                continue
            if bid_ask_mid(short.bid, short.ask) is None:
                rejects["Short leg quote is missing or crossed"] += 1
                continue
            problem = iv_problem(short, spot=chain.spot, right=right, dte=dte, risk_free_rate=risk_free_rate)
            for width in widths:
                long_strike = short.strike - width if strategy == BULL_PUT else short.strike + width
                long = by_strike.get(round(long_strike, 2))
                if long is None:
                    continue
                if bid_ask_mid(long.bid, long.ask) is None:
                    rejects["Long leg quote is missing or crossed"] += 1
                    continue
                credit = short.bid - long.ask
                if credit <= 0:
                    rejects["No credit after bid and ask"] += 1
                    continue
                if credit >= width:
                    rejects["Credit at or above the width: bad quotes"] += 1
                    continue
                pop = None
                if problem is None:
                    pop = probability_of_profit(
                        strategy=strategy,
                        spot=chain.spot,
                        breakeven_price=breakeven(strategy, short.strike, credit),
                        days=dte,
                        iv=short.iv,
                        risk_free_rate=risk_free_rate,
                    )
                spreads.append(
                    RawSpread(
                        chain.ticker, strategy, chain.expiry, dte, chain.spot, chain.timing, short, long, credit,
                        mid_credit(short, long), delta, pop, problem, fee_per_contract,
                    )
                )
    return spreads, rejects


# ---------------------------------------------------------------- portfolio


@dataclass(frozen=True)
class Exposure:
    """Open credit spreads, measured by max loss: the money at risk if each
    goes fully wrong, and the same figure that fills the risk limits."""

    account_value: float | None
    account_source: str
    total: float
    by_ticker: Mapping[str, float]
    by_sector: Mapping[str, float]
    tickers_closes: Mapping[str, Mapping[date, float]]
    # Filled on first use: one ticker can have thousands of candidate spreads.
    _closest: dict[str, tuple[str, float] | None] = field(default_factory=dict)

    def limit(self, pct: float) -> float | None:
        return None if not self.account_value else self.account_value * pct / 100

    def closest_held(self, ticker: str, technicals: "Technicals | None") -> tuple[str, float] | None:
        """The open-position ticker this one moves most like, with the
        correlation, or ``None`` if there is nothing to compare."""
        if ticker not in self._closest:
            found: list[tuple[float, str]] = []
            if technicals is not None and technicals.closes_by_day:
                for other, closes in self.tickers_closes.items():
                    if other != ticker and self.by_ticker.get(other, 0.0) > 0:
                        correlation = return_correlation(technicals.closes_by_day, closes)
                        if correlation is not None:
                            found.append((correlation, other))
            best = max(found, default=None)
            self._closest[ticker] = (best[1], best[0]) if best else None
        return self._closest[ticker]

    def payload(self, settings: ScannerSettings) -> dict[str, Any]:
        def row(name: str, used: float, pct: float) -> dict[str, Any]:
            limit = self.limit(pct)
            return {"name": name, "used": round(used, 2), "limit": _round(limit), "over": limit is not None and used > limit}

        return {
            "account_value": self.account_value,
            "account_source": self.account_source,
            "max_potential_loss": round(self.total, 2),
            "total": row("All open spreads", self.total, settings.max_total_pct),
            "tickers": [row(t, v, settings.max_ticker_pct) for t, v in sorted(self.by_ticker.items(), key=lambda kv: -kv[1])],
            "sectors": [row(s, v, settings.max_sector_pct) for s, v in sorted(self.by_sector.items(), key=lambda kv: -kv[1])],
        }


def build_exposure(
    positions: Iterable[SpreadPosition],
    *,
    sector_of: Mapping[str, str | None],
    account_value: float | None,
    account_source: str,
    closes_by_ticker: Mapping[str, Mapping[date, float]],
) -> Exposure:
    by_ticker: dict[str, float] = {}
    by_sector: dict[str, float] = {}
    total = 0.0
    for position in positions:
        if position.is_closed or position.contracts_open <= 1e-9:
            continue
        risk = max_loss_per_contract(position.width, position.credit) * position.contracts_open
        total += risk
        by_ticker[position.ticker] = by_ticker.get(position.ticker, 0.0) + risk
        sector = sector_of.get(position.ticker)
        if sector:
            by_sector[sector] = by_sector.get(sector, 0.0) + risk
    return Exposure(account_value, account_source, total, by_ticker, by_sector, closes_by_ticker)


def return_correlation(a: Mapping[date, float], b: Mapping[date, float]) -> float | None:
    """Correlation of daily returns over the sessions both have, or ``None``
    with fewer than 20 shared sessions or a series that never moves."""
    days = sorted(set(a) & set(b))
    if len(days) < 21:
        return None
    first = [a[days[i]] / a[days[i - 1]] - 1 for i in range(1, len(days))]
    second = [b[days[i]] / b[days[i - 1]] - 1 for i in range(1, len(days))]
    try:
        return statistics.correlation(first, second)
    except statistics.StatisticsError:
        return None


# ---------------------------------------------------------------- stage 4: scoring


def logistic(value: float, midpoint: float, spread: float) -> float:
    return 1.0 / (1.0 + math.exp(-(value - midpoint) / spread))


def liquidity_score(raw: RawSpread) -> tuple[float, list[str]]:
    """0-100. Reported separately because it is both a category and a filter."""
    cost_share = raw.leg_spread_cost / raw.credit_mid if raw.credit_mid > 0 else math.inf
    cost_credit = logistic(-cost_share, -0.5, 0.15) if math.isfinite(cost_share) else 0.0
    interests = [leg.open_interest for leg in (raw.short, raw.long)]
    volumes = [leg.volume for leg in (raw.short, raw.long)]
    interest_credit = 0.0 if None in interests else min(math.log10(min(interests) + 1) / 3, 1.0)
    volume_credit = 0.0 if None in volumes else min(math.log10(min(volumes) + 1) / 2.5, 1.0)
    notes = [f"Crossing both bid/ask spreads costs {_usd(raw.leg_spread_cost)}, {cost_share * 100:.0f}% of the midpoint credit."]
    notes.append(
        "Open interest not reported on a leg." if None in interests
        else f"Open interest: short {interests[0]:,}, long {interests[1]:,}."
    )
    notes.append(
        "Today's volume not reported on a leg." if None in volumes
        else f"Today's volume: short {volumes[0]:,}, long {volumes[1]:,}."
    )
    return 100 * (0.5 * cost_credit + 0.35 * interest_credit + 0.15 * volume_credit), notes


def zone_text(zone: Zone) -> str:
    """A price zone in words; a level touched once is a single price."""
    return _usd(zone.low) if abs(zone.high - zone.low) < 0.005 else f"{_usd(zone.low)}-{_usd(zone.high)}"


def _relevant_zone(zones: Sequence[Zone], spot: float, strategy: str) -> Zone | None:
    """The nearest real level on the side that protects this spread."""
    if strategy == BULL_PUT:
        below = [z for z in zones if z.low < spot]
        return max(below, key=lambda z: z.high) if below else None
    above = [z for z in zones if z.high > spot]
    return min(above, key=lambda z: z.low) if above else None


def technical_read(raw: RawSpread, tech: Technicals | None) -> tuple[float, str, Zone | None]:
    """``(0-1 credit, one plain sentence, the zone used)``."""
    label = STRATEGY_LABELS[raw.strategy]
    direction = "below" if raw.strategy == BULL_PUT else "above"
    lead = f"{label} spread: short strike is {raw.distance_pct:.1f}% {direction} the price"
    if tech is None or not tech.chart:
        return 0.0, f"{lead}. {(tech.limitation if tech else None) or 'No price history, so no technical read.'}", None

    level_word = "support" if raw.strategy == BULL_PUT else "resistance"
    zones = tech.support if raw.strategy == BULL_PUT else tech.resistance
    zone = _relevant_zone(zones, raw.spot, raw.strategy)
    strike = raw.short.strike
    if zone is None:
        level_credit, level_text = 0.0, f"no clear {level_word} level found"
    elif zone.low <= strike <= zone.high:
        level_credit, level_text = 0.5, f"inside the {level_word} zone at {zone_text(zone)}"
    elif (raw.strategy == BULL_PUT and strike < zone.low) or (raw.strategy == BEAR_CALL and strike > zone.high):
        level_credit, level_text = 1.0, f"{direction} {level_word} at {zone_text(zone)}"
    else:
        between = "above" if raw.strategy == BULL_PUT else "below"
        level_credit, level_text = 0.2, f"{between} {level_word} at {zone_text(zone)}, so that level doesn't protect it"

    supportive = 0.0
    words = []
    for name, average, weight in (("20-day", tech.ma20, 0.2), ("50-day", tech.ma50, 0.4), ("200-day", tech.ma200, 0.4)):
        if average is None:
            continue
        above = raw.spot >= average
        if above == (raw.strategy == BULL_PUT):
            supportive += weight
        words.append(f"{'above' if above else 'below'} its {name}")
    credit = 0.6 * level_credit + 0.4 * supportive
    averages = f"Price is {', '.join(words)} average." if words else "Not enough history for moving averages."
    verdict = "Technical setup is favorable." if credit >= 0.65 else "Technical setup is mixed." if credit >= 0.35 else "Technical setup is weak."
    return credit, f"{lead} and {level_text}. {averages} {verdict}", zone


def volatility_read(raw: RawSpread, tech: Technicals | None, earnings_in_window: bool) -> tuple[float, str]:
    iv = raw.short.iv
    realized = tech.realized_vol_pct if tech else None
    if iv is None or raw.pop is None:
        return 0.0, "No trustworthy implied volatility."
    if not realized:
        return 0.0, f"Implied volatility {iv * 100:.0f}%; no realized volatility to compare it with."
    ratio = iv * 100 / realized
    if ratio < 1.1:
        credit = max(0.0, (ratio - 0.7) / 0.4)
    elif ratio <= 1.6:
        credit = 1.0
    else:
        credit = max(0.0, 1.0 - (ratio - 1.6) / 1.0)
    text = f"Implied volatility {iv * 100:.0f}% is {ratio:.2f}x the 30-day realized {realized:.0f}%."
    if earnings_in_window:
        credit *= 0.5
        text += " Earnings inside the trade likely inflate it, so the extra premium is pay for a known risk."
    elif iv >= IV_VERY_HIGH:
        credit *= 0.5
        text += " Volatility this high usually means real risk in the stock, not free premium."
    return credit, text


def market_read(raw: RawSpread, market: MarketConditions, sector: str | None) -> tuple[float, str]:
    wants_up = raw.strategy == BULL_PUT
    credit = 0.0
    words = []
    for name, up in (("SPY", market.spy_up), ("QQQ", market.qqq_up)):
        if up is None:
            words.append(f"{name} trend unknown")
            continue
        credit += 0.25 if up == wants_up else 0.0
        words.append(f"{name} {'up' if up else 'down'}")
    if market.vix is None:
        words.append("VIX unavailable")
    else:
        rising = bool(market.vix_rising)
        if wants_up:
            credit += 0.25 if market.vix < VIX_CALM and not rising else 0.125 if market.vix < VIX_STRESSED else 0.0
        else:
            credit += 0.25 if market.vix >= VIX_CALM or rising else 0.125
        words.append(f"VIX {market.vix:.1f}{', rising' if rising else ''}")
    sector_up = market.sector_up.get(sector) if sector else None
    if sector_up is None:
        words.append("sector trend unknown")
    else:
        credit += 0.25 if sector_up == wants_up else 0.0
        words.append(f"{sector} {'up' if sector_up else 'down'}")
    return credit, ", ".join(words) + "."


@dataclass(frozen=True)
class ComponentScore:
    key: str
    label: str
    weight: float
    points: float
    detail: str


@dataclass(frozen=True)
class Penalty:
    label: str
    points: float
    detail: str


@dataclass(frozen=True)
class ScoredSpread:
    raw: RawSpread
    underlying: Underlying
    score: float
    rating: str
    rating_cap_reasons: tuple[str, ...]
    components: tuple[ComponentScore, ...]
    penalties: tuple[Penalty, ...]
    liquidity: float
    liquidity_notes: tuple[str, ...]
    technical_text: str
    zone: Zone | None
    earnings_in_window: bool
    warnings: tuple[str, ...]
    quote: tuple[str, float | None]


def earnings_inside(u: Underlying, raw: RawSpread, today: date) -> bool:
    return u.earnings_date is not None and today <= u.earnings_date <= raw.expiry


def rate(score: float, min_score: float, caps: Sequence[str]) -> str:
    """The rating the score earns, then any cap. ``caps`` holds ratings the
    trade may not rise above."""
    if score < min_score:
        return REJECTED
    earned = WATCH
    for rating, floor in RATING_MIN_SCORE:
        if score >= floor:
            earned = rating
            break
    limit = min((RATING_ORDER.index(c) for c in caps), default=len(RATING_ORDER) - 1)
    return RATING_ORDER[min(RATING_ORDER.index(earned), limit)]


def portfolio_adjustments(
    raw: RawSpread, u: Underlying, exposure: Exposure, settings: ScannerSettings, technicals: Technicals | None
) -> tuple[list[Penalty], list[str]]:
    """Open positions lower the score and warn; they never exclude a trade.
    Every risk check uses one contract's max loss on top of what's already open."""
    penalties: list[Penalty] = []
    warnings: list[str] = []
    risk = raw.max_loss
    held = exposure.by_ticker.get(raw.ticker, 0.0)
    if held > 0:
        penalties.append(Penalty("Already holding this ticker", PENALTY_SAME_TICKER, f"{_usd(held)} of max loss already open in {raw.ticker}."))
        warnings.append(f"You already have {_usd(held)} of max loss open in {raw.ticker}.")

    def over(name: str, used: float, pct: float) -> None:
        limit = exposure.limit(pct)
        if limit is not None and used + risk > limit:
            penalties.append(Penalty(f"Over the {name} limit", PENALTY_OVER_LIMIT, f"{_usd(used + risk)} with this trade; limit {_usd(limit)} ({pct:g}% of account)."))
            warnings.append(f"One contract takes {name} risk to {_usd(used + risk)}, over your {_usd(limit)} limit.")

    trade_limit = exposure.limit(settings.max_trade_pct)
    if trade_limit is not None and risk > trade_limit:
        penalties.append(Penalty("Over the per-trade limit", PENALTY_OVER_LIMIT, f"One contract risks {_usd(risk)}; limit {_usd(trade_limit)} ({settings.max_trade_pct:g}% of account)."))
        warnings.append(f"One contract risks {_usd(risk)}, over your {_usd(trade_limit)} per-trade limit.")
    over("ticker", held, settings.max_ticker_pct)
    if u.sector:
        over(u.sector, exposure.by_sector.get(u.sector, 0.0), settings.max_sector_pct)
    over("total", exposure.total, settings.max_total_pct)

    closest = exposure.closest_held(raw.ticker, technicals)
    if closest is not None and closest[1] >= HIGH_CORRELATION:
        ticker, correlation = closest
        penalties.append(Penalty("Moves with an open position", PENALTY_CORRELATED, f"Daily returns have a {correlation:.2f} correlation with {ticker}."))
        warnings.append(f"{raw.ticker} has moved closely with {ticker}, which you already hold (correlation {correlation:.2f}).")
    return penalties, warnings


def score_spread(
    raw: RawSpread,
    u: Underlying,
    *,
    technicals: Technicals | None,
    market: MarketConditions,
    exposure: Exposure,
    settings: ScannerSettings,
    today: date,
    now: datetime,
    realtime: bool = False,
) -> ScoredSpread:
    earnings_in_window = earnings_inside(u, raw, today)
    liquidity, liquidity_notes = liquidity_score(raw)
    technical_credit, technical_text, zone = technical_read(raw, technicals)
    volatility_credit, volatility_text = volatility_read(raw, technicals, earnings_in_window)
    market_credit, market_text = market_read(raw, market, u.sector)
    if raw.pop is None:
        pop_credit, pop_text = 0.0, raw.pop_problem or "No estimate available."
    else:
        pop_credit = logistic(raw.pop, 0.72, 0.05)
        pop_text = f"{raw.pop * 100:.0f}% model estimate of finishing past the {_usd(raw.breakeven)} breakeven."

    credits = {
        "pop": (pop_credit, pop_text),
        "ror": (logistic(raw.return_on_risk_pct, 18.0, 6.0), f"{raw.return_on_risk_pct:.1f}% return on the {_usd(raw.max_loss)} at risk."),
        "annualized": (logistic(raw.annualized_ror_pct, 170.0, 60.0), f"{raw.annualized_ror_pct:.0f}% annualized over {raw.dte} DTE."),
        "liquidity": (liquidity / 100, f"{liquidity:.0f} of 100. {' '.join(liquidity_notes)}"),
        "technical": (technical_credit, technical_text),
        "volatility": (volatility_credit, volatility_text),
        "market": (market_credit, market_text),
    }
    components = tuple(
        ComponentScore(c.key, c.label, c.weight, c.weight * credits[c.key][0], credits[c.key][1]) for c in SCORE_CATEGORIES
    )

    penalties: list[Penalty] = []
    warnings: list[str] = []
    if liquidity < POOR_LIQUIDITY_BELOW:
        points = (POOR_LIQUIDITY_BELOW - liquidity) / POOR_LIQUIDITY_BELOW * PENALTY_POOR_LIQUIDITY_MAX
        penalties.append(Penalty("Poor liquidity", points, f"Liquidity {liquidity:.0f} of 100: expect a worse fill than shown."))
    if earnings_in_window:
        penalties.append(Penalty("Earnings before expiration", PENALTY_EARNINGS_IN_WINDOW, f"Reports {_day(u.earnings_date)}, before {_day(raw.expiry)}."))
        warnings.append(f"Earnings on {_day(u.earnings_date)}, before this expiration: a gap can jump past both strikes.")
    elif not u.is_etf and u.earnings_date is None:
        penalties.append(Penalty("Earnings date unknown", PENALTY_EARNINGS_UNKNOWN, "No upcoming earnings date on file."))
        warnings.append("No earnings date on file, so one could fall before this expiration.")
    if not u.is_etf and u.eps_ttm is not None and u.eps_ttm <= 0:
        penalties.append(Penalty("Not profitable", PENALTY_UNPROFITABLE, f"Trailing 12-month earnings per share: {_usd(u.eps_ttm)}."))
    extra_penalties, extra_warnings = portfolio_adjustments(raw, u, exposure, settings, technicals)
    penalties += extra_penalties
    warnings += extra_warnings

    earned = sum(c.points for c in components)
    score = max(0.0, min(100.0, earned - sum(p.points for p in penalties)))

    status, age = quote_status(raw.timing, now, realtime=realtime)
    caps: list[str] = []
    cap_reasons: list[str] = []

    def cap(rating: str, reason: str) -> None:
        caps.append(rating)
        cap_reasons.append(f"Held at {rating}: {reason}")

    if status in (QUOTE_STALE, QUOTE_UNAVAILABLE):
        cap(WATCH, "the quotes aren't current.")
        warnings.append(quote_label(status, age))
    if earnings_in_window:
        cap(WATCH, "earnings fall inside the trade.")
    if raw.expected_value is None:
        cap(WATCH, "no reliable expected value.")
    else:
        if raw.expected_value / raw.max_loss * 100 < GOOD_CAP_MIN_EV_PCT_OF_RISK:
            cap(GOOD, f"expected value is {_usd(raw.expected_value)}, so the premium doesn't pay enough for the risk.")
    if raw.pop is not None and raw.pop < GOOD_CAP_MIN_POP:
        cap(GOOD, f"chance of profit is under {GOOD_CAP_MIN_POP * 100:.0f}%.")
    if raw.return_on_risk_pct < GOOD_CAP_MIN_ROR_PCT:
        cap(GOOD, f"return on risk is under {GOOD_CAP_MIN_ROR_PCT:g}%.")
    if liquidity < GOOD_CAP_MIN_LIQUIDITY:
        cap(GOOD, "liquidity is thin.")
    if raw.short.open_interest is None or raw.long.open_interest is None:
        warnings.append("Open interest isn't reported on a leg, so how easily it trades is uncertain.")
    rating = rate(score, settings.min_score, caps)
    return ScoredSpread(
        raw, u, score, rating, tuple(cap_reasons), components, tuple(penalties), liquidity, tuple(liquidity_notes),
        technical_text, zone, earnings_in_window, tuple(warnings), (status, age),
    )


def hard_filter_failure(raw: RawSpread, u: Underlying, settings: ScannerSettings, today: date) -> str | None:
    """The first hard rule this spread breaks, or ``None``. Checked before any
    scoring. Liquidity is a hard rule too (:func:`liquidity_score`)."""
    if settings.strategy != BOTH and raw.strategy != settings.strategy:
        return "Other strategy selected"
    if settings.asset_class == ETF_ONLY and not u.is_etf:
        return "Stock, and ETFs only is selected"
    if settings.asset_class == STOCKS_ONLY and u.is_etf:
        return "ETF, and stocks only is selected"
    if not settings.dte_min <= raw.dte <= settings.dte_max:
        return f"DTE outside {settings.dte_min}-{settings.dte_max}"
    if not settings.delta_min - 1e-9 <= raw.short_delta <= settings.delta_max + 1e-9:
        return f"Short delta outside {settings.delta_min * 100:g}-{settings.delta_max * 100:g}"
    if raw.width > settings.max_width + 1e-9:
        return f"Width over ${settings.max_width:g}"
    if raw.credit + 1e-9 < settings.min_credit:
        return f"Credit under ${settings.min_credit:.2f}"
    if raw.pop is None:
        return "No reliable chance-of-profit estimate"
    if raw.pop * 100 + 1e-9 < settings.min_pop_pct:
        return f"Chance of profit under {settings.min_pop_pct:g}%"
    if raw.return_on_risk_pct + 1e-9 < settings.min_ror_pct:
        return f"Return on risk under {settings.min_ror_pct:g}%"
    if raw.annualized_ror_pct + 1e-9 < settings.min_annualized_ror_pct:
        return f"Annualized return under {settings.min_annualized_ror_pct:g}%"
    for leg in (raw.short, raw.long):
        if leg.open_interest is None or leg.open_interest < settings.min_open_interest:
            return f"Open interest under {settings.min_open_interest} on a leg"
    if settings.earnings_policy == EARNINGS_EXCLUDE and earnings_inside(u, raw, today):
        return "Earnings before expiration"
    return None


@dataclass(frozen=True)
class RankedView:
    top: tuple[ScoredSpread, ...]
    also_considered: tuple[tuple[ScoredSpread, str], ...]
    reject_counts: tuple[tuple[str, int], ...]
    rejected_examples: tuple[tuple[RawSpread, str], ...]
    evaluated: int
    qualified: int


def _ranking_key(s: ScoredSpread) -> tuple:
    """Highest score first; ties go to expected value, return on risk, then
    the ticker and id so the order never depends on input order."""
    return (-s.score, -(s.raw.expected_value if s.raw.expected_value is not None else -math.inf), -s.raw.return_on_risk_pct, s.raw.ticker, s.raw.id)


def _rating_rank(s: ScoredSpread) -> int:
    return RATING_ORDER.index(s.rating)


def pick_width(group: Sequence[ScoredSpread]) -> ScoredSpread:
    """The protective leg for one short strike: the best score, or within
    :data:`WIDTH_SCORE_TIE` of it, whichever risks the least."""
    best = min(group, key=_ranking_key)
    close = [s for s in group if best.score - s.score <= WIDTH_SCORE_TIE and s.rating == best.rating]
    return min(close, key=lambda s: (s.raw.max_loss, _ranking_key(s)))


def rank_opportunities(
    spreads: Iterable[RawSpread],
    *,
    underlyings: Mapping[str, Underlying],
    technicals: Mapping[str, Technicals],
    market: MarketConditions,
    exposure: Exposure,
    settings: ScannerSettings,
    today: date,
    now: datetime,
    realtime: bool = False,
) -> RankedView:
    """Filter, score, keep the best protective leg for each short strike, then
    fill the list one by one while limiting how many share a ticker or sector,
    so several look-alike trades can't crowd it. A better rating always outranks
    a higher score: a capped trade never sits above an uncapped one."""
    counts: Counter = Counter()
    examples: dict[tuple[str, str], tuple[RawSpread, str]] = {}
    by_short: dict[tuple, list[ScoredSpread]] = {}
    evaluated = 0

    def reject(raw: RawSpread, reason: str) -> None:
        counts[reason] += 1
        examples.setdefault((raw.ticker, raw.strategy), (raw, reason))

    for raw in spreads:
        evaluated += 1
        u = underlyings.get(raw.ticker)
        if u is None:
            continue
        failure = hard_filter_failure(raw, u, settings, today)
        if failure:
            reject(raw, failure)
            continue
        liquidity, _ = liquidity_score(raw)
        if liquidity + 1e-9 < settings.min_liquidity:
            reject(raw, f"Liquidity under {settings.min_liquidity:g}")
            continue
        scored = score_spread(
            raw, u, technicals=technicals.get(raw.ticker), market=market, exposure=exposure,
            settings=settings, today=today, now=now, realtime=realtime,
        )
        if scored.rating == REJECTED:
            reject(raw, f"Score under {settings.min_score:g}")
            continue
        by_short.setdefault((raw.ticker, raw.expiry, raw.strategy, raw.short.strike), []).append(scored)

    best_per_short = [pick_width(group) for group in by_short.values()]
    ranked = sorted(best_per_short, key=lambda s: (-_rating_rank(s), _ranking_key(s)))
    top: list[ScoredSpread] = []
    held_back: list[tuple[ScoredSpread, str]] = []
    per_ticker: Counter = Counter()
    per_sector: Counter = Counter()
    for scored in ranked:
        ticker, sector = scored.raw.ticker, scored.underlying.sector
        if len(top) >= settings.top_n:
            reason = f"Outside the top {settings.top_n}"
        elif per_ticker[ticker] >= settings.max_per_ticker:
            reason = f"{ticker} is already on the list"
        elif sector and per_sector[sector] >= settings.max_per_sector:
            reason = f"{sector} already has {settings.max_per_sector} on the list"
        else:
            top.append(scored)
            per_ticker[ticker] += 1
            if sector:
                per_sector[sector] += 1
            continue
        held_back.append((scored, reason))

    # One row per ticker and strategy: the other strikes of a listed trade add noise, not choice.
    seen: set[tuple[str, str]] = {(s.raw.ticker, s.raw.strategy) for s in top}
    also: list[tuple[ScoredSpread, str]] = []
    for scored, reason in held_back:
        key = (scored.raw.ticker, scored.raw.strategy)
        if key not in seen:
            seen.add(key)
            also.append((scored, reason))
    return RankedView(
        tuple(top),
        tuple(also[:ALSO_CONSIDERED_SHOWN]),
        tuple(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        tuple(list(examples.values())[:REJECTED_EXAMPLES_SHOWN]),
        evaluated,
        len(best_per_short),
    )


def apply_diversification_warnings(top: Sequence[ScoredSpread], settings: ScannerSettings) -> dict[str, list[str]]:
    """Warnings about the list as a whole, by spread id: bigger picture than
    any one trade, so they stay out of the score."""
    by_id: dict[str, list[str]] = {}
    sector_counts = Counter(s.underlying.sector for s in top if s.underlying.sector)
    for scored in top:
        sector = scored.underlying.sector
        if sector and sector_counts[sector] > 1:
            by_id.setdefault(scored.raw.id, []).append(
                f"{sector_counts[sector]} of the listed trades are in {sector}; taking them all concentrates your risk."
            )
    return by_id


# ---------------------------------------------------------------- explanations


def management_targets(raw: RawSpread, management: SpreadSettings) -> dict[str, Any]:
    """Exit plan from the same rules and functions the Open spreads table uses."""
    target = target_closing_debit(raw.credit, management.profit_target_pct)
    trigger = loss_trigger_debit(raw.credit, management.loss_multiple)
    return {
        "profit_target_pct": management.profit_target_pct,
        "loss_multiple": management.loss_multiple,
        "target_debit": round(target, 2),
        "target_profit": round((raw.credit - target) * OPTION_MULTIPLIER, 2),
        "loss_trigger": round(trigger, 2),
        "loss_at_trigger": round((trigger - raw.credit) * OPTION_MULTIPLIER, 2),
        "review_dte": management.time_exit_dte,
        "review_date": _iso(raw.expiry - timedelta(days=management.time_exit_dte)),
        "strike_warning": (
            f"Review sooner if {raw.ticker} {'falls toward' if raw.strategy == BULL_PUT else 'rises toward'} "
            f"the {_strike(raw.short.strike)} short strike, {raw.distance_pct:.1f}% away today."
        ),
    }


def qualification_reasons(s: ScoredSpread) -> list[str]:
    raw = s.raw
    reasons = []
    by_key = {c.key: c for c in s.components}
    for key, floor, text in (
        ("liquidity", 0.6, "Strong liquidity for a tighter fill."),
        ("ror", 0.5, f"Pays {raw.return_on_risk_pct:.1f}% on the money at risk."),
        ("pop", 0.5, f"About {raw.pop * 100:.0f}% estimated chance of profit." if raw.pop is not None else ""),
        ("annualized", 0.5, f"{raw.annualized_ror_pct:.0f}% annualized return."),
        ("technical", 0.65, "Short strike has technical backing."),
        ("volatility", 0.65, "Option premium is rich compared to how much the stock actually moves."),
        ("market", 0.6, "Market and sector trends suit this direction."),
    ):
        component = by_key[key]
        if text and component.points / component.weight >= floor:
            reasons.append(text)
    return reasons


def risk_reasons(s: ScoredSpread) -> list[str]:
    raw = s.raw
    side = "below" if raw.strategy == BULL_PUT else "above"
    risks = [f"Max loss {_usd(raw.max_loss)} per spread if {raw.ticker} closes {side} {_strike(raw.long.strike)} at expiration."]
    risks += list(s.warnings)
    risks += [p.detail for p in s.penalties if p.label in ("Poor liquidity", "Not profitable")]
    risks += list(s.rating_cap_reasons)
    return list(dict.fromkeys(risks))


def explanation(s: ScoredSpread) -> str:
    """One or two sentences a reader can act on, built only from this trade's numbers."""
    raw = s.raw
    label = STRATEGY_LABELS[raw.strategy].lower()
    strengths = []
    by_key = {c.key: c.points / c.weight for c in s.components}
    if by_key["liquidity"] >= 0.6:
        strengths.append("strong liquidity")
    if by_key["ror"] >= 0.6:
        strengths.append("attractive premium for the risk")
    if by_key["pop"] >= 0.6:
        strengths.append("a high estimated chance of profit")
    if s.zone is not None and by_key["technical"] >= 0.5:
        level = "support" if raw.strategy == BULL_PUT else "resistance"
        strengths.append(f"short strike backed by {level}")
    if by_key["volatility"] >= 0.65:
        strengths.append("rich option premium")

    weaknesses = []
    if s.earnings_in_window:
        weaknesses.append("earnings fall inside the trade, so event risk is high")
    if by_key["liquidity"] < 0.5:
        weaknesses.append("thin options may fill worse than shown")
    if by_key["ror"] < 0.35:
        weaknesses.append("the premium is small for the risk")
    if by_key["pop"] < 0.35:
        weaknesses.append("the chance of profit is modest")
    if by_key["technical"] < 0.35:
        weaknesses.append("little technical backing")
    if s.quote[0] in (QUOTE_STALE, QUOTE_UNAVAILABLE):
        weaknesses.append("quotes aren't current")

    opening = f"{s.rating} {label} spread"
    if strengths:
        first = f"{opening}: {', '.join(strengths)}."
    else:
        first = f"{opening}: nothing stands out, but it clears your rules."
    if not weaknesses:
        return first
    joined = ", ".join(weaknesses)
    return f"{first} Watch out: {joined}."


# ---------------------------------------------------------------- payloads


def _leg_payload(leg: LegQuote) -> dict[str, Any]:
    return {
        "strike": leg.strike,
        "bid": leg.bid,
        "ask": leg.ask,
        "mid": _round(bid_ask_mid(leg.bid, leg.ask), 3),
        "iv_pct": _round(leg.iv * 100 if leg.iv is not None else None, 1),
        "open_interest": leg.open_interest,
        "volume": leg.volume,
    }


def _zones_payload(zones: Sequence[Zone], spot: float, *, below: bool) -> list[dict[str, Any]]:
    relevant = [z for z in zones if (z.low < spot if below else z.high > spot)]
    relevant.sort(key=(lambda z: -z.high) if below else (lambda z: z.low))
    return [{"text": zone_text(z), "low": round(z.low, 2), "high": round(z.high, 2), "score": round(z.score), "touches": z.touches} for z in relevant[:3]]


def opportunity_summary(s: ScoredSpread, rank: int | None = None) -> dict[str, Any]:
    raw = s.raw
    return {
        "id": raw.id,
        "rank": rank,
        "ticker": raw.ticker,
        "name": s.underlying.name,
        "sector": s.underlying.sector,
        "is_etf": s.underlying.is_etf,
        "strategy": raw.strategy,
        "strategy_label": STRATEGY_LABELS[raw.strategy],
        "spot": raw.spot,
        "short_strike": raw.short.strike,
        "long_strike": raw.long.strike,
        "width": raw.width,
        "expiry": raw.expiry.isoformat(),
        "dte": raw.dte,
        "credit": round(raw.credit, 2),
        "credit_mid": round(raw.credit_mid, 3),
        "max_profit": round(raw.max_profit, 2),
        "max_loss": round(raw.max_loss, 2),
        "pop_pct": _round(raw.pop * 100 if raw.pop is not None else None, 1),
        "ror_pct": round(raw.return_on_risk_pct, 1),
        "annualized_ror_pct": round(raw.annualized_ror_pct, 0),
        "short_delta": round(raw.short_delta * 100, 1),
        "score": round(s.score, 1),
        "rating": s.rating,
        "explanation": explanation(s),
        "quote_status": s.quote[0],
        "quote_label": quote_label(*s.quote),
    }


def opportunity_payload(
    s: ScoredSpread,
    rank: int,
    *,
    technicals: Technicals | None,
    settings: ScannerSettings,
    management: SpreadSettings,
    account_value: float | None,
    risk_free_rate: float,
    list_warnings: Sequence[str] = (),
) -> dict[str, Any]:
    raw = s.raw
    u = s.underlying
    contracts = suggested_contracts(account_value, settings.max_trade_pct, raw.risk_per_contract)
    touch = touch_probability(raw, risk_free_rate)
    in_preferred = settings.dte_pref_min <= raw.dte <= settings.dte_pref_max
    tech = technicals
    earned = sum(c.points for c in s.components)
    return {
        **opportunity_summary(s, rank),
        "breakeven": round(raw.breakeven, 2),
        "distance_pct": round(raw.distance_pct, 1),
        "expected_value": _round(raw.expected_value),
        "expected_value_note": (
            raw.pop_problem or "Unavailable without a reliable chance of profit."
            if raw.expected_value is None
            else "Uses market-implied odds and counts every loss as max loss, so it leans pessimistic. It is not evidence of an edge."
        ),
        "round_trip_fees": round(raw.round_trip_fees, 2),
        "open_fees": round(2 * raw.fee_per_contract, 2),
        "pop_model_note": "Black-Scholes estimate of finishing past the breakeven, from the short leg's implied volatility. Not a forecast.",
        "pop_delta_pct": round((1 - raw.short_delta) * 100, 1),
        "pop_broker": None,
        "pop_problem": raw.pop_problem,
        "touch_pct": _round(touch * 100 if touch is not None else None, 0),
        "short_leg": _leg_payload(raw.short),
        "long_leg": _leg_payload(raw.long),
        "dte_in_preferred": in_preferred,
        "suggested_contracts": contracts,
        "technical_text": s.technical_text,
        "moving_averages": None if tech is None else {"ma20": _round(tech.ma20), "ma50": _round(tech.ma50), "ma200": _round(tech.ma200)},
        "support": [] if tech is None else _zones_payload(tech.support, raw.spot, below=True),
        "resistance": [] if tech is None else _zones_payload(tech.resistance, raw.spot, below=False),
        "chart": [] if tech is None else [{"day": d, "close": round(c, 2)} for d, c in tech.chart],
        "history_note": None if tech is None else tech.limitation,
        "iv_pct": _round(raw.short.iv * 100 if raw.short.iv is not None else None, 1),
        "realized_vol_pct": _round(tech.realized_vol_pct if tech else None, 1),
        "iv_rank_note": "IV rank unavailable: Yahoo keeps no implied volatility history.",
        "earnings_date": _iso(u.earnings_date),
        "earnings_in_window": s.earnings_in_window,
        "market_cap_b": u.market_cap_b,
        "avg_volume_m": u.avg_volume_m,
        "liquidity": round(s.liquidity),
        "liquidity_notes": list(s.liquidity_notes),
        "score_earned": round(earned, 1),
        "components": [
            {"key": c.key, "label": c.label, "weight": c.weight, "points": round(c.points, 1), "detail": c.detail, "how": _CATEGORY_BY_KEY[c.key].how}
            for c in s.components
        ],
        "penalties": [{"label": p.label, "points": round(p.points, 1), "detail": p.detail} for p in s.penalties],
        "reasons": qualification_reasons(s),
        "risks": list(dict.fromkeys(risk_reasons(s) + list(list_warnings))),
        "management": management_targets(raw, management),
        "quote_as_of": _iso_dt(raw.timing.as_of),
        "quote_fetched_at": _iso_dt(raw.timing.fetched_at),
    }
