"""Credit spreads: entry scanning, open-spread management and performance for
defined-risk bull put and bear call credit spreads. The one implementation
the Credit Spreads tab reads (``/api/spreads``, assembled by
``Dashboard.build_credit_spreads`` in ``wheel/api.py``); the frontend only
displays what this module returns.

Pure: no network, no clock. Callers pass today's date, quotes and positions.

Definitions (per share unless a name says otherwise; x100 per contract):

* Width = |short strike - long strike|.
* Executable credit to open = short bid - long ask (sell at the bid, buy at
  the ask). The bid/ask midpoint credit is shown too, always as an estimate.
* Max profit per contract = credit x 100. Max loss per contract =
  (width - credit) x 100. Both before fees, which are shown separately.
* Breakeven at expiry: bull put = short strike - credit; bear call = short
  strike + credit.
* Return on max risk = credit / (width - credit).
* Probability of profit = Black-Scholes risk-neutral chance the underlying
  finishes past breakeven, from the short leg's implied volatility. A model
  estimate, never presented as a forecast.
* Risk per contract for sizing = max loss + the fees to open both legs.
  Suggested contracts = floor(account value x max risk % / risk per contract).
* Closing cost now = short ask - long bid (buy the short back at the ask,
  sell the long at the bid): what closing would actually cost. Both the
  profit target and the loss threshold are tested against it.
* Target closing debit = original credit x (1 - profit target %).
  Loss trigger = original credit x loss multiple.
* Time exit date = expiration - time-exit DTE.

Original credit and every realized figure come from the broker ledger
(``wheel.engine.Spread`` pairs a short and long leg opened the same day on
the same ticker, right and expiry). Cash is the broker's Amount, already net
of fees; gross = net + fees, so fees are always reported separately and
never double-counted. An assignment row carries no cash (the share row
does): both legs assigned settle at width x 100 per contract against the
trader; one leg assigned alone leaves the result to the share trade, so
that spread is flagged and kept out of every P/L figure
(``SpreadPosition.result_settled``).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from wheel.engine import SHORT, OptionLeg, Spread
from wheel.optionsmath import CALL, PUT, bid_ask_mid, black_scholes_greeks, probability_finish_above
from wheel.parser import ASSIGNED, EXPIRED, OPTION_MULTIPLIER

# Bumped whenever /api/spreads gains or changes a field the page reads.
# spreads.js expects the same number, so a server still running older code
# says "restart" instead of showing blanks. Must match SPREADS_API_VERSION there.
API_VERSION = 4

BULL_PUT = "BULL_PUT"
BEAR_CALL = "BEAR_CALL"
STRATEGY_LABELS = {BULL_PUT: "Bull put", BEAR_CALL: "Bear call"}
STRATEGY_RIGHT = {BULL_PUT: PUT, BEAR_CALL: CALL}

# Candidate status, worded so it never relies on colour alone.
QUALIFIES = "QUALIFIES"
FAILS = "DOES NOT QUALIFY"
UNVERIFIED = "UNVERIFIED"

# A quote older than this is flagged stale (Yahoo's free feed can lag, and
# outside market hours every quote is the last session's).
QUOTE_STALE_MINUTES = 20
# Short-leg delta at or past this flags the trade for review. Roughly twice
# the default 15-18 entry delta: the market now prices the short strike as
# about twice as likely to finish in the money as when the trade was opened.
SHORT_DELTA_ALERT = 0.30
# Open interest under this on either leg is called out as a liquidity risk.
LOW_OPEN_INTEREST = 100
# Price trend: last close vs its simple moving average over this many sessions.
TREND_SMA_SESSIONS = 50
# Realized volatility: stdev of daily log returns over this many sessions, x sqrt(252).
REALIZED_VOL_SESSIONS = 30
# The scanner evaluates at most this many listed expirations per ticker
# inside the entry window (daily-expiring ETFs list a dozen), nearest the
# window's middle first, so a scan stays a handful of chain fetches.
MAX_SCAN_EXPIRATIONS = 4
# Closing-debit comparisons are made to the cent.
_CENT = 0.005
_EPS = 1e-9


# ---------------------------------------------------------------- settings


@dataclass(frozen=True)
class SpreadSettings:
    """The reader's strategy rules. Defaults are starting points, not proven
    optima. Deltas are fractions (0.15); percentages are percent (30.0)."""

    underlyings: tuple[str, ...] = ("SPY", "QQQ")
    entry_dte_min: int = 30
    entry_dte_max: int = 45
    short_delta_min: float = 0.15
    short_delta_max: float = 0.18
    widths: tuple[float, ...] = (5.0,)
    min_credit_pct_of_width: float = 30.0
    profit_target_pct: float = 50.0
    loss_multiple: float = 2.0
    time_exit_dte: int = 21
    max_risk_pct_of_equity: float = 1.0
    max_open_spreads: int = 5
    # Bull puts only when price is above its 50-day average, bear calls only below.
    require_trend: bool = True
    # Optional: skip entries whose short leg's implied volatility is below this %.
    min_short_iv_pct: float | None = None

    @property
    def entry_dte_target(self) -> float:
        return (self.entry_dte_min + self.entry_dte_max) / 2

    def as_dict(self) -> dict[str, Any]:
        return {
            "underlyings": list(self.underlyings),
            "dte_min": self.entry_dte_min,
            "dte_max": self.entry_dte_max,
            "delta_min": round(self.short_delta_min * 100, 2),
            "delta_max": round(self.short_delta_max * 100, 2),
            "widths": list(self.widths),
            "min_credit_pct": self.min_credit_pct_of_width,
            "profit_target_pct": self.profit_target_pct,
            "loss_multiple": self.loss_multiple,
            "time_exit_dte": self.time_exit_dte,
            "max_risk_pct": self.max_risk_pct_of_equity,
            "max_open": self.max_open_spreads,
            "require_trend": self.require_trend,
            "min_iv_pct": self.min_short_iv_pct,
        }

    @classmethod
    def from_query(cls, query: dict[str, list[str]]) -> tuple["SpreadSettings", list[str]]:
        """Settings from ``/api/spreads`` query parameters (the names
        :meth:`as_dict` emits; deltas in delta points, 15 = 0.15). A missing
        parameter keeps its default; an invalid one keeps its default and
        adds a warning, so a bad value is never silently used."""
        defaults = cls()
        warnings: list[str] = []

        def raw(name: str) -> str | None:
            values = query.get(name)
            text = values[0].strip() if values else ""
            return text or None

        def number(
            name: str, default: float, low: float, high: float, *, integer: bool = False, fallback: str | None = None
        ) -> float:
            """``fallback`` words what an invalid value falls back to when
            ``default`` isn't a number worth printing (an optional filter)."""
            text = raw(name)
            if text is None:
                return default
            try:
                parsed = float(text)
            except ValueError:
                parsed = math.nan
            if not math.isfinite(parsed) or not low <= parsed <= high or (integer and parsed != int(parsed)):
                kind = "a whole number" if integer else "a number"
                instead = fallback if fallback is not None else f"using {default:g}"
                warnings.append(f"{name} must be {kind} from {low:g} to {high:g}; {instead}.")
                return default
            return int(parsed) if integer else parsed

        underlyings = defaults.underlyings
        if raw("underlyings") is not None:
            tickers = [t.strip().upper() for t in raw("underlyings").split(",") if t.strip()]
            if tickers and len(tickers) <= 8 and all(re.fullmatch(r"[A-Z][A-Z.\-]{0,9}", t) for t in tickers):
                underlyings = tuple(dict.fromkeys(tickers))
            else:
                warnings.append("underlyings must be 1 to 8 ticker symbols separated by commas; using SPY, QQQ.")

        dte_min = number("dte_min", defaults.entry_dte_min, 1, 365, integer=True)
        dte_max = number("dte_max", defaults.entry_dte_max, 1, 365, integer=True)
        if dte_min > dte_max:
            warnings.append("dte_min is above dte_max; using 30 to 45.")
            dte_min, dte_max = defaults.entry_dte_min, defaults.entry_dte_max

        delta_min = number("delta_min", defaults.short_delta_min * 100, 1, 50) / 100
        delta_max = number("delta_max", defaults.short_delta_max * 100, 1, 50) / 100
        if delta_min > delta_max:
            warnings.append("delta_min is above delta_max; using 15 to 18.")
            delta_min, delta_max = defaults.short_delta_min, defaults.short_delta_max

        widths = defaults.widths
        if raw("widths") is not None:
            try:
                parsed = tuple(sorted({float(w) for w in raw("widths").split(",") if w.strip()}))
            except ValueError:
                parsed = ()
            if parsed and len(parsed) <= 5 and all(0 < w <= 100 for w in parsed):
                widths = parsed
            else:
                warnings.append("widths must be 1 to 5 dollar amounts above 0 and up to 100; using 5.")

        time_exit = number("time_exit_dte", defaults.time_exit_dte, 0, 365, integer=True)
        if time_exit >= dte_min:
            warnings.append(
                f"time_exit_dte ({time_exit}) must be below the entry minimum DTE ({dte_min}), "
                "or every new spread would already be at its exit; using 21 or less."
            )
            time_exit = min(defaults.time_exit_dte, dte_min - 1)

        min_iv = None
        if raw("min_iv_pct") is not None:
            min_iv = number("min_iv_pct", math.nan, 0, 500, fallback="no volatility filter used")
            min_iv = None if math.isnan(min_iv) else min_iv

        require_trend = defaults.require_trend
        if raw("require_trend") is not None:
            if raw("require_trend").lower() in ("1", "true", "yes", "on"):
                require_trend = True
            elif raw("require_trend").lower() in ("0", "false", "no", "off"):
                require_trend = False
            else:
                warnings.append("require_trend must be on or off; using on.")

        settings = cls(
            underlyings=underlyings,
            entry_dte_min=dte_min,
            entry_dte_max=dte_max,
            short_delta_min=delta_min,
            short_delta_max=delta_max,
            widths=widths,
            min_credit_pct_of_width=number("min_credit_pct", defaults.min_credit_pct_of_width, 1, 100),
            profit_target_pct=number("profit_target_pct", defaults.profit_target_pct, 1, 99),
            loss_multiple=number("loss_multiple", defaults.loss_multiple, 1.01, 10),
            time_exit_dte=time_exit,
            max_risk_pct_of_equity=number("max_risk_pct", defaults.max_risk_pct_of_equity, 0.01, 100),
            max_open_spreads=number("max_open", defaults.max_open_spreads, 1, 100, integer=True),
            require_trend=require_trend,
            min_short_iv_pct=min_iv,
        )
        return settings, warnings


# ---------------------------------------------------------------- formatting


def _usd(value: float | None, *, cents: bool = True) -> str:
    if value is None:
        return "—"
    text = f"${abs(value):,.2f}" if cents else f"${abs(value):,.0f}"
    return f"-{text}" if value < -_EPS else text


def _strike(value: float) -> str:
    return f"{value:g}"


def _pct(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}%"


def _day(value: date) -> str:
    return value.strftime("%b %d, %Y").replace(" 0", " ")


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _iso_dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


# ---------------------------------------------------------------- market inputs


@dataclass(frozen=True)
class LegQuote:
    """One listed contract's quote. ``iv`` is a fraction (0.18)."""

    strike: float
    bid: float | None
    ask: float | None
    iv: float | None
    open_interest: int | None = None


@dataclass(frozen=True)
class QuoteTiming:
    """When a chain's prices are from. ``as_of`` is the quote time Yahoo
    reports; ``fetched_at`` is when this app downloaded it (a cached chain
    keeps its original fetch time)."""

    as_of: datetime | None
    fetched_at: datetime | None

    def status(self, now: datetime) -> tuple[str, float | None]:
        """``("FRESH" | "STALE" | "UNKNOWN", age in minutes)``."""
        if self.as_of is None:
            return "UNKNOWN", None
        age = max((now - self.as_of).total_seconds() / 60, 0.0)
        return ("FRESH" if age <= QUOTE_STALE_MINUTES else "STALE"), age

    def payload(self, now: datetime) -> dict[str, Any]:
        status, age = self.status(now)
        if status == "UNKNOWN":
            label = "Quote time unknown"
        elif status == "FRESH":
            label = f"Yahoo quote, {age:.0f} min old (free feed, may be delayed)"
        else:
            label = f"Stale: Yahoo quote is {_age_text(age)} old, likely the last session's prices"
        return {
            "status": status,
            "age_minutes": _round(age, 1),
            "as_of": _iso_dt(self.as_of),
            "fetched_at": _iso_dt(self.fetched_at),
            "label": label,
        }


def _age_text(minutes: float) -> str:
    if minutes < 90:
        return f"{minutes:.0f} min"
    if minutes < 48 * 60:
        return f"{minutes / 60:.0f} hours"
    return f"{minutes / 1440:.0f} days"


@dataclass(frozen=True)
class ChainQuotes:
    """One ticker's chain for one expiration, with the underlying price from
    the same response so delta and distance use one moment's prices."""

    ticker: str
    expiry: date
    spot: float | None
    timing: QuoteTiming
    puts: tuple[LegQuote, ...]
    calls: tuple[LegQuote, ...]

    def side(self, right: str) -> tuple[LegQuote, ...]:
        return self.puts if right == PUT else self.calls

    def quote(self, strike: float, right: str) -> LegQuote | None:
        for candidate in self.side(right):
            if abs(candidate.strike - strike) < _CENT:
                return candidate
        return None


@dataclass(frozen=True)
class MarketContext:
    """Per-ticker context from daily history, never a filter on its own
    except the trend rule when ``require_trend`` is on."""

    ticker: str
    last_close: float | None = None
    sma: float | None = None
    realized_vol_pct: float | None = None
    earnings_date: date | None = None
    history_reason: str | None = None

    @property
    def trend(self) -> str | None:
        """``"UP"`` (last close above its 50-day average), ``"DOWN"``, or ``None``."""
        if self.last_close is None or self.sma is None:
            return None
        return "UP" if self.last_close >= self.sma else "DOWN"

    def payload(self) -> dict[str, Any]:
        trend = self.trend
        if trend is None:
            label = self.history_reason or "Not enough price history for a trend"
        else:
            relation = "above" if trend == "UP" else "below"
            label = (
                f"{'Uptrend' if trend == 'UP' else 'Downtrend'}: last close {_usd(self.last_close)} is "
                f"{relation} its {TREND_SMA_SESSIONS}-day average {_usd(self.sma)}"
            )
        return {
            "trend": trend,
            "trend_label": label,
            "last_close": _round(self.last_close),
            "sma": _round(self.sma),
            "sma_sessions": TREND_SMA_SESSIONS,
            "realized_vol_pct": _round(self.realized_vol_pct, 1),
            "earnings_date": _iso(self.earnings_date),
        }


def market_context(
    ticker: str, closes: Sequence[float], earnings_date: date | None = None, *, today: date | None = None
) -> MarketContext:
    """Trend and realized volatility from daily closes (oldest first).

    An earnings date before ``today`` is dropped: it is a report that already
    happened and the fetch hasn't refreshed yet, not an upcoming risk (the
    same rule as the Planner's earnings list, ``_build_earnings_in_view``)."""
    if earnings_date is not None and today is not None and earnings_date < today:
        earnings_date = None
    usable = [c for c in closes if c and c > 0]
    if len(usable) < TREND_SMA_SESSIONS:
        return MarketContext(
            ticker,
            last_close=usable[-1] if usable else None,
            earnings_date=earnings_date,
            history_reason=f"Fewer than {TREND_SMA_SESSIONS} daily closes loaded, so no trend",
        )
    sma = sum(usable[-TREND_SMA_SESSIONS:]) / TREND_SMA_SESSIONS
    window = usable[-(REALIZED_VOL_SESSIONS + 1):]
    returns = [math.log(window[i] / window[i - 1]) for i in range(1, len(window))]
    vol = None
    if len(returns) >= 2:
        mean = sum(returns) / len(returns)
        vol = math.sqrt(sum((r - mean) ** 2 for r in returns) / len(returns)) * math.sqrt(252) * 100
    return MarketContext(ticker, last_close=usable[-1], sma=sma, realized_vol_pct=vol, earnings_date=earnings_date)


def scan_expirations(expirations: Sequence[date], today: date, settings: SpreadSettings) -> list[date]:
    """Listed expirations inside the entry DTE window, nearest the window's
    middle first, at most :data:`MAX_SCAN_EXPIRATIONS`, returned in date order."""
    inside = [d for d in expirations if settings.entry_dte_min <= (d - today).days <= settings.entry_dte_max]
    nearest = sorted(inside, key=lambda d: (abs((d - today).days - settings.entry_dte_target), d))
    return sorted(nearest[:MAX_SCAN_EXPIRATIONS])


# ---------------------------------------------------------------- spread math


def spread_width(short_strike: float, long_strike: float) -> float:
    return abs(short_strike - long_strike)


def max_profit_per_contract(credit: float) -> float:
    return credit * OPTION_MULTIPLIER


def max_loss_per_contract(width: float, credit: float) -> float:
    return (width - credit) * OPTION_MULTIPLIER


def breakeven(strategy: str, short_strike: float, credit: float) -> float:
    return short_strike - credit if strategy == BULL_PUT else short_strike + credit


def target_closing_debit(credit: float, profit_target_pct: float) -> float:
    return credit * (1 - profit_target_pct / 100)


def loss_trigger_debit(credit: float, loss_multiple: float) -> float:
    return credit * loss_multiple


def executable_open_credit(short: LegQuote, long: LegQuote) -> float | None:
    """Short bid - long ask, or ``None`` without a usable two-sided quote on both legs."""
    if bid_ask_mid(short.bid, short.ask) is None or bid_ask_mid(long.bid, long.ask) is None:
        return None
    return short.bid - long.ask


def mid_credit(short: LegQuote, long: LegQuote) -> float | None:
    short_mid, long_mid = bid_ask_mid(short.bid, short.ask), bid_ask_mid(long.bid, long.ask)
    return None if short_mid is None or long_mid is None else short_mid - long_mid


def executable_close_debit(short: LegQuote | None, long: LegQuote | None) -> float | None:
    """Short ask - long bid: what buying the short back and selling the long costs now."""
    if short is None or long is None:
        return None
    if bid_ask_mid(short.bid, short.ask) is None or bid_ask_mid(long.bid, long.ask) is None:
        return None
    return short.ask - long.bid


def mid_close_debit(short: LegQuote | None, long: LegQuote | None) -> float | None:
    if short is None or long is None:
        return None
    return mid_credit(short, long)


def suggested_contracts(account_equity: float | None, max_risk_pct: float, risk_per_contract: float) -> int | None:
    """floor(account value x max risk % / risk per contract); ``None`` without an account value."""
    if account_equity is None or account_equity <= 0 or risk_per_contract <= 0:
        return None
    return int(math.floor(account_equity * max_risk_pct / 100 / risk_per_contract + _EPS))


def short_leg_delta(
    *, spot: float | None, short: LegQuote | None, right: str, days: int, risk_free_rate: float
) -> float | None:
    """Model delta of the short leg as a positive number (0.16), or ``None``."""
    if short is None:
        return None
    greeks = black_scholes_greeks(
        spot=spot,
        strike=short.strike,
        days_to_expiry=days,
        implied_volatility=short.iv,
        right=right,
        risk_free_rate=risk_free_rate,
    )
    return None if greeks is None else abs(greeks.delta)


def probability_of_profit(
    *, strategy: str, spot: float | None, breakeven_price: float, days: int, iv: float | None, risk_free_rate: float
) -> float | None:
    above = probability_finish_above(
        spot=spot, level=breakeven_price, days_to_expiry=days, implied_volatility=iv, risk_free_rate=risk_free_rate
    )
    if above is None:
        return None
    return above if strategy == BULL_PUT else 1 - above


# ---------------------------------------------------------------- entry candidates


@dataclass(frozen=True)
class Check:
    """One entry rule. ``passed`` is ``None`` when the data to test it is missing."""

    key: str
    label: str
    passed: bool | None
    detail: str


@dataclass(frozen=True)
class SpreadCandidate:
    ticker: str
    strategy: str
    expiry: date
    dte: int
    short_strike: float
    long_strike: float
    width: float
    spot: float | None
    timing: QuoteTiming
    short: LegQuote | None
    long: LegQuote | None
    credit: float | None  # executable: short bid - long ask
    credit_mid: float | None  # estimate
    short_delta: float | None
    pop: float | None
    fee_per_contract: float
    contracts: int | None
    account_equity: float | None
    settings: SpreadSettings
    context: MarketContext
    checks: tuple[Check, ...]

    @property
    def status(self) -> str:
        if any(c.passed is False for c in self.checks):
            return FAILS
        if any(c.passed is None for c in self.checks):
            return UNVERIFIED
        return QUALIFIES

    @property
    def right(self) -> str:
        return STRATEGY_RIGHT[self.strategy]

    @property
    def min_credit(self) -> float:
        return self.width * self.settings.min_credit_pct_of_width / 100

    @property
    def open_fees_per_spread(self) -> float:
        return 2 * self.fee_per_contract

    @property
    def max_profit(self) -> float | None:
        return None if self.credit is None else max_profit_per_contract(self.credit)

    @property
    def max_loss(self) -> float | None:
        return None if self.credit is None else max_loss_per_contract(self.width, self.credit)

    @property
    def breakeven(self) -> float | None:
        return None if self.credit is None else breakeven(self.strategy, self.short_strike, self.credit)

    @property
    def credit_pct_of_width(self) -> float | None:
        return None if self.credit is None or self.width <= 0 else self.credit / self.width * 100

    @property
    def return_on_risk_pct(self) -> float | None:
        if self.credit is None or self.width - self.credit <= _EPS:
            return None
        return self.credit / (self.width - self.credit) * 100

    @property
    def risk_per_contract(self) -> float | None:
        return None if self.max_loss is None else self.max_loss + self.open_fees_per_spread

    @property
    def rank_key(self) -> tuple:
        """Best first: higher credit / width, then DTE nearest the window's middle."""
        return (-(self.credit_pct_of_width or -1e9), abs(self.dte - self.settings.entry_dte_target), self.expiry)


def evaluate_spread(
    *,
    ticker: str,
    strategy: str,
    chain: ChainQuotes,
    short_strike: float,
    long_strike: float,
    today: date,
    settings: SpreadSettings,
    context: MarketContext,
    account_equity: float | None,
    risk_free_rate: float,
    fee_per_contract: float,
) -> SpreadCandidate:
    """Every entry rule for one spread. Raises ``ValueError`` when the strikes
    don't form a credit spread of ``strategy`` (bull put: short above long;
    bear call: short below long)."""
    if strategy not in STRATEGY_LABELS:
        raise ValueError(f"unknown strategy {strategy!r}")
    if strategy == BULL_PUT and not short_strike > long_strike:
        raise ValueError("A bull put spread sells the higher strike and buys the lower one.")
    if strategy == BEAR_CALL and not short_strike < long_strike:
        raise ValueError("A bear call spread sells the lower strike and buys the higher one.")

    right = STRATEGY_RIGHT[strategy]
    expiry = chain.expiry
    dte = (expiry - today).days
    width = spread_width(short_strike, long_strike)
    short, long = chain.quote(short_strike, right), chain.quote(long_strike, right)
    credit = executable_open_credit(short, long) if short and long else None
    credit_mid = mid_credit(short, long) if short and long else None
    delta = short_leg_delta(spot=chain.spot, short=short, right=right, days=dte, risk_free_rate=risk_free_rate)
    pop = None
    if credit is not None and credit > 0:
        pop = probability_of_profit(
            strategy=strategy,
            spot=chain.spot,
            breakeven_price=breakeven(strategy, short_strike, credit),
            days=dte,
            iv=short.iv if short else None,
            risk_free_rate=risk_free_rate,
        )
    contracts = None
    if credit is not None and width - credit > _EPS:
        contracts = suggested_contracts(
            account_equity,
            settings.max_risk_pct_of_equity,
            max_loss_per_contract(width, credit) + 2 * fee_per_contract,
        )

    checks: list[Check] = []
    if short is None or long is None:
        missing = [_strike(s) for s, q in ((short_strike, short), (long_strike, long)) if q is None]
        checks.append(Check("quotes", "Both legs quoted", None, f"Strike {', '.join(missing)} is not listed for {_day(expiry)}."))
    elif credit is None:
        checks.append(Check("quotes", "Both legs quoted", None, "One leg has no usable bid and ask right now."))
    else:
        checks.append(Check("quotes", "Both legs quoted", True, "Both legs have a two-sided quote."))

    in_window = settings.entry_dte_min <= dte <= settings.entry_dte_max
    checks.append(
        Check(
            "dte",
            "Entry DTE",
            in_window,
            f"{dte} DTE is {'inside' if in_window else 'outside'} {settings.entry_dte_min}-{settings.entry_dte_max}.",
        )
    )

    if delta is None:
        checks.append(Check("delta", "Short delta", None, "No implied volatility on the short leg, so no delta."))
    else:
        low, high = settings.short_delta_min, settings.short_delta_max
        ok = low - _EPS <= delta <= high + _EPS
        checks.append(
            Check(
                "delta",
                "Short delta",
                ok,
                f"{delta * 100:.1f} is {'inside' if ok else 'outside'} {low * 100:g}-{high * 100:g}.",
            )
        )

    if credit is not None:
        if credit <= _EPS:
            checks.append(
                Check("credit", "Credit / width", False, f"Selling at the bid and buying at the ask pays {_usd(credit)}, no credit.")
            )
        else:
            ratio = credit / width * 100
            ok = ratio + _EPS >= settings.min_credit_pct_of_width
            checks.append(
                Check(
                    "credit",
                    "Credit / width",
                    ok,
                    f"{_usd(credit)} is {ratio:.1f}% of the {_usd(width)} width; minimum {settings.min_credit_pct_of_width:g}% "
                    f"({_usd(width * settings.min_credit_pct_of_width / 100)}).",
                )
            )

    trend = context.trend
    if settings.require_trend:
        wanted = "UP" if strategy == BULL_PUT else "DOWN"
        if trend is None:
            checks.append(Check("trend", "Trend", None, context.history_reason or "No trend available."))
        else:
            ok = trend == wanted
            word = "above" if trend == "UP" else "below"
            needs = "above" if wanted == "UP" else "below"
            checks.append(
                Check(
                    "trend",
                    "Trend",
                    ok,
                    f"Price is {word} its {TREND_SMA_SESSIONS}-day average; a {STRATEGY_LABELS[strategy].lower()} "
                    f"spread needs it {needs}.",
                )
            )

    if settings.min_short_iv_pct is not None:
        if short is None or short.iv is None:
            checks.append(Check("iv", "Implied volatility", None, "No implied volatility on the short leg."))
        else:
            iv_pct = short.iv * 100
            ok = iv_pct + _EPS >= settings.min_short_iv_pct
            checks.append(
                Check("iv", "Implied volatility", ok, f"Short-leg IV {iv_pct:.1f}%; minimum {settings.min_short_iv_pct:g}%.")
            )

    if contracts is not None:
        risk = max_loss_per_contract(width, credit) + 2 * fee_per_contract
        budget = account_equity * settings.max_risk_pct_of_equity / 100
        checks.append(
            Check(
                "size",
                "Fits risk limit",
                contracts >= 1,
                f"One spread risks {_usd(risk)}; {settings.max_risk_pct_of_equity:g}% of {_usd(account_equity, cents=False)} "
                f"is {_usd(budget)}, room for {contracts}.",
            )
        )

    return SpreadCandidate(
        ticker=ticker,
        strategy=strategy,
        expiry=expiry,
        dte=dte,
        short_strike=short_strike,
        long_strike=long_strike,
        width=width,
        spot=chain.spot,
        timing=chain.timing,
        short=short,
        long=long,
        credit=credit,
        credit_mid=credit_mid,
        short_delta=delta,
        pop=pop,
        fee_per_contract=fee_per_contract,
        contracts=contracts,
        account_equity=account_equity,
        settings=settings,
        context=context,
        checks=tuple(checks),
    )


def scan_chain(
    *,
    chain: ChainQuotes,
    today: date,
    settings: SpreadSettings,
    context: MarketContext,
    account_equity: float | None,
    risk_free_rate: float,
    fee_per_contract: float,
) -> tuple[list[SpreadCandidate], list[str]]:
    """Every spread whose short leg's model delta is inside the delta range,
    at each configured width whose long strike is listed. Returns the
    candidates and plain notes on what was skipped and why."""
    candidates: list[SpreadCandidate] = []
    notes: list[str] = []
    dte = (chain.expiry - today).days
    for strategy in (BULL_PUT, BEAR_CALL):
        right = STRATEGY_RIGHT[strategy]
        shorts = []
        for quote in chain.side(right):
            delta = short_leg_delta(spot=chain.spot, short=quote, right=right, days=dte, risk_free_rate=risk_free_rate)
            if delta is not None and settings.short_delta_min - _EPS <= delta <= settings.short_delta_max + _EPS:
                shorts.append(quote)
        label = f"{STRATEGY_LABELS[strategy]} {_day(chain.expiry)}"
        if not shorts:
            notes.append(
                f"{label}: no short strike with a delta of {settings.short_delta_min * 100:g}-{settings.short_delta_max * 100:g}."
            )
            continue
        unlisted = 0
        for short in shorts:
            for width in settings.widths:
                long_strike = short.strike - width if strategy == BULL_PUT else short.strike + width
                if chain.quote(long_strike, right) is None:
                    unlisted += 1
                    continue
                candidates.append(
                    evaluate_spread(
                        ticker=chain.ticker,
                        strategy=strategy,
                        chain=chain,
                        short_strike=short.strike,
                        long_strike=long_strike,
                        today=today,
                        settings=settings,
                        context=context,
                        account_equity=account_equity,
                        risk_free_rate=risk_free_rate,
                        fee_per_contract=fee_per_contract,
                    )
                )
        if unlisted:
            notes.append(f"{label}: {unlisted} long strike(s) at the configured width are not listed.")
    return candidates, notes


@dataclass(frozen=True)
class SpreadCheck:
    """A spread the reader picked by hand to evaluate against the same rules."""

    ticker: str
    expiry: date
    strategy: str
    short_strike: float
    long_strike: float

    @classmethod
    def from_query(cls, query: dict[str, list[str]]) -> tuple["SpreadCheck | None", str | None]:
        """``(check, None)``, ``(None, None)`` when no check was asked for, or
        ``(None, reason)`` when one was asked for but is incomplete or invalid."""
        fields = {name: ((query.get(f"check_{name}") or [""])[0]).strip() for name in ("ticker", "expiry", "strategy", "short", "long")}
        if not any(fields.values()):
            return None, None
        if not all(fields.values()):
            return None, "Pick a ticker, expiration, spread type, short strike and long strike to check a spread."
        ticker = fields["ticker"].upper()
        if not re.fullmatch(r"[A-Z][A-Z.\-]{0,9}", ticker):
            return None, f"{fields['ticker']!r} is not a ticker symbol."
        if fields["strategy"] not in STRATEGY_LABELS:
            return None, "Spread type must be bull put or bear call."
        try:
            expiry = date.fromisoformat(fields["expiry"])
            short_strike, long_strike = float(fields["short"]), float(fields["long"])
        except ValueError:
            return None, "Expiration must be a date and strikes must be numbers."
        if not (short_strike > 0 and long_strike > 0 and math.isfinite(short_strike) and math.isfinite(long_strike)):
            return None, "Strikes must be above 0."
        return cls(ticker, expiry, fields["strategy"], short_strike, long_strike), None


# ---------------------------------------------------------------- recommendations


@dataclass(frozen=True)
class Recommendation:
    ticker: str
    status: str  # "RECOMMENDED" | "AT POSITION LIMIT" | "NO QUALIFYING SPREADS"
    headline: str
    pick: SpreadCandidate | None
    reasons: tuple[str, ...]
    risks: tuple[str, ...]


def _fail_summary(candidates: Sequence[SpreadCandidate]) -> list[str]:
    """The most common failing rules among evaluated candidates, worded."""
    counts: dict[str, int] = {}
    for candidate in candidates:
        for check in candidate.checks:
            if check.passed is False:
                counts[check.label] = counts.get(check.label, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [f"{label}: failed by {n} of {len(candidates)} spreads checked." for label, n in ranked[:3]]


def candidate_risks(candidate: SpreadCandidate, now: datetime) -> list[str]:
    risks: list[str] = []
    if candidate.max_loss is not None:
        side = "below" if candidate.strategy == BULL_PUT else "above"
        risks.append(
            f"Max loss {_usd(candidate.max_loss)} per spread if {candidate.ticker} closes {side} "
            f"{_strike(candidate.long_strike)} at expiration."
        )
    if candidate.short_delta is not None:
        risks.append(
            f"Short delta {candidate.short_delta * 100:.0f}: the market prices roughly a "
            f"{candidate.short_delta * 100:.0f}% chance the short strike finishes in the money."
        )
    if candidate.timing.status(now)[0] != "FRESH":
        risks.append("Quotes are not current; recheck prices before placing the order.")
    earnings = candidate.context.earnings_date
    # Only a report between the scan date and expiration is a risk to this
    # trade; a past date is one the earnings fetch hasn't refreshed yet.
    scan_day = candidate.expiry - timedelta(days=candidate.dte)
    if earnings is not None and scan_day <= earnings <= candidate.expiry:
        risks.append(f"Earnings on {_day(earnings)}, before this expiration.")
    for leg_name, quote in (("short", candidate.short), ("long", candidate.long)):
        if quote is not None and quote.open_interest is not None and quote.open_interest < LOW_OPEN_INTEREST:
            risks.append(f"Thin market: the {leg_name} leg has open interest {quote.open_interest}.")
    if candidate.settings.require_trend is False and candidate.context.trend is not None:
        wanted = "UP" if candidate.strategy == BULL_PUT else "DOWN"
        if candidate.context.trend != wanted:
            risks.append("Trading against the 50-day trend (trend filter is off).")
    return risks


def recommend(
    ticker: str,
    candidates: Sequence[SpreadCandidate],
    *,
    open_spread_count: int,
    settings: SpreadSettings,
    now: datetime,
    unavailable_reason: str | None = None,
) -> Recommendation:
    """The best qualifying spread for one ticker: highest credit / width,
    then DTE closest to the middle of the entry window. Never forced: with
    nothing qualifying the answer is NO QUALIFYING SPREADS and why."""
    qualifying = sorted((c for c in candidates if c.status == QUALIFIES), key=lambda c: c.rank_key)
    if not qualifying:
        if unavailable_reason:
            reasons = [unavailable_reason]
        elif not candidates:
            reasons = ["No spread was found with a short strike inside the delta range."]
        else:
            reasons = _fail_summary(candidates)
            if any(c.status == UNVERIFIED for c in candidates) and not reasons:
                reasons = ["Prices or volatility were missing for the spreads checked."]
        return Recommendation(ticker, "NO QUALIFYING SPREADS", f"{ticker}: no qualifying spreads", None, tuple(reasons), ())

    pick = qualifying[0]
    label = STRATEGY_LABELS[pick.strategy]
    headline = (
        f"{ticker} {label.lower()} {_strike(pick.short_strike)}/{_strike(pick.long_strike)}, "
        f"{_day(pick.expiry)} ({pick.dte} DTE), credit at least {_usd(pick.min_credit)}"
    )
    trend_text = (
        f"Trend supports it: {pick.context.payload()['trend_label']}."
        if pick.context.trend == ("UP" if pick.strategy == BULL_PUT else "DOWN")
        else f"Trend: {pick.context.payload()['trend_label']}."
    )
    reasons = [
        f"Credit {_usd(pick.credit)} is {pick.credit_pct_of_width:.1f}% of the {_usd(pick.width)} width "
        f"(minimum {settings.min_credit_pct_of_width:g}%, {_usd(pick.min_credit)}).",
        f"Short delta {pick.short_delta * 100:.1f}, inside {settings.short_delta_min * 100:g}-{settings.short_delta_max * 100:g}.",
        trend_text,
    ]
    if pick.contracts is not None:
        reasons.append(
            f"Size: {pick.contracts} contract(s), {_usd(pick.risk_per_contract * pick.contracts)} total max risk incl. fees to open."
        )
    else:
        reasons.append("Size: add a Positions export so the account value can size this trade.")
    status = "RECOMMENDED"
    if open_spread_count >= settings.max_open_spreads:
        status = "AT POSITION LIMIT"
        reasons.insert(
            0, f"You already have {open_spread_count} open spreads; your limit is {settings.max_open_spreads}."
        )
    return Recommendation(ticker, status, headline, pick, tuple(reasons), tuple(candidate_risks(pick, now)))


# ---------------------------------------------------------------- positions from the ledger


@dataclass(frozen=True)
class LegFill:
    """A closing fill on one leg, scaled to this spread's share of the leg."""

    date: date
    action: str
    contracts: float
    cash: float  # net of fees, signed (+ received)
    fees: float


@dataclass(frozen=True)
class LegSlice:
    """The part of one engine leg that belongs to a spread."""

    leg_id: str
    side: str
    strike: float
    contracts: float
    open_cash: float  # net of fees, signed
    open_fees: float
    closes: tuple[LegFill, ...]
    right: str = PUT
    open_price: float | None = None  # broker fill price per share

    @property
    def closed_contracts(self) -> float:
        return sum(c.contracts for c in self.closes)

    @property
    def remaining(self) -> float:
        return max(self.contracts - self.closed_contracts, 0.0)

    def realized_net(self, fills: Iterable[LegFill] | None = None) -> float:
        """Opening cash for the contracts these fills closed, plus the fills' own cash."""
        fills = self.closes if fills is None else tuple(fills)
        per_contract = self.open_cash / self.contracts if self.contracts else 0.0
        return sum(per_contract * f.contracts + f.cash for f in fills)

    def realized_fees(self, fills: Iterable[LegFill] | None = None) -> float:
        fills = self.closes if fills is None else tuple(fills)
        per_contract = self.open_fees / self.contracts if self.contracts else 0.0
        return sum(per_contract * f.contracts + f.fees for f in fills)

    @property
    def assigned_contracts(self) -> float:
        """Contracts closed by assignment or exercise (the parser books both as ASSIGNED)."""
        return sum(f.contracts for f in self.closes if f.action == ASSIGNED)


def _slice(leg: OptionLeg, contracts: float) -> LegSlice:
    """This spread's ``contracts`` of ``leg`` and the closing fills that belong to them.

    The engine pairs a leg into at most one spread (``_detect_spreads`` pairs
    only on the open day, one short with one long), so the rest of the leg,
    ``leg.contracts - contracts``, is unpaired. Closes go to that unpaired
    part first, oldest first, and reach the spread only once it is used up:
    the spread is the hedged position, so trimming an oversized leg trims
    its extra contracts. Fills are split in whole contracts as the broker
    filled them, never pro rata, so a slice never holds a fraction of a
    contract; a split fill's cash and fees follow its contracts.
    """
    open_fraction = contracts / leg.contracts if leg.contracts else 0.0
    unpaired = max(leg.contracts - contracts, 0.0)
    closes: list[LegFill] = []
    for close in sorted(leg.closes, key=lambda c: c.date):  # stable: same-day fills keep ledger order
        to_unpaired = min(close.contracts, unpaired)
        unpaired -= to_unpaired
        taken = close.contracts - to_unpaired
        if taken <= _EPS:
            continue
        share = taken / close.contracts
        closes.append(LegFill(close.date, close.action, taken, close.cash * share, close.fees * share))
    return LegSlice(
        leg_id=leg.leg_id,
        side=leg.side,
        strike=leg.strike,
        contracts=contracts,
        open_cash=leg.open_cash * open_fraction,
        open_fees=leg.open_fees * open_fraction,
        closes=tuple(closes),
        right=leg.right,
        open_price=leg.open_price,
    )


@dataclass(frozen=True)
class SpreadPosition:
    """One credit spread from the broker ledger, short and long legs as one position."""

    spread_id: str
    account_id: str | None
    account_label: str | None
    cycle_id: str
    ticker: str
    strategy: str
    open_date: date
    expiry: date
    short: LegSlice
    long: LegSlice

    @property
    def short_strike(self) -> float:
        return self.short.strike

    @property
    def long_strike(self) -> float:
        return self.long.strike

    @property
    def width(self) -> float:
        return spread_width(self.short.strike, self.long.strike)

    @property
    def contracts(self) -> float:
        return self.short.contracts

    @property
    def credit(self) -> float:
        """Original gross credit per share: (net opening cash + opening fees) / (100 x contracts)."""
        gross = self.short.open_cash + self.short.open_fees + self.long.open_cash + self.long.open_fees
        return gross / (OPTION_MULTIPLIER * self.contracts) if self.contracts else 0.0

    @property
    def open_fees(self) -> float:
        return self.short.open_fees + self.long.open_fees

    @property
    def premium_collected(self) -> float:
        """Gross opening credit in dollars: credit x 100 x contracts, before fees."""
        return self.credit * OPTION_MULTIPLIER * self.contracts

    @property
    def net_premium_collected(self) -> float:
        """Cash actually received at open: short leg's Amount + long leg's Amount, both
        already net of commission and fees (= premium collected - opening fees)."""
        return self.short.open_cash + self.long.open_cash

    @property
    def contracts_open(self) -> float:
        return min(self.short.remaining, self.long.remaining)

    @property
    def is_broken(self) -> bool:
        """One leg still open without the other, so the risk is no longer defined by the width."""
        return abs(self.short.remaining - self.long.remaining) > 1e-6

    @property
    def is_closed(self) -> bool:
        return self.short.remaining <= 1e-6 and self.long.remaining <= 1e-6

    @property
    def close_date(self) -> date | None:
        if not self.is_closed:
            return None
        return max((f.date for f in self.short.closes + self.long.closes), default=None)

    @property
    def max_profit(self) -> float:
        return max_profit_per_contract(self.credit) * self.contracts

    @property
    def max_loss(self) -> float:
        return max_loss_per_contract(self.width, self.credit) * self.contracts

    # Assignment. A broker's ASSIGNED (or EXERCISED) option row carries $0;
    # the cash moves in the separate share row, which this module can't see.
    # When both legs are assigned for the same contracts the share trades are
    # fixed by the strikes alone: the short leg's shares change hands at the
    # short strike and the long leg's at the long strike, so the pair settles
    # for exactly width x 100 per contract against the trader (bull put: buy
    # at the higher strike, sell at the lower; bear call: the reverse). Any
    # regulatory fee on the share sale sits in the share row and is not
    # included. When the legs' assigned counts differ, the result depends on
    # what happened to the shares afterward, so the spread can't settle it.

    @property
    def result_settled(self) -> bool:
        """True when the spread's own fills (plus the strike settlement of a
        both-legs assignment) are its whole result. False when one leg was
        assigned or exercised without the other."""
        return abs(self.short.assigned_contracts - self.long.assigned_contracts) <= 1e-6

    @property
    def unsettled_note(self) -> str | None:
        if self.result_settled:
            return None
        return (
            "Assigned without the other leg: the result depends on the share trade, which this tab can't see; "
            "see the Wheel tab."
        )

    @property
    def assignment_settlement(self) -> tuple[date, float, float] | None:
        """``(date, net cash, contracts)`` of a both-legs assignment, booked on
        the last assignment date: -width x 100 x contracts. ``None`` without one."""
        if not self.result_settled:
            return None
        contracts = self.short.assigned_contracts
        if contracts <= _EPS:
            return None
        day = max(f.date for f in self.short.closes + self.long.closes if f.action == ASSIGNED)
        return day, -self.width * OPTION_MULTIPLIER * contracts, contracts

    @property
    def realized_net(self) -> float | None:
        """Net realized P/L, fees included. ``None`` when the result isn't
        settled by the spread (see :attr:`result_settled`), never the option
        cash alone, which would book a short-only assignment as a win."""
        if not self.result_settled:
            return None
        settlement = self.assignment_settlement
        return self.short.realized_net() + self.long.realized_net() + (settlement[1] if settlement else 0.0)

    @property
    def realized_fees(self) -> float:
        return self.short.realized_fees() + self.long.realized_fees()

    @property
    def closing_debit(self) -> float | None:
        """Average gross closing debit per share once fully closed:
        -(close cash + close fees + assignment settlement) / (100 x contracts).
        ``None`` while open or when the result isn't settled by the spread."""
        if not self.is_closed or not self.contracts or not self.result_settled:
            return None
        settlement = self.assignment_settlement
        gross = sum(f.cash + f.fees for f in self.short.closes + self.long.closes)
        gross += settlement[1] if settlement else 0.0
        return -gross / (OPTION_MULTIPLIER * self.contracts)

    def realized_events(self) -> list[tuple[date, float, float]]:
        """``[(date, net realized, fees)]`` per close date, for P/L by period
        and drawdown. Empty when the result isn't settled by the spread."""
        if not self.result_settled:
            return []
        settlement = self.assignment_settlement
        days = sorted({f.date for f in self.short.closes + self.long.closes})
        out = []
        for day in days:
            net = fees = 0.0
            for leg in (self.short, self.long):
                fills = [f for f in leg.closes if f.date == day]
                net += leg.realized_net(fills)
                fees += leg.realized_fees(fills)
            if settlement and settlement[0] == day:
                net += settlement[1]
            out.append((day, net, fees))
        return out


def credit_spread_positions(
    spreads: Iterable[Spread],
    legs_by_id: dict[str, OptionLeg],
    *,
    account_id: str | None = None,
    account_label: str | None = None,
) -> list[SpreadPosition]:
    """The engine's same-day pairs that are credit spreads: a bull put
    (short put above the long put) or bear call (short call below the long
    call) that took in a net credit. Debit pairs (hedges) are left out."""
    out: list[SpreadPosition] = []
    for spread in spreads:
        short_leg, long_leg = legs_by_id.get(spread.short_leg_id), legs_by_id.get(spread.long_leg_id)
        if short_leg is None or long_leg is None or short_leg.side != SHORT:
            continue
        if spread.right == PUT and spread.short_strike > spread.long_strike:
            strategy = BULL_PUT
        elif spread.right == CALL and spread.short_strike < spread.long_strike:
            strategy = BEAR_CALL
        else:
            continue
        position = SpreadPosition(
            spread_id=spread.spread_id if account_id is None else f"{account_id}:{spread.spread_id}",
            account_id=account_id,
            account_label=account_label,
            cycle_id=spread.cycle_id,
            ticker=spread.underlying,
            strategy=strategy,
            open_date=spread.open_date,
            expiry=spread.expiry,
            short=_slice(short_leg, spread.paired_contracts),
            long=_slice(long_leg, spread.paired_contracts),
        )
        if position.credit > _EPS:
            out.append(position)
    return out


# ---------------------------------------------------------------- open-spread management

ACTION_CLOSE = "close"
ACTION_REVIEW = "review"
ACTION_HOLD = "hold"
ACTION_UNVERIFIED = "unverified"


@dataclass(frozen=True)
class Guidance:
    position: SpreadPosition
    today: date
    dte: int
    spot: float | None
    timing: QuoteTiming | None
    short_quote: LegQuote | None
    long_quote: LegQuote | None
    closing_debit: float | None  # executable, per share
    closing_debit_mid: float | None
    short_delta: float | None
    target_debit: float
    loss_trigger: float
    time_exit_date: date
    action: str
    action_label: str
    reason: str
    flags: tuple[str, ...]

    @property
    def unrealized_pl(self) -> float | None:
        """(credit - closing debit) x 100 x open contracts, before closing fees."""
        if self.closing_debit is None:
            return None
        return (self.position.credit - self.closing_debit) * OPTION_MULTIPLIER * self.position.contracts_open

    @property
    def pct_of_max_profit(self) -> float | None:
        if self.closing_debit is None or self.position.credit <= _EPS:
            return None
        return (self.position.credit - self.closing_debit) / self.position.credit * 100

    @property
    def distance_to_short_pct(self) -> float | None:
        """How far the price is from the short strike, as % of price. Positive:
        the short strike is out of the money; negative: price has crossed it."""
        if self.spot is None or self.spot <= 0:
            return None
        gap = self.spot - self.position.short_strike
        return (gap if self.position.strategy == BULL_PUT else -gap) / self.spot * 100


def manage_spread(
    position: SpreadPosition,
    *,
    today: date,
    chain: ChainQuotes | None,
    settings: SpreadSettings,
    risk_free_rate: float,
    now: datetime,
    chain_unavailable_reason: str | None = None,
) -> Guidance:
    """Exit guidance for one open spread. Priority when several apply:
    past expiration in the ledger > one leg open > profit target > loss
    threshold > time exit > short strike under pressure > no quote > hold.
    A trigger is a prompt to act, never an automatic close."""
    right = STRATEGY_RIGHT[position.strategy]
    dte = (position.expiry - today).days
    credit = position.credit
    target = target_closing_debit(credit, settings.profit_target_pct)
    trigger = loss_trigger_debit(credit, settings.loss_multiple)
    exit_date = position.expiry - timedelta(days=settings.time_exit_dte)
    short_q = chain.quote(position.short_strike, right) if chain else None
    long_q = chain.quote(position.long_strike, right) if chain else None
    spot = chain.spot if chain else None
    debit = executable_close_debit(short_q, long_q)
    debit_mid = mid_close_debit(short_q, long_q)
    delta = short_leg_delta(spot=spot, short=short_q, right=right, days=dte, risk_free_rate=risk_free_rate)
    n = position.contracts_open

    flags: list[str] = []
    breached = spot is not None and (
        spot < position.short_strike if position.strategy == BULL_PUT else spot > position.short_strike
    )
    if breached:
        flags.append(f"Price {_usd(spot)} has crossed the short strike {_strike(position.short_strike)}.")
    if delta is not None and delta + _EPS >= SHORT_DELTA_ALERT:
        flags.append(f"Short delta is {delta * 100:.0f}, at or above {SHORT_DELTA_ALERT * 100:.0f}.")
    if chain is not None:
        status, _ = chain.timing.status(now)
        if status != "FRESH":
            flags.append("Quotes are not current; recheck before acting.")
    if debit is not None and debit > position.width + _CENT:
        flags.append("The quoted closing cost is above the spread width, so the quotes are unreliable.")

    def guidance(action: str, label: str, reason: str) -> Guidance:
        return Guidance(
            position, today, dte, spot, chain.timing if chain else None, short_q, long_q, debit, debit_mid, delta,
            target, trigger, exit_date, action, label, reason, tuple(flags),
        )

    pl_now = None if debit is None else (credit - debit) * OPTION_MULTIPLIER * n
    still_at_risk = None if debit is None else (position.width - debit) * OPTION_MULTIPLIER * n

    if dte < 0:
        return guidance(
            ACTION_REVIEW,
            "CHECK BROKER: PAST EXPIRATION",
            f"Expired {_day(position.expiry)} but the ledger still shows it open. Load your latest export to record "
            "how it settled (expired, closed or assigned).",
        )
    if position.is_broken:
        return guidance(
            ACTION_REVIEW,
            "REVIEW: ONE LEG OPEN",
            f"Short has {position.short.remaining:g} contract(s) open and long has {position.long.remaining:g}. "
            "The loss is no longer capped by the width.",
        )
    if debit is not None and debit <= target + _CENT:
        return guidance(
            ACTION_CLOSE,
            "CLOSE: PROFIT TARGET REACHED",
            f"Closing costs {_usd(debit)}, at or below the {_usd(target)} target "
            f"({settings.profit_target_pct:g}% of the {_usd(credit)} credit). Locks in about {_usd(pl_now)} before fees.",
        )
    if debit is not None and debit + _CENT >= trigger:
        return guidance(
            ACTION_CLOSE,
            "LOSS THRESHOLD REACHED: REVIEW TO CLOSE",
            f"Closing costs {_usd(debit)}, at or above {settings.loss_multiple:g}x the credit ({_usd(trigger)}). "
            f"Closing now loses about {_usd(-pl_now if pl_now is not None else None)} before fees; "
            f"holding risks up to {_usd(still_at_risk)} more.",
        )
    if dte <= settings.time_exit_dte:
        if pl_now is None:
            reason = (
                f"{dte} DTE, at or past the {settings.time_exit_dte}-DTE exit ({_day(exit_date)}). "
                "No current quote, so the P/L is unknown; check the price before closing."
            )
        elif pl_now >= 0:
            reason = (
                f"{dte} DTE, at or past the {settings.time_exit_dte}-DTE exit. Closing now keeps about "
                f"{_usd(pl_now)} before fees; holding adds risk faster than the remaining premium."
            )
        else:
            reason = (
                f"{dte} DTE, at or past the {settings.time_exit_dte}-DTE exit. Losing about {_usd(-pl_now)} before fees, "
                f"with up to {_usd(still_at_risk)} more at risk if held; time will not fix this on its own."
            )
        return guidance(ACTION_CLOSE, f"CLOSE: TIME EXIT ({settings.time_exit_dte} DTE)", reason)
    pressured = breached or (delta is not None and delta + _EPS >= SHORT_DELTA_ALERT)
    if pressured:
        return guidance(
            ACTION_REVIEW,
            "REVIEW: SHORT STRIKE UNDER PRESSURE",
            " ".join(flags[:2])
            + f" Not at the loss threshold yet ({_usd(debit)} vs {_usd(trigger)}). Decide whether the trade's reason still holds.",
        )
    if debit is None:
        why = chain_unavailable_reason or "No usable bid and ask on both legs right now."
        return guidance(
            ACTION_UNVERIFIED,
            "UNVERIFIED: NO CURRENT QUOTE",
            f"{why} Target close {_usd(target)}; time exit {_day(exit_date)}.",
        )
    return guidance(
        ACTION_HOLD,
        "HOLD",
        f"Close at {_usd(target)} or less for the {settings.profit_target_pct:g}% target; "
        f"review at {_usd(trigger)}; time exit {_day(exit_date)}.",
    )


# ---------------------------------------------------------------- closed-spread outcomes

EXIT_PROFIT_TARGET = "PROFIT_TARGET"
EXIT_LOSS_THRESHOLD = "LOSS_THRESHOLD"
EXIT_TIME = "TIME_EXIT"
EXIT_EXPIRED = "EXPIRED"
EXIT_ASSIGNED = "ASSIGNED"
EXIT_OTHER = "OTHER"
EXIT_LABELS = {
    EXIT_PROFIT_TARGET: "Profit target",
    EXIT_LOSS_THRESHOLD: "Loss threshold",
    EXIT_TIME: "Time exit",
    EXIT_EXPIRED: "Expired",
    EXIT_ASSIGNED: "Assigned",
    EXIT_OTHER: "Closed early, no rule hit",
}


def classify_exit(position: SpreadPosition, settings: SpreadSettings) -> str | None:
    """Which rule a fully closed spread's exit matches, judged against the
    current settings: assigned > expired worthless > profit target > loss
    threshold > time exit > other. ``None`` while it is still open."""
    if not position.is_closed:
        return None
    actions = {f.action for f in position.short.closes + position.long.closes}
    if ASSIGNED in actions:
        return EXIT_ASSIGNED
    if actions == {EXPIRED}:
        return EXIT_EXPIRED
    debit = position.closing_debit
    if debit is not None and debit <= target_closing_debit(position.credit, settings.profit_target_pct) + _CENT:
        return EXIT_PROFIT_TARGET
    if debit is not None and debit + _CENT >= loss_trigger_debit(position.credit, settings.loss_multiple):
        return EXIT_LOSS_THRESHOLD
    if (position.expiry - position.close_date).days <= settings.time_exit_dte:
        return EXIT_TIME
    return EXIT_OTHER


# ---------------------------------------------------------------- performance


def in_date_range(day: date | None, start: date | None, end: date | None) -> bool:
    """Inclusive on both ends; an open end (``None``) doesn't limit."""
    return day is not None and (start is None or day >= start) and (end is None or day <= end)


def closed_in_range(
    positions: Iterable[SpreadPosition], start: date | None, end: date | None
) -> list[SpreadPosition]:
    """Fully closed spreads whose final close falls in the range: the one rule
    for both the Closed spreads table and the per-trade performance figures."""
    return [p for p in positions if p.is_closed and in_date_range(p.close_date, start, end)]


def performance(
    positions: Sequence[SpreadPosition],
    guidance_by_id: dict[str, Guidance],
    settings: SpreadSettings,
    *,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    """Spread-only performance. Each figure has its own base, named in its key:

    * premium collected: gross opening credit of spreads opened in the range;
      a credit received is not profit.
    * realized: closing fills dated in the range (partial closes included),
      gross, fees, and net = gross - fees (the broker's Amounts).
    * per-trade figures (win rate, averages, profit factor, holding period,
      return on max risk, exit counts): spreads fully closed in the range.
    * a spread whose result isn't settled by the spread (one leg assigned
      without the other, :attr:`SpreadPosition.result_settled`) is left out
      of every P/L figure (realized, win/loss, averages, profit factor,
      return on max risk, drawdown) and counted in ``unsettled_closed_count``
      instead; its exit and holding period still count.
    * unrealized: open spreads now, at the executable closing cost, before
      closing fees.
    * max drawdown: largest drop from a running peak of cumulative net
      realized P/L, close by close, starting from $0.
    """

    opened = [p for p in positions if in_date_range(p.open_date, start, end)]
    events = sorted(
        (day, net, fees, p.spread_id)
        for p in positions
        for day, net, fees in p.realized_events()
        if in_date_range(day, start, end)
    )
    net_realized = sum(e[1] for e in events)
    fees = sum(e[2] for e in events)

    every_closed = closed_in_range(positions, start, end)
    closed = [p for p in every_closed if p.result_settled]
    nets = [p.realized_net for p in closed]
    wins = [v for v in nets if v > _EPS]
    losses = [v for v in nets if v < -_EPS]
    risk_total = sum(p.max_loss for p in closed)
    holding = [(p.close_date - p.open_date).days for p in every_closed]

    peak = cumulative = drawdown = 0.0
    for _, net, _, _ in events:
        cumulative += net
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)

    unrealized_values = [
        guidance_by_id[p.spread_id].unrealized_pl if p.spread_id in guidance_by_id else None
        for p in positions
        if not p.is_closed
    ]
    unpriced = sum(1 for v in unrealized_values if v is None)

    exits = {key: 0 for key in EXIT_LABELS}
    for p in every_closed:
        exits[classify_exit(p, settings)] += 1

    return {
        "premium_collected_opened_in_range": round(sum(p.premium_collected for p in opened), 2),
        "net_premium_collected_opened_in_range": round(sum(p.net_premium_collected for p in opened), 2),
        "open_fees_opened_in_range": round(sum(p.open_fees for p in opened), 2),
        "spreads_opened_in_range": len(opened),
        "realized_gross": round(net_realized + fees, 2),
        "realized_fees": round(fees, 2),
        "realized_net": round(net_realized, 2),
        "unrealized": round(sum(v for v in unrealized_values if v is not None), 2),
        "unrealized_unpriced_count": unpriced,
        "open_count": sum(1 for p in positions if not p.is_closed),
        "closed_count": len(closed),
        "unsettled_closed_count": len(every_closed) - len(closed),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate_pct": _round(len(wins) / len(closed) * 100, 1) if closed else None,
        "avg_net_per_closed": _round(sum(nets) / len(closed)) if closed else None,
        "avg_win": _round(sum(wins) / len(wins)) if wins else None,
        "avg_loss": _round(sum(losses) / len(losses)) if losses else None,
        "largest_loss": _round(min(losses)) if losses else None,
        "profit_factor": _round(sum(wins) / -sum(losses)) if losses else None,
        "avg_holding_days": _round(sum(holding) / len(holding), 1) if holding else None,
        "return_on_max_risk_pct": _round(sum(nets) / risk_total * 100, 1) if risk_total > _EPS else None,
        "max_risk_closed": round(risk_total, 2),
        "max_drawdown": round(drawdown, 2),
        "exit_counts": [{"key": k, "label": EXIT_LABELS[k], "count": exits[k]} for k in EXIT_LABELS],
        "range": {"start": _iso(start), "end": _iso(end)},
    }


# ---------------------------------------------------------------- payloads


def _leg_quote_payload(quote: LegQuote | None) -> dict[str, Any] | None:
    if quote is None:
        return None
    return {
        "strike": quote.strike,
        "bid": quote.bid,
        "ask": quote.ask,
        "mid": _round(bid_ask_mid(quote.bid, quote.ask), 3),
        "iv_pct": _round(quote.iv * 100 if quote.iv is not None else None, 1),
        "open_interest": quote.open_interest,
    }


def candidate_tip(c: SpreadCandidate) -> str:
    """The candidate's calculation, in the tooltip line convention."""
    if c.credit is None:
        return "Spread\nNo usable quote on both legs, so nothing could be calculated."
    lines = [
        "Credit = short bid - long ask",
        f"= {_usd(c.short.bid)} - {_usd(c.long.ask)}",
        f"= {_usd(c.credit)}",
        "",
        "Max loss = (width - credit) × 100",
        f"= ({_usd(c.width)} - {_usd(c.credit)}) × 100",
        f"= {_usd(c.max_loss)}",
        "",
        "Breakeven = " + ("short strike - credit" if c.strategy == BULL_PUT else "short strike + credit"),
        f"= {_usd(c.short_strike)} {'-' if c.strategy == BULL_PUT else '+'} {_usd(c.credit)}",
        f"= {_usd(c.breakeven)}",
    ]
    if c.contracts is not None and c.risk_per_contract:
        lines += [
            "",
            "Contracts = account value × max risk % ÷ risk per spread",
            f"= {_usd(c.account_equity, cents=False)} × {c.settings.max_risk_pct_of_equity:g}% ÷ {_usd(c.risk_per_contract)}",
            f"= {c.contracts} contracts",
        ]
    lines += [
        "",
        f"Short {_strike(c.short_strike)} bid / ask: {_usd(c.short.bid)} / {_usd(c.short.ask)}",
        f"Long {_strike(c.long_strike)} bid / ask: {_usd(c.long.bid)} / {_usd(c.long.ask)}",
    ]
    if c.credit_mid is not None:
        lines += [f"Midpoint credit (estimate): {_usd(c.credit_mid)}"]
    if c.pop is not None:
        lines += [f"Chance of profit (model estimate): {c.pop * 100:.0f}%"]
    return "\n".join(lines)


def candidate_payload(c: SpreadCandidate, now: datetime) -> dict[str, Any]:
    return {
        "id": f"{c.ticker}|{c.expiry.isoformat()}|{c.strategy}|{_strike(c.short_strike)}|{_strike(c.long_strike)}",
        "ticker": c.ticker,
        "strategy": c.strategy,
        "strategy_label": STRATEGY_LABELS[c.strategy],
        "direction": "Bullish to neutral" if c.strategy == BULL_PUT else "Bearish to neutral",
        "expiry": c.expiry.isoformat(),
        "dte": c.dte,
        "short_strike": c.short_strike,
        "long_strike": c.long_strike,
        "width": c.width,
        "spot": c.spot,
        "quote": c.timing.payload(now),
        "short": _leg_quote_payload(c.short),
        "long": _leg_quote_payload(c.long),
        "short_delta": _round(c.short_delta * 100 if c.short_delta is not None else None, 1),
        "credit": _round(c.credit, 2),
        "credit_mid": _round(c.credit_mid, 3),
        "min_credit": round(c.min_credit, 2),
        "max_profit": _round(c.max_profit),
        "max_loss": _round(c.max_loss),
        "open_fees_per_spread": round(c.open_fees_per_spread, 2),
        "breakeven": _round(c.breakeven),
        "credit_pct_of_width": _round(c.credit_pct_of_width, 1),
        "return_on_risk_pct": _round(c.return_on_risk_pct, 1),
        "pop_pct": _round(c.pop * 100 if c.pop is not None else None, 1),
        "pop_source": "Black-Scholes estimate from the short leg's implied volatility",
        "contracts": c.contracts,
        "risk_per_contract": _round(c.risk_per_contract),
        "status": c.status,
        "checks": [{"key": k.key, "label": k.label, "passed": k.passed, "detail": k.detail} for k in c.checks],
        "tip": candidate_tip(c),
    }


def recommendation_payload(r: Recommendation, now: datetime) -> dict[str, Any]:
    return {
        "ticker": r.ticker,
        "status": r.status,
        "headline": r.headline,
        "pick": candidate_payload(r.pick, now) if r.pick else None,
        "reasons": list(r.reasons),
        "risks": list(r.risks),
    }


def guidance_tip(g: Guidance, settings: SpreadSettings) -> str:
    p = g.position
    lines = [
        "Target close = credit × (1 - profit target %)",
        f"= {_usd(p.credit)} × (1 - {settings.profit_target_pct:g}%)",
        f"= {_usd(g.target_debit)}",
        "",
        "Loss trigger = credit × loss multiple",
        f"= {_usd(p.credit)} × {settings.loss_multiple:g}",
        f"= {_usd(g.loss_trigger)}",
    ]
    if g.closing_debit is not None:
        lines += [
            "",
            "Closing cost = short ask - long bid",
            f"= {_usd(g.short_quote.ask)} - {_usd(g.long_quote.bid)}",
            f"= {_usd(g.closing_debit)}",
        ]
    lines += ["", f"Time exit: {_day(g.time_exit_date)}"]
    return "\n".join(lines)


def _close_price(fill: LegFill) -> float | None:
    """Per-share fill price of a buy or sell to close: |cash + fees| / (100 x contracts).
    ``None`` for an expiration or assignment, which has no trade price."""
    if fill.action in (ASSIGNED, EXPIRED) or fill.contracts <= _EPS:
        return None
    return round(abs(fill.cash + fill.fees) / (OPTION_MULTIPLIER * fill.contracts), 4)


def _trades(p: SpreadPosition) -> list[dict[str, Any]]:
    """Every fill behind the spread, opening legs first, in this spread's share of each leg."""
    rows = []
    for leg in (p.short, p.long):
        kind = "put" if leg.right == PUT else "call"
        rows.append(
            {
                "date": p.open_date.isoformat(),
                "action": f"{'Sold' if leg.side == SHORT else 'Bought'} to open",
                "description": f"{leg.contracts:g} × {p.ticker} {_strike(leg.strike)} {kind}",
                "price": leg.open_price,
                "net_cash": round(leg.open_cash, 2),
                "fees": round(leg.open_fees, 2),
            }
        )
        for fill in leg.closes:
            rows.append(
                {
                    "date": fill.date.isoformat(),
                    "action": {ASSIGNED: "Assigned", EXPIRED: "Expired"}.get(
                        fill.action, f"{'Bought' if leg.side == SHORT else 'Sold'} to close"
                    ),
                    "description": f"{fill.contracts:g} × {p.ticker} {_strike(leg.strike)} {kind}",
                    "price": _close_price(fill),
                    "net_cash": round(fill.cash, 2),
                    "fees": round(fill.fees, 2),
                }
            )
    settlement = p.assignment_settlement
    if settlement:
        day, cash, contracts = settlement
        rows.append(
            {
                "date": day.isoformat(),
                "action": "Assignment settled",
                "description": (
                    f"{contracts * OPTION_MULTIPLIER:g} {p.ticker} shares at {_strike(p.short_strike)} and "
                    f"{_strike(p.long_strike)}: {_usd(p.width)} width × 100 × {contracts:g}"
                ),
                "price": None,
                "net_cash": round(cash, 2),
                "fees": 0.0,
            }
        )
    return sorted(rows, key=lambda row: row["date"])


def trades_tip(p: SpreadPosition) -> str:
    lines = ["Trades"]
    for row in _trades(p):
        price = f" @ {_usd(row['price'])}" if row["price"] is not None else ""
        lines.append(f"{_day(date.fromisoformat(row['date']))}: {row['action']} {row['description']}{price}, net {_usd(row['net_cash'])}")
    return "\n".join(lines)


def net_collected_tip(p: SpreadPosition) -> str:
    return "\n".join(
        [
            "Net collected = short leg cash - long leg cost, after fees",
            f"= {_usd(p.short.open_cash)} - {_usd(-p.long.open_cash)}",
            f"= {_usd(p.net_premium_collected)}",
            "",
            f"Gross credit: {_usd(p.premium_collected)}",
            f"Opening fees: {_usd(p.open_fees)}",
        ]
    )


def breakeven_tip(p: SpreadPosition) -> str:
    sign = "-" if p.strategy == BULL_PUT else "+"
    side = "above" if p.strategy == BULL_PUT else "below"
    return "\n".join(
        [
            f"Breakeven = short strike {sign} credit per share",
            f"= {_usd(p.short_strike)} {sign} {_usd(p.credit)}",
            f"= {_usd(breakeven(p.strategy, p.short_strike, p.credit))}",
            f"  {p.ticker} must close {side} this at expiration for the spread to make money, before fees.",
        ]
    )


def position_payload(p: SpreadPosition, settings: SpreadSettings) -> dict[str, Any]:
    exit_key = classify_exit(p, settings)
    return {
        "spread_id": p.spread_id,
        "account_id": p.account_id,
        "account_label": p.account_label,
        "cycle_id": p.cycle_id,
        "ticker": p.ticker,
        "strategy": p.strategy,
        "strategy_label": STRATEGY_LABELS[p.strategy],
        "open_date": p.open_date.isoformat(),
        "expiry": p.expiry.isoformat(),
        "short_strike": p.short_strike,
        "long_strike": p.long_strike,
        "width": p.width,
        "contracts": p.contracts,
        "contracts_open": p.contracts_open,
        "credit": round(p.credit, 4),
        "premium_collected": round(p.premium_collected, 2),
        "net_premium_collected": round(p.net_premium_collected, 2),
        "open_fees": round(p.open_fees, 2),
        "max_profit": round(p.max_profit, 2),
        "max_loss": round(p.max_loss, 2),
        "breakeven": round(breakeven(p.strategy, p.short_strike, p.credit), 2),
        "trades": _trades(p),
        "trades_tip": trades_tip(p),
        "net_collected_tip": net_collected_tip(p),
        "breakeven_tip": breakeven_tip(p),
        "is_closed": p.is_closed,
        "close_date": _iso(p.close_date),
        "closing_debit": _round(p.closing_debit, 4),
        "realized_net": _round(p.realized_net),
        "realized_fees": round(p.realized_fees, 2),
        "realized_gross": None if p.realized_net is None else round(p.realized_net + p.realized_fees, 2),
        "result_settled": p.result_settled,
        "result_note": p.unsettled_note,
        "holding_days": (p.close_date - p.open_date).days if p.close_date else None,
        "exit": exit_key,
        "exit_label": EXIT_LABELS.get(exit_key) if exit_key else None,
    }


def guidance_payload(g: Guidance, settings: SpreadSettings, now: datetime) -> dict[str, Any]:
    payload = position_payload(g.position, settings)
    payload.update(
        {
            "dte": g.dte,
            "spot": g.spot,
            "quote": g.timing.payload(now) if g.timing else None,
            "short_quote": _leg_quote_payload(g.short_quote),
            "long_quote": _leg_quote_payload(g.long_quote),
            "closing_debit_now": _round(g.closing_debit, 2),
            "closing_debit_mid": _round(g.closing_debit_mid, 3),
            "unrealized_pl": _round(g.unrealized_pl),
            "pct_of_max_profit": _round(g.pct_of_max_profit, 1),
            "distance_to_short_pct": _round(g.distance_to_short_pct, 2),
            "short_delta": _round(g.short_delta * 100 if g.short_delta is not None else None, 1),
            "target_debit": round(g.target_debit, 2),
            "target_profit": round((g.position.credit - g.target_debit) * OPTION_MULTIPLIER * g.position.contracts_open, 2),
            "loss_trigger": round(g.loss_trigger, 2),
            "loss_at_trigger": round((g.loss_trigger - g.position.credit) * OPTION_MULTIPLIER * g.position.contracts_open, 2),
            "time_exit_date": g.time_exit_date.isoformat(),
            "action": g.action,
            "action_label": g.action_label,
            "reason": g.reason,
            "flags": list(g.flags),
            "tip": guidance_tip(g, settings),
        }
    )
    return payload


OVERLAP_NOTE = (
    "These spreads are also inside your Wheel figures, where the short leg counts as a CSP or covered call. "
    "Spread figures here are separate; don't add the two together."
)
PAIRING_NOTE = (
    "A spread is recognized when both legs were opened the same day on the same ticker, type and expiration. "
    "Spreads entered one leg at a time on different days are not shown."
)
